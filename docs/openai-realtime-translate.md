# OpenAI Realtime Translate integration

Twin Tongue treats speech-to-speech translation as a pipeline engine, not as another STT, text-translation, or TTS provider. This keeps the existing `classic` path unchanged while allowing future streaming speech-to-speech engines to use the same device and audio infrastructure.

## Directional architecture

Both engines reuse `QueuedAudioInput`, `QueuedAudioOutput`, `AudioDeviceManager`, PCM channel conversion, streaming resampling, WASAPI/PortAudio device selection, and the existing VB-CABLE routes.

```text
                           +-> classic: VAD -> segmenter -> STT -> translate -> TTS -+
capture + device routing --+                                                       +-> playback + device routing
                           +-> openai_realtime: continuous speech-to-speech --------+
```

`mode = "passthrough"` remains independent from `engine`. When translation is disabled, the selected direction routes original PCM. When it is enabled, the supervisor runs exactly one selected engine.

The pipeline type is configuration-only. The control panel can switch the live
mode between passthrough and translation, but it cannot replace Classic with
Realtime without a process restart.

## Call-aware API sessions

Enabling Realtime translation arms the selected direction but does not by itself
open an OpenAI WebSocket. Twin Tongue monitors the external WASAPI application
sessions attached to the relevant VB-CABLE endpoint:

- `remote_to_agent` watches applications rendering to CABLE-A Input;
- `agent_to_remote` watches applications capturing from CABLE-B Output.

The OpenAI session opens only while the corresponding application session is
active. When no call application is attached, the pipeline reports
`waiting_for_call`, keeps local passthrough available, and sends no audio to the
API. Session discovery runs independently from the heavier PortAudio topology
poll and does not write diagnostic audio or state to disk.

After a call has been detected, audio sending and the WebSocket remain active for
the configured disconnect grace when the WASAPI session disappears. This absorbs
the active/inactive transitions produced by calling applications during silence
or device changes. If the application does not reappear before the grace expires,
Twin Tongue stops sending audio and closes the session. A failed WASAPI
observation retains the last known state only for the configured failure grace
and then fails closed.

OpenAI's official conversational-translation guidance says to keep participant tracks separate and use one translation session per direction. Twin Tongue therefore creates two independent sessions when both directions select `openai_realtime`:

- remote participant audio -> agent language -> agent output device;
- agent microphone -> remote participant language -> CABLE B output route.

## Current API contract

The implementation follows the current official [Realtime translation guide](https://developers.openai.com/api/docs/guides/realtime-translation) and [GPT-Realtime-Translate model page](https://developers.openai.com/api/docs/models/gpt-realtime-translate):

- WebSocket endpoint: `/v1/realtime/translations?model=gpt-realtime-translate`;
- input/output: mono little-endian PCM16 at 24 kHz;
- configure `near_field` input noise reduction for the close physical microphone;
- configure target language with `session.update` at `session.audio.output.language`;
- append continuous base64 audio, including silence, with `session.input_audio_buffer.append`;
- consume `session.output_audio.delta`, `session.input_transcript.delta`, and `session.output_transcript.delta`;
- do not use `response.create` for translation sessions;
- on graceful source shutdown, send `session.close`, keep receiving output, and wait for `session.closed`.

Because the dedicated service performs continuous interpretation, the classic Silero VAD and adaptive segmenter are not instantiated by this engine. They remain unchanged and active only in the classic pipelines. Their TOML settings are deliberately namespaced under `pipeline.classic.<direction>.vad` and `pipeline.classic.<direction>.segmentation` so they cannot be mistaken for speech-to-speech settings.

## Configuration

The current product default selects speech-to-speech independently in both
directions:

```toml
[pipeline.runtime.remote_to_agent]
type = "speech_to_speech"

[pipeline.runtime.agent_to_remote]
type = "speech_to_speech"
```

Set either `type` to `"classic"` to use the existing staged pipeline for that
direction. Engine selection is configuration-only: the control panel reports it
but cannot change it. Add `OPENAI_API_KEY` to `.env`; never place secrets in TOML
or source files. `OPENAI_SAFETY_IDENTIFIER` is optional and should be a stable
pseudonymous identifier, not raw personal information.

Realtime uses independent capture, network-send, and playback queues. Each is
capped at 25 blocks of 20 ms (500 ms). Network writes run in a separate task, so
WebSocket backpressure cannot block device changes, passthrough, or call-session
detection. If a cap is reached, the oldest queued blocks are dropped and reported
by the metrics.

Capture is callback-driven: the pipeline sleeps until PortAudio supplies a block
instead of polling every millisecond. Device reopening runs outside the asyncio
event loop, and audio-level metrics sample one of every five blocks. When
diagnostic capture is enabled, Realtime writes `captured`, `accepted`, `sent`,
and callback-confirmed `played` tracks plus a per-call JSON manifest. CABLE A is
recorded as the far-end reference even in passthrough. The shared echo guard
suppresses microphone blocks only when their delayed level envelope correlates
with callback-confirmed far-end playback. Uncorrelated weak speech is preserved
for downstream voice detection.

`OPENAI_API_KEY` is the credential consumed by the runtime.
`OPENAI_API_KEY_NAME` is only an optional descriptive entry in the environment
template and is not sent to OpenAI. The standard API key remains server-side in
this native application and is never exposed by the loopback control page.

`log_transcript_deltas = false` is the default. This avoids one log write per text
fragment while retaining aggregate transcript counters and does not disable the
transcription shown in the control panel. Input and translated transcript deltas
are placed on an in-memory queue, coalesced, and published to the UI at most once
per `transcript_ui_update_interval_ms` (150 ms by default). This work is separate
from the WebSocket receiver so subtitle rendering cannot delay translated audio.

Because translation sessions emit continuous deltas rather than Classic-style final
segments, Twin Tongue closes a displayed entry after
`transcript_segment_idle_ms` (500 ms by default) without new text. The dedicated
translation endpoint currently may omit the source transcript; when translated text
is available Twin Tongue displays only that supported output and does not wait for
the missing source side. A source-only partial is retained for up to five times the
normal interval to accommodate provider latency. Session shutdown, language changes,
and reconnects also close the current entry. No transcript audio or delta is written
to disk by this path.

Twin Tongue preserves equal final phrases as separate turns. It does not deduplicate
captions by normalized text or by a time window, because a caller may intentionally
repeat the same sentence.

Realtime queue and latency metrics are sampled every 10 seconds when application
metrics are enabled. `[observability.metrics].enabled` is `false` in the shipped
configuration.

## Failure and latency behavior

While a Realtime session is connecting or reconnecting, Twin Tongue preserves the
existing safety behavior and routes original audio. Reconnection uses bounded
exponential backoff. After the fast attempts are exhausted it reports
`translation_unavailable`, waits the configured 10-second cooldown, and starts a
new retry cycle without requiring an application restart. The control panel shows
both the requested translation mode and whether translated audio is effectively
ready.

Structured logs and CSV metrics expose:

- latency from the first input audio sent to the first translated audio received;
- trailing total latency from the latest input block to the latest output block;
- cumulative input and generated audio duration;
- connection errors and successful reconnections;
- capture drops and overflows;
- network-send backlog and dropped blocks;
- current and maximum playback backlog, dropped blocks, empty-buffer events, and
  output underflows.

The two directions fail independently. A pipeline-level exception restarts only
that direction with bounded backoff; it does not stop the opposite direction,
device manager, or control server. Normal task cancellation is not counted as a
provider error.
