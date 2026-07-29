# Twin Tongue documentation

## Support and operations

- [Realtime support runbook](support-runbook.md): first response, evidence and
  symptom-based diagnosis.
- [CSV metrics reference](metrics-reference.md): every metric, queue diagrams
  and interpretation.
- [Realtime log event reference](log-events-reference.md): lifecycle, retry,
  recovery and device events with support actions.
- [Configuration reference](configuration.md): every shipped parameter,
  recommended values and development findings.
- [Diagnostic and support tools](tools.md): focused commands after the failing
  layer has been identified.
- [Isolated pipeline tests](isolated-pipeline-tests.md): staged device, cable,
  AEC3 and provider qualification.

## Architecture and installation

- [Audio device and routing architecture](audio-routing-architecture.md):
  endpoint ownership, signal flow and AEC3 insertion.
- [VB-CABLE A+B setup](vb-cable-setup.md): Windows installation and route
  validation.
- [OpenAI Realtime Translate integration](openai-realtime-translate.md):
  provider contract, session lifecycle and implementation design.
- [Windows distribution guide](distribution.md): packaging, signing and
  recipient setup.

The supported operational path is OpenAI Realtime in both directions. Classic
documentation remains where needed to explain compatibility configuration, but
support investigations should not use Classic VAD/STT/TTS counters or settings
for a Realtime incident.
