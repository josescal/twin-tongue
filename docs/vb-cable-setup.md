# VB-CABLE A+B setup and validation

This guide helps a support technician install and validate the two virtual audio routes used by Twin Tongue on Windows.

After routing passes, use the [Realtime support runbook](support-runbook.md) for
application incidents. Queue pressure and callback health are explained in the
[CSV metrics reference](metrics-reference.md).

## Routing model

See [Audio device and routing architecture](audio-routing-architecture.md) for
the complete diagram, the exact CABLE B connection to the calling application,
and the `captured` → WebRTC AEC3 → `accepted` → `sent` → `received` → `played`
diagnostic
stages.

Each virtual cable has a playback endpoint named `Input` and a recording endpoint named `Output`:

```text
An application plays to CABLE-A Input
  -> audio crosses the virtual cable
  -> another application records from CABLE-A Output
```

Twin Tongue uses the normal stereo endpoints:

- CABLE A carries the remote participant toward Twin Tongue.
- CABLE B carries the translated agent voice toward the calling application.

Ignore `CABLE-A Input 16ch`, `CABLE-A Output 16ch`, `CABLE-B Input 16ch`, and `CABLE-B Output 16ch` for this project.

## Prerequisites

- Windows administrator access for driver installation.
- VB-CABLE A+B driver archive supplied with the distribution.
- A physical microphone and headphones.
- A browser or calling application with selectable input and output devices.

Use headphones to prevent feedback.

## Install CABLE A and CABLE B

1. Extract `VBCABLE_A_B_Driver_Pack45.zip` completely.
2. Open the extracted directory.
3. Right-click the 64-bit setup executable and choose **Run as administrator**. Use the 32-bit installer only on a 32-bit Windows installation.
4. Select **Install Driver** and accept the Windows prompts.
5. Restart Windows when requested.
6. Press `Win+R`, run `mmsys.cpl`, and confirm that the normal CABLE A and CABLE B endpoints appear on the Playback and Recording tabs.

## Validate agent-to-remote routing without code

This temporary Windows bridge confirms that the physical microphone can reach the calling application through CABLE B.

1. In `mmsys.cpl`, open the **Recording** tab.
2. Open the physical microphone properties and select **Listen**.
3. Enable **Listen to this device**.
4. Set **Playback through this device** to `CABLE-B Input`.
5. Apply the change.
6. In the browser or calling application, select `CABLE-B Output` as the microphone.
7. Start its microphone test and speak.

The route is correct when the application detects the physical microphone through CABLE B.

```text
Physical microphone -> Windows Listen bridge -> CABLE-B Input
  -> CABLE-B Output -> calling application microphone
```

## Validate remote-to-agent routing without code

This temporary bridge confirms that audio from the calling application can cross CABLE A and reach the headphones.

1. In `mmsys.cpl`, open the **Recording** tab.
2. Open `CABLE-A Output` properties and select **Listen**.
3. Enable **Listen to this device**.
4. Set **Playback through this device** to the physical headphones.
5. Apply the change.
6. In the browser or calling application, select `CABLE-A Input` as its speaker/output device.
7. Play test audio.

The route is correct when the test audio is heard through the physical headphones.

```text
Calling application output -> CABLE-A Input -> CABLE-A Output
  -> Windows Listen bridge -> physical headphones
```

The Windows Listen feature adds latency. These tests validate routing only, not Twin Tongue performance.

## Clean up after the no-code test

Disable every temporary **Listen to this device** bridge created above before starting Twin Tongue. Leaving one enabled can cause echo, duplicate audio, or routing loops.

Then configure the calling application for normal Twin Tongue operation:

- Speaker/output: `CABLE-A Input`.
- Microphone/input: `CABLE-B Output`.

Twin Tongue captures `CABLE-A Output`, captures the physical communications microphone, plays remote translations to the physical communications output, and sends agent translations to `CABLE-B Input`.

Twin Tongue also monitors active Windows application sessions on these routes.
For Realtime translation:

- `remote_to_agent` opens its API session only when a call application renders
  audio to CABLE A;
- `agent_to_remote` opens its API session only when a call application captures
  audio from CABLE B.

The control panel should list the calling application under both cable
connections. If it does not, translation remains armed but shows that it is
waiting for a call application and consumes no OpenAI session.

## Troubleshooting

### CABLE endpoints are missing

- Restart Windows after installing the driver.
- Check both Playback and Recording tabs in `mmsys.cpl`.
- Enable **Show Disabled Devices** and **Show Disconnected Devices**.
- Reinstall the correct driver as administrator if necessary.

### The calling application receives no microphone audio

- Confirm its microphone is `CABLE-B Output`.
- During the no-code test only, confirm that the physical microphone's Listen destination is `CABLE-B Input`.
- Confirm that the physical microphone level moves in `mmsys.cpl`.
- Check Windows microphone privacy permissions.

### Incoming audio is not heard

- Confirm the calling application's output is `CABLE-A Input`.
- During the no-code test only, confirm that `CABLE-A Output` listens through the physical headphones.
- Verify that the headphones can play normal Windows audio.

### Echo, doubled audio, or a loop

- Disable all Windows Listen bridges after validation.
- Make sure the calling application is not also using the physical microphone or headphones directly.
- Check that CABLE A and CABLE B have not been reversed.
- During testing, the calling application or Windows may monitor the same
  microphone through another route. A duplicate line in the control panel is
  not itself proof that Twin Tongue sent audio twice; verify the physical
  routes first.

### Translation is armed but no audio or transcript appears

- Confirm the call application is listed on the relevant cable in the control
  panel.
- Wait for the direction to report `Ready`; while it is initializing,
  reconnecting, unavailable, or waiting for a call, original audio is used.
- Confirm `OPENAI_API_KEY` is present when the configured type is
  `speech_to_speech`.
- Check `logs/twin-tongue.log` for connection, timeout, retry-cycle, or audio
  queue events.
- Changing languages closes the old directional session and opens a new one for
  the new target language; a short return to passthrough is expected.

## Support checklist

- [ ] CABLE A and CABLE B are installed.
- [ ] Normal endpoints are used; 16-channel variants are ignored.
- [ ] `CABLE-A Input` is the calling application's output.
- [ ] `CABLE-B Output` is the calling application's microphone.
- [ ] The CABLE B no-code test reaches the microphone test.
- [ ] The CABLE A no-code test reaches the headphones.
- [ ] All temporary Windows Listen bridges are disabled afterward.
- [ ] Headphones are used to prevent feedback.
- [ ] The calling application appears under both cable connections in the panel.
