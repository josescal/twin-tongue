"""Tests for pitch-preserving realtime time compression."""

import math
import unittest

import numpy as np

from audio.time_stretch import StreamingWsola


class StreamingWsolaTests(unittest.TestCase):
    def test_unity_speed_preserves_duration_and_waveform_shape(self) -> None:
        rate = 24_000
        samples = (
            np.sin(2 * math.pi * 440 * np.arange(rate) / rate) * 12_000
        ).astype("<i2")
        stretcher = StreamingWsola(rate)
        output: list[bytes] = []
        for start in range(0, len(samples), 480):
            output.extend(
                stretcher.process(samples[start : start + 480].tobytes())
            )
        output.extend(stretcher.process(b"", final=True))
        rendered = np.frombuffer(b"".join(output), dtype="<i2")

        self.assertLess(abs(len(rendered) - len(samples)), 1_000)
        self.assertGreater(float(np.sqrt(np.mean(rendered.astype(float) ** 2))), 5_000)

    def test_acceleration_shortens_audio_without_raising_pitch(self) -> None:
        rate = 24_000
        source = (
            np.sin(2 * math.pi * 440 * np.arange(rate * 2) / rate) * 12_000
        ).astype("<i2")
        stretcher = StreamingWsola(rate)
        output: list[bytes] = []
        for start in range(0, len(source), 480):
            output.extend(
                stretcher.process(
                    source[start : start + 480].tobytes(),
                    speed=1.15,
                )
            )
        output.extend(stretcher.process(b"", speed=1.15, final=True))
        rendered = np.frombuffer(b"".join(output), dtype="<i2")
        zero_crossings = np.flatnonzero(np.diff(np.signbit(rendered)))
        estimated_hz = len(zero_crossings) / 2 / (len(rendered) / rate)

        self.assertLess(len(rendered), len(source) * 0.92)
        self.assertAlmostEqual(440.0, estimated_hz, delta=20.0)


if __name__ == "__main__":
    unittest.main()
