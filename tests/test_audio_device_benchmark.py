"""Tests for physical audio callback metric calculations."""

import argparse
import unittest
from unittest.mock import Mock, patch

from tools.audio_device_benchmark import (
    CallbackProbe,
    _prepare_device,
    failure_reasons,
    run_benchmark,
)
from audio.physical_devices import PhysicalDevice


class FakeStream:
    def __init__(self) -> None:
        self.cpu_load = 0.01
        self.latency = 0.005
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


def benchmark_args() -> argparse.Namespace:
    return argparse.Namespace(
        mode="duplex",
        input_device=None,
        output_device=None,
        duration=0.001,
        warmup_seconds=0.0,
        sample_rate=48_000,
        input_channels=1,
        output_channels=2,
        dtype="int16",
        frame_duration_ms=20.0,
        max_late_percent=1.0,
        json_output=None,
    )


class CallbackProbeTests(unittest.TestCase):
    @patch(
        "tools.audio_device_benchmark.time.perf_counter_ns",
        side_effect=(
            1_000_000_000,
            1_000_010_000,
            1_020_000_000,
            1_020_020_000,
            1_040_000_000,
            1_040_030_000,
        ),
    )
    def test_summary_reports_stable_twenty_millisecond_callbacks(
        self,
        perf_counter_ns: object,
    ) -> None:
        probe = CallbackProbe("input", 48_000, 960, warmup_callbacks=0, sample_capacity=10)
        for _ in range(3):
            started_at = probe.start(960, None)
            probe.finish(started_at)

        metrics = probe.summarize(cpu_load=0.01, latency_seconds=0.005)

        self.assertEqual(metrics.callbacks, 3)
        self.assertEqual(metrics.mean_interval_ms, 20.0)
        self.assertEqual(metrics.maximum_absolute_jitter_ms, 0.0)
        self.assertEqual(metrics.mean_callback_us, 20.0)
        self.assertEqual(metrics.effective_sample_rate, 48_000.0)
        self.assertEqual(metrics.clock_drift_ppm, 0.0)
        self.assertEqual(metrics.stream_cpu_load_percent, 1.0)
        self.assertEqual(metrics.reported_latency_ms, 5.0)
        self.assertEqual(failure_reasons(metrics, max_late_percent=1.0), ())

    @patch(
        "tools.audio_device_benchmark.time.perf_counter_ns",
        side_effect=(
            1_000_000_000,
            1_000_010_000,
            1_040_000_000,
            1_060_010_000,
        ),
    )
    def test_summary_detects_xrun_late_callback_and_deadline_miss(
        self,
        perf_counter_ns: object,
    ) -> None:
        probe = CallbackProbe("input", 48_000, 960, warmup_callbacks=0, sample_capacity=10)
        first = probe.start(960, None)
        probe.finish(first)
        status = Mock(input_overflow=True)
        second = probe.start(960, status)
        probe.finish(second)

        metrics = probe.summarize(cpu_load=0.75, latency_seconds=0.02)
        reasons = failure_reasons(metrics, max_late_percent=1.0)

        self.assertEqual(metrics.xruns, 1)
        self.assertEqual(metrics.late_callbacks, 1)
        self.assertEqual(metrics.callback_deadline_misses, 1)
        self.assertTrue(any("xrun" in reason for reason in reasons))
        self.assertTrue(any("deadline" in reason for reason in reasons))
        self.assertTrue(any("late callbacks" in reason for reason in reasons))

    @patch(
        "tools.audio_device_benchmark.time.perf_counter_ns",
        side_effect=(1_000_000_000, 1_020_000_000, 1_020_010_000),
    )
    def test_warmup_callback_is_excluded(
        self,
        perf_counter_ns: object,
    ) -> None:
        probe = CallbackProbe("output", 48_000, 960, warmup_callbacks=1, sample_capacity=10)
        warmup = probe.start(960, None)
        probe.finish(warmup)
        measured = probe.start(960, None)
        probe.finish(measured)

        metrics = probe.summarize(cpu_load=0, latency_seconds=0)

        self.assertEqual(metrics.callbacks, 1)
        self.assertEqual(metrics.interval_samples, 0)


class PhysicalBenchmarkLifecycleTests(unittest.TestCase):
    @patch("tools.audio_device_benchmark.validate_input_settings")
    @patch("tools.audio_device_benchmark.resolve_device")
    @patch("tools.audio_device_benchmark.discover_audio_topology")
    def test_omitted_device_uses_wasapi_communications_default(
        self,
        discover_topology: Mock,
        resolve_device: Mock,
        validate_input: Mock,
    ) -> None:
        discover_topology.return_value = (
            [
                PhysicalDevice("mic-other", "Other", 30, "input"),
                PhysicalDevice("mic-default", "Default", 32, "input", True),
            ],
            [],
        )

        identifier = _prepare_device(None, "input", 48_000, 1, "int16")

        self.assertEqual(identifier, 32)
        discover_topology.assert_called_once_with(include_sessions=False)
        resolve_device.assert_not_called()
        validate_input.assert_called_once_with(32, 48_000, 1, "int16")

    @patch("tools.audio_device_benchmark.validate_output_settings")
    @patch("tools.audio_device_benchmark.discover_audio_topology")
    @patch("tools.audio_device_benchmark.resolve_device", return_value=25)
    def test_explicit_device_keeps_standard_resolution(
        self,
        resolve_device: Mock,
        discover_topology: Mock,
        validate_output: Mock,
    ) -> None:
        identifier = _prepare_device("Windows WASAPI::Realtek", "output", 48_000, 2, "int16")

        self.assertEqual(identifier, 25)
        discover_topology.assert_not_called()
        resolve_device.assert_called_once_with("Windows WASAPI::Realtek", "output")
        validate_output.assert_called_once_with(25, 48_000, 2, "int16")

    @patch("tools.audio_device_benchmark.print")
    @patch("tools.audio_device_benchmark.time.sleep")
    @patch("tools.audio_device_benchmark.format_device", return_value="Device")
    @patch("tools.audio_device_benchmark._prepare_device", side_effect=(1, 2))
    @patch("tools.audio_device_benchmark.sd.RawOutputStream")
    @patch("tools.audio_device_benchmark.sd.RawInputStream")
    def test_duplex_benchmark_stops_and_closes_both_streams(
        self,
        raw_input_stream: Mock,
        raw_output_stream: Mock,
        prepare_device: Mock,
        format_device: Mock,
        sleep: Mock,
        print_output: Mock,
    ) -> None:
        input_stream = FakeStream()
        output_stream = FakeStream()
        raw_input_stream.return_value = input_stream
        raw_output_stream.return_value = output_stream

        metrics = run_benchmark(benchmark_args())

        self.assertEqual([item.direction for item in metrics], ["input", "output"])
        self.assertTrue(input_stream.started)
        self.assertTrue(input_stream.stopped)
        self.assertTrue(input_stream.closed)
        self.assertTrue(output_stream.started)
        self.assertTrue(output_stream.stopped)
        self.assertTrue(output_stream.closed)

    @patch("tools.audio_device_benchmark.print")
    @patch("tools.audio_device_benchmark.format_device", return_value="Device")
    @patch("tools.audio_device_benchmark._prepare_device", side_effect=(1, 2))
    @patch(
        "tools.audio_device_benchmark.sd.RawOutputStream",
        side_effect=RuntimeError("output failed"),
    )
    @patch("tools.audio_device_benchmark.sd.RawInputStream")
    def test_input_stream_is_closed_if_output_construction_fails(
        self,
        raw_input_stream: Mock,
        raw_output_stream: Mock,
        prepare_device: Mock,
        format_device: Mock,
        print_output: Mock,
    ) -> None:
        input_stream = FakeStream()
        raw_input_stream.return_value = input_stream

        with self.assertRaisesRegex(RuntimeError, "output failed"):
            run_benchmark(benchmark_args())

        self.assertFalse(input_stream.started)
        self.assertFalse(input_stream.stopped)
        self.assertTrue(input_stream.closed)


if __name__ == "__main__":
    unittest.main()
