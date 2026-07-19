"""Manual record-then-playback audio diagnostic."""

import argparse
import logging
from pathlib import Path

import sounddevice as sd

from audio.capture import record_audio
from audio.pcm import calculate_block_frames
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
    return parser.parse_args()


def main() -> int:
    """Run a safe sequential record-and-playback diagnostic."""
    configure_logging()
    try:
        config = load_config(PROJECT_ROOT / "config" / "default.toml")
        args = parse_args(config["audio"], config["pipelines"]["agent_to_remote"])
        input_identifier = resolve_device(args.input_device, "input")
        output_identifier = resolve_device(args.output_device, "output")
        block_frames = calculate_block_frames(args.sample_rate, args.frame_duration_ms)
        validate_input_settings(input_identifier, args.sample_rate, args.channels, args.dtype)
        try:
            validate_output_settings(output_identifier, args.sample_rate, args.channels, args.dtype)
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

        audio = record_audio(
            input_device=input_identifier,
            duration=args.duration,
            sample_rate=args.sample_rate,
            channels=args.channels,
            dtype=args.dtype,
            frame_duration_ms=args.frame_duration_ms,
        )
        play_audio(audio, output_identifier)
    except KeyboardInterrupt:
        logging.warning("Audio diagnostic interrupted by the user.")
        return 130
    except (AudioDeviceError, ConfigurationError, sd.PortAudioError, OSError, ValueError) as error:
        logging.error("Audio diagnostic failed: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
