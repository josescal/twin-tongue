"""Measure physical PortAudio callback stability without storing or emitting audio."""

import argparse
from contextlib import suppress
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import statistics
import time

import sounddevice as sd

from audio.buffering import PcmBlockBuffer
from audio.device_manager import discover_audio_topology
from audio.pcm import calculate_block_bytes, calculate_block_frames
from audio.physical_devices import select_automatic_physical_device
from audio.portaudio import (
    AudioDeviceError,
    DeviceDirection,
    DeviceReference,
    format_device,
    resolve_device,
    validate_input_settings,
    validate_output_settings,
)
from config import ConfigurationError, load_config

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class CallbackMetrics:
    """Summary of one physical stream after excluding its warm-up callbacks."""

    direction: str
    callbacks: int
    interval_samples: int
    expected_period_ms: float
    mean_interval_ms: float
    p95_absolute_jitter_ms: float
    p99_absolute_jitter_ms: float
    maximum_absolute_jitter_ms: float
    late_callbacks: int
    late_callback_percent: float
    mean_callback_us: float
    p95_callback_us: float
    maximum_callback_us: float
    callback_deadline_misses: int
    status_events: int
    xruns: int
    invalid_frame_counts: int
    effective_sample_rate: float
    clock_drift_ppm: float
    stream_cpu_load_percent: float
    reported_latency_ms: float


class CallbackProbe:
    """Collect bounded callback timings with storage allocated before streaming."""

    def __init__(
        self,
        direction: str,
        sample_rate: int,
        frames_per_block: int,
        warmup_callbacks: int,
        sample_capacity: int,
    ) -> None:
        self.direction = direction
        self.sample_rate = sample_rate
        self.frames_per_block = frames_per_block
        self.expected_period_ns = round(frames_per_block / sample_rate * 1_000_000_000)
        self.warmup_callbacks = warmup_callbacks
        self._intervals_ns = [0] * sample_capacity
        self._durations_ns = [0] * sample_capacity
        self._seen_callbacks = 0
        self._callbacks = 0
        self._interval_count = 0
        self._duration_count = 0
        self._last_started_at = 0
        self._first_started_at = 0
        self._last_recorded_at = 0
        self._frames_after_first = 0
        self.status_events = 0
        self.xruns = 0
        self.invalid_frame_counts = 0

    def start(self, frames: int, status: sd.CallbackFlags | None) -> int:
        """Start one bounded timing sample from inside the callback."""
        started_at = time.perf_counter_ns()
        self._seen_callbacks += 1
        if self._seen_callbacks <= self.warmup_callbacks:
            return 0

        if frames != self.frames_per_block:
            self.invalid_frame_counts += 1
        if status:
            self.status_events += 1
            if self.direction == "input" and status.input_overflow:
                self.xruns += 1
            elif self.direction == "output" and status.output_underflow:
                self.xruns += 1

        if self._callbacks == 0:
            self._first_started_at = started_at
        else:
            self._frames_after_first += frames
            if self._interval_count < len(self._intervals_ns):
                self._intervals_ns[self._interval_count] = started_at - self._last_started_at
                self._interval_count += 1
        self._last_started_at = started_at
        self._last_recorded_at = started_at
        self._callbacks += 1
        return started_at

    def finish(self, started_at: int) -> None:
        """Finish a timing sample without growing callback-owned storage."""
        if started_at and self._duration_count < len(self._durations_ns):
            self._durations_ns[self._duration_count] = time.perf_counter_ns() - started_at
            self._duration_count += 1

    def summarize(self, *, cpu_load: float, latency_seconds: float) -> CallbackMetrics:
        """Calculate report metrics outside the realtime callback."""
        intervals = self._intervals_ns[: self._interval_count]
        durations = self._durations_ns[: self._duration_count]
        jitters = [abs(value - self.expected_period_ns) for value in intervals]
        late_callbacks = sum(value > self.expected_period_ns * 1.5 for value in intervals)
        elapsed_ns = self._last_recorded_at - self._first_started_at
        effective_sample_rate = (
            self._frames_after_first / (elapsed_ns / 1_000_000_000)
            if elapsed_ns > 0
            else 0.0
        )
        clock_drift_ppm = (
            (effective_sample_rate / self.sample_rate - 1) * 1_000_000
            if effective_sample_rate
            else 0.0
        )
        return CallbackMetrics(
            direction=self.direction,
            callbacks=self._callbacks,
            interval_samples=len(intervals),
            expected_period_ms=self.expected_period_ns / 1_000_000,
            mean_interval_ms=_mean(intervals) / 1_000_000,
            p95_absolute_jitter_ms=_percentile(jitters, 95) / 1_000_000,
            p99_absolute_jitter_ms=_percentile(jitters, 99) / 1_000_000,
            maximum_absolute_jitter_ms=(max(jitters) if jitters else 0) / 1_000_000,
            late_callbacks=late_callbacks,
            late_callback_percent=(late_callbacks / len(intervals) * 100 if intervals else 0.0),
            mean_callback_us=_mean(durations) / 1_000,
            p95_callback_us=_percentile(durations, 95) / 1_000,
            maximum_callback_us=(max(durations) if durations else 0) / 1_000,
            callback_deadline_misses=sum(value > self.expected_period_ns for value in durations),
            status_events=self.status_events,
            xruns=self.xruns,
            invalid_frame_counts=self.invalid_frame_counts,
            effective_sample_rate=effective_sample_rate,
            clock_drift_ppm=clock_drift_ppm,
            stream_cpu_load_percent=cpu_load * 100,
            reported_latency_ms=latency_seconds * 1_000,
        )


def _mean(values: list[int]) -> float:
    return statistics.fmean(values) if values else 0.0


def _percentile(values: list[int], percentile: float) -> float:
    """Return a linearly interpolated percentile for deterministic reporting."""
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def failure_reasons(metrics: CallbackMetrics, max_late_percent: float) -> tuple[str, ...]:
    """Return physical-stream conditions that make the benchmark unhealthy."""
    reasons: list[str] = []
    if metrics.callbacks < 2:
        reasons.append("fewer than two measured callbacks")
    if metrics.xruns:
        reasons.append(f"{metrics.xruns} xrun event(s)")
    if metrics.invalid_frame_counts:
        reasons.append(f"{metrics.invalid_frame_counts} unexpected frame count(s)")
    if metrics.callback_deadline_misses:
        reasons.append(f"{metrics.callback_deadline_misses} callback deadline miss(es)")
    if metrics.late_callback_percent > max_late_percent:
        reasons.append(
            f"late callbacks {metrics.late_callback_percent:.2f}% > {max_late_percent:.2f}%"
        )
    return tuple(reasons)


def parse_args(
    audio_config: dict[str, object],
    pipeline_config: dict[str, object],
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark physical PortAudio callbacks using discarded input and silent output."
    )
    parser.add_argument("--mode", choices=("input", "output", "duplex"), default="duplex")
    parser.add_argument("--input-device", help="Input device identifier or unique name fragment.")
    parser.add_argument("--output-device", help="Output device identifier or unique name fragment.")
    parser.add_argument("--duration", type=float, default=30.0, help="Measured seconds after warm-up.")
    parser.add_argument("--warmup-seconds", type=float, default=2.0)
    parser.add_argument("--sample-rate", type=int, default=pipeline_config["input_sample_rate"])
    parser.add_argument("--input-channels", type=int, default=1)
    parser.add_argument("--output-channels", type=int, default=2)
    parser.add_argument("--dtype", default=audio_config["sample_format"])
    parser.add_argument("--frame-duration-ms", type=float, default=audio_config["frame_duration_ms"])
    parser.add_argument(
        "--max-late-percent",
        type=float,
        default=1.0,
        help="Maximum accepted percentage of intervals over 150%% of their target period.",
    )
    parser.add_argument("--json-output", type=Path, help="Optional path for the machine-readable report.")
    return parser.parse_args()


def run_benchmark(args: argparse.Namespace) -> list[CallbackMetrics]:
    """Open the requested physical streams and return their callback metrics."""
    if args.duration <= 0:
        raise ValueError("Benchmark duration must be greater than zero.")
    if args.warmup_seconds < 0:
        raise ValueError("Warm-up duration must not be negative.")
    if args.max_late_percent < 0:
        raise ValueError("Maximum late callback percentage must not be negative.")

    frames_per_block = calculate_block_frames(args.sample_rate, args.frame_duration_ms)
    warmup_callbacks = math.ceil(args.warmup_seconds * args.sample_rate / frames_per_block)
    sample_capacity = math.ceil(args.duration * args.sample_rate / frames_per_block) + 100
    streams: list[tuple[str, sd.RawInputStream | sd.RawOutputStream, CallbackProbe]] = []
    started_streams: list[sd.RawInputStream | sd.RawOutputStream] = []
    load_and_latency: dict[str, tuple[float, float]] = {}
    try:
        if args.mode in {"input", "duplex"}:
            input_device, input_stream, input_probe = _create_input_stream(
                args, frames_per_block, warmup_callbacks, sample_capacity
            )
            streams.append(("input", input_stream, input_probe))
            print(f"Input device:  {format_device(input_device)}")
        if args.mode in {"output", "duplex"}:
            output_device, output_stream, output_probe = _create_output_stream(
                args, frames_per_block, warmup_callbacks, sample_capacity
            )
            streams.append(("output", output_stream, output_probe))
            print(f"Output device: {format_device(output_device)}")

        print(
            f"Configuration: {args.sample_rate} Hz, {args.dtype}, "
            f"{frames_per_block} frames/block ({args.frame_duration_ms:g} ms)"
        )
        print(
            f"Running {args.warmup_seconds:g} s warm-up + {args.duration:g} s measurement; "
            "captured audio is discarded and output is silence."
        )
        for _, stream, _ in streams:
            stream.start()
            started_streams.append(stream)
        time.sleep(args.warmup_seconds + args.duration)
        load_and_latency = {
            direction: (stream.cpu_load, float(stream.latency))
            for direction, stream, _ in streams
        }
    finally:
        for stream in reversed(started_streams):
            with suppress(sd.PortAudioError):
                stream.stop()
        for _, stream, _ in reversed(streams):
            with suppress(sd.PortAudioError):
                stream.close()

    return [
        probe.summarize(
            cpu_load=load_and_latency[direction][0],
            latency_seconds=load_and_latency[direction][1],
        )
        for direction, _, probe in streams
    ]


def _create_input_stream(
    args: argparse.Namespace,
    frames_per_block: int,
    warmup_callbacks: int,
    sample_capacity: int,
) -> tuple[int, sd.RawInputStream, CallbackProbe]:
    input_device = _prepare_device(
        args.input_device,
        "input",
        args.sample_rate,
        args.input_channels,
        args.dtype,
    )
    probe = CallbackProbe(
        "input", args.sample_rate, frames_per_block, warmup_callbacks, sample_capacity
    )
    input_buffer = PcmBlockBuffer(
        20,
        calculate_block_bytes(frames_per_block, args.input_channels, args.dtype),
    )

    def input_callback(
        indata: object,
        frames: int,
        time_info: object,
        status: sd.CallbackFlags,
    ) -> None:
        started_at = probe.start(frames, status)
        try:
            input_buffer.write(indata)
        finally:
            probe.finish(started_at)

    stream = sd.RawInputStream(
        device=input_device,
        samplerate=args.sample_rate,
        channels=args.input_channels,
        dtype=args.dtype,
        blocksize=frames_per_block,
        callback=input_callback,
    )
    return input_device, stream, probe


def _create_output_stream(
    args: argparse.Namespace,
    frames_per_block: int,
    warmup_callbacks: int,
    sample_capacity: int,
) -> tuple[int, sd.RawOutputStream, CallbackProbe]:
    output_device = _prepare_device(
        args.output_device,
        "output",
        args.sample_rate,
        args.output_channels,
        args.dtype,
    )
    probe = CallbackProbe(
        "output", args.sample_rate, frames_per_block, warmup_callbacks, sample_capacity
    )
    silence = bytes(calculate_block_bytes(frames_per_block, args.output_channels, args.dtype))

    def output_callback(
        outdata: object,
        frames: int,
        time_info: object,
        status: sd.CallbackFlags,
    ) -> None:
        started_at = probe.start(frames, status)
        try:
            if len(outdata) != len(silence):  # type: ignore[arg-type]
                raise sd.CallbackAbort
            outdata[:] = silence  # type: ignore[index]
        finally:
            probe.finish(started_at)

    stream = sd.RawOutputStream(
        device=output_device,
        samplerate=args.sample_rate,
        channels=args.output_channels,
        dtype=args.dtype,
        blocksize=frames_per_block,
        callback=output_callback,
    )
    return output_device, stream, probe


def _prepare_device(
    reference: DeviceReference,
    direction: DeviceDirection,
    sample_rate: int,
    channels: int,
    dtype: str,
) -> int:
    if reference is None:
        physical_devices, _ = discover_audio_topology(include_sessions=False)
        selected = select_automatic_physical_device(physical_devices, direction)
        assert selected is not None
        identifier = selected.portaudio_index
    else:
        identifier = resolve_device(reference, direction)
    if direction == "input":
        validate_input_settings(identifier, sample_rate, channels, dtype)
    else:
        validate_output_settings(identifier, sample_rate, channels, dtype)
    return identifier


def print_report(metrics: CallbackMetrics, reasons: tuple[str, ...]) -> None:
    status = "FAIL" if reasons else "PASS"
    print(f"\n[{status}] {metrics.direction.upper()} callback metrics")
    print(f"  Callbacks measured:       {metrics.callbacks}")
    print(f"  Expected period:          {metrics.expected_period_ms:.3f} ms")
    print(f"  Mean interval:            {metrics.mean_interval_ms:.3f} ms")
    print(
        "  Absolute jitter p95/p99:  "
        f"{metrics.p95_absolute_jitter_ms:.3f} / "
        f"{metrics.p99_absolute_jitter_ms:.3f} ms"
    )
    print(f"  Maximum absolute jitter:  {metrics.maximum_absolute_jitter_ms:.3f} ms")
    print(
        f"  Late callbacks:           {metrics.late_callbacks} "
        f"({metrics.late_callback_percent:.3f}%)"
    )
    print(
        "  Callback time mean/p95:   "
        f"{metrics.mean_callback_us:.3f} / {metrics.p95_callback_us:.3f} us"
    )
    print(f"  Maximum callback time:    {metrics.maximum_callback_us:.3f} us")
    print(f"  Deadline misses:          {metrics.callback_deadline_misses}")
    print(f"  Status events / xruns:    {metrics.status_events} / {metrics.xruns}")
    print(f"  Unexpected frame counts:  {metrics.invalid_frame_counts}")
    print(f"  Effective sample rate:    {metrics.effective_sample_rate:.3f} Hz")
    print(f"  Estimated clock drift:    {metrics.clock_drift_ppm:+.1f} ppm")
    print(f"  PortAudio CPU load:       {metrics.stream_cpu_load_percent:.3f}%")
    print(f"  PortAudio latency:        {metrics.reported_latency_ms:.3f} ms")
    for reason in reasons:
        print(f"  Failure: {reason}")


def main() -> int:
    try:
        config = load_config(PROJECT_ROOT / "config" / "default.toml")
        args = parse_args(config["audio"], config["pipelines"]["agent_to_remote"])
        metrics = run_benchmark(args)
        failures = {
            item.direction: failure_reasons(item, args.max_late_percent)
            for item in metrics
        }
        for item in metrics:
            print_report(item, failures[item.direction])
        relative_clock_drift_ppm = _relative_clock_drift_ppm(metrics)
        if relative_clock_drift_ppm is not None:
            print(
                "\nInput-output relative clock drift: "
                f"{relative_clock_drift_ppm:+.1f} ppm"
            )
        if args.json_output is not None:
            report = {
                "configuration": {
                    "mode": args.mode,
                    "duration_seconds": args.duration,
                    "warmup_seconds": args.warmup_seconds,
                    "sample_rate": args.sample_rate,
                    "input_channels": args.input_channels,
                    "output_channels": args.output_channels,
                    "dtype": args.dtype,
                    "frame_duration_ms": args.frame_duration_ms,
                    "max_late_percent": args.max_late_percent,
                },
                "streams": [
                    {
                        **asdict(item),
                        "passed": not failures[item.direction],
                        "failure_reasons": list(failures[item.direction]),
                    }
                    for item in metrics
                ],
                "input_output_relative_clock_drift_ppm": relative_clock_drift_ppm,
            }
            args.json_output.parent.mkdir(parents=True, exist_ok=True)
            args.json_output.write_text(
                json.dumps(report, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            print(f"\nJSON report written to {args.json_output}")
        return 2 if any(failures.values()) else 0
    except KeyboardInterrupt:
        print("Audio device benchmark interrupted by the user.")
        return 130
    except (
        AudioDeviceError,
        ConfigurationError,
        sd.PortAudioError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as error:
        print(f"Audio device benchmark failed: {error}")
        return 1


def _relative_clock_drift_ppm(metrics: list[CallbackMetrics]) -> float | None:
    by_direction = {item.direction: item for item in metrics}
    if "input" not in by_direction or "output" not in by_direction:
        return None
    return (
        by_direction["input"].clock_drift_ppm
        - by_direction["output"].clock_drift_ppm
    )


if __name__ == "__main__":
    raise SystemExit(main())
