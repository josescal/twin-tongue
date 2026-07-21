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

OpenAI's official conversational-translation guidance says to keep participant tracks separate and use one translation session per direction. Twin Tongue therefore creates two independent sessions when both directions select `openai_realtime`:

- remote participant audio -> agent language -> agent output device;
- agent microphone -> remote participant language -> CABLE B output route.

## Current API contract

The implementation follows the current official [Realtime translation guide](https://developers.openai.com/api/docs/guides/realtime-translation) and [GPT-Realtime-Translate model page](https://developers.openai.com/api/docs/models/gpt-realtime-translate):

- WebSocket endpoint: `/v1/realtime/translations?model=gpt-realtime-translate`;
- input/output: mono little-endian PCM16 at 24 kHz;
- configure target language with `session.update` at `session.audio.output.language`;
- append continuous base64 audio, including silence, with `session.input_audio_buffer.append`;
- consume `session.output_audio.delta`, `session.input_transcript.delta`, and `session.output_transcript.delta`;
- do not use `response.create` for translation sessions;
- on graceful source shutdown, send `session.close`, keep receiving output, and wait for `session.closed`.

Because the dedicated service performs continuous interpretation, the classic Silero VAD and adaptive segmenter are not instantiated by this engine. They remain unchanged and active only in the classic pipelines. Their TOML settings are deliberately namespaced under `pipeline.classic.<direction>.vad` and `pipeline.classic.<direction>.segmentation` so they cannot be mistaken for speech-to-speech settings.

## Configuration

The default is behavior-preserving:

```toml
[pipeline.runtime.remote_to_agent]
type = "classic"

[pipeline.runtime.agent_to_remote]
type = "classic"
```

Set either `type` to `"speech_to_speech"` to use the engine selected at `pipeline.speech_to_speech.<direction>.translation.engine`. Engine selection is configuration-only: the control panel reports it but cannot change it, and Twin Tongue must be restarted after editing the file. Add `OPENAI_API_KEY` to `.env`; never place secrets in TOML or source files. `OPENAI_SAFETY_IDENTIFIER` is optional and should be a stable pseudonymous identifier, not raw personal information.

Realtime uses independent capture and playback queues. Both are capped at 25 blocks
of 20 ms (500 ms), so neither a stalled event loop nor a burst from the service can
build up seconds of stale audio. If either cap is reached, the oldest queued blocks
are dropped and reported by the metrics.

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

Realtime queue and latency metrics are sampled every 10 seconds.

## Failure and latency behavior

While a Realtime session is connecting or reconnecting, Twin Tongue preserves the existing safety behavior and routes original audio. Reconnection uses bounded exponential backoff and reports `initializing`, `reconnecting`, `translation_ready`, or `translation_unavailable` through the existing state/UI channel.

Structured logs and CSV metrics expose:

- latency from the first input audio sent to the first translated audio received;
- trailing total latency from the latest input block to the latest output block;
- cumulative input and generated audio duration;
- connection errors and successful reconnections;
- capture drops and overflows;
- current and maximum playback backlog, dropped blocks, empty-buffer events, and
  output underflows.
