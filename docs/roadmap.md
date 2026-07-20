# Technical roadmap

Twin Tongue has completed functional end-to-end calls with bidirectional capture, transcription, translation, synthesized speech, virtual audio routing, and live conversation visibility. These tests validate the complete product loop and provide recorded audio and debug traces for measuring the remaining limitations.

## Next reliability work

Future versions will focus on three connected areas:

- **Lower conversational latency.** Reduce provider startup time, segment-boundary delay, synthesis time to first byte, buffering, and playback backlog while preserving intelligibility and stable turn-taking.
- **Acoustic echo cancellation.** Replace the current configurable `agent_to_remote` barge-in guard with reference-based echo cancellation. The guard prevents locally played remote translations from being captured and translated back to the caller, but it temporarily limits agent barge-in. Echo cancellation should preserve full-duplex conversation without that feedback loop.
- **Noise reduction.** Add preprocessing before VAD and STT so background noise, room reflections, and low-level playback leakage are less likely to activate speech detection or degrade transcription confidence.

The target is a 100% reliable experience under documented and supported audio conditions. Reaching that target requires repeatable end-to-end tests across headsets, microphones, Windows audio drivers, virtual-cable configurations, calling applications, network conditions, languages, and speaker accents. Reliability will therefore be measured from captured evidence rather than inferred from isolated provider success.
