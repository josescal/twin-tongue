# Twin Tongue

Twin Tongue is a Windows application for bidirectional, real-time speech translation during calls. It connects physical audio devices and two VB-CABLE routes to provide two independent flows:

- `remote_to_agent`: captures the remote participant and plays either original or translated audio through the agent's headphones.
- `agent_to_remote`: captures the agent's microphone and sends either original or translated audio to the calling application.

Each direction can run in translated mode or as direct PCM passthrough. Its immutable
startup type is selected in `config/default.toml`: the existing segmented
STT -> text translation -> TTS pipeline or OpenAI Realtime Translate
speech-to-speech streaming. The control panel reports that selection but does not
change it while Twin Tongue is running.

## Problem and motivation

Language barriers make everyday conversations slower and less effective. In customer service and technical support, they can lead to transfers, repeated explanations, longer resolution times, or prevent the right specialist from helping at all. Most real-time translation tools focus on helping one person understand translated audio; a productive conversation also requires that person to answer and be understood.

This project comes directly from situations I see in my day-to-day work. My goal is to explore a practical tool for companies and individuals who want to communicate more effectively without requiring both participants to speak the same language or change their usual calling application.

Twin Tongue is submitted in the **Productivity and Business** category because its main purpose is to reduce communication friction: helping support teams, specialists, colleagues, and individuals hold a two-way conversation using the language in which each person communicates best.

## How it works

```text
Calling application -> CABLE-A Input -> CABLE-A Output -> Twin Tongue
Twin Tongue -> selected remote_to_agent pipeline -> agent headphones

Agent microphone -> Twin Tongue -> selected agent_to_remote pipeline
Twin Tongue -> CABLE-B Input -> CABLE-B Output -> calling application
```

The Classic pipeline uses:

```text
captured PCM -> Silero VAD -> adaptive segmentation -> ElevenLabs STT
-> Google text translation -> ElevenLabs TTS -> destination
```

The speech-to-speech pipeline uses:

```text
captured PCM -> 24 kHz mono stream -> OpenAI Realtime Translate
-> translated PCM stream -> existing output resampling/routing -> destination
```

The default configuration selects OpenAI Realtime in both directions and starts
both in passthrough. Realtime does not instantiate Silero or the Classic
segmenter, and does not use Classic STT/TTS recording. Its separate diagnostic
capture can write raw input, provider-bound audio, and translated output while
translation is active. Switching either direction to `type = "classic"` restores
the existing Classic behavior for that direction without changing the other.

Audio is captured as 48 kHz `int16` PCM in 20 ms blocks. Callback-driven bounded
queues, streaming resampling, asynchronous logging, independent directional
sessions, and passthrough fallback keep both paths responsive and isolated.

## Core capabilities

- Independent bidirectional pipelines with isolated provider sessions and queues.
- Runtime switching between `passthrough` and `translate` mode per direction.
- Configuration-only selection between Classic and speech-to-speech pipelines per direction.
- English, Spanish, French, and Catalan language selection.
- Classic-only male/female translated voice selection through configured ElevenLabs voices.
- Classic-only Silero VAD, adaptive segmentation, ElevenLabs Realtime STT,
  Google Cloud Translation Basic v2, and ElevenLabs streaming TTS.
- OpenAI Realtime Translate streaming speech-to-speech without local VAD.
- Automatic Windows communications-device discovery and selectable physical devices.
- Fixed CABLE A and CABLE B routing with active call-session visibility.
- A loopback-only local web control panel at `http://127.0.0.1:8765`.
- Classic final transcripts and available OpenAI Realtime translated transcript
  deltas in a left/right conversation view.
- Call-aware Realtime sessions that consume no OpenAI resources while no
  application is connected to the corresponding cable.
- Per-direction requested/effective translation status and original-audio
  fallback during initialization or recovery.
- Physical-microphone health checks based on real callbacks, automatic
  sample-rate/channel negotiation, transactional hot switching, and fallback
  to the last working microphone when an endpoint stops delivering audio.
- Rotating logs, daily CSV metrics, and automated tests.
- Windows distribution packaging through PyInstaller.

## Screenshots

The control panel exposes both translation directions, participant languages,
physical audio devices, passthrough or translation mode, selected pipeline,
operational state, and applications connected to the virtual call routes. Voice
and diagnostic-recording controls appear only when they apply to an active
Classic direction.

![Twin Tongue control panel](docs/images/screenshot-control_panel.png)

The transcription view groups recognized and translated phrases so the agent can review what Twin Tongue understood and what the customer hears.

![Twin Tongue transcription panel](docs/images/screenshot-transcription.png)

## Demo

Watch the [Twin Tongue end-to-end call demo on YouTube](https://youtu.be/kdVc0LEfKAI).

## Current status

Twin Tongue is a working Windows desktop/runtime prototype with real audio routing, provider integrations, a local control panel, diagnostic tools, packaging support, automated tests, and successful end-to-end functional calls. It is not yet a polished consumer application or hosted service.

Current development priorities include:

- simpler first-run setup for non-technical users;
- stronger audio-device and virtual-route validation;
- continued tuning of STT segmentation and false-positive rejection;
- lower end-to-end conversational latency;
- acoustic echo cancellation for robust full-duplex conversation;
- noise reduction before VAD and STT;
- broader reliability validation with real meeting and contact-center software;
- evolution into a distributable Windows service with an installable package and a supported desktop control surface;
- packaging and usability improvements for internal pilots.

## Requirements

- Windows with native Windows Python 3.12 or later.
- VB-CABLE A+B for integration with calling applications.
- An OpenAI API key for the default OpenAI Realtime pipeline.
- ElevenLabs and Google Cloud Translation credentials only for directions
  configured as Classic.
- Headphones, strongly recommended during audio tests.

Do not run hardware audio diagnostics from WSL. PortAudio must access native Windows audio devices.

## Installation

From PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

If script execution is disabled in the current shell, use the virtual-environment interpreter directly:

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
- `GOOGLE_TRANSLATE_PROJECT_ID` as local project metadata only. It is not sent by the translation client.
- `OPENAI_API_KEY` for the OpenAI Realtime Translate pipeline.
- `OPENAI_SAFETY_IDENTIFIER` optionally supplies a stable pseudonymous end-user identifier.

`ELEVENLABS_API_KEY_NAME` and `OPENAI_API_KEY_NAME` are optional descriptive
labels retained in the environment template. The runtime does not use them as
credentials and does not send them to providers.

Never commit or share `.env`. API keys are not written to application logs.

Runtime defaults live in the namespaced schema-v2 `config/default.toml`. Select the immutable startup type for each direction with `pipeline.runtime.<direction>.type`: `classic` or `speech_to_speech`. Classic stages and vendors live below `pipeline.classic`; OpenAI settings live below `pipeline.speech_to_speech`. The control panel displays this selection but cannot change it; restart Twin Tongue after editing the file. See [Configuration namespaces](docs/configuration.md) and [OpenAI Realtime Translate architecture](docs/openai-realtime-translate.md).

The application stores the selected physical input and output devices, interface
language, participant languages, and Classic voice gender in
`config/preferences.json`. Device preferences use stable endpoint IDs and fall
back to the Windows communications defaults while a saved device is unavailable.

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

Open the control panel at `http://127.0.0.1:8765` when the web server is enabled. Both directions start in passthrough mode by default so audio routing can be validated before translation providers are used. Translation can then be enabled independently for each direction.

With `--pipeline both` (the default), only directions whose
`pipeline.runtime.<direction>.enabled` value is `true` are started. Explicitly
requesting a disabled direction is a configuration error.

For OpenAI Realtime, enabling translation arms the direction. The API session is
opened only after Twin Tongue detects a call application on the corresponding
VB-CABLE route. Until then the panel shows “waiting for a call application” and
no audio is sent to OpenAI.

## VB-CABLE setup

Install and validate the two virtual routes before running the complete
application. See the
[audio device and routing architecture](docs/audio-routing-architecture.md)
for the complete calling application → CABLE A → Twin Tongue → CABLE B flow
and the exact WebRTC AEC3 insertion point. Follow
[VB-CABLE setup and validation](docs/vb-cable-setup.md) for installation,
no-code routing tests, cleanup, and troubleshooting.

The normal endpoints are:

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

Tests cover application state, audio buffering and pacing, device discovery, provider behavior, resampling, Classic-only VAD and speech segmentation, pipeline latency policy, logging, metrics, and the local UI server. OpenAI Realtime streams audio directly and never loads the local VAD. Hardware diagnostics remain explicit manual tests because their result depends on the host devices and drivers.

## Logs, metrics, and diagnostic audio

Runtime logs are written asynchronously to `logs/twin-tongue.log`. Rotation size, retained file count, queue capacity, and level are configured in `[observability.logging]`.

When `[observability.metrics].enabled` is `true` (it is `false` by default),
metrics are written to daily `logs/metrics-YYYY-MM-DD.csv` files. They include the
selected engine, queue pressure, discarded blocks and segments, callback gaps,
output underflows, event-loop lag, recording drops, provider activity, playback
latency, VAD, and segmentation counters. Realtime rows additionally include
first-audio and trailing latency, input/output duration, send/playback backlog,
errors, reconnections, and call-session gate state.

Timestamped ElevenLabs transcripts expose word-level `logprob` values. Debug traces record word count, average and minimum log probability, timing, and word data to support analysis of STT false positives.

When `[pipeline.classic.defaults.stt.elevenlabs.audio_capture].enabled` is `true`,
diagnostic audio is recorded automatically for each translated Classic direction.
Writes are buffered and flushed once per second to reduce filesystem and antivirus
overhead. The control panel also provides manual recording controls when at least
one active direction is Classic. Realtime diagnostic capture writes distinct
`captured`, `accepted`, `sent`, `received`, and `played` tracks with a per-call
manifest.
Generated audio, logs, metrics, and preferences are excluded from source control
and distribution packages.

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

Codex was used as an engineering collaborator throughout the project, with GPT-5.6 providing reasoning and implementation support behind repository exploration, code changes, validation, and documentation. This accelerated repetitive and cross-cutting work such as tracing audio flows across modules, keeping configuration and tests aligned, implementing bounded real-time components, and running broad regression checks.

The project owner remained responsible for the product, engineering, and design decisions. These include treating translation as a two-way conversation, focusing on real communication problems observed through day-to-day work, choosing customer support and business communication as primary contexts, using explicit CABLE A and CABLE B routes, supporting passthrough alongside translation, and defining the latency, privacy, and reliability trade-offs.

Codex contributed most strongly where fast iteration and repository-wide consistency mattered: proposing implementation approaches, identifying edge cases, updating tests with production code, checking packaging metadata, and verifying changes with the automated suite. The result is a human-led product and technical implementation accelerated by Codex-assisted engineering.

## Third-party components

Twin Tongue bundles only the Silero VAD ONNX models it uses. Their license is stored beside the models and copied into distribution packages. All provider services remain subject to their respective terms, privacy requirements, and usage charges.
