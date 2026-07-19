"""Tests for configurable continuous PCM resampling."""

import random
import unittest

import numpy as np

from audio.resampling import (
    SoxrStreamingFloatResampler,
    create_resampler,
    float32_to_pcm_int16le,
    pcm_int16le_to_float32,
    resample_pcm_int16,
)


class PcmConversionTests(unittest.TestCase):
    def test_pcm_int16_round_trip_handles_full_range(self) -> None:
        source = np.asarray([-32768, -16384, 0, 16384, 32767], dtype="<i2").tobytes()
        converted = pcm_int16le_to_float32(source)
        self.assertEqual(float32_to_pcm_int16le(converted), source)

    def test_pcm_rejects_incomplete_sample(self) -> None:
        with self.assertRaisesRegex(ValueError, "complete samples"):
            pcm_int16le_to_float32(b"\x00")


class SoxrStreamingResamplerTests(unittest.TestCase):
    def test_48k_to_16k_is_continuous_across_arbitrary_blocks(self) -> None:
        time = np.arange(48_000, dtype=np.float32) / 48_000
        source = (0.5 * np.sin(2 * np.pi * 440 * time)).astype(np.float32)
        expected_resampler = SoxrStreamingFloatResampler(48_000, 16_000, quality="HQ")
        expected = expected_resampler.process(source)

        resampler = SoxrStreamingFloatResampler(48_000, 16_000, quality="HQ")
        output: list[np.ndarray] = []
        random.seed(7)
        position = 0
        while position < len(source):
            end = min(len(source), position + random.randint(1, 137))
            output.append(resampler.process(source[position:end]))
            position = end

        actual = np.concatenate(output)
        np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-6)

    def test_reset_discards_pending_stream_state(self) -> None:
        resampler = SoxrStreamingFloatResampler(48_000, 16_000)
        resampler.process(np.ones(2, dtype=np.float32))
        resampler.reset()
        actual = resampler.process(np.ones(960, dtype=np.float32))

        fresh = SoxrStreamingFloatResampler(48_000, 16_000)
        expected = fresh.process(np.ones(960, dtype=np.float32))
        np.testing.assert_array_equal(actual, expected)

    def test_factory_selects_configured_backend_and_quality(self) -> None:
        resampler = create_resampler(
            {"backend": "soxr", "quality": "VHQ"},
            48_000,
            16_000,
        )
        self.assertEqual(resampler.output_rate, 16_000)
        self.assertEqual(resampler._backend.quality, "VHQ")
        output = resampler.process(np.zeros(960, dtype="<i2").tobytes())
        self.assertIsInstance(output, tuple)

    def test_pcm_stream_normalizes_soxr_bursts_to_fixed_blocks(self) -> None:
        resampler = create_resampler(
            {"backend": "soxr", "quality": "HQ"},
            48_000,
            16_000,
            output_block_frames=320,
        )
        blocks = tuple(
            block
            for _ in range(20)
            for block in resampler.process(bytes(960 * 2))
        )

        self.assertGreater(len(blocks), 10)
        self.assertTrue(all(len(block) == 320 * 2 for block in blocks))

    def test_complete_segment_is_flushed_and_preserves_duration(self) -> None:
        source = np.zeros(16_000, dtype="<i2").tobytes()
        output = resample_pcm_int16(
            {"backend": "soxr", "quality": "HQ"},
            source,
            16_000,
            48_000,
        )
        self.assertEqual(len(output) // 2, 48_000)

    def test_same_rate_segment_is_not_modified(self) -> None:
        source = b"\x01\x02\x03\x04"
        self.assertIs(
            resample_pcm_int16({"backend": "soxr"}, source, 16_000, 16_000),
            source,
        )

    def test_factory_rejects_unknown_backend(self) -> None:
        with self.assertRaisesRegex(ValueError, "backend"):
            create_resampler({"backend": "future-library"}, 48_000, 16_000)


if __name__ == "__main__":
    unittest.main()
