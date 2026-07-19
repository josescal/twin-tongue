"""Tests for preallocated callback-driven PCM input."""

import unittest
from unittest.mock import Mock

from audio.capture import QueuedAudioInput


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


if __name__ == "__main__":
    unittest.main()
