# Isolated audio pipeline test runbook

Use this document to qualify devices and routes layer by layer. For an incident
on an already qualified installation, start with the
[Realtime support runbook](support-runbook.md) and preserve the matching
[CSV metrics](metrics-reference.md) before changing configuration.

Use this runbook before testing both Twin Tongue directions together. Each
phase introduces only one new layer, so a failed result has a limited set of
possible causes.

## Preconditions

1. Stop every running Twin Tongue process.
2. Use headphones.
3. Disable **Listen to this device** on physical microphones and virtual
   cables.
4. Keep **Stereo Mix** disabled.
5. Activate the project environment:

   ```powershell
   .\.venv\Scripts\Activate.ps1
   ```

6. In the calling application, use:

   - speaker/output: `CABLE-A Input`;
   - microphone/input: `CABLE-B Output`.

Do not use the 16-channel or `VB-Audio Point` endpoints.

## Phase 0: identify the current endpoint IDs

```powershell
python tools/list_audio_devices.py --host-api WASAPI
```

Record the current IDs for:

- the physical microphone being tested;
- the physical headphones/output;
- `CABLE-A Input` and `CABLE-A Output`;
- `CABLE-B Input` and `CABLE-B Output`.

PortAudio IDs can change after a restart or device change. The commands below
use placeholders such as `<MIC_ID>` rather than assuming a fixed ID.

## Phase 1: physical microphone and headphones only

This test records into memory and plays the result only after recording stops.
It does not use VB-CABLE, WebRTC AEC3, translation, or the calling application.

```powershell
python tools/test_audio_loop.py `
  --input-device <MIC_ID> `
  --output-device <HEADPHONES_ID> `
  --duration 5 `
  --sample-rate 48000 `
  --channels 2 `
  --downmix-to-mono
```

Repeat it separately for every candidate microphone. Say a fixed phrase:

> Physical microphone test, one two three.

Pass criteria:

- the selected microphone is named explicitly in the command;
- both captured-channel levels are printed;
- the recording is clear and at a usable level;
- no remote audio or Windows playback is present;
- speaking close to a different microphone does not produce the same level.

`--downmix-to-mono` applies the same stereo-to-mono conversion used before Echo
Guard and OpenAI. A stereo recording that sounds correct without this option
does not pass the pipeline test unless the downmixed playback is also correct.

When Twin Tongue is already running, the single **Test audio** button in the
web control panel performs the equivalent check without reopening the
microphone. Press it once to start and again to stop and listen; recording
stops automatically after ten seconds if the button is not pressed again. It
reports RMS and peak for each captured channel, applies the production
stereo-to-mono downmix, reports the mono level, and plays that exact result
duplicated across both channels of the selected physical output. WebRTC AEC3
is not applied by this test. It warns about missing or low signal, clipping,
channel imbalance, and destructive downmix cancellation.

Do not continue with a microphone that is silent or extremely low.

Optionally measure callback stability:

```powershell
python tools/audio_device_benchmark.py `
  --mode input `
  --input-device <MIC_ID> `
  --duration 30 `
  --sample-rate 48000 `
  --input-channels 2
```

## Phase 2: CABLE A only

This verifies the incoming virtual route without Twin Tongue or translation.

1. Keep the calling application's output on `CABLE-A Input`.
2. Start:

   ```powershell
   python tools/audio_bridge.py `
     --input-device <CABLE_A_OUTPUT_ID> `
     --output-device <HEADPHONES_ID> `
     --duration 30 `
     --sample-rate 48000 `
     --channels 2
   ```

3. Play test audio from the calling application.

Expected route:

```text
Calling application -> CABLE-A Input -> CABLE-A Output
  -> diagnostic bridge -> physical headphones
```

Pass criteria:

- remote/test audio is heard once through the headphones;
- the physical microphone is not present;
- no CABLE B endpoint participates;
- queue/drop counters remain near zero.

## Phase 3: CABLE B only

This verifies the outgoing virtual route without Twin Tongue, WebRTC AEC3, or
translation.

1. Open the calling application's microphone test so it actively captures
   `CABLE-B Output`.
2. Start:

   ```powershell
   python tools/audio_bridge.py `
     --input-device <MIC_ID> `
     --output-device <CABLE_B_INPUT_ID> `
     --duration 30 `
     --sample-rate 48000 `
     --channels 2
   ```

3. Speak the fixed phrase:

   > CABLE B microphone route, one two three.

Expected route:

```text
Physical microphone -> diagnostic bridge -> CABLE-B Input
  -> CABLE-B Output -> calling application microphone test
```

Pass criteria:

- the microphone test receives the fixed phrase;
- application playback is not present in the captured microphone;
- CABLE A does not participate;
- disabling or stopping the bridge immediately removes the microphone signal
  from the calling application.

## Phase 4: one Twin Tongue direction in passthrough

### 4A. `remote_to_agent`

```powershell
python .\src\main.py `
  --pipeline remote_to_agent `
  --web `
  --duration 120
```

Keep this direction in passthrough and play audio from the calling application.

Pass criteria:

- Twin Tongue captures only `CABLE-A Output`;
- audio reaches the physical headphones;
- the physical microphone and CABLE B are absent from this process;
- no OpenAI session is required.

### 4B. `agent_to_remote`

Stop 4A, then run:

```powershell
python .\src\main.py `
  --pipeline agent_to_remote `
  --web `
  --duration 120
```

Keep the calling application's microphone test active on `CABLE-B Output`.

Pass criteria:

- Twin Tongue reports the selected physical microphone as its active input;
- the fixed phrase reaches the calling application's microphone test through
  CABLE B;
- CABLE A and physical playback are absent from this process;
- no OpenAI session is required.

Running only `agent_to_remote` deliberately provides no far-end AEC3 render
reference. Microphone audio must still pass without an artificial look-ahead
delay.

## Phase 5: WebRTC AEC3 integration

Run the adapter and native-binding tests:

```powershell
python -m pytest tests/test_webrtc_aec3.py -v
```

They verify:

- render frames reach WebRTC before the corresponding capture frames;
- 20 ms application blocks are split into the required 10 ms frames;
- disabled and native-error paths preserve capture audio bit for bit;
- call reset discards the previous adaptive state;
- the installed native WebRTC Audio Processing binding accepts a real frame.

These tests validate the integration contract. Acoustic cancellation quality
must be validated in phase 8 with the actual microphone, headphones/speaker,
driver processing, and call timing.

Then compare the physical capture graph in exclusive and shared mode:

```powershell
python tools/test_echo_isolation.py `
  --input <MIC_ID> `
  --output <HEADPHONES_ID>

python tools/test_echo_isolation.py `
  --input <MIC_ID> `
  --output <HEADPHONES_ID> `
  --shared
```

This controlled test measures whether deterministic physical playback appears
inside the raw microphone capture. It validates the Windows/driver capture
path; it does not use the translation provider.

## Phase 6: Classic provider components

These checks are optional when both directions use OpenAI Realtime, but they
isolate the Classic STT, text translation, and TTS providers.

### STT from a physical microphone

```powershell
python tools/test_realtime_stt.py `
  --input-device <MIC_ID> `
  --duration 30 `
  --sample-rate 48000 `
  --channels 2 `
  --language en `
  --yes
```

### Text translation without audio

```powershell
python tools/test_translation.py `
  "I need assistance" `
  --source-language en `
  --target-language es
```

### TTS without capture, STT, or translation

```powershell
python tools/test_tts.py `
  "Necesito asistencia" `
  --language es `
  --output artifacts/tests/tts/isolated-es.wav
```

Each tool loads its own provider credential from `.env` and does not expose the
credential in its output.

## Phase 7: one OpenAI Realtime direction

Realtime remains integrated with the live application-session gate, so test
one production direction at a time.

### 7A. `agent_to_remote`

1. Run only `agent_to_remote` as in phase 4B.
2. Keep the calling application actively capturing `CABLE-B Output`.
3. Enable translation only for this direction.
4. Wait for `Ready`.
5. Say:

   > I am calling from Banco Santander. I need assistance.

6. Verify the translated audio in the calling application's microphone test or
   remote endpoint.

Required evidence:

- `captured`: the English physical-microphone phrase;
- `accepted`: the microphone phrase after WebRTC AEC3;
- `sent`: non-empty 24 kHz PCM sent to OpenAI;
- `received`: translated Spanish PCM returned by OpenAI before local playback;
- `played`: translated Spanish PCM written to `CABLE-B Input`.

### 7B. `remote_to_agent`

1. Stop 7A and run only `remote_to_agent`.
2. Keep the calling application rendering to `CABLE-A Input`.
3. Enable translation only for this direction.
4. Wait for `Ready`.
5. Play or speak a fixed remote-language phrase.

Required evidence:

- `captured`: CABLE A input only;
- `accepted`: canonical mono remote audio;
- `sent`: non-empty provider input;
- `received`: translated PCM returned by OpenAI before local playback;
- `played`: translated PCM confirmed at the physical output callback.

## Phase 8: combined echo-reference validation

Run both directions only after phases 1–7 pass. WebRTC AEC3 is enabled in the
default configuration:

```powershell
python .\src\main.py --pipeline both --web
```

Use three separate periods:

1. remote audio while the local microphone is silent;
2. local speech while remote playback is silent;
3. simultaneous local and remote speech.

The diagnostic comparison should show:

- bot/remote leakage may exist in `agent_to_remote-captured`;
- correlated leakage is removed from `agent_to_remote-accepted`;
- local uncorrelated speech remains in `accepted`;
- only `accepted` audio proceeds to passthrough or `sent`.

Analyze a completed pair with:

```powershell
python tools/analyze_echo_path.py `
  <REMOTE_TO_AGENT_PLAYED_WAV> `
  <AGENT_TO_REMOTE_CAPTURED_WAV>
```

Use the per-call manifests to confirm the exact device names, formats, times,
track files, queue counters, and AEC3 statistics.

## Stop rule

Do not compensate for a failing phase by changing a later layer. For example:

- do not tune AEC3 delay when phase 1 selected the wrong microphone;
- do not change translation prompts when phase 3 cannot cross CABLE B;
- do not diagnose the calling application's STT from `captured`; inspect
  `played` for the audio that actually reached CABLE B;
- do not enable both directions until each one passes independently.
