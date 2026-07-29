"""User-triggered checks for the physical microphone and playback path."""

from array import array
import math
from threading import Event
import time

import sounddevice as sd

from audio.capture import QueuedAudioInput, active_audio_input
from audio.pcm import RecordedAudio, convert_int16_channels
from audio.playback import play_audio
from audio.portaudio import (
    AudioDeviceError,
    device_name,
    query_devices,
    validate_output_settings,
)

MICROPHONE_TEST_SECONDS = 1.0
SPEAKER_TEST_SECONDS = 0.45
PHYSICAL_PATH_TEST_MAX_SECONDS = 10.0
TEST_TONE_FREQUENCY_HZ = 660.0
TEST_TONE_AMPLITUDE = 0.18
LOW_SIGNAL_PEAK_DBFS = -30.0
LOW_SIGNAL_RMS_DBFS = -50.0


def test_physical_audio_path(
    input_device: int,
    output_device: int,
    stop_event: Event | None = None,
) -> dict[str, object]:
    """Record the physical input, apply pipeline downmix, and play it back."""
    active_capture = active_audio_input(input_device)
    if active_capture is not None:
        captured = active_capture.record_diagnostic_audio(
            PHYSICAL_PATH_TEST_MAX_SECONDS,
            stop_event=stop_event,
        )
        source = "active_pipeline"
    else:
        capture = QueuedAudioInput(
            input_device=input_device,
            sample_rate=48_000,
            channels=2,
            dtype="int16",
            frame_duration_ms=20,
            queue_capacity_blocks=64,
            negotiate_format=True,
        )
        try:
            capture.prepare()
            capture.start()
            captured = capture.record_diagnostic_audio(
                PHYSICAL_PATH_TEST_MAX_SECONDS,
                stop_event=stop_event,
            )
        finally:
            capture.stop()
        source = "diagnostic_stream"

    if captured.channels == 2:
        downmixed = _convert_recording_channels(captured, 1)
        conversion = "stereo_to_mono_average"
    elif captured.channels == 1:
        downmixed = captured
        conversion = "already_mono"
    else:
        raise AudioDeviceError(
            f"Unsupported microphone channel count for the physical audio test: "
            f"{captured.channels}."
        )

    captured_levels = _int16_levels(captured)
    mono_levels = _int16_levels(downmixed)
    assessment = _assess_physical_path(captured_levels, mono_levels)
    output_info = query_devices()[output_device]
    monitor_channels = (
        2 if int(output_info["max_output_channels"]) >= 2 else 1
    )
    monitor_audio = (
        _convert_recording_channels(downmixed, monitor_channels)
        if monitor_channels != downmixed.channels
        else downmixed
    )
    play_audio(monitor_audio, output_device)
    return {
        "direction": "physical_path",
        "input_device_index": input_device,
        "input_device_name": device_name(input_device),
        "output_device_index": output_device,
        "output_device_name": device_name(output_device),
        "source": source,
        "duration_ms": round(
            captured.frame_count / captured.sample_rate * 1000
        ),
        "sample_rate": captured.sample_rate,
        "captured_channels": captured.channels,
        "pipeline_channels": downmixed.channels,
        "playback_channels": monitor_audio.channels,
        "conversion": conversion,
        "aec3_applied": False,
        "channel_levels": captured_levels,
        "downmixed_level": mono_levels[0],
        "assessment": assessment,
        "passed": assessment == "passed",
    }


def test_microphone(device: int) -> dict[str, object]:
    """Capture briefly and return an objective input-level summary."""
    active_capture = active_audio_input(device)
    if active_capture is not None:
        started_at = time.perf_counter()
        initial_blocks = active_capture.statistics.captured_blocks
        time.sleep(MICROPHONE_TEST_SECONDS)
        levels = active_capture.recent_level_summary(started_at)
        if levels is None:
            raise AudioDeviceError(
                "The active microphone stream produced no level samples."
            )
        peak_dbfs, rms_dbfs = levels
        return {
            "direction": "input",
            "device_index": device,
            "device_name": device_name(device),
            "sample_rate": active_capture.sample_rate,
            "channels": active_capture.channels,
            "captured_blocks": (
                active_capture.statistics.captured_blocks - initial_blocks
            ),
            "peak_dbfs": round(peak_dbfs, 1),
            "rms_dbfs": round(rms_dbfs, 1),
            "signal_detected": peak_dbfs >= -50.0,
            "source": "active_pipeline",
        }
    capture = QueuedAudioInput(
        input_device=device,
        sample_rate=48_000,
        channels=1,
        dtype="int16",
        frame_duration_ms=20,
        queue_capacity_blocks=64,
        negotiate_format=True,
    )
    samples = array("h")
    try:
        capture.prepare()
        capture.start()
        deadline = time.monotonic() + MICROPHONE_TEST_SECONDS
        while time.monotonic() < deadline:
            block = capture.pop_block()
            if block is None:
                time.sleep(0.01)
                continue
            block_samples = array("h")
            block_samples.frombytes(block)
            samples.extend(block_samples)
    finally:
        capture.stop()
    if not samples:
        raise AudioDeviceError("The microphone produced no PCM samples.")
    peak = max(abs(value) for value in samples)
    square_sum = sum(value * value for value in samples)
    rms = math.sqrt(square_sum / len(samples))
    peak_dbfs = _dbfs(peak)
    rms_dbfs = _dbfs(rms)
    return {
        "direction": "input",
        "device_index": device,
        "device_name": device_name(device),
        "sample_rate": capture.sample_rate,
        "channels": capture.channels,
        "captured_blocks": capture.statistics.captured_blocks,
        "peak_dbfs": round(peak_dbfs, 1),
        "rms_dbfs": round(rms_dbfs, 1),
        "signal_detected": peak_dbfs >= -50.0,
        "source": "diagnostic_stream",
    }


def test_speaker(device: int) -> dict[str, object]:
    """Play a short, faded tone through one physical output endpoint."""
    info = query_devices()[device]
    maximum_channels = int(info["max_output_channels"])
    if maximum_channels <= 0:
        raise AudioDeviceError(f"Device {device} has no output channels.")
    sample_rate = int(round(float(info["default_samplerate"])))
    channels = 2 if maximum_channels >= 2 else 1
    validate_output_settings(device, sample_rate, channels, "int16")
    frame_count = int(sample_rate * SPEAKER_TEST_SECONDS)
    fade_frames = max(1, int(sample_rate * 0.025))
    pcm = array("h")
    for frame in range(frame_count):
        envelope = min(
            1.0,
            frame / fade_frames,
            (frame_count - frame - 1) / fade_frames,
        )
        sample = int(
            32_767
            * TEST_TONE_AMPLITUDE
            * envelope
            * math.sin(2 * math.pi * TEST_TONE_FREQUENCY_HZ * frame / sample_rate)
        )
        pcm.extend((sample,) * channels)
    stream: sd.RawOutputStream | None = None
    try:
        stream = sd.RawOutputStream(
            device=device,
            samplerate=sample_rate,
            channels=channels,
            dtype="int16",
            blocksize=0,
        )
        stream.start()
        stream.write(pcm.tobytes())
        stream.stop()
    finally:
        if stream is not None:
            stream.close()
    return {
        "direction": "output",
        "device_index": device,
        "device_name": device_name(device),
        "sample_rate": sample_rate,
        "channels": channels,
        "duration_ms": round(SPEAKER_TEST_SECONDS * 1000),
        "tone_hz": TEST_TONE_FREQUENCY_HZ,
    }


def _dbfs(amplitude: float) -> float:
    if amplitude <= 0:
        return -96.0
    return max(-96.0, 20.0 * math.log10(amplitude / 32_768.0))


def _convert_recording_channels(
    audio: RecordedAudio,
    output_channels: int,
) -> RecordedAudio:
    return RecordedAudio(
        blocks=tuple(
            convert_int16_channels(block, audio.channels, output_channels)
            for block in audio.blocks
        ),
        sample_rate=audio.sample_rate,
        channels=output_channels,
        dtype=audio.dtype,
        frames_per_block=audio.frames_per_block,
        frame_count=audio.frame_count,
    )


def _int16_levels(audio: RecordedAudio) -> list[dict[str, float | int]]:
    """Return full-recording RMS and peak levels for every channel."""
    if audio.dtype != "int16":
        raise AudioDeviceError("The physical audio test requires int16 PCM.")
    samples = array("h")
    for block in audio.blocks:
        samples.frombytes(block)
    if not samples:
        raise AudioDeviceError("The microphone produced no PCM samples.")
    if len(samples) % audio.channels:
        raise AudioDeviceError("The microphone produced incomplete PCM frames.")
    levels: list[dict[str, float | int]] = []
    for channel_index in range(audio.channels):
        channel_samples = samples[channel_index :: audio.channels]
        peak = max(abs(value) for value in channel_samples)
        square_sum = sum(value * value for value in channel_samples)
        rms = math.sqrt(square_sum / len(channel_samples))
        levels.append(
            {
                "channel": channel_index + 1,
                "peak_dbfs": round(_dbfs(peak), 1),
                "rms_dbfs": round(_dbfs(rms), 1),
            }
        )
    return levels


def _assess_physical_path(
    captured_levels: list[dict[str, float | int]],
    mono_levels: list[dict[str, float | int]],
) -> str:
    """Classify level, channel balance, and destructive stereo downmix."""
    strongest_peak = max(float(level["peak_dbfs"]) for level in captured_levels)
    strongest_rms = max(float(level["rms_dbfs"]) for level in captured_levels)
    mono_peak = float(mono_levels[0]["peak_dbfs"])
    mono_rms = float(mono_levels[0]["rms_dbfs"])
    if strongest_peak < -50.0:
        return "no_signal"
    if mono_peak < strongest_peak - 15.0 or mono_rms < strongest_rms - 15.0:
        return "downmix_cancellation"
    if strongest_peak >= -1.0:
        return "clipping"
    # Full-recording RMS includes silence and therefore falls as the user
    # waits before stopping the manual test. Treat the path as low only when
    # both the spoken peak and the average level are weak.
    if (
        mono_peak < LOW_SIGNAL_PEAK_DBFS
        and mono_rms < LOW_SIGNAL_RMS_DBFS
    ):
        return "low_signal"
    if len(captured_levels) == 2:
        rms_difference = abs(
            float(captured_levels[0]["rms_dbfs"])
            - float(captured_levels[1]["rms_dbfs"])
        )
        if rms_difference > 18.0:
            return "channel_imbalance"
    return "passed"
