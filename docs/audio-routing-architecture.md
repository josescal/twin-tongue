# Audio device and routing architecture

This document explains how a calling application, VB-CABLE A+B, the physical
audio devices, and Twin Tongue connect to each other. The endpoint names are
initially confusing: for a virtual cable, `Input` is the playback side where an
application writes audio, while `Output` is the recording side where another
application captures that same audio.

![Twin Tongue audio routing architecture](images/audio-routing-architecture.png)

## How CABLE B connects the calling application to Twin Tongue

The calling application does not capture the physical microphone directly
during normal Twin Tongue operation. The complete outgoing route is:

```text
Physical microphone
  -> Twin Tongue captures the microphone
  -> Echo Guard
  -> passthrough or translation
  -> Twin Tongue plays PCM to CABLE-B Input
  -> the VB-CABLE B driver transports it internally
  -> the calling application captures the same PCM from CABLE-B Output
  -> remote participant
```

`CABLE-B Input` and `CABLE-B Output` are the two sides of one virtual device;
they are not two independent sources. Twin Tongue opens `CABLE-B Input` as an
audio output. The calling application opens `CABLE-B Output` as its microphone.
That is the connection between them.

## Required endpoints

| Consumer | Setting | Endpoint |
|---|---|---|
| Calling application | Speaker/output | `CABLE-A Input` |
| Calling application | Microphone/input | `CABLE-B Output` |
| Twin Tongue | Remote audio capture | `CABLE-A Output` |
| Twin Tongue | Physical microphone capture | Selected physical input |
| Twin Tongue | Agent listening output | Selected physical headphones/speaker |
| Twin Tongue | Audio sent to the calling application | `CABLE-B Input` |

Use the normal stereo CABLE endpoints. Do not select the 16-channel or
`VB-Audio Point` variants.

## Incoming direction: `remote_to_agent`

1. The calling application renders the remote participant through its selected
   speaker/output, `CABLE-A Input`.
2. VB-CABLE A exposes that PCM on its recording side, `CABLE-A Output`.
3. Twin Tongue captures `CABLE-A Output`.
4. In passthrough, Twin Tongue forwards the original audio. In translation
   mode, the direction sends mono 24 kHz PCM to its own OpenAI Realtime session
   and receives translated audio.
5. Twin Tongue plays the selected result through the physical headphones or
   speaker.
6. The PortAudio playback callback confirms the PCM that was actually delivered
   to the physical output. That confirmed signal becomes the far-end reference
   used by Echo Guard in the opposite direction.

Using callback-confirmed playback is important: Echo Guard compares the
microphone with what could really have reached it acoustically, rather than
with the original CABLE A stream or with translated audio that was generated
but never played.

## Outgoing direction: `agent_to_remote`

1. Twin Tongue captures the selected physical microphone through the Windows
   shared graph so its Realtek gain and processing match the native Windows
   microphone test. Exclusive mode remains configurable for controlled tests.
   CABLE endpoints stay shared because the calling application and Twin Tongue
   must use the two sides simultaneously.
2. The raw endpoint PCM is written to the diagnostic `captured` track.
3. The PCM is converted to mono and, when enabled, passed through Echo Guard.
4. The resulting canonical mono PCM is written to `accepted`.
5. In passthrough, this accepted PCM is written directly to `CABLE-B Input`.
   In translation mode, it is resampled and sent to the independent
   `agent_to_remote` OpenAI Realtime session.
6. The original or translated PCM is played into `CABLE-B Input`.
7. VB-CABLE B exposes it on `CABLE-B Output`, which the calling application is
   already capturing as its microphone/input.
8. The calling application sends that audio to the remote participant.

The two translation directions use independent sessions. Activating one does
not automatically activate the other.

## Exact Echo Guard position

Echo Guard can run only in `agent_to_remote`, after physical microphone capture
and channel conversion, and before either passthrough or API transmission. It
is currently disabled by default so low-level local speech cannot be rejected:

```text
physical capture -> captured -> mono -> optional ECHO GUARD -> accepted
                                                    |-> passthrough -> CABLE-B Input
                                                    `-> resample -> sent -> OpenAI
```

The shared echo-reference bus is populated only by audio confirmed as played
in `remote_to_agent`:

```text
remote_to_agent physical playback callback
  -> mono reference history
  -> delayed-envelope correlation
  -> agent_to_remote Echo Guard decision
```

When explicitly enabled, Echo Guard:

- buffers a 200 ms microphone window;
- searches the playback-reference history over delays up to 500 ms;
- suppresses a block only when its level envelope reaches the configured
  correlation threshold;
- preserves weak microphone audio when it is not correlated with far-end
  playback, leaving speech/noise classification to the downstream VAD;
- records correlation, suppression, and near-end counters in the call
  manifest.

This is a reference-driven echo guard, not a full adaptive acoustic echo
canceller that estimates and subtracts the room impulse response.

The Realtime insertion point is implemented in
[`src/pipelines/openai_realtime.py`](../src/pipelines/openai_realtime.py), and
the correlation decision is implemented in
[`src/audio/echo_guard.py`](../src/audio/echo_guard.py). The Classic pipeline
uses the same logical position and shared reference.

## Call-aware session gate

Twin Tongue monitors the Windows application sessions attached to the cable
endpoints:

- `remote_to_agent` watches the calling application rendering to
  `CABLE-A Input`;
- `agent_to_remote` watches the calling application capturing from
  `CABLE-B Output`.

Enabling translation arms the direction, but its API session is opened only
when the corresponding application session is active. Passthrough remains
local and does not require an API session.

## Diagnostic audio stages

Each direction separates the evidence into four tracks:

| Track | Exact meaning |
|---|---|
| `captured` | Raw PCM received from the input endpoint. For `agent_to_remote`, this is the physical microphone; for `remote_to_agent`, this is CABLE A. |
| `accepted` | Canonical mono PCM after the optional Echo Guard. The guard can be active only for `agent_to_remote` and is disabled by default. |
| `sent` | PCM successfully written to the translation provider. It is normally empty in passthrough. |
| `played` | PCM confirmed by the output callback: physical playback for `remote_to_agent`, or PCM written to CABLE B for `agent_to_remote`. |

Diagnostic capture also includes:

- CABLE A recording even while the direction is in passthrough;
- JSONL timing marks for each track;
- a per-call JSON manifest with devices, formats, opening/closing times,
  gate events, track paths, queue counters, and Echo Guard statistics;
- periodic WAV-header refresh and startup repair for files left incomplete by
  an abnormal shutdown.

These stages make it possible to determine whether a signal was lost at the
physical endpoint, rejected by Echo Guard, not sent to the provider, or not
delivered to the output device.

## Device resilience and latency protection

The integrated audio protections are:

- stable Windows endpoint identity instead of relying only on volatile
  PortAudio indexes;
- health probing and first-callback validation;
- quarantine of an unresponsive physical device;
- automatic replacement by a working endpoint, persisted as the new
  selection and shown in the web interface;
- configurable WASAPI shared/exclusive physical capture, using shared mode by
  default to retain the validated Realtek gain and audio processing;
- bounded asynchronous capture, network-send, and playback queues;
- dropping of stale/backlogged audio rather than emitting an old translation
  late;
- independent session, retry, and status handling for each direction.

## Windows settings that must remain disabled

During normal Twin Tongue operation:

- disable **Listen to this device** on every microphone and virtual cable;
- keep **Stereo Mix** disabled;
- do not make a physical microphone listen through CABLE B;
- do not make CABLE A listen through the physical output;
- use headphones when testing to reduce real acoustic coupling.

Windows **Listen** bridges are useful only for the isolated no-code cable tests
described in [VB-CABLE setup and validation](vb-cable-setup.md). Disable them
before starting Twin Tongue.

For layer-by-layer acceptance tests, follow the
[isolated audio pipeline test runbook](isolated-pipeline-tests.md).
