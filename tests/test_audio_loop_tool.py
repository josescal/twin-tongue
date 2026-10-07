"""Tests for the isolated record/playback diagnostic."""

from array import array
import unittest

from audio.pcm import RecordedAudio
from tools.test_audio_loop import _pipeline_downmix


class AudioLoopToolTests(unittest.TestCase):
    def test_pipeline_downmix_matches_production_channel_conversion(self) -> None:
        stereo = RecordedAudio(
            blocks=(array("h", [1_000, -1_000, 2_000, 0]).tobytes(),),
            sample_rate=48_000,
            channels=2,
            dtype="int16",
            frames_per_block=2,
            frame_count=2,
        )

        mono = _pipeline_downmix(stereo)

        self.assertEqual([0, 1_000], list(array("h", mono.blocks[0])))
        self.assertEqual(1, mono.channels)
        self.assertEqual(stereo.sample_rate, mono.sample_rate)
        self.assertEqual(stereo.frame_count, mono.frame_count)

    def test_pipeline_downmix_rejects_non_stereo_input(self) -> None:
        mono = RecordedAudio(
            blocks=(array("h", [1_000]).tobytes(),),
            sample_rate=48_000,
            channels=1,
            dtype="int16",
            frames_per_block=1,
            frame_count=1,
        )

        with self.assertRaisesRegex(ValueError, "--channels 2"):
            _pipeline_downmix(mono)


if __name__ == "__main__":
    unittest.main()
