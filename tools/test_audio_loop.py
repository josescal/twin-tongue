"""Manual record-then-playback audio diagnostic."""

import argparse
import logging
import math
from pathlib import Path

import numpy as np
import sounddevice as sd

from audio.capture import record_audio
from audio.pcm import (
    RecordedAudio,
    calculate_block_frames,
    convert_int16_channels,
)
from audio.portaudio import (
    AudioDeviceError,
    format_device,
    resolve_device,
    validate_input_settings,
    validate_output_settings,
)
from audio.playback import play_audio
from config import ConfigurationError, configure_logging, load_config

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def parse_args(
    audio_config: dict[str, object],
    pipeline_config: dict[str, object],
) -> argparse.Namespace:
    """Parse command-line options using the project audio defaults."""
    parser = argparse.ArgumentParser(description="Record audio, then play it back through another device.")
    parser.add_argument("--input-device", help="Input device identifier or name fragment.")
    parser.add_argument("--output-device", help="Output device identifier or name fragment.")
    parser.add_argument("--duration", type=float, default=5.0, help="Recording duration in seconds (default: 5).")
    parser.add_argument("--sample-rate", type=int, default=pipeline_config["input_sample_rate"])
    parser.add_argument("--channels", type=int, default=1)
    parser.add_argument("--dtype", default=audio_config["sample_format"])
    parser.add_argument("--frame-duration-ms", type=float, default=audio_config["frame_duration_ms"])
    parser.add_argument(
        "--downmix-to-mono",
        action="store_true",
        help=(
            "Record stereo, then apply Twin Tongue's exact 2-to-1 channel "
            "conversion before playback."
        ),
    )
    return parser.parse_args()


def _pipeline_downmix(audio: RecordedAudio) -> RecordedAudio:
    """Apply the same stereo-to-mono conversion used before WebRTC AEC3."""
    if audio.dtype != "int16":
        raise ValueError("--downmix-to-mono requires dtype int16.")
    if audio.channels != 2:
        raise ValueError("--downmix-to-mono requires --channels 2.")
    return RecordedAudio(
        blocks=tuple(
            convert_int16_channels(block, 2, 1) for block in audio.blocks
        ),
        sample_rate=audio.sample_rate,
        channels=1,
        dtype=audio.dtype,
        frames_per_block=audio.frames_per_block,
        frame_count=audio.frame_count,
    )


def _log_int16_levels(audio: RecordedAudio, *, label: str) -> None:
    """Report objective full-recording and per-channel levels."""
    if audio.dtype != "int16" or not audio.blocks:
        return
    samples = np.frombuffer(b"".join(audio.blocks), dtype="<i2")
    if samples.size == 0:
        return
    channels = samples.reshape(-1, audio.channels)
    for channel_index in range(audio.channels):
        channel = channels[:, channel_index].astype(np.float64)
        rms = math.sqrt(float(np.mean(channel * channel)))
        peak = float(np.max(np.abs(channel)))
        logging.info(
            "%s channel %s: RMS %.1f dBFS, peak %.1f dBFS",
            label,
            channel_index + 1,
            _dbfs(rms),
            _dbfs(peak),
        )


def _dbfs(amplitude: float) -> float:
    if amplitude <= 0:
        return -96.0
    return max(-96.0, 20.0 * math.log10(amplitude / 32_768.0))


def main() -> int:
    """Run a safe sequential record-and-playback diagnostic."""
    configure_logging()
    try:
        config = load_config(PROJECT_ROOT / "config" / "default.toml")
        args = parse_args(config["audio"], config["pipelines"]["agent_to_remote"])
        input_identifier = resolve_device(args.input_device, "input")
        output_identifier = resolve_device(args.output_device, "output")
        if args.downmix_to_mono and args.channels != 2:
            raise ValueError("--downmix-to-mono requires --channels 2.")
        playback_channels = 1 if args.downmix_to_mono else args.channels
        block_frames = calculate_block_frames(args.sample_rate, args.frame_duration_ms)
        validate_input_settings(input_identifier, args.sample_rate, args.channels, args.dtype)
        try:
            validate_output_settings(
                output_identifier,
                args.sample_rate,
                playback_channels,
                args.dtype,
            )
        except AudioDeviceError as error:
            raise AudioDeviceError(
                f"{error} Resampling and format conversion are not implemented."
            ) from error

        logging.info("Input device:  %s", format_device(input_identifier))
        logging.info("Output device: %s", format_device(output_identifier))
        logging.info("Sample rate:   %s Hz", args.sample_rate)
        logging.info("Channels:      %s", args.channels)
        logging.info("Format:        %s", args.dtype)
        logging.info("Duration:      %g seconds", args.duration)
        logging.info("Block size:    %s frames", block_frames)
        logging.info(
            "Playback mode: %s",
            "pipeline stereo-to-mono downmix"
            if args.downmix_to_mono
            else "recorded channels unchanged",
        )

        audio = record_audio(
            input_device=input_identifier,
            duration=args.duration,
            sample_rate=args.sample_rate,
            channels=args.channels,
            dtype=args.dtype,
            frame_duration_ms=args.frame_duration_ms,
        )
        _log_int16_levels(audio, label="Captured")
        playback_audio = _pipeline_downmix(audio) if args.downmix_to_mono else audio
        if args.downmix_to_mono:
            _log_int16_levels(playback_audio, label="Downmixed mono")
        play_audio(playback_audio, output_identifier)
    except KeyboardInterrupt:
        logging.warning("Audio diagnostic interrupted by the user.")
        return 130
    except (AudioDeviceError, ConfigurationError, sd.PortAudioError, OSError, ValueError) as error:
        logging.error("Audio diagnostic failed: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
