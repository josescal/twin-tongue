# Diagnostic and support tools

This is the operator catalog for the commands in `tools/`. Each tool isolates
one layer; a pass should never be interpreted as proof that later layers work.
For a live Realtime incident, begin with the
[support runbook](support-runbook.md), [CSV metrics reference](metrics-reference.md)
and [log event reference](log-events-reference.md), then select the narrowest
tool for the suspected layer.

Run tools from the repository root in native Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Alternatively prefix commands with `.\.venv\Scripts\python.exe`. Do not run
hardware audio diagnostics from WSL. Use headphones at moderate volume. PortAudio
device IDs can change after a restart or hardware change, so list them again
before relying on a saved ID.

For the full device → cable → passthrough → AEC3 → Realtime sequence, follow the
[isolated pipeline test runbook](isolated-pipeline-tests.md).

## Tool selection matrix

| Suspected layer | Tool | What a pass proves | What it does not prove |
| --- | --- | --- | --- |
| Device discovery | `list_audio_devices.py` | PortAudio/WASAPI exposes the endpoint in the expected input/output direction. | The device sustains the configured format. |
| Callback/driver stability | `audio_device_benchmark.py` | The selected stream runs at the tested format with acceptable callback timing. | Signal quality, routing, provider, or AEC3 quality. |
| Physical capture/playback | `test_audio_loop.py` | The input records usable PCM and the output plays it after capture stops. | Continuous full-duplex timing or cable routing. |
| One continuous PCM route | `audio_bridge.py` | Input → bounded queue → output works without a provider. | Resampling, OpenAI, AEC3, or the opposite route. |
| Physical echo path | `test_echo_isolation.py` | Measures whether known playback leaks into the raw microphone and estimates delay. | AEC3 cancellation quality inside Twin Tongue. |
| Recorded echo evidence | `analyze_echo_path.py` | Offline correlation, delay, coherence and attenuation between timestamped WAVs. | Live device health outside those recordings. |
| Classic STT | `test_realtime_stt.py` | Capture, conversion, Silero/segmentation and ElevenLabs STT work together. | OpenAI Realtime, which bypasses this path. |
| Classic text translation | `test_translation.py` | Google credential/request/language path works without audio. | Realtime speech translation. |
| Classic synthesis | `test_tts.py` | ElevenLabs creates PCM/WAV with the selected model and voice. | Realtime translated output. |
| Production Realtime | `src/main.py` | Gate, routing, provider session, adaptive queues and actual playback work together. | Use the earlier tools to isolate a failure found here. |

## Recommended Realtime order

1. `list_audio_devices.py` — verify physical endpoints and CABLE A/B.
2. `audio_device_benchmark.py` — qualify callback stability.
3. `test_audio_loop.py` — qualify raw physical signal and exact mono downmix.
4. `audio_bridge.py` — validate CABLE A, then CABLE B, independently.
5. `test_echo_isolation.py` — use only when echo/leakage is suspected.
6. `src/main.py` — test one Realtime direction, then the other.
7. `analyze_echo_path.py` — analyze the captured evidence if echo remains.

The three Classic provider tools are optional compatibility diagnostics, not
steps in a Realtime investigation.

## `list_audio_devices.py`

Lists PortAudio endpoints with ID, name, host API, channel capacities, default
sample rate and default status.

```powershell
python tools/list_audio_devices.py
python tools/list_audio_devices.py --inputs
python tools/list_audio_devices.py --outputs
python tools/list_audio_devices.py --host-api WASAPI
```

Use the exact WASAPI-qualified name or current numeric ID when a Windows endpoint
appears through several host APIs.

Support interpretation:

- missing CABLE A/B: repair VB-CABLE installation before testing Twin Tongue;
- multiple matches: use the exact name or ID, not a short fragment;
- correct listing but stream-open failure: benchmark the device at the configured
  rate, channels and frame duration.

The listing proves discovery only. It does not open or exercise a stream.

## `audio_device_benchmark.py`

Measures PortAudio callbacks without saving input or producing audible output.
It reports interval jitter, late callbacks, deadline misses, xruns, effective
sample rate, clock drift, CPU load and stream latency.

```powershell
python tools/audio_device_benchmark.py --mode duplex --duration 30
python tools/audio_device_benchmark.py --mode input --duration 30
python tools/audio_device_benchmark.py --mode output --duration 30
```

Useful options:

```powershell
python tools/audio_device_benchmark.py `
  --mode input `
  --input-device <MIC_ID> `
  --sample-rate 48000 `
  --input-channels 2 `
  --frame-duration-ms 20 `
  --duration 30 `
  --json-output artifacts/tests/audio-device-benchmark/microphone.json
```

- exit `0`: configured health thresholds passed;
- exit `2`: measurement completed but the stream was unhealthy;
- exit `1`: format/device configuration or stream opening failed.

For Realtime, test 48 kHz, the configured channel count and 20 ms frames. Late
callbacks, deadline misses, xruns or drift reproduce below the provider layer;
changing OpenAI queue settings will not repair them.

## `test_audio_loop.py`

Records PCM into memory, closes capture and then plays it. Separating the phases
makes input and output failures easy to distinguish.

```powershell
python tools/test_audio_loop.py
python tools/test_audio_loop.py `
  --input-device <MIC_ID> `
  --output-device <HEADPHONES_ID> `
  --duration 5 `
  --sample-rate 48000 `
  --channels 2 `
  --downmix-to-mono
```

`--downmix-to-mono` applies Twin Tongue's exact 2→1 channel conversion and can
reveal destructive phase cancellation hidden when stereo channels are heard
separately. Other overrides are `--dtype` and `--frame-duration-ms`.

The input and output must both support the requested format. This tool does not
resample, use VB-CABLE, run AEC3, or call a provider.

Pass when the intended microphone is clearly audible after recording, level is
usable without clipping, the mono result remains clear, and playback uses the
intended output.

## `audio_bridge.py`

Continuously forwards one PCM input to one output with independent bounded
queues. It prebuffers audio to absorb small clock differences and prints queue
and callback statistics.

```powershell
python tools/audio_bridge.py `
  --input-device "CABLE-A Output" `
  --output-device "Headphones" `
  --duration 30
```

The defaults come from `config/default.toml`. Overrides include
`--sample-rate`, `--channels`, `--dtype`, `--frame-duration-ms` and
`--buffer-ms`. Stop with `Ctrl+C`.

A healthy route has stable callbacks and near-zero drops/underflows. Test CABLE A
and CABLE B separately using phases 2 and 3 of the
[isolated runbook](isolated-pipeline-tests.md). The bridge does not resample,
run AEC3, or call OpenAI, so a failure belongs to format, Windows routing, device
clock/callback behavior, or local scheduling.

## `test_echo_isolation.py`

Plays a deterministic low-level two-tone envelope through a physical output
while measuring the raw physical microphone. It reports the best delay,
level-envelope correlation, baseline level and playback-window level as JSON.

```powershell
python tools/test_echo_isolation.py `
  --input <MIC_ID> `
  --output <HEADPHONES_ID>

python tools/test_echo_isolation.py `
  --input <MIC_ID> `
  --output <HEADPHONES_ID> `
  --shared
```

The first command uses WASAPI exclusive microphone capture; `--shared` provides
the controlled comparison used by the supported default. Higher positive
correlation means the microphone level follows the known playback envelope more
closely. Interpret it with the baseline/playback-window dBFS change—there is no
universal pass threshold across rooms and headsets.

Use it when the remote side hears local playback or shared/exclusive microphone
behavior differs. It measures the physical path only and does not instantiate
WebRTC AEC3 or a provider. It emits tones and must not run during a live call.

## `analyze_echo_path.py`

Compares two existing PCM16 diagnostic WAVs offline. It uses their adjacent
timing JSONL files to align them and reports path delay, energy/waveform
correlation, speech-band coherence and attenuation.

```powershell
python tools/analyze_echo_path.py `
  <REMOTE_TO_AGENT_PLAYED_WAV> `
  <AGENT_TO_REMOTE_CAPTURED_WAV>
```

The first argument is the played render reference; the second is raw microphone
capture. Both need the same sample rate and a matching `.jsonl` beginning with
`capture_started`. Use `--min-delay-ms` and `--max-delay-ms` only if the physical
delay is known to fall outside the default 0–1500 ms search.

The tool never opens devices and never modifies recordings. To evaluate AEC3,
compare leakage visible in `captured` with the corresponding `accepted` track;
one correlation value alone is not a universal pass/fail result.

## `test_realtime_stt.py` (Classic only)

Captures a required input, converts it to the configured ElevenLabs format,
applies Silero VAD and Classic segmentation, and streams to ElevenLabs Realtime
STT. Partial transcripts update in place and final transcripts remain visible.

```powershell
python tools/test_realtime_stt.py `
  --input-device "CABLE-A Output" `
  --duration 60 `
  --yes
```

It loads `ELEVENLABS_API_KEY` from `.env`. A healthy run connects, produces
partial/final transcripts during speech, keeps overflow/drop counts near zero
and exits cleanly. Use `--help` for VAD, segmentation, language, format and timing
overrides.

Do not use this result to infer the health of `gpt-realtime-translate`: the
supported Realtime pipeline bypasses Silero, Classic segmentation and
ElevenLabs STT.

## `test_translation.py` (Classic only)

Sends one text request to Google Cloud Translation Basic v2 without audio. It
isolates credential, service, language selection and request latency.

```powershell
python tools/test_translation.py "Good morning" `
  --source-language en `
  --target-language es
```

It loads `GOOGLE_TRANSLATE_API_KEY` from `.env`, never prints the key, and prints
source, translation and latency. It has no bearing on OpenAI Realtime.

## `test_tts.py` (Classic only)

Synthesizes one ElevenLabs WAV without capture, STT or translation and reports
time to first byte, total latency and output size.

```powershell
python tools/test_tts.py `
  "How can I help you today?" `
  --language en `
  --voice-gender female `
  --output artifacts/tests/tts/sample.wav
```

Model, voices, format and speed default from `config/default.toml`.
`--voice-id`, `--speed` and `--output` override them. Generated audio belongs
under `artifacts/`, which is excluded from source control. This tool has no
bearing on Realtime translated playback.

## OpenAI Realtime integrated check

There is deliberately no isolated tool pretending to reproduce production
Realtime. The pipeline depends on the live VB-CABLE application-session gate,
directional routing, WebSocket receive timing, adaptive playout and hardware
callback.

```powershell
python .\src\main.py
```

Then:

1. Open `http://127.0.0.1:8765`.
2. Confirm the calling application appears on CABLE A and CABLE B.
3. Enable one translation direction.
4. Wait for `Ready`.
5. Speak through it and verify destination audio and captions.
6. Filter Metrics for that direction.
7. Disable it, then repeat for the opposite direction.

Without a connected application, `waiting for a call` is expected and no OpenAI
session should be created. When Realtime diagnostic capture is enabled, it
writes `captured`, `accepted`, `sent`, `received`, `played` and a call manifest
below `logs/realtime-audio`.

Use:

- the [metrics reference](metrics-reference.md) for queues, signal and latency;
- the [log event reference](log-events-reference.md) for state/retry/recovery;
- the five tracks to locate where audio changed or disappeared.

## Common failures

| Failure | Next action |
| --- | --- |
| `ModuleNotFoundError` | Activate `.venv` or use `.\.venv\Scripts\python.exe`. |
| No matching device | Rerun `list_audio_devices.py --host-api WASAPI`; use an exact current name/ID. |
| Ambiguous device | Avoid fragments matching several PortAudio endpoints. |
| Format/open failure | Benchmark that exact endpoint/rate/channel combination. |
| Drop/overflow counters rise | Close competing apps, inspect callback gaps and verify format. |
| Classic provider does not respond | Verify the matching `.env` credential, access and model/language. |
| Realtime remains unavailable | Verify `OPENAI_API_KEY`, call detection, network, endpoint/model and retry events. |
| Realtime is choppy | Compare receive backlog with playback empty/underflow/callback-gap metrics, then `received` versus `played`. |

Never paste `.env` into a command, log, ticket, or support bundle.
