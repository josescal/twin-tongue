import unittest
from unittest.mock import Mock, patch
import struct

from audio.playback import QueuedAudioOutput


class FakeRawOutputStream:
    def __init__(self, **settings: object) -> None:
        self.settings = settings
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


class QueuedAudioOutputTests(unittest.TestCase):
    def test_rejects_non_positive_queue_capacity(self) -> None:
        with self.assertRaisesRegex(ValueError, "Queue capacity"):
            QueuedAudioOutput(7, 48_000, 1, "int16", 2, queue_capacity_blocks=0)

    @patch("audio.playback.validate_output_settings")
    @patch("audio.playback.resolve_device", return_value=7)
    @patch("audio.playback.device_name", return_value="Output")
    @patch("audio.playback.sd.RawOutputStream", side_effect=FakeRawOutputStream)
    def test_callback_forwards_queued_pcm_and_stops_stream(
        self,
        raw_stream: object,
        device_name: object,
        resolve_device: object,
        validate_output: object,
    ) -> None:
        output = QueuedAudioOutput(7, 48_000, 1, "int16", 2)
        output.start()
        output.push_block(b"\x01\x02\x03\x04")
        destination = bytearray(4)

        output._output_callback(destination, 2, None, None)
        stream = output.stream
        output.stop()

        self.assertEqual(b"\x01\x02\x03\x04", bytes(destination))
        self.assertTrue(stream.started)
        self.assertTrue(stream.stopped)
        self.assertTrue(stream.closed)
        self.assertEqual(output.played_blocks, 1)

    @patch("audio.playback.validate_output_settings")
    @patch("audio.playback.resolve_device", return_value=7)
    @patch("audio.playback.device_name", return_value="Output")
    @patch("audio.playback.sd.RawOutputStream", side_effect=FakeRawOutputStream)
    def test_start_preloads_blocks_before_starting_stream(
        self,
        raw_stream: object,
        device_name: object,
        resolve_device: object,
        validate_output: object,
    ) -> None:
        output = QueuedAudioOutput(7, 48_000, 1, "int16", 2)

        output.start(initial_blocks=(b"\x01\x02\x03\x04",))

        self.assertEqual(output.buffered_blocks, 1)

    @patch("audio.playback.validate_output_settings")
    @patch("audio.playback.resolve_device", return_value=7)
    def test_callback_counts_underflow_and_empty_buffer(
        self,
        resolve_device: object,
        validate_output: object,
    ) -> None:
        output = QueuedAudioOutput(7, 48_000, 1, "int16", 2)
        status = Mock(output_underflow=True)
        destination = bytearray(4)

        output._output_callback(destination, 2, None, status)

        self.assertEqual(bytes(destination), bytes(4))
        self.assertEqual(output.output_underflows, 1)
        self.assertEqual(output.empty_buffer_events, 1)

    @patch("audio.playback.validate_output_settings")
    @patch("audio.playback.resolve_device", return_value=7)
    def test_callback_fades_out_and_back_in_around_an_empty_buffer(
        self,
        resolve_device: object,
        validate_output: object,
    ) -> None:
        output = QueuedAudioOutput(7, 1_000, 1, "int16", 10)
        steady = struct.pack("<10h", *([1_000] * 10))
        played = bytearray(20)
        faded_out = bytearray(20)
        faded_in = bytearray(20)

        output._enqueue(steady)
        output._output_callback(played, 10, None, None)
        output._output_callback(faded_out, 10, None, None)
        output._enqueue(steady)
        output._output_callback(faded_in, 10, None, None)

        self.assertEqual(
            struct.unpack("<10h", faded_out),
            (1_000, 750, 500, 250, 0, 0, 0, 0, 0, 0),
        )
        self.assertEqual(
            struct.unpack("<10h", faded_in),
            (0, 250, 500, 750, 1_000, 1_000, 1_000, 1_000, 1_000, 1_000),
        )

    @patch("audio.playback.validate_output_settings")
    @patch("audio.playback.resolve_device", return_value=7)
    @patch("audio.playback.device_name", return_value="Output")
    @patch("audio.playback.sd.RawOutputStream", side_effect=FakeRawOutputStream)
    def test_full_buffer_discards_the_oldest_block(
        self,
        raw_stream: object,
        device_name: object,
        resolve_device: object,
        validate_output: object,
    ) -> None:
        output = QueuedAudioOutput(
            7, 48_000, 1, "int16", 2, queue_capacity_blocks=2
        )
        output.start()
        output.push_block(b"\x01\x00\x01\x00")
        output.push_block(b"\x02\x00\x02\x00")
        output.push_block(b"\x03\x00\x03\x00")
        destination = bytearray(4)

        output._output_callback(destination, 2, None, None)

        self.assertEqual(bytes(destination), b"\x02\x00\x02\x00")
        self.assertEqual(output.dropped_blocks, 1)

    @patch("audio.playback.validate_output_settings")
    @patch("audio.playback.resolve_device", return_value=7)
    def test_callback_crossfades_after_discarding_old_audio(
        self,
        resolve_device: object,
        validate_output: object,
    ) -> None:
        output = QueuedAudioOutput(
            7, 1_000, 1, "int16", 10, queue_capacity_blocks=2
        )
        destination = bytearray(20)
        output._enqueue(struct.pack("<10h", *([1_000] * 10)))
        output._output_callback(destination, 10, None, None)
        output._enqueue(struct.pack("<10h", *([2_000] * 10)))
        output._enqueue(struct.pack("<10h", *([3_000] * 10)))
        output._enqueue(struct.pack("<10h", *([4_000] * 10)))

        output._output_callback(destination, 10, None, None)

        self.assertEqual(
            struct.unpack("<10h", destination),
            (1_000, 1_500, 2_000, 2_500, 3_000, 3_000, 3_000, 3_000, 3_000, 3_000),
        )
        self.assertEqual(output.dropped_blocks, 1)

    @patch("audio.playback.validate_output_settings")
    @patch("audio.playback.resolve_device", return_value=7)
    @patch("audio.playback.device_name", return_value="Output")
    @patch("audio.playback.sd.RawOutputStream", side_effect=FakeRawOutputStream)
    def test_invalid_block_is_rejected_before_the_callback(
        self,
        raw_stream: object,
        device_name: object,
        resolve_device: object,
        validate_output: object,
    ) -> None:
        output = QueuedAudioOutput(7, 48_000, 1, "int16", 2)
        output.start()

        output.push_block(b"\x01\x00")

        self.assertEqual(output.buffered_blocks, 0)
        self.assertEqual(output.invalid_block_sizes, 1)
        self.assertEqual(output.dropped_blocks, 1)

    @patch("audio.playback.validate_output_settings")
    @patch("audio.playback.resolve_device", return_value=7)
    @patch("audio.playback.sd.RawOutputStream", side_effect=FakeRawOutputStream)
    def test_discard_pending_blocks_keeps_the_callback_stream_open(
        self,
        raw_stream: object,
        resolve_device: object,
        validate_output: object,
    ) -> None:
        output = QueuedAudioOutput(7, 48_000, 1, "int16", 2)
        output.start()
        output.push_block(b"\x01\x00\x01\x00")

        self.assertEqual(output.discard_pending_blocks(), 1)
        self.assertTrue(output.started)
        self.assertEqual(output.buffered_blocks, 0)
