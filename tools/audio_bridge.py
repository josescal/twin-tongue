"""Manual continuous audio monitor diagnostic."""

import argparse
import logging
import math
from pathlib import Path
import time

import sounddevice as sd

from audio.capture import QueuedAudioInput
from audio.playback import QueuedAudioOutput
from audio.portaudio import AudioDeviceError
from config import ConfigurationError, configure_logging, load_config

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PREBUFFER_TIMEOUT_SECONDS = 5.0
STATISTICS_INTERVAL_SECONDS = 5.0


def parse_args(
    audio_config: dict[str, object],
    pipeline_config: dict[str, object],
) -> argparse.Namespace:
    """Parse live monitor options."""
    parser = argparse.ArgumentParser(
        description="Continuously monitor one PCM input device through one output device."
    )
    parser.add_argument("--input-device", help="Input device identifier or unique name fragment.")
    parser.add_argument("--output-device", help="Output device identifier or unique name fragment.")
    parser.add_argument("--duration", type=float, default=30.0, help="Monitor duration in seconds (default: 30).")
    parser.add_argument("--sample-rate", type=int, default=pipeline_config["input_sample_rate"])
    parser.add_argument("--channels", type=int, default=2, help="Channel count (default: 2 for live monitoring).")
    parser.add_argument("--dtype", default=audio_config["sample_format"])
    parser.add_argument("--frame-duration-ms", type=float, default=audio_config["frame_duration_ms"])
    parser.add_argument("--buffer-ms", type=float, default=100.0, help="Target pre-buffer in milliseconds.")
    return parser.parse_args()


def run_monitor(args: argparse.Namespace) -> None:
    """Bridge queued capture to queued playback for the requested duration."""
    if args.duration <= 0:
        raise ValueError("Monitor duration must be greater than zero.")
    if args.frame_duration_ms <= 0:
        raise ValueError("Frame duration must be greater than zero.")
    if args.buffer_ms <= 0:
        raise ValueError("Buffer duration must be greater than zero.")

    target_buffer_blocks = max(1, math.ceil(args.buffer_ms / args.frame_duration_ms))
    queue_capacity = max(target_buffer_blocks * 4, 20)
    capture = QueuedAudioInput(
        input_device=args.input_device,
        sample_rate=args.sample_rate,
        channels=args.channels,
        dtype=str(args.dtype),
        frame_duration_ms=args.frame_duration_ms,
        queue_capacity_blocks=queue_capacity,
    )
    output = QueuedAudioOutput(
        output_device=args.output_device,
        sample_rate=args.sample_rate,
        channels=args.channels,
        dtype=str(args.dtype),
        frames_per_block=capture.frames_per_block,
        queue_capacity_blocks=queue_capacity,
    )

    try:
        capture.start()
        logging.info(
            "Pre-buffering %s blocks (approximately %g ms)",
            target_buffer_blocks,
            target_buffer_blocks * args.frame_duration_ms,
        )
        initial_blocks = _take_prebuffer(capture, target_buffer_blocks)
        output.start(initial_blocks=initial_blocks)
        logging.info("Audio monitor running for %g seconds. Press Ctrl+C to stop.", args.duration)
        started_at = time.monotonic()
        next_statistics = started_at + STATISTICS_INTERVAL_SECONDS
        while time.monotonic() - started_at < args.duration:
            _forward_available_blocks(capture, output)
            now = time.monotonic()
            if now >= next_statistics:
                _log_statistics(capture, output)
                next_statistics = now + STATISTICS_INTERVAL_SECONDS
            remaining = args.duration - (now - started_at)
            time.sleep(min(0.005, max(0, remaining)))
    finally:
        output.stop()
        capture.stop()
        logging.info("Final audio monitor statistics:")
        _log_statistics(capture, output)


def _take_prebuffer(
    capture: QueuedAudioInput,
    target_blocks: int,
) -> tuple[bytes, ...]:
    deadline = time.monotonic() + PREBUFFER_TIMEOUT_SECONDS
    blocks: list[bytes] = []
    while len(blocks) < target_blocks:
        block = capture.pop_block()
        if block is not None:
            blocks.append(block)
            continue
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Pre-buffer did not reach {target_blocks} blocks "
                f"within {PREBUFFER_TIMEOUT_SECONDS:g} seconds."
            )
        time.sleep(0.005)
    return tuple(blocks)


def _forward_available_blocks(
    capture: QueuedAudioInput,
    output: QueuedAudioOutput,
) -> None:
    while True:
        block = capture.pop_block()
        if block is None:
            return
        output.push_block(block)


def _log_statistics(
    capture: QueuedAudioInput,
    output: QueuedAudioOutput,
) -> None:
    logging.info("  Captured blocks:       %s", capture.statistics.captured_blocks)
    logging.info("  Played blocks:         %s", output.played_blocks)
    logging.info("  Capture buffer:        %s", capture.buffered_blocks)
    logging.info("  Playback buffer:       %s", output.buffered_blocks)
    logging.info("  Input overflows:       %s", capture.statistics.input_overflows)
    logging.info("  Output underflows:     %s", output.output_underflows)
    logging.info("  Capture drops:         %s", capture.statistics.dropped_blocks)
    logging.info("  Playback drops:        %s", output.dropped_blocks)
    logging.info("  Empty playback events: %s", output.empty_buffer_events)
    logging.info("  Invalid input blocks:  %s", capture.statistics.invalid_block_sizes)
    logging.info("  Invalid output blocks: %s", output.invalid_block_sizes)


def main() -> int:
    """Run the live audio monitor and return a process exit code."""
    configure_logging()
    try:
        config = load_config(PROJECT_ROOT / "config" / "default.toml")
        args = parse_args(config["audio"], config["pipelines"]["remote_to_agent"])
        run_monitor(args)
    except KeyboardInterrupt:
        logging.warning("Audio monitor interrupted by the user.")
        return 130
    except (
        AudioDeviceError,
        ConfigurationError,
        sd.PortAudioError,
        OSError,
        RuntimeError,
        ValueError,
    ) as error:
        logging.error("Audio monitor failed: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
