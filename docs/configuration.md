# Configuration namespaces

`config/default.toml` uses `schema_version = 2`. Engine selection is immutable for
the lifetime of the process and is changed only by editing this file and restarting
Twin Tongue.

The shipped default enables both directions, selects `speech_to_speech`, and
starts them in `passthrough`.

## Namespace layout

```text
audio                                      shared PCM and resampling infrastructure
pipeline.runtime.<direction>               enabled, mode, type, audio geometry
pipeline.classic.defaults                  shared Classic defaults
pipeline.classic.<direction>               directional VAD/STT/translation/TTS binding
pipeline.speech_to_speech.defaults         shared direct speech translation defaults
pipeline.speech_to_speech.<direction>      directional speech-to-speech binding
observability.logging                      asynchronous application logs
observability.metrics                      CSV metric snapshots
control_server                             local read/control web server
```

Directional engine paths follow:

```text
pipeline.<type>.<direction>.<functionality>.<engine>
```

Examples include:

```toml
[pipeline.classic.remote_to_agent.vad.silero]
[pipeline.classic.remote_to_agent.stt]
engine = "elevenlabs"

[pipeline.speech_to_speech.remote_to_agent.translation]
engine = "openai_realtime"
```

Shared engine settings live below the corresponding `defaults` namespace and are
resolved before directional overrides. Directional values take precedence. The
configuration loader normalizes the resolved result into one stable runtime contract,
so audio pipelines and provider clients do not read physical TOML paths directly.

## Physical endpoint isolation

`audio.physical_capture_exclusive_mode = false` keeps the physical microphone
in the Windows shared capture graph. This is the current default because the
Realtek endpoint applies its normal Windows input gain and audio processing in
that mode, matching the native Windows microphone test. Set it to `true` only
when validating a device that provides equivalent level and quality through
WASAPI exclusive mode. Physical playback and CABLE A/B remain shared; the
cables must be shared because the calling application and Twin Tongue use
opposite ends concurrently.

## Selecting a pipeline type

```toml
[pipeline.runtime.remote_to_agent]
enabled = true
mode = "passthrough"
type = "speech_to_speech"

[pipeline.runtime.agent_to_remote]
enabled = true
mode = "passthrough"
type = "speech_to_speech"
```

`classic` activates local VAD, segmentation, STT, text translation, and TTS.
`speech_to_speech` streams capture audio directly to the selected speech translation
engine and never loads or executes local VAD or segmentation for that direction.

`mode` is the initial audio behavior and can be changed from the control panel.
`type` and the engine bindings cannot be changed there. Restart Twin Tongue after
editing them.

With the default `--pipeline both`, disabled directions are skipped. An explicit
request for a disabled direction fails configuration validation. At least one
selected direction must be enabled.

## OpenAI Realtime settings

```toml
[pipeline.speech_to_speech.defaults.translation.openai_realtime]
endpoint = "wss://api.openai.com/v1/realtime/translations"
model = "gpt-realtime-translate"
sample_rate = 24000
send_chunk_duration_ms = 200
input_queue_capacity_blocks = 25
send_queue_capacity_frames = 3
received_queue_capacity_blocks = 25
playback_queue_capacity_blocks = 4
reconnect_max_attempts = 5
reconnect_base_delay_seconds = 0.5
reconnect_cooldown_seconds = 10.0
session_setup_timeout_seconds = 10.0
close_timeout_seconds = 2.0
metrics_interval_seconds = 10.0
log_transcript_deltas = false
transcript_ui_update_interval_ms = 150
transcript_segment_idle_ms = 500

[pipeline.speech_to_speech.defaults.translation.openai_realtime.input_transcription]
enabled = true
model = "gpt-realtime-whisper"

[pipeline.speech_to_speech.defaults.translation.openai_realtime.audio_capture]
enabled = true
directory = "logs/realtime-audio"
max_seconds_per_file = 120.0
write_timing_marks = true
queue_capacity_blocks = 500
write_buffer_kb = 64
flush_interval_seconds = 1.0

[audio.aec3]
enabled = true
stream_delay_ms = 0
render_queue_capacity_blocks = 100

[pipeline.speech_to_speech.defaults.translation.openai_realtime.session_gate]
enabled = true
disconnect_grace_seconds = 3.0
```

Realtime requires 24 kHz mono PCM16 at the provider boundary. Capture and output
devices keep their directional runtime formats; Twin Tongue resamples between
them. When diagnostic capture is enabled it writes five explicit tracks:
`captured` is raw endpoint PCM (including CABLE A in passthrough), `accepted` is
canonical mono PCM after WebRTC AEC3 on the microphone direction, `sent` is
24 kHz PCM successfully sent to OpenAI, `received` is the translated 24 kHz
PCM returned by OpenAI before local conversion, and `played` is the PCM
actually delivered by the PortAudio output callback.
AEC3 uses callback-delivered `remote_to_agent` playback as its reverse-stream
reference, so it receives the translated signal actually heard locally rather
than the original CABLE A input. Twin Tongue converts its 20 ms blocks into
10 ms WebRTC frames and resets adaptive state between calls. Set `enabled =
false` only to compare the raw microphone route during diagnosis.
Capture and translated-output queues retain the application's 20 ms blocks.
Input sent to OpenAI is accumulated into the provider's documented 200 ms
engine frames. The network-send queue retains three such frames (600 ms), while
the provider-receive queue retains 25 internal blocks (500 ms) and translated
playback retains four (80 ms). Asynchronous backpressure makes provider bursts
wait for the PortAudio callback instead of discarding older speech blocks.
Timing JSONL files and an atomic per-call manifest record devices, formats,
timeline events and AEC3 counters. WAV headers are refreshed on every
buffer flush so a `sent` file remains readable after an abnormal exit. Enabling
translation arms a direction, but its OpenAI session opens only when the session
gate confirms an attached call application. Audio then streams continuously,
including silence. When the stabilized call ends, Twin Tongue flushes its local
resampler and send queue, sends `session.close`, receives the remaining audio and
transcript events through `session.closed`, and creates a fresh session for the
next call. Directional sessions isolate transport and runtime state, but share
the applicable OpenAI organization/project/model quota.

Source-language captions are optional and enabled by default through
`input_transcription.model = "gpt-realtime-whisper"`. Set `enabled = false` to
run translation without the additional transcription model. Its usage and
applicable model quota are independent from `gpt-realtime-translate`.

Five fast retries use randomized exponential backoff. A rate-limit error extends
a shared cooldown for both directions so they do not reconnect in lockstep
against the same quota. Exhausting the fast attempts marks translation
unavailable, preserves original-audio passthrough, waits 10 seconds, and starts a
new retry cycle.

## Classic VAD and STT recording

All Silero and adaptive-segmentation settings are below `pipeline.classic`.
Twin Tongue does not preload the Silero ONNX package when none of the selected
directions is Classic.

```toml
[pipeline.classic.defaults.stt.elevenlabs.audio_capture]
enabled = true
directory = "logs/stt-audio"
max_seconds_per_file = 120.0
write_timing_marks = false
write_buffer_kb = 64
flush_interval_seconds = 1.0
```

These recording settings apply only to Classic STT. Buffered writes avoid
creating or flushing a file for every small audio block. Realtime diagnostic
recording is configured separately in its `audio_capture` section above.

## Environment variables

Secrets belong in `.env`, never in TOML:

- `OPENAI_API_KEY`: Realtime translation credential.
- `OPENAI_SAFETY_IDENTIFIER`: optional stable pseudonymous identifier.
- `ELEVENLABS_API_KEY`: Classic STT and TTS credential.
- `GOOGLE_TRANSLATE_API_KEY`: Classic text-translation credential.
- `GOOGLE_TRANSLATE_PROJECT_ID`: optional local project metadata; the translation
  client does not send it to Google.

`OPENAI_API_KEY_NAME` and `ELEVENLABS_API_KEY_NAME` are optional descriptive
labels in the template; the runtime does not use them as credentials.

## Observability defaults

Application logging is enabled and asynchronous. Metrics are configured
separately:

```toml
[observability.metrics]
enabled = false
file = "logs/metrics.csv"
retention_days = 14
interval_seconds = 60.0
```

Enabling metrics creates daily files derived from the configured path. Realtime
also reports its own 10-second session snapshots, including latency, audio
duration, send/playback backlog, drops, errors, reconnections, and gate state.
