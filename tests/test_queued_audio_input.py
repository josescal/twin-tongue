"""Tests for preallocated callback-driven PCM input."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time
import unittest
from unittest.mock import Mock, patch

from audio.capture import (
    AudioInputUnresponsiveError,
    InputStreamFormat,
    QueuedAudioInput,
)


class QueuedAudioInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.capture = QueuedAudioInput(
            input_device=1,
            sample_rate=1_000,
            channels=1,
            dtype="int16",
            frame_duration_ms=2,
            queue_capacity_blocks=2,
        )
        self.status = Mock(input_overflow=False)

    def test_callback_keeps_the_newest_blocks_without_blocking(self) -> None:
        self.capture._input_callback(b"\x01\x00\x01\x00", 2, None, self.status)
        self.capture._input_callback(b"\x02\x00\x02\x00", 2, None, self.status)
        self.capture._input_callback(b"\x03\x00\x03\x00", 2, None, self.status)

        self.assertEqual(self.capture.pop_block(), b"\x02\x00\x02\x00")
        self.assertEqual(self.capture.pop_block(), b"\x03\x00\x03\x00")
        self.assertIsNone(self.capture.pop_block())
        self.assertEqual(self.capture.statistics.dropped_blocks, 1)

    def test_callback_rejects_an_invalid_block_without_allocating_fallback_storage(self) -> None:
        self.capture._input_callback(b"\x01\x00", 1, None, self.status)

        self.assertIsNone(self.capture.pop_block())
        self.assertEqual(self.capture.statistics.invalid_block_sizes, 1)
        self.assertEqual(self.capture.statistics.dropped_blocks, 1)

    def test_callback_records_input_overflow(self) -> None:
        status = Mock(input_overflow=True)

        self.capture._input_callback(b"\x01\x00\x01\x00", 2, None, status)

        self.assertEqual(self.capture.statistics.input_overflows, 1)

    def test_diagnostic_recording_copies_callback_without_consuming_pipeline_queue(
        self,
    ) -> None:
        self.capture.stream = Mock()
        with ThreadPoolExecutor(max_workers=1) as executor:
            recording = executor.submit(
                self.capture.record_diagnostic_audio,
                0.004,
            )
            deadline = time.monotonic() + 1
            while self.capture._diagnostic_tap is None:
                if time.monotonic() >= deadline:
                    self.fail("Diagnostic tap was not installed.")
                time.sleep(0.001)
            self.capture._input_callback(
                b"\x01\x00\x02\x00",
                2,
                None,
                self.status,
            )
            self.capture._input_callback(
                b"\x03\x00\x04\x00",
                2,
                None,
                self.status,
            )
            audio = recording.result(timeout=1)

        self.assertEqual(
            (b"\x01\x00\x02\x00", b"\x03\x00\x04\x00"),
            audio.blocks,
        )
        self.assertEqual(4, audio.frame_count)
        self.assertEqual(b"\x01\x00\x02\x00", self.capture.pop_block())
        self.assertEqual(b"\x03\x00\x04\x00", self.capture.pop_block())

    def test_diagnostic_recording_tolerates_a_delayed_callback(self) -> None:
        self.capture.stream = Mock()
        self.capture.first_callback_timeout_seconds = 0.001
        with ThreadPoolExecutor(max_workers=1) as executor:
            recording = executor.submit(
                self.capture.record_diagnostic_audio,
                0.004,
            )
            deadline = time.monotonic() + 1
            while self.capture._diagnostic_tap is None:
                if time.monotonic() >= deadline:
                    self.fail("Diagnostic tap was not installed.")
                time.sleep(0.001)
            # This exceeds the old 5 ms wall-clock deadline even though the
            # stream still provides the complete four frames.
            time.sleep(0.01)
            self.capture._input_callback(
                b"\x01\x00\x02\x00",
                2,
                None,
                self.status,
            )
            self.capture._input_callback(
                b"\x03\x00\x04\x00",
                2,
                None,
                self.status,
            )
            audio = recording.result(timeout=1)

        self.assertEqual(4, audio.frame_count)

    def test_diagnostic_recording_can_be_stopped_and_returns_captured_audio(
        self,
    ) -> None:
        self.capture.stream = Mock()
        stop_event = Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            recording = executor.submit(
                self.capture.record_diagnostic_audio,
                1.0,
                stop_event=stop_event,
            )
            deadline = time.monotonic() + 1
            while self.capture._diagnostic_tap is None:
                if time.monotonic() >= deadline:
                    self.fail("Diagnostic tap was not installed.")
                time.sleep(0.001)
            self.capture._input_callback(
                b"\x01\x00\x02\x00",
                2,
                None,
                self.status,
            )
            stop_event.set()
            audio = recording.result(timeout=1)

        self.assertEqual(2, audio.frame_count)
        self.assertEqual((b"\x01\x00\x02\x00",), audio.blocks)

    def test_incomplete_diagnostic_recording_reports_received_duration(self) -> None:
        self.capture.stream = Mock()
        self.capture.first_callback_timeout_seconds = 0.001
        with (
            patch("audio.capture.DIAGNOSTIC_CAPTURE_TIMEOUT_GRACE_SECONDS", 0.001),
            self.assertRaisesRegex(
                AudioInputUnresponsiveError,
                r"provided only 0\.00 of 0\.00 seconds",
            ),
        ):
            self.capture.record_diagnostic_audio(0.004)

    def test_prepare_negotiates_a_format_that_really_produces_callbacks(self) -> None:
        capture = QueuedAudioInput(
            input_device=1,
            sample_rate=48_000,
            channels=1,
            dtype="int16",
            frame_duration_ms=20,
            queue_capacity_blocks=2,
            negotiate_format=True,
            first_callback_timeout_seconds=0.001,
        )

        class ProbeStream:
            def __init__(self, **settings: object) -> None:
                if (settings["samplerate"], settings["channels"]) != (16_000, 2):
                    raise ValueError("unsupported")
                self.callback = settings["callback"]

            def start(self) -> None:
                self.callback(b"", 0, None, Mock(input_overflow=False))

            def abort(self) -> None:
                pass

            def close(self) -> None:
                pass

        with (
            patch("audio.capture.resolve_device", return_value=1),
            patch(
                "audio.capture.query_devices",
                return_value=[
                    {},
                    {"max_input_channels": 2, "default_samplerate": 16_000},
                ],
            ),
            patch("audio.capture.validate_input_settings"),
            patch("audio.capture.sd.RawInputStream", side_effect=ProbeStream),
        ):
            selected = capture.prepare()

        self.assertEqual(InputStreamFormat(16_000, 2), selected)
        self.assertEqual(16_000, capture.sample_rate)
        self.assertEqual(2, capture.channels)

    def test_exclusive_capture_requests_wasapi_exclusive_mode(self) -> None:
        capture = QueuedAudioInput(
            input_device=1,
            sample_rate=48_000,
            channels=1,
            dtype="int16",
            frame_duration_ms=20,
            queue_capacity_blocks=2,
            exclusive=True,
            first_callback_timeout_seconds=0.01,
        )
        settings = object()

        class ProbeStream:
            def __init__(self, **stream_settings: object) -> None:
                self.callback = stream_settings["callback"]

            def start(self) -> None:
                self.callback(b"", 0, None, Mock(input_overflow=False))

            def abort(self) -> None:
                pass

            def close(self) -> None:
                pass

        with (
            patch("audio.capture.resolve_device", return_value=1),
            patch(
                "audio.capture.query_devices",
                return_value=[
                    {},
                    {"max_input_channels": 1, "default_samplerate": 48_000},
                ],
            ),
            patch("audio.capture.validate_input_settings"),
            patch(
                "audio.capture.sd.WasapiSettings", return_value=settings
            ) as wasapi,
            patch(
                "audio.capture.sd.RawInputStream", side_effect=ProbeStream
            ) as raw_stream,
        ):
            capture.prepare()

        wasapi.assert_called_once_with(exclusive=True)
        self.assertIs(
            settings,
            raw_stream.call_args.kwargs["extra_settings"],
        )

    def test_start_aborts_a_stream_that_opens_without_callbacks(self) -> None:
        capture = QueuedAudioInput(
            input_device=1,
            sample_rate=16_000,
            channels=1,
            dtype="int16",
            frame_duration_ms=20,
            queue_capacity_blocks=2,
            first_callback_timeout_seconds=0.001,
        )
        responsive = Mock()
        responsive.start.side_effect = lambda: responsive.callback(
            b"", 0, None, Mock(input_overflow=False)
        )
        silent = Mock()
        streams = iter((responsive, silent))

        def stream_factory(**settings: object) -> Mock:
            stream = next(streams)
            stream.callback = settings["callback"]
            return stream

        with (
            patch("audio.capture.resolve_device", return_value=1),
            patch(
                "audio.capture.query_devices",
                return_value=[
                    {},
                    {"max_input_channels": 1, "default_samplerate": 16_000},
                ],
            ),
            patch("audio.capture.validate_input_settings"),
            patch("audio.capture.sd.RawInputStream", side_effect=stream_factory),
        ):
            with self.assertRaises(AudioInputUnresponsiveError):
                capture.start()

        silent.abort.assert_called_once_with()
        silent.close.assert_called_once_with()

    def test_failed_switch_reopens_previous_device_and_format(self) -> None:
        capture = self.capture
        capture.input_device_reference = 1
        capture.input_device = 1
        capture.stream = Mock()

        with (
            patch("audio.capture.resolve_device", return_value=2),
            patch.object(
                capture,
                "_select_working_format",
                return_value=InputStreamFormat(16_000, 2),
            ),
            patch.object(
                capture,
                "_open_stream",
                side_effect=[RuntimeError("new device failed"), None],
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "new device failed"):
                capture.switch_device(2)

        self.assertEqual(1, capture.input_device)
        self.assertEqual(1_000, capture.sample_rate)
        self.assertEqual(1, capture.channels)


class QueuedAudioInputAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_for_block_is_notified_by_audio_callback(self) -> None:
        capture = QueuedAudioInput(
            input_device=1,
            sample_rate=1_000,
            channels=1,
            dtype="int16",
            frame_duration_ms=2,
            queue_capacity_blocks=2,
        )
        status = Mock(input_overflow=False)
        waiter = asyncio.create_task(capture.wait_for_block(1.0))
        await asyncio.sleep(0)

        capture._input_callback(b"\x01\x00\x02\x00", 2, None, status)

        self.assertEqual(b"\x01\x00\x02\x00", await waiter)

    async def test_wait_for_block_times_out_without_busy_polling(self) -> None:
        capture = QueuedAudioInput(
            input_device=1,
            sample_rate=1_000,
            channels=1,
            dtype="int16",
            frame_duration_ms=2,
            queue_capacity_blocks=2,
        )

        self.assertIsNone(await capture.wait_for_block(0.001))


if __name__ == "__main__":
    unittest.main()
