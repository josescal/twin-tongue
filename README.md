# Twin Tongue

Twin Tongue is a Windows application for bidirectional, real-time speech translation during calls. It connects physical audio devices and two VB-CABLE routes to provide two independent flows:

- `remote_to_agent`: captures the remote participant, transcribes and translates their speech, and plays the result through the agent's headphones.
- `agent_to_remote`: captures the agent's microphone, transcribes and translates their speech, and sends the synthesized result to the calling application.

Each direction can run in translated mode or as direct PCM passthrough. The local control panel exposes pipeline state, participant languages, voice selection, final transcripts, and operational status.

## How it works

```text
Remote application -> CABLE-A Input -> CABLE-A Output -> Twin Tongue
Twin Tongue -> STT -> translation -> TTS -> agent headphones

Agent microphone -> Twin Tongue -> STT -> translation -> TTS
Twin Tongue -> CABLE-B Input -> CABLE-B Output -> calling application
```

Audio is captured as 48 kHz `int16` PCM in 20 ms blocks. Input is converted to mono and resampled once to 16 kHz for Silero VAD and ElevenLabs Realtime STT. Translated text is synthesized with ElevenLabs streaming TTS, resampled for the selected output, and played incrementally. Bounded queues, stale-segment protection, asynchronous logging, and periodic metrics keep the real-time path responsive.

Core capabilities include:

- Independent bidirectional pipelines with isolated provider sessions and queues.
- English, Spanish, French, and Catalan language selection at runtime.
- Silero VAD through bundled ONNX models.
- ElevenLabs Realtime STT and streaming TTS.
- Google Cloud Translation Basic v2.
- Automatic Windows communications-device discovery.
- Fixed CABLE A and CABLE B routing with active-session visibility.
- A loopback-only web control panel at `http://127.0.0.1:8765`.
- Rotating logs and daily CSV metrics with bounded retention.

## Requirements

- Windows and native Windows Python 3.12 or later.
- VB-CABLE A+B for integration with calling applications.
- ElevenLabs and Google Cloud Translation credentials.
- Headphones are strongly recommended during audio tests.

Do not run hardware audio diagnostics from WSL; PortAudio must access the native Windows devices.

## Installation

From PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

If script execution is disabled for the current shell, use the virtual-environment interpreter directly:

```powershell
.\.venv\Scripts\python.exe .\src\main.py
```

## Credentials and configuration

Copy the environment template and provide your credentials:

```powershell
Copy-Item .env.example .env
```

Twin Tongue reads these values from `.env`:

- `ELEVENLABS_API_KEY` for speech-to-text and text-to-speech.
- `GOOGLE_TRANSLATE_API_KEY` for Google Cloud Translation Basic v2.
- `GOOGLE_TRANSLATE_PROJECT_ID` as local project metadata only; it is not sent by the translation client.

Never commit or share `.env`. API keys are not written to application logs.

Runtime defaults live in `config/default.toml`. This file defines audio formats, languages, providers, pipeline devices, voice detection, segmentation, latency protection, TTS voices, logging, metrics, and the local server. The application stores only the user's voice-gender preference in `config/audio-device-preferences.json`.

## Running the application

Activate the environment and start both directions:

```powershell
.\.venv\Scripts\Activate.ps1
python .\src\main.py
```

Run only one direction or stop automatically after a fixed duration:

```powershell
python .\src\main.py --pipeline remote_to_agent
python .\src\main.py --pipeline agent_to_remote --duration 60
```

The default duration is `0`, which runs until `Ctrl+C`. The web interface is enabled by configuration and accepts loopback addresses only. It can be disabled or moved to another local port:

```powershell
python .\src\main.py --no-web
python .\src\main.py --web-port 9000
```

The application starts in passthrough mode by default. Translation can be enabled independently for each direction from the control panel. Changing a source language renews only the affected STT session; changing voice gender applies without restarting the application.

## VB-CABLE setup

Install and validate the two virtual routes before running the complete application. Follow [VB-CABLE setup and validation](docs/vb-cable-setup.md) for installation, no-code routing tests, cleanup, and troubleshooting.

The normal endpoints are required:

- Remote to agent: the calling application outputs to `CABLE-A Input`; Twin Tongue captures `CABLE-A Output`.
- Agent to remote: Twin Tongue outputs to `CABLE-B Input`; the calling application captures `CABLE-B Output`.

Do not select the 16-channel variants.

## Diagnostic tools

The `tools/` directory contains focused diagnostics for device discovery, callback stability, capture/playback, continuous monitoring, STT, translation, and TTS. Use the narrowest tool that can isolate the failing layer before running the full pipeline.

See [Diagnostic and support tools](docs/tools.md) for commands, options, expected output, and safety notes.

## Tests

The project uses the standard library's `unittest` runner:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests cover application state, audio buffering and pacing, device discovery, provider behavior, resampling, VAD, speech segmentation, pipeline latency policy, logging, metrics, and the local UI server. Hardware diagnostics remain explicit manual tests because their result depends on the host devices and drivers.

## Logs and metrics

Runtime logs are written asynchronously to `logs/twin-tongue.log`. Rotation size, retained file count, queue capacity, and level are configured in `[logging]`.

Metrics are written to daily `logs/metrics-YYYY-MM-DD.csv` files. They include queue pressure, discarded blocks and segments, provider activity, playback latency, VAD, and segmentation counters. Generated logs and metrics are excluded from source control and distribution packages.

## Building a Windows distribution

Create a portable `win64` directory and ZIP:

```powershell
.\packaging\build_distribution.ps1
```

Use `-SkipDependencyInstall` when the build dependencies are already available. Output is written under `artifacts/distribution/` and includes a SHA-256 checksum. The package excludes `.env`, private keys, logs, metrics, and local preferences.

For controlled internal testing, a reusable self-signed identity can be created and supplied to the build:

```powershell
.\tools\create_signing_certificate.ps1
.\packaging\build_distribution.ps1 `
  -SigningCertificateThumbprint CERTIFICATE_THUMBPRINT
```

A self-signed certificate is not appropriate for public distribution and does not replace organizational security approval. See [Distribution guide](docs/distribution.md) for package contents, certificate handling, and recipient instructions.

## Project structure

```text
config/      Runtime defaults and local preference location
docs/        Project, operations, and support documentation
packaging/   Build scripts, certificates, and bundled driver assets
src/         Application, audio, providers, pipelines, and local UI
tests/       Unit and synthetic integration tests
tools/       Focused diagnostics and support utilities
```

## Collaboration with Codex and GPT-5.6

Codex was used as an engineering collaborator throughout the project, with GPT-5.6 providing the reasoning and implementation support behind repository exploration, code changes, validation, and documentation. This accelerated repetitive and cross-cutting work: tracing audio flows across modules, keeping configuration and tests aligned, implementing bounded real-time components, finding stale naming references, and running broad regression checks after changes.

The project owner remained responsible for the key product, engineering, and design decisions. Those decisions include delivering two independently controlled translation directions, using CABLE A and CABLE B as explicit routes, supporting passthrough alongside translation, keeping the control server loopback-only, exposing only final transcripts, selecting the supported languages and providers, and choosing latency and retention behavior appropriate for live calls. Codex helped turn those decisions into consistent code, tests, packaging, and operational guidance rather than deciding the product direction autonomously.

GPT-5.6 and Codex contributed most strongly where fast iteration and repository-wide consistency mattered: proposing implementation approaches, identifying edge cases, updating tests with production code, checking imports and packaging metadata, and verifying the final result with the full automated suite. Human review and judgment were used to evaluate behavior, security boundaries, audio-routing assumptions, and the quality of the user experience. The final result is therefore a collaborative implementation: human-led product intent and technical trade-offs, accelerated by Codex-assisted engineering and verification.

## Third-party components

Twin Tongue bundles only the Silero VAD ONNX models it uses. Their license is stored beside the models and copied into distribution packages. All provider services remain subject to their respective terms, privacy requirements, and usage charges.
