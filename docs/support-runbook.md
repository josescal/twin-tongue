# Realtime support runbook

Use this runbook for the deployed OpenAI Realtime pipeline. The Classic pipeline
is retained in the codebase for compatibility but is not part of the normal
support path.

## Start here

1. Record the incident time and affected direction:
   `remote_to_agent` (customer to agent) or `agent_to_remote` (agent to customer).
2. Check the control panel for requested mode, effective mode, operational state,
   call-application detection, and selected devices.
3. Read `logs/twin-tongue.log` around the incident for lifecycle changes and
   actionable warnings/errors using the
   [Realtime log event reference](log-events-reference.md).
4. Open `logs/metrics-YYYY-MM-DD.csv`, filter by direction, and compare consecutive
   rows using the [CSV metrics reference](metrics-reference.md).
5. Only then inspect diagnostic audio or run a focused hardware tool.

Do not start by changing several queue or latency settings. Preserve the failing
evidence first and isolate whether the problem is capture, provider transport,
receive backlog, or physical playback.

## Expected route

```text
Calling application -> CABLE-A Input -> CABLE-A Output -> Twin Tongue
Twin Tongue -> remote_to_agent -> agent headphones

Agent microphone -> Twin Tongue -> agent_to_remote -> CABLE-B Input
CABLE-B Output -> calling application microphone input
```

The calling application must use `CABLE-A Input` as its speaker/output and
`CABLE-B Output` as its microphone/input. Do not select the 16-channel endpoints.

## Evidence and purpose

| Evidence | Use it for |
| --- | --- |
| Control panel | Current requested/effective state, device selection, call detection. |
| `twin-tongue.log` | Starts/stops, device changes, gate transitions, recovery, reconnects, failures. |
| Daily metrics CSV | Queue pressure, callback health, latency, signal level, drops, reconnections. |
| Realtime call manifest | Exact devices/formats, track names, timeline and AEC3/recovery counters for one call. |
| `captured.wav` | Raw endpoint signal before canonical conversion/AEC3. |
| `accepted.wav` | Canonical input accepted after microphone AEC3 processing. |
| `sent.wav` | Exact 24 kHz mono PCM successfully sent to OpenAI. |
| `received.wav` | Provider-returned translated PCM before local output conversion. |
| `played.wav` | Audio actually emitted by the PortAudio callback, including recovery behavior. |

The WAV tracks are diagnostic recordings and may contain personal data. Collect
or share them only under the applicable retention and privacy policy.

## Symptom runbooks

### Translation switch stays on “Applying change…”

1. Wait for the effective state or a visible failure; connecting can take longer
   than the UI request itself.
2. Confirm the relevant call application is attached to CABLE A/B. With the
   session gate enabled, translation may be armed while the provider session
   legitimately waits for a call.
3. Check whether `realtime_session_gate_observation` is `active`, `inactive`, or
   `unknown` and whether `realtime_session_gate_audio_allowed` is true.
4. Correlate with session/reconnect events in the log.
5. Refresh the panel only to recover a stale browser view; the server's effective
   state is authoritative. If refresh changes the display without a new server
   transition, record it as a UI-state incident rather than an audio failure.

### Translated audio is choppy

1. Check whether `realtime_playback_empty_buffer_events` increased during speech.
2. Check `translated_output_underflows` and
   `realtime_playback_maximum_callback_gap_ms`.
3. Inspect receive backlog percentiles and adaptive speed. A high backlog with
   no empty events indicates delayed/bursty delivery; empty events with a low
   backlog indicate starvation.
4. Compare `received.wav` with `played.wav`:
   - clean `received`, choppy `played`: local queue/callback/device issue;
   - choppy or gapped `received`: provider/network arrival issue;
   - emergency receive discontinuities: latency protection intentionally skipped
     stale audio.
5. Disable diagnostic recording temporarily only if
   `recording_dropped_blocks`, callback gaps, or host disk pressure indicate it
   is contributing to overload.

### Translation is smooth but too late

Check `realtime_voice_latency_last_ms`, `realtime_voice_latency_average_ms`,
`realtime_backlog_p95_ms`, time above target, current speed, and receive
discontinuities. A rising backlog with speed at 1.15x means adaptive playout is
already at the configured limit. Do not increase maximum speed beyond the
validated range merely to hide provider/network bursts; it can reduce
intelligibility.

### No input or very low input

Check input RMS/peak, `capture_blocks`, callback gaps and overflows. Verify the
selected endpoint with:

```powershell
.\.venv\Scripts\python.exe .\tools\list_audio_devices.py --host-api WASAPI
```

Then use the control panel audio test or phase 1 of the
[isolated pipeline runbook](isolated-pipeline-tests.md). Shared-mode physical
capture is the supported default because it preserved normal Realtek gain and
processing during development.

### Captions appear but translated audio does not

Confirm `realtime_output_audio_duration_ms` and output signal level increase.
Then compare `received.wav` and `played.wav`. If output duration grows but the
playback queue repeatedly empties, investigate conversion/output scheduling. If
output duration does not grow, correlate provider errors and the call manifest.

### Session repeatedly connects and disconnects

Check raw gate observation versus stabilized call state. Short raw inactive
periods should be absorbed by the configured 5-second disconnect grace. Repeated
stabilized deactivations indicate that the calling application really releases
the endpoint, session discovery is failing for longer than its grace, or the
wrong cable endpoint is selected.

### The remote side hears local playback/echo

Use headphones first. Verify that `agent_to_remote` captures the intended
physical microphone and that AEC3 is enabled. Compare `captured` with `accepted`;
correlated remote playback may exist in `captured` but should be reduced in
`accepted`. Follow phase 8 of the
[isolated pipeline runbook](isolated-pipeline-tests.md).

## Support log policy

The default `INFO` log is intentionally event-oriented. Normal periodic queue,
latency, RMS, transcript-delta and callback measurements belong in Metrics, not
the log. Useful support events include:

- pipeline and audio stream lifecycle;
- call gate activation/deactivation;
- translation requested, waiting, ready, draining or unavailable;
- device changes and fallback;
- retry/cooldown/reconnection;
- queue recovery or intentional discontinuity;
- diagnostic capture failure;
- configuration, authentication, rate-limit and provider errors.

Set `observability.logging.level = "DEBUG"` only for a reproduced investigation.
DEBUG can include protocol discovery, transcript fragments, normal queue cleanup
and diagnostic-file details, so restore `INFO` afterward.

## Escalation package

Provide a narrow time window rather than an entire workstation history:

- affected direction and local timestamp;
- application version/commit if known;
- calling application;
- selected input/output device names;
- effective configuration with secrets removed;
- matching log and metrics slice;
- matching call manifest;
- WAV tracks only with explicit authorization.

Never include `.env` or API keys. State whether the problem reproduces in
passthrough. If passthrough also fails, prioritize Windows/device/cable routing;
if only translation fails, prioritize gate, provider, receive backlog and
translated playback.
