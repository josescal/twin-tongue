"""Tests for common PCM helpers."""

import struct
import unittest

from audio.pcm import (
    apply_int16_gain,
    calculate_block_bytes,
    calculate_block_frames,
    convert_int16_channels,
)


class PCMConversionTests(unittest.TestCase):
    def test_adaptation_from_mono_to_stereo_duplicates_every_sample(self) -> None:
        mono = struct.pack("<hhh", -32768, 0, 32767)

        stereo = convert_int16_channels(mono, 1, 2)

        self.assertEqual(
            struct.unpack("<hhhhhh", stereo),
            (-32768, -32768, 0, 0, 32767, 32767),
        )

    def test_adaptation_from_mono_rejects_incomplete_sample(self) -> None:
        with self.assertRaisesRegex(ValueError, "complete 2-byte samples"):
            convert_int16_channels(b"\x00", 1, 2)

    def test_mono_stereo_round_trip_preserves_samples(self) -> None:
        mono = struct.pack("<hhhh", -12345, -1, 1, 12345)

        stereo = convert_int16_channels(mono, 1, 2)

        self.assertEqual(convert_int16_channels(stereo, 2, 1), mono)

    def test_stereo_downmix_truncates_half_samples_towards_zero(self) -> None:
        stereo = struct.pack("<hhhh", -1, 0, 1, 0)

        mono = convert_int16_channels(stereo, 2, 1)

        self.assertEqual(struct.unpack("<hh", mono), (0, 0))

    def test_adaptation_from_stereo_rejects_incomplete_frame(self) -> None:
        with self.assertRaisesRegex(ValueError, "complete 4-byte"):
            convert_int16_channels(b"\x00\x00", 2, 1)

    def test_passthrough_keeps_matching_channel_layout_unchanged(self) -> None:
        block = struct.pack("<hhhh", 1, 2, 3, 4)

        self.assertIs(block, convert_int16_channels(block, 2, 2))

    def test_passthrough_rejects_unsupported_channel_layout(self) -> None:
        with self.assertRaisesRegex(ValueError, "3 -> 2"):
            convert_int16_channels(b"", 3, 2)

    def test_gain_amplifies_and_saturates_pcm16_safely(self) -> None:
        pcm = struct.pack("<hhhh", -20_000, -1_000, 1_000, 20_000)

        amplified, clipped = apply_int16_gain(pcm, 6.0)

        samples = struct.unpack("<hhhh", amplified)
        self.assertEqual((-32768, 32767), (samples[0], samples[-1]))
        self.assertGreater(abs(samples[1]), 1_900)
        self.assertEqual(2, clipped)

    def test_zero_gain_preserves_the_original_buffer(self) -> None:
        pcm = struct.pack("<hh", -123, 456)

        unchanged, clipped = apply_int16_gain(pcm, 0.0)

        self.assertIs(unchanged, pcm)
        self.assertEqual(0, clipped)

    def test_calculate_block_frames_rounds_to_a_positive_frame_count(self) -> None:
        self.assertEqual(calculate_block_frames(44_100, 20), 882)
        self.assertEqual(calculate_block_frames(1, 1), 1)

    def test_calculate_block_frames_rejects_invalid_inputs(self) -> None:
        with self.assertRaisesRegex(ValueError, "Sample rate"):
            calculate_block_frames(0, 20)
        with self.assertRaisesRegex(ValueError, "Frame duration"):
            calculate_block_frames(48_000, 0)

    def test_calculate_block_bytes_supports_raw_portaudio_formats(self) -> None:
        self.assertEqual(calculate_block_bytes(960, 2, "int16"), 3_840)
        self.assertEqual(calculate_block_bytes(960, 2, "int24"), 5_760)


if __name__ == "__main__":
    unittest.main()
