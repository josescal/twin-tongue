# Configuration namespaces

`config/default.toml` uses `schema_version = 2`. Engine selection is immutable for
the lifetime of the process and is changed only by editing this file and restarting
Twin Tongue.

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

## Selecting a pipeline type

```toml
[pipeline.runtime.remote_to_agent]
type = "classic"

[pipeline.runtime.agent_to_remote]
type = "speech_to_speech"
```

`classic` activates local VAD, segmentation, STT, text translation, and TTS.
`speech_to_speech` streams capture audio directly to the selected speech translation
engine and never loads or executes local VAD or segmentation for that direction.
