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

Here, independent means transport, runtime state, and failure isolation. It does
not mean independent OpenAI quota or spend. OpenAI rate limits are applied at
organization and project level and vary by model, so both directional sessions
consume the same applicable `gpt-realtime-translate` capacity. Translation and
transcription streaming are billed by processed audio duration, and usage from
the two sessions is additive.

## Current API contract

The implementation follows the current official [Realtime translation guide](https://developers.openai.com/api/docs/guides/realtime-translation) and [GPT-Realtime-Translate model page](https://developers.openai.com/api/docs/models/gpt-realtime-translate):

- WebSocket endpoint: `/v1/realtime/translations?model=gpt-realtime-translate`;
- input/output: mono little-endian PCM16 at 24 kHz;
- configure `near_field` input noise reduction for the close physical microphone;
- configure target language with `session.update` at `session.audio.output.language`;
- optionally configure source transcription with
  `session.audio.input.transcription.model = "gpt-realtime-whisper"`;
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

Realtime uses independent capture, network-send, provider-receive, and playback
queues. Capture retains 25 application blocks of 20 ms (500 ms). The resampler
accumulates them into the 200 ms frames recommended by OpenAI, and the
network-send queue retains three provider frames (600 ms). Provider output is
split back into 20 ms blocks; its receive queue retains 150 blocks (3,000 ms) and
translated playback is limited to twelve blocks (240 ms).
Network writes and translated playback run in separate tasks, so neither
WebSocket backpressure nor a slow physical output can block reception of audio,
transcripts, or control events. Provider audio deltas are split into 20 ms
blocks before being queued. Adaptive, pitch-preserving playout accelerates from
1.00x up to 1.15x as backlog grows. Before the receive cap is reached, emergency
recovery searches for a quiet boundary, removes enough stale audio to return
near the recovery watermark, and crossfades the discontinuity. Drops and
discontinuities are reported by the metrics.

The queue diagram, watermarks, CSV fields, and support interpretation are
documented in [CSV metrics reference](metrics-reference.md). Configuration
semantics and tuning recommendations are in
[Configuration reference](configuration.md).

Capture is callback-driven: the pipeline sleeps until PortAudio supplies a block
instead of polling every millisecond. Device reopening runs outside the asyncio
event loop, and audio-level metrics sample one of every five blocks. When
diagnostic capture is enabled, Realtime writes `captured`, `accepted`, `sent`,
provider-returned `received`, and callback-confirmed `played` tracks plus a
per-call JSON manifest. CABLE A is recorded even in passthrough. The
physical-output callback supplies the render
reference to the shared WebRTC AEC3 processor; the microphone direction supplies
the capture stream. Echo estimation, double-talk handling, and cancellation are
performed by native WebRTC Audio Processing before audio is sent or passed
through to CABLE B.

`OPENAI_API_KEY` is the credential consumed by the runtime.
`OPENAI_API_KEY_NAME` is only an optional descriptive entry in the environment
template and is not sent to OpenAI. The standard API key remains server-side in
this native application and is never exposed by the loopback control page.

`input_transcription.enabled = true` configures
`gpt-realtime-whisper` by default so `session.input_transcript.delta` supplies the
source-language column. Disable it when source captions are not required; target
transcript and translated audio continue to come from
`gpt-realtime-translate`. The transcription model has its own usage and
applicable model quota.

`log_transcript_deltas = false` avoids one log write per text fragment while
retaining aggregate transcript counters and does not disable the transcription
shown in the control panel. Input and translated transcript deltas are placed on
an in-memory queue, coalesced, and published to the UI at most once per
`transcript_ui_update_interval_ms` (150 ms by default). This work is separate
from the WebSocket receiver so subtitle rendering cannot delay translated audio.

Because translation sessions emit continuous deltas rather than Classic-style
final segments, Twin Tongue closes a displayed entry after
`transcript_segment_idle_ms` (500 ms by default) without new text. When source
transcription is disabled or unavailable, translated text is still displayed
without waiting for the missing source side. A source-only partial is retained
for up to five times the normal interval to accommodate provider latency.
Session shutdown, language changes, and reconnects also close the current entry.
No transcript audio or delta is written to disk by this path.

Twin Tongue preserves equal final phrases as separate turns. It does not deduplicate
captions by normalized text or by a time window, because a caller may intentionally
repeat the same sentence.

The control panel retains the newest 50 transcript entries independently for
each direction, for a maximum of 100 interleaved entries. A busy speaker can no
longer evict the other participant's entire visible history. Clearing the
transcription still removes both directions together.

Realtime queue and latency metrics are sampled every 10 seconds when application
metrics are enabled. `[observability.metrics].enabled` is `true` in the shipped
configuration. CSV rows include backlog p50/p95/p99, adaptive speed and maximum
speed, time-compression ratio, time above the target watermark, silence-boundary
drops, and separate translated/passthrough playback-drop counters.
Signal RMS and peaks, callback health, session-gate state, errors, and
reconnections are stored in the same CSV. Periodic metric snapshots and
per-utterance latency measurements are not duplicated in `twin-tongue.log`.
The log retains call/session transitions, retries, unavailable states, buffer
recovery warnings, device changes, and failures requiring support attention.

The main Realtime support events are intentionally small in number:

- `realtime_call_state_changed`: the calling application became active or inactive;
- `pipeline_status_changed`: the user-visible state changed;
- `realtime_translation_connection_failed` or `connection_lost`: a retry started;
- `realtime_translation_retry_cycle_exhausted`: the cooldown started;
- `realtime_session_close_timeout` or `close_failed`: graceful shutdown failed;
- `realtime_translation_backlog_recovered`: emergency audio recovery was required;
- `realtime_translation_pipeline_failed`: the direction stopped unexpectedly.

Protocol event discovery, WAV-file creation, ordinary queue cleanup, periodic
measurements, and per-utterance latency are available at `DEBUG`, in Metrics, or
in the per-call manifest instead of occupying the normal support log.

## Failure and latency behavior

Enabling translation arms the direction. The WebSocket is created only after the
stabilized cable-session gate detects a call, then receives continuous source
audio for that call. The disconnect grace keeps AEC3, diagnostics, and the
session active across brief false-negative Windows observations. When it
expires, Twin Tongue flushes the local 200 ms framing queue, sends
`session.close`, drains remaining provider audio and transcripts through
`session.closed`, waits for physical playback to empty, and starts the next call
with a fresh session. During this exclusive `draining` state it does not inject
passthrough audio into the same output.

Provider audio is split into 20 ms blocks and held in a three-second receive
buffer with 240 ms of physical playback capacity. Above one second of backlog,
WSOLA time-scale compression gradually increases playback speed while preserving
pitch, up to 1.15x near the 2.8-second emergency watermark. If pressure still
reaches the hard limit, only enough old audio to return to 1.5 seconds is removed,
preferably at the lowest-energy boundary within 250 ms, followed by a 15 ms
crossfade.

While a Realtime session is connecting or reconnecting, Twin Tongue preserves the
existing safety behavior and routes original audio. Reconnection uses bounded,
randomized exponential backoff. Rate-limit failures also extend a shared
cooldown consulted by both directional sessions, preventing them from retrying
in lockstep against the same project/model quota. After the fast attempts are
exhausted Twin Tongue reports `translation_unavailable`, waits the configured
10-second cooldown, and starts a new retry cycle without requiring an application
restart. The control panel shows both the requested translation mode and whether
translated audio is effectively ready.

Structured logs and CSV metrics expose:

- latency from the first input audio sent to the first translated audio received;
- trailing total latency from the latest input block to the latest output block;
- cumulative input and generated audio duration;
- connection errors and successful reconnections;
- capture drops and overflows;
- network-send backlog and dropped blocks;
- provider-receive backlog, dropped blocks, and discontinuities;
- per-intervention voice-onset latency for `accepted → sent → received → played`;
- current and maximum playback backlog, dropped blocks, empty-buffer events, and
  output underflows.

The two directions fail independently. A pipeline-level exception restarts only
that direction with bounded backoff; it does not stop the opposite direction,
device manager, or control server. Normal task cancellation is not counted as a
provider error.
