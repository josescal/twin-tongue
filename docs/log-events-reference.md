# Realtime log event reference

`logs/twin-tongue.log` is the support event timeline. Periodic latency, RMS,
queue depth and callback measurements belong in the daily
[Metrics CSV](metrics-reference.md), not in this log.

## Reading and filtering

```text
2026-07-29 14:03:12.481 INFO [agent_to_remote]
event=realtime_translation_session_prepared target_language=es audio_allowed=True
```

The line contains local timestamp, severity, affected direction, a stable
`event` name, and contextual `key=value` fields. Filter a direction or severity:

```powershell
Select-String `
  -Path .\logs\twin-tongue.log* `
  -Pattern "\[agent_to_remote\]| ERROR | WARNING "
```

Filter one event:

```powershell
Select-String `
  -Path .\logs\twin-tongue.log* `
  -Pattern "event=realtime_translation_backlog_recovered"
```

One historical event is not necessarily an active fault. Give priority to a
repeating ERROR/WARNING or an event whose matching cumulative metric increases
across consecutive rows.

## Application and pipeline

| Event | Meaning | Support action |
| --- | --- | --- |
| `application_started` / `application_stopped` | Normal process boundaries. | Establish the run containing the incident. |
| `application_degraded` | At least one direction could not remain healthy. | Identify its preceding failure and retry events. |
| `application_failed` | The application cannot continue normally. | Preserve traceback and preceding device/provider events. |
| `translation_engine_started` | Supervisor selected the configured engine. | Confirm `openai_realtime` for the supported path. |
| `pipeline_started` / `pipeline_stopped` | One direction opened/closed. | Confirm devices, formats and direction. |
| `pipeline_status_changed` | Effective state changed. | Explains initializing, waiting, ready, draining, reconnecting and unavailable UI states. |
| `realtime_translation_pipeline_failed` | Unhandled Realtime direction failure. | Preserve traceback, metrics and manifest. |
| `translation_pipeline_failed` | Supervisor observed a direction failure. | Correlate with the immediately preceding engine event. |

## Call gate and mode

| Event | Meaning | Support action |
| --- | --- | --- |
| `realtime_call_state_changed` | Stabilized gate permission changed; includes route, request and raw observation. | Verify the app uses CABLE-A Input/CABLE-B Output and compare gate metrics. |
| `realtime_translation_mode_changed` | Translation was disabled; translated output drains before passthrough. | A short drain is normal; repeated or stuck draining is not. |
| `realtime_translation_source_ended` | Source ended and graceful provider close began. | Look for successful teardown or a close/drain timeout. |
| `realtime_translation_session_prepared` | WebSocket setup completed for the target language. | `audio_allowed=false` is valid while the call gate is closed. |

## Provider connection and retry

| Event | Meaning | Support action |
| --- | --- | --- |
| `realtime_translation_connection_failed` | Initial/reconnect setup failed. | Inspect credential, network, endpoint, quota or timeout error. |
| `realtime_translation_connection_lost` | An established session failed. | Compare error/reconnection metrics and network stability. |
| `realtime_translation_send_failed` | A provider audio append failed. | Expect reconnect; inspect send drops and pending voice latency. |
| `realtime_translation_session_error` | Provider sent a session error event. | Preserve provider code/message; avoid repeated toggles during rate limiting. |
| `realtime_translation_retry_cycle_exhausted` | Fast attempts were consumed; cooldown begins. | Passthrough remains fallback. Resolve root cause and wait through cooldown. |
| `realtime_session_close_timeout` | Graceful provider close exceeded its timeout. | Trailing translation may be incomplete. |
| `realtime_session_close_failed` | Provider close failed for another reason. | Preserve the error and inspect the next session. |
| `realtime_translation_send_drain_timeout` | Provider-bound frames did not drain on close. | Possible lost source tail; inspect send queue/network. |
| `realtime_translation_receive_drain_timeout` | Returned audio did not drain on close. | Possible lost translated tail; inspect receive/playback backlog. |
| `realtime_translation_source_close_failed` | Direction aborted after graceful close failed. | Correlate with the specific close/drain event. |

Backoff is the randomized exponentially increasing delay between fast attempts.
Cooldown is the longer pause after the fast cycle and the shared minimum wait
after rate limits, preventing both directions from reconnecting in lockstep.

## Queue and audio continuity

| Event | Meaning | Support action |
| --- | --- | --- |
| `realtime_translation_backlog_recovered` | Receive backlog crossed the emergency watermark; stale audio was removed near a quiet boundary and crossfaded. | Check dropped duration, discontinuities, backlog p95/p99 and maximum speed. |
| `realtime_translation_received_queue_discarded` | Provider-output blocks were cleared during lifecycle/recovery. | Read `reason`; normal mode cleanup differs from sustained pressure. |
| `realtime_translation_send_queue_discarded` | Provider-bound frames were cleared. | Read `reason`; old frames become invalid after connection/mode changes. |
| `audio_input_overflow` | PortAudio reported capture overflow. | Benchmark input and inspect capture callback gaps. |
| `audio_output_underflow` | PortAudio reported output underflow. | Benchmark output and correlate with playback metrics. |
| `audio_played_observer_failed` | Callback-confirmed observer failed. | AEC3 reference/diagnostic observation may be incomplete. |
| `webrtc_aec3_processing_failed` | AEC3 failed and microphone PCM was passed through. | Echo protection is degraded but speech is preserved. |
| `webrtc_aec3_capture_bypassed` | A block did not meet AEC3 frame requirements. | Sustained occurrences are abnormal with 20 ms/48 kHz defaults. |

Normal queue cleanup is DEBUG-level. Use Metrics for continuous pressure and
INFO/WARNING recovery events for exceptional interventions.

## Device and diagnostic evidence

| Event | Meaning | Support action |
| --- | --- | --- |
| `audio_input_stream_opened` / `audio_output_stream_opened` | Stream opened with device, mode, rate and channels. | Verify the intended endpoint and format. |
| `audio_input_format_negotiated` | Physical capture selected a supported fallback format. | Confirm signal quality; canonical conversion remains automatic. |
| `audio_device_changed` | Runtime switched an endpoint. | Correlate with incident time and callback gaps. |
| `physical_audio_device_quarantined` | Preferred physical endpoint failed health checks and fallback was selected. | Test the quarantined device independently. |
| `preferred_audio_device_unavailable` / `preferred_audio_device_replaced` | Saved device is absent and replacement is active. | Confirm selection in the panel. |
| `audio_device_refresh_failed` / `audio_device_refresh_recovered` | Topology polling failed/recovered. | Short recovered failure may be transient; repeated failure needs host investigation. |
| `diagnostic_wav_capture_failed` | Recording stopped to protect live audio. | Check disk/permissions; live translation may remain healthy. |
| `diagnostic_wav_header_repaired` | Interrupted WAV header was repaired. | Informational; inspect only if it belongs to the call. |
| `diagnostic_call_manifest_closed` | Per-call evidence manifest finalized. | Use it to locate tracks and timeline. |
| `metrics_write_failed` | CSV append failed; retry occurs next interval. | Check path, permissions and free space; expect a metrics gap. |
| `file_logging_unavailable` | File could not open; console-only logs remain. | Restore path/permissions before reproducing. |

## DEBUG-only discovery

`realtime_translation_event_observed`,
`realtime_translation_transcript_delta`, ordinary queue cleanup and diagnostic
file-creation details are discovery data. Enable DEBUG only for a controlled
reproduction: it increases volume and may include conversation text. Restore
`observability.logging.level = "INFO"` afterward.

Escalate when an ERROR repeats, a direction remains unavailable after a complete
retry/cooldown cycle, emergency recovery repeatedly cuts speech, callback
overflows/underflows increase on a benchmarked device, or server and refreshed
UI state disagree. Include the narrow evidence set from the
[support runbook](support-runbook.md), never `.env`.
