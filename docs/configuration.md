# Configuration reference

This is the canonical operator reference for `config/default.toml`
(`schema_version = 2`). It explains every shipped parameter, its operational
effect, and the recommendation derived from development and hardware testing.

Changes to `type`, engine bindings, audio geometry, or provider settings require
a Twin Tongue restart. `mode`, languages, voices and physical device preferences
can be changed through the control panel where exposed.

## Configuration model

```text
audio                                      shared audio infrastructure
pipeline.runtime.<direction>               startup and device-side audio format
pipeline.speech_to_speech.defaults         shared Realtime settings
pipeline.speech_to_speech.<direction>      directional Realtime engine binding
pipeline.classic.defaults                  compatibility pipeline defaults
pipeline.classic.<direction>               compatibility directional settings
observability.logging                      event log
observability.metrics                      daily CSV metrics
control_server                             loopback control panel
```

`defaults` are deeply merged with direction-specific values. A directional value
wins. The loader then converts schema v2 into a stable internal configuration, so
runtime modules do not depend on the TOML layout.

The shipped configuration enables both directions, selects
`speech_to_speech`, and starts in `passthrough`. Support should focus on
Realtime unless a direction was explicitly changed to `classic`.

## Units and queue calculations

Audio callback queues use blocks. With the default
`audio.frame_duration_ms = 20`:

```text
queue duration (ms) = capacity_blocks × 20
```

The Realtime send queue uses provider frames:

```text
send queue duration (ms)
  = send_queue_capacity_frames × send_chunk_duration_ms
```

Do not compare capacities without converting them to time. See
[CSV metrics and queue behavior](metrics-reference.md) for the complete flow.

## Root and shared audio

| Parameter | Default | Meaning and recommendation |
| --- | ---: | --- |
| `schema_version` | `2` | Required physical schema. Do not change unless a future migration explicitly requires it. |
| `audio.sample_format` | `"int16"` | PCM format used by the current audio pipeline. Realtime requires PCM16 at the provider boundary; keep this value. |
| `audio.frame_duration_ms` | `20` | Application callback block duration. It matches two 10 ms WebRTC AEC3 frames and gives stable low-latency callbacks. Changing it affects every queue-duration calculation and is not a routine tuning control. |
| `audio.device_poll_interval_seconds` | `5.0` | Interval for heavier device/topology refresh. Shorter polling adds device-enumeration work; keep 5 s unless diagnosing slow hot-plug detection. |
| `audio.physical_capture_exclusive_mode` | `false` | Opens the physical microphone in Windows shared mode. This preserved expected Realtek gain/processing in testing. Enable exclusive mode only after validating signal level and quality on the target device. Virtual cables and playback remain shared. |

### Resampling

| Parameter | Default | Meaning and recommendation |
| --- | ---: | --- |
| `audio.resampling.engine` | `"soxr"` | Streaming sample-rate converter. The runtime currently supports SOXR. If input and output rates are equal, Twin Tongue automatically uses a bit-exact bypass and does not invoke SOXR. |
| `audio.resampling.quality` | `"HQ"` | SOXR quality preset. Keep `HQ`; measured CPU cost was small relative to real-time duration. Lowering quality is unlikely to solve callback or network starvation. |

Realtime's provider boundary is fixed at 24 kHz mono. The shipped endpoints run
at 48 kHz, so capture is converted 48→24 kHz and translated output 24→48 kHz.
Hardware tests found that the current Realtek microphone did not support 24 kHz
in either shared or exclusive mode; VB-CABLE and playback supported 24 kHz only
in exclusive mode, which conflicts with the shared call route. Therefore, setting
all device rates to 24 kHz is not the supported default.

The equal-rate bypass is still useful wherever two adjacent configured rates
match: it preserves PCM bytes and block framing while avoiding unnecessary
resampler state and latency.

### WebRTC AEC3

| Parameter | Default | Meaning and recommendation |
| --- | ---: | --- |
| `audio.aec3.enabled` | `true` | Applies acoustic echo cancellation to the physical microphone direction. Keep enabled for bidirectional calls, especially without perfect headphone isolation. |
| `audio.aec3.stream_delay_ms` | `0` | Explicit capture/render delay hint supplied to AEC3. Zero avoids adding artificial look-ahead. Change only after a measured echo-path analysis. |
| `audio.aec3.render_queue_capacity_blocks` | `100` | Reverse-stream reference capacity: 2 s at 20 ms/block. It absorbs scheduling skew between rendered and captured audio. A larger queue consumes memory and may retain irrelevant reference history; tune only from AEC3 evidence. |

AEC3 receives the exact `remote_to_agent` output callback timeline, including
silence, and converts 20 ms application blocks into 10 ms WebRTC frames. Its
adaptive state is reset between calls.

### Virtual-cable session monitoring

| Parameter | Default | Meaning and recommendation |
| --- | ---: | --- |
| `audio.virtual_cables.session_monitor.poll_interval_seconds` | `1.0` | Frequency of lightweight WASAPI application-session checks. One second balances responsive call detection and low overhead. |
| `audio.virtual_cables.session_monitor.failure_grace_seconds` | `5.0` | How long to preserve the last known session state when observation itself fails. After this, the monitor fails closed. Keep aligned with expected transient Windows query failures. |

This monitor is separate from the Realtime `session_gate` grace: monitor failure
grace handles _unknown observations_; gate disconnect grace handles confirmed
_inactive observations_.

## Runtime direction settings

The same keys exist below `pipeline.runtime.remote_to_agent` and
`pipeline.runtime.agent_to_remote`.

| Parameter | Default | Meaning and recommendation |
| --- | ---: | --- |
| `enabled` | `true` | Whether the direction starts under `--pipeline both`. Explicitly requesting a disabled direction is a configuration error. |
| `mode` | `"passthrough"` | Initial behavior: `passthrough` or `translate`. Passthrough is the safer default because routing can be verified before provider use. The UI can change it at runtime. |
| `type` | `"speech_to_speech"` | Immutable engine family: `speech_to_speech` or `classic`. Keep Realtime for the supported deployment and restart after changing it. |

### `pipeline.runtime.remote_to_agent.audio`

| Parameter | Default | Meaning and recommendation |
| --- | ---: | --- |
| `input_sample_rate` | `48000` | CABLE-A capture rate. Keep at the shared-mode cable rate validated on the host. |
| `input_channels` | `2` | CABLE-A channels. The canonical provider stream is downmixed to mono. |
| `output_sample_rate` | `48000` | Physical playback rate. Keep the native shared-mode device rate. |
| `output_channels` | `1` | Agent playback channel count requested by Twin Tongue. Change only if the target output requires/has been validated with two channels. |

### `pipeline.runtime.agent_to_remote.audio`

| Parameter | Default | Meaning and recommendation |
| --- | ---: | --- |
| `input_sample_rate` | `48000` | Preferred physical microphone rate before runtime device negotiation. |
| `input_channels` | `2` | Preferred microphone channel count. Twin Tongue can negotiate a working physical format and downmixes to canonical mono. |
| `output_sample_rate` | `48000` | CABLE-B render rate. Keep the shared-mode cable rate used by the calling application. |
| `output_channels` | `2` | CABLE-B render channels. Mono translated audio is duplicated as required for the route. |

Do not force 24 kHz merely to avoid resampling without first proving that every
physical and virtual endpoint supports that rate in the required shared graph.
A failed or differently processed microphone is worse than the small measured
SOXR cost.

## OpenAI Realtime

These settings live below:

`pipeline.speech_to_speech.defaults.translation.openai_realtime`

| Parameter | Default | Meaning and recommendation |
| --- | ---: | --- |
| `endpoint` | `wss://api.openai.com/v1/realtime/translations` | Translation WebSocket base endpoint. Keep the official endpoint; the model is added by the client. |
| `model` | `"gpt-realtime-translate"` | Speech-to-speech translation model. Treat model changes as compatibility changes and revalidate protocol events, audio and latency. |
| `sample_rate` | `24000` | Required provider-side PCM rate. Keep 24 kHz. |
| `send_chunk_duration_ms` | `200` | Audio accumulated per provider append. It follows the provider recommendation and gives predictable framing. Smaller chunks increase event overhead; larger chunks add input latency. |
| `input_queue_capacity_blocks` | `25` | Capture-to-processing queue: 500 ms. This protects callbacks from short stalls without permitting long stale input. |
| `send_queue_capacity_frames` | `3` | Network-send queue: 600 ms at 200 ms/frame. It absorbs brief WebSocket backpressure. A growing/full queue means the sender cannot keep up. |
| `received_queue_capacity_blocks` | `150` | Provider-output receive queue: 3,000 ms. It absorbs burst delivery and gives adaptive playout room to recover. Do not reduce below the emergency threshold. |
| `playback_queue_capacity_blocks` | `12` | Hardware playback queue: 240 ms. It separates asynchronous conversion from the callback while keeping local output latency bounded. |
| `reconnect_max_attempts` | `5` | Fast attempts in one retry cycle. Five allows transient recovery without an endless tight loop. |
| `reconnect_base_delay_seconds` | `0.5` | Base for randomized exponential backoff. Delays grow per attempt and are capped internally. |
| `reconnect_cooldown_seconds` | `10.0` | Pause after a fast retry cycle, and shared minimum delay after rate limiting. It prevents both directions repeatedly hitting the same quota in lockstep. |
| `session_setup_timeout_seconds` | `10.0` | Maximum wait for a usable provider session after connect. Increase only for confirmed slow setup, not ordinary audio latency. |
| `close_timeout_seconds` | `5.0` | Time allowed for graceful drain and `session.closed`. A longer value can delay teardown; a shorter value can truncate trailing translation. |
| `metrics_interval_seconds` | `10.0` | Realtime CSV snapshot cadence. Ten seconds is suitable for support without periodic log noise. |
| `log_transcript_deltas` | `false` | Writes every transcript fragment only at DEBUG when enabled. Keep false in support/production; it adds noise and may expose conversation text. It does not disable UI captions or counters. |
| `transcript_ui_update_interval_ms` | `150` | Coalescing interval for UI caption updates. Lower values add browser/server churn; 150 ms remains responsive without coupling UI work to audio receipt. |
| `transcript_segment_idle_ms` | `500` | Closes a displayed translation entry after no new delta. This affects presentation, not provider audio segmentation. |

### Why there are two output queues

The 3-second receive queue absorbs provider bursts and is managed by adaptive
playout. The 240 ms playback queue is the final hardware feeder. Enlarging the
playback queue adds direct audible latency; enlarging the receive queue only
raises the hard safety ceiling and must be paired with sensible adaptive
thresholds.

### Adaptive playout

These keys live below `.adaptive_playout`.

| Parameter | Default | Meaning and recommendation |
| --- | ---: | --- |
| `enabled` | `true` | Enables pitch-preserving backlog recovery and emergency silence-boundary skips. Keep enabled for bursty provider output. |
| `target_backlog_ms` | `1000.0` | Desired upper operating backlog. At or below this, playback stays at 1.00x. |
| `accelerated_backlog_ms` | `2000.0` | Backlog at which the controller may approach maximum speed. |
| `emergency_backlog_ms` | `2800.0` | Trigger for a silence-boundary skip before the 3,000 ms receive queue overflows. It must remain below receive capacity in milliseconds. |
| `recovery_backlog_ms` | `1500.0` | Backlog sought after emergency recovery and the lower hysteresis point before leaving aggressive recovery. |
| `moderate_speed` | `1.10` | Pitch-preserving speed reached at `accelerated_backlog_ms`. Between target and accelerated backlog, speed ramps from 1.05x to this value. |
| `maximum_speed` | `1.15` | Highest validated automatic speed. Higher values recover faster but can reduce naturalness/intelligibility; do not exceed 1.25, the validation limit. |
| `silence_search_ms` | `250.0` | Window searched for the quietest safe cut during emergency recovery. |
| `silence_threshold_dbfs` | `-42.0` | A candidate at or below this level is treated as silence. Raise it only if recovery never finds boundaries; doing so increases the risk of cutting speech. |
| `crossfade_ms` | `15.0` | Crossfade applied after an intentional timeline jump to suppress clicks. |

Validation requires:

```text
target_backlog_ms
  < recovery_backlog_ms
  < accelerated_backlog_ms
  < emergency_backlog_ms
  < received queue duration
```

With the defaults: `1000 < 1500 < 2000 < 2800 < 3000`.

Do not tune from one subjective call. Use at least the backlog p95/p99, time above
target, maximum speed, empty-buffer events and receive discontinuities from
[Metrics](metrics-reference.md). Lower thresholds reduce latency sooner but make
speed changes more frequent. Higher thresholds sound more natural during small
bursts but retain more delay.

### Input transcription

These keys live below `.input_transcription`.

| Parameter | Default | Meaning and recommendation |
| --- | ---: | --- |
| `enabled` | `true` | Requests source-language captions. Disable when source captions are not needed; translated audio/output captions continue independently. |
| `model` | `"gpt-realtime-whisper"` | Source transcription model. Its usage/quota is separate from the translation model. Revalidate before changing it. |

### Realtime diagnostic audio

These keys live below `.audio_capture`.

| Parameter | Default | Meaning and recommendation |
| --- | ---: | --- |
| `enabled` | `true` | Records `captured`, `accepted`, `sent`, `received`, and `played` diagnostic tracks during active translation. Disable where call-audio retention is not authorized. |
| `directory` | `"logs/realtime-audio"` | Local root for call directories, manifests, WAV and timing files. |
| `max_seconds_per_file` | `120.0` | Rotates each track after two minutes so abnormal exits and investigations remain manageable. |
| `write_timing_marks` | `true` | Writes JSONL timing metadata. Useful for queue/timeline analysis but creates additional sensitive diagnostic data. |
| `queue_capacity_blocks` | `500` | Per-writer asynchronous recording queue, about 10 s at 20 ms/block. When full, recording blocks are dropped rather than delaying live audio. |
| `write_buffer_kb` | `64` | Buffered file-write size. Keep 64 KiB unless storage profiling shows a specific need. |
| `flush_interval_seconds` | `1.0` | Flush/header-refresh cadence. One second balances crash readability and filesystem/antivirus overhead. |

The tracks mean:

- `captured`: raw device-side PCM;
- `accepted`: canonical mono after AEC3 on the microphone direction;
- `sent`: PCM successfully written to OpenAI;
- `received`: translated PCM returned by OpenAI;
- `played`: callback-confirmed output PCM.

### Session gate

These keys live below `.session_gate`.

| Parameter | Default | Meaning and recommendation |
| --- | ---: | --- |
| `enabled` | `true` | Opens/sends to OpenAI only while a relevant call application is detected. Keep enabled to avoid idle cost and unintended capture. |
| `disconnect_grace_seconds` | `5.0` | Requires continuous confirmed inactivity for five seconds before closing. This absorbs transient application state changes. Zero makes teardown immediate but can cause reconnect churn. |

`remote_to_agent` watches applications rendering to CABLE-A Input.
`agent_to_remote` watches applications capturing from CABLE-B Output.

### Directional Realtime bindings

| Parameter | Default | Meaning and recommendation |
| --- | ---: | --- |
| `pipeline.speech_to_speech.defaults.translation.engine` | `"openai_realtime"` | Default speech-to-speech engine binding. |
| `pipeline.speech_to_speech.remote_to_agent.translation.engine` | `"openai_realtime"` | Directional binding; keep equal to the implemented default engine. |
| `pipeline.speech_to_speech.agent_to_remote.translation.engine` | `"openai_realtime"` | Directional binding; keep equal to the implemented default engine. |

## Observability

### Event logging

| Parameter | Default | Meaning and recommendation |
| --- | ---: | --- |
| `observability.logging.level` | `"INFO"` | Support-oriented lifecycle and actionable events. Use DEBUG only for a controlled reproduction; restore INFO afterward. |
| `observability.logging.file_enabled` | `true` | Writes the rotating file in addition to the console. Keep enabled on supported installations. |
| `observability.logging.file` | `"logs/twin-tongue.log"` | Base event-log path. |
| `observability.logging.max_size_mb` | `5` | Rotation threshold for each file. |
| `observability.logging.max_files` | `5` | Total active plus retained rotated files; must be at least 2. |
| `observability.logging.queue_capacity` | `1000` | Non-blocking asynchronous log queue. When full, logs are dropped so audio is not delayed. |
| `observability.logging.file_flush_interval_seconds` | `5.0` | Batches ordinary log flushes; errors flush immediately. Reduce only when crash-tail durability outweighs extra I/O. |

Periodic queue, latency, RMS and callback values belong in Metrics and are not
written at INFO.

### CSV metrics

| Parameter | Default | Meaning and recommendation |
| --- | ---: | --- |
| `observability.metrics.enabled` | `true` | Enables daily metrics CSVs. Keep enabled for support. |
| `observability.metrics.file` | `"logs/metrics.csv"` | Base name; the writer inserts `-YYYY-MM-DD` before `.csv`. |
| `observability.metrics.retention_days` | `14` | Calendar-day retention. Adjust to organizational policy and incident-response window. |
| `observability.metrics.interval_seconds` | `60.0` | Classic snapshot cadence. Realtime uses its own 10 s setting above. |

See [CSV metrics reference](metrics-reference.md) for every column and diagnostic
patterns.

## Control server

| Parameter | Default | Meaning and recommendation |
| --- | ---: | --- |
| `control_server.enabled` | `true` | Starts the local control panel. |
| `control_server.host` | `"127.0.0.1"` | Loopback-only binding. Keep loopback; the application rejects/does not require remote exposure. |
| `control_server.port` | `8765` | Local TCP port. Change if occupied; CLI `--web-port` can override it for a run. |

## Environment variables

Secrets belong in `.env`, never in TOML:

| Variable | Used by | Recommendation |
| --- | --- | --- |
| `OPENAI_API_KEY` | Realtime | Required for the shipped pipeline. Never log, commit or include it in support bundles. |
| `OPENAI_SAFETY_IDENTIFIER` | Realtime | Optional stable pseudonymous end-user identifier; do not use raw personal information. |
| `ELEVENLABS_API_KEY` | Classic | Not required when both directions are Realtime. |
| `GOOGLE_TRANSLATE_API_KEY` | Classic | Not required when both directions are Realtime. |
| `GOOGLE_TRANSLATE_PROJECT_ID` | Classic/local metadata | Not sent by the translation client. |
| `OPENAI_API_KEY_NAME` | None | Optional descriptive template label; not a credential and not sent. |
| `ELEVENLABS_API_KEY_NAME` | None | Optional descriptive template label; not a credential and not sent. |

## Classic compatibility appendix

The following settings are parsed and tested but do nothing when both runtime
directions use `type = "speech_to_speech"`. Do not tune them to solve a Realtime
incident.

### Shared Classic behavior

| Parameter | Default | Meaning |
| --- | ---: | --- |
| `pipeline.classic.defaults.stt_max_audio_catchup_ms` | `100` | Maximum paced STT catch-up allowance before stale input protection applies. |
| `pipeline.classic.defaults.retry.max_attempts` | `2` | Attempts for retryable Classic provider calls. |
| `pipeline.classic.defaults.retry.base_delay_seconds` | `0.75` | Initial Classic retry backoff. |
| `pipeline.classic.defaults.vad.engine` | `"silero"` | Default Classic VAD binding. |
| `pipeline.classic.defaults.vad.silero.runtime` | `"onnx"` | Silero inference runtime. |
| `pipeline.classic.defaults.vad.silero.opset_version` | `16` | Bundled ONNX model variant. |

### ElevenLabs STT and Classic recording

| Parameter | Default | Meaning |
| --- | ---: | --- |
| `pipeline.classic.defaults.stt.engine` | `"elevenlabs"` | Classic STT binding. |
| `.stt.elevenlabs.model` | `"scribe_v2_realtime"` | Streaming STT model. |
| `.stt.elevenlabs.audio_format` | `"pcm_16000"` | Raw provider input format; must match sample rate. |
| `.stt.elevenlabs.sample_rate` | `16000` | Provider STT PCM rate. |
| `.stt.elevenlabs.include_timestamps` | `true` | Requests word timing where available. |
| `.stt.elevenlabs.no_verbatim` | `true` | Requests normalized/non-verbatim transcription behavior. |
| `.stt.elevenlabs.keyterms` | `[]` | Optional domain terms. Add only validated, high-value vocabulary. |
| `.stt.elevenlabs.send_chunk_duration_ms` | `100` | STT network frame duration. |
| `.stt.elevenlabs.queue_capacity_blocks` | `50` | Bounded STT send queue. |
| `.stt.elevenlabs.idle_keepalive_seconds` | `5.0` | Keepalive cadence while the Classic STT stream is idle. |
| `.stt.elevenlabs.audio_capture.enabled` | `true` | Enables Classic STT diagnostic recording. |
| `.audio_capture.directory` | `"logs/stt-audio"` | Classic recording directory. |
| `.audio_capture.max_seconds_per_file` | `120.0` | Classic WAV rotation duration. |
| `.audio_capture.write_timing_marks` | `false` | Optional Classic timing JSONL. |
| `.audio_capture.write_buffer_kb` | `64` | Buffered write size. |
| `.audio_capture.flush_interval_seconds` | `1.0` | File flush cadence. |

### Text translation and TTS

| Parameter | Default | Meaning |
| --- | ---: | --- |
| `pipeline.classic.defaults.translation.engine` | `"google_translate_v2"` | Classic text-translation binding. |
| `.translation.google_translate_v2.endpoint` | Google v2 translate URL | Provider endpoint. |
| `.translation.google_translate_v2.format` | `"text"` | Input content format. |
| `pipeline.classic.defaults.tts.engine` | `"elevenlabs"` | Classic TTS binding. |
| `.tts.elevenlabs.model` | `"eleven_flash_v2_5"` | Default TTS model. |
| `.tts.elevenlabs.voice_id` | configured ID | Fallback voice ID. |
| `.tts.elevenlabs.output_format` | `"pcm_16000"` | Raw PCM output format; must match sample rate. |
| `.tts.elevenlabs.sample_rate` | `16000` | TTS PCM rate. |
| `.tts.elevenlabs.speed` | `1.1` | Provider speech speed. |
| `.tts.elevenlabs.language_voices.<language>.male` | configured ID | Male voice for `en`, `fr`, `es`, or `ca`. |
| `.tts.elevenlabs.language_voices.<language>.female` | configured ID | Female voice for `en`, `fr`, `es`, or `ca`. |
| `.tts.elevenlabs.language_models.ca` | `"eleven_v3"` | Catalan-specific TTS model override. |

### Directional Classic settings

Both directions set `voice_gender = "male"` and explicitly bind the supported
engines:

- `.vad.engine = "silero"`;
- `.segmentation.engine = "adaptive"`;
- `.stt.engine = "elevenlabs"`;
- `.translation.engine = "google_translate_v2"`;
- `.tts.engine = "elevenlabs"`.

Latency-protection keys are identical in both directions:

| Parameter | Default | Meaning |
| --- | ---: | --- |
| `.latency_protection.enabled` | `true` | Drops/supersedes stale Classic work. |
| `.playback_backlog_discard_age_seconds` | `8.0` | Age at which stale queued playback can be discarded. |
| `.segment_discard_age_seconds` | `15.0` | Age at which a segment is too old to continue processing; must be at least the playback discard age. |
| `.playback_backlog_segments_to_keep` | `2` | Newest synthesized segments retained during backlog cleanup. |

Directional VAD defaults differ because CABLE-A audio and a physical microphone
have different signal behavior:

| Parameter | Remote→agent | Agent→remote | Meaning |
| --- | ---: | ---: | --- |
| `.vad.silero.threshold` | `0.50` | `0.70` | Probability that activates speech. |
| `.vad.silero.negative_threshold` | `0.35` | `0.50` | Lower hysteresis threshold for ending speech. |
| `.vad.silero.min_speech_duration_ms` | `100` | `200` | Minimum accepted speech duration. |
| `.vad.silero.min_silence_duration_ms` | `350` | `250` | Silence required to end speech. |
| `.vad.silero.speech_pad_ms` | `100` | `100` | Audio retained around detected speech. |

Directional adaptive segmentation:

| Parameter | Remote→agent | Agent→remote | Meaning |
| --- | ---: | ---: | --- |
| `.segmentation.adaptive.minimum_segment_duration_ms` | `1500` | `2200` | Minimum segment before an adaptive boundary. |
| `.preferred_segment_duration_ms` | `2500` | `2500` | Target conversational segment duration. |
| `.short_pause_duration_ms` | `150` | `150` | Pause eligible for an early boundary after other conditions. |
| `.partial_boundary_stability_ms` | `200` | `350` | Time a partial transcript boundary must remain stable. |
| `.maximum_segment_duration_ms` | `5000` | `5000` | Forced maximum segment duration. |

## Safe change procedure

1. Preserve the current TOML and incident metrics.
2. Change one related parameter group at a time.
3. Restart Twin Tongue when changing anything except a UI-exposed runtime choice.
4. Validate passthrough before enabling translation.
5. Test each direction independently, then together.
6. Compare at least a few minutes of metrics before and after the change.
7. Revert if drops, empty-buffer events, callback gaps or voice latency worsen.

Use the [isolated pipeline test runbook](isolated-pipeline-tests.md) for device and
route validation and the [Realtime support runbook](support-runbook.md) for
incident triage.
