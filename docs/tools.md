# Diagnostic and support tools

Twin Tongue includes focused command-line tools for validating one layer of the audio and provider stack at a time. Run them from the repository root with the virtual environment activated:

For a complete device → cable → passthrough → Echo Guard → translation
sequence, follow the
[isolated audio pipeline test runbook](isolated-pipeline-tests.md).

```powershell
.\.venv\Scripts\Activate.ps1
```

Use headphones at a moderate volume for every tool that records or plays audio. Device identifiers can change after a restart or hardware change, so list devices again before relying on a saved identifier.

## Recommended diagnostic order

1. `list_audio_devices.py` — confirm that PortAudio can see the required endpoints.
2. `audio_device_benchmark.py` — measure physical-device callback stability.
3. `test_audio_loop.py` — validate sequential recording and playback.
4. `audio_bridge.py` — validate continuous PCM routing and buffering.
5. `test_realtime_stt.py` — validate capture, resampling, VAD, and ElevenLabs STT.
6. `test_translation.py` — validate Google Cloud Translation independently.
7. `test_tts.py` — validate ElevenLabs synthesis independently.
8. `src/main.py` — validate OpenAI Realtime or the integrated Classic pipelines.

## `list_audio_devices.py`

Lists devices reported by SoundDevice and PortAudio, including identifier, name, host API, input/output channel counts, default sample rate, and default-device status.

```powershell
python tools/list_audio_devices.py
python tools/list_audio_devices.py --inputs
python tools/list_audio_devices.py --outputs
python tools/list_audio_devices.py --host-api WASAPI
```

Use the WASAPI-qualified name or numeric identifier when the same Windows device appears through several host APIs.

## `audio_device_benchmark.py`

Measures PortAudio callback stability without storing captured audio or producing an audible signal. It reports callback interval jitter, late callbacks, deadline misses, xruns, effective sample rate, clock drift, CPU load, and stream latency.

```powershell
python tools/audio_device_benchmark.py --mode duplex --duration 30
python tools/audio_device_benchmark.py --mode input --duration 30
python tools/audio_device_benchmark.py --mode output --duration 30
```

Use `--input-device` and `--output-device` to override the Windows communications defaults. Add `--json-output artifacts/tests/audio-device-benchmark/result.json` for machine-readable output. Exit code `0` means the configured health thresholds passed, `2` means the measurement completed but detected an unhealthy stream, and `1` means the stream could not be configured or opened.

## `test_audio_loop.py`

Records PCM into memory, stops capture, and then plays the recording. It intentionally avoids live passthrough, which makes input and output failures easier to distinguish.

```powershell
python tools/test_audio_loop.py
python tools/test_audio_loop.py --input-device 4 --output-device 8 --duration 5
```

Optional PCM overrides include `--sample-rate`, `--channels`, `--dtype`, and `--frame-duration-ms`. The selected input and output must both support the requested format; this tool does not resample or convert it.

## `audio_bridge.py`

Continuously forwards one PCM input to one output using independent queued streams. It prebuffers audio to absorb small clock differences and prints periodic queue and callback statistics.

```powershell
python tools/audio_bridge.py `
  --input-device "CABLE-A Output" `
  --output-device "Headphones" `
  --duration 30
```

The defaults come from `config/default.toml`. Use `--sample-rate`, `--channels`, `--dtype`, `--frame-duration-ms`, and `--buffer-ms` when testing a specific device format. Stop the bridge with `Ctrl+C`.

## `test_realtime_stt.py`

Captures PCM from a required input device, converts it to the configured ElevenLabs format, applies voice detection and segmentation, and streams it to ElevenLabs Realtime STT. Partial transcripts update in place; final transcripts remain visible.

```powershell
python tools/test_realtime_stt.py --input-device "CABLE-A Output" --duration 60
```

The tool loads `ELEVENLABS_API_KEY` from `.env`. Use `--yes` to skip the interactive confirmation and `--help` to inspect VAD, segmentation, language, format, and timing overrides. A healthy run should establish the session, produce partial and final transcripts while speech is present, keep overflow/drop counts near zero, and exit cleanly.

## `test_translation.py`

Sends one text request to Google Cloud Translation Basic v2 without using any audio component. This isolates credentials, service availability, language selection, and translation latency.

```powershell
python tools/test_translation.py
python tools/test_translation.py "Good morning" `
  --source-language en `
  --target-language es
```

The tool loads `GOOGLE_TRANSLATE_API_KEY` from `.env` and never prints the key. It displays the source text, translated text, and request latency.

## `test_tts.py`

Synthesizes one WAV file with ElevenLabs without capture, STT, or translation. It reports time to first byte, total latency, and output size.

```powershell
python tools/test_tts.py
python tools/test_tts.py `
  "How can I help you today?" `
  --language en `
  --voice-gender female `
  --output artifacts/tests/tts/sample.wav
```

The tool uses the model, voices, output format, and speed from `config/default.toml`. Explicit `--voice-id`, `--speed`, or `--output` options take precedence. Generated audio belongs under `artifacts/`, which is excluded from source control.

## OpenAI Realtime integrated check

There is no separate production-equivalent Realtime test tool. Realtime depends
on the live VB-CABLE application-session gate, directional routing, and streaming
playback, so validate it through the application:

```powershell
python .\src\main.py
```

Then:

1. open `http://127.0.0.1:8765`;
2. confirm the calling application appears on CABLE A and CABLE B;
3. enable only one translation direction;
4. wait for `Ready`;
5. speak through that direction and verify the destination and transcript;
6. repeat for the opposite direction.

Without a connected call application the panel should show that translation is
waiting for a call and no OpenAI session should be created. Realtime uses
bounded capture/send/playback queues. When
`pipeline.speech_to_speech.openai_realtime.audio_capture.enabled` is true, it
writes `captured`, `accepted`, `sent`, and `played` WAV/JSONL tracks plus a
per-call manifest below the configured directory. Inspect
`logs/twin-tongue.log` for structured connection and queue events. Enable
`[observability.metrics]` only when CSV measurements are needed.

## Common failure checks

- `ModuleNotFoundError`: activate `.venv` or run the tool with `.\.venv\Scripts\python.exe`.
- No matching device: rerun `list_audio_devices.py --host-api WASAPI` and use a qualified name or current identifier.
- Ambiguous device: avoid short name fragments that match multiple PortAudio endpoints.
- No STT/TTS/translation response: verify `.env`, provider access, network connectivity, and language/model compatibility.
- Drop or overflow counters increase: close competing audio applications, check the benchmark, and verify the configured sample rate and channels.
- Realtime stays unavailable: verify `OPENAI_API_KEY`, cable application
  detection, network access, and the configured endpoint/model. Twin Tongue uses
  original-audio passthrough while retrying.
