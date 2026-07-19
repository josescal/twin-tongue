"""Tests for adaptive STT segmentation."""

import unittest

from audio.speech_segmentation import (
    AdaptiveSpeechSegmenter,
    validate_speech_segmentation_settings,
)
from audio.voice_detection import VoiceDetectionResult


def _detection(
    *,
    started: bool = False,
    ended: bool = False,
    active: bool = True,
    silence_ms: float = 0,
) -> VoiceDetectionResult:
    return VoiceDetectionResult(
        forwarded_blocks=(b"pcm",),
        speech_started=started,
        speech_ended=ended,
        active=active,
        silence_duration_ms=silence_ms,
    )


def _segmenter() -> AdaptiveSpeechSegmenter:
    return AdaptiveSpeechSegmenter(
        frame_duration_ms=20,
        minimum_segment_duration_ms=40,
        preferred_segment_duration_ms=60,
        short_pause_duration_ms=20,
        partial_boundary_stability_ms=10,
        maximum_segment_duration_ms=100,
    )


class AdaptiveSpeechSegmenterTests(unittest.TestCase):
    def test_voice_end_commits_a_normal_utterance(self) -> None:
        segmenter = _segmenter()
        segmenter.process(_detection(started=True), now=0)

        decision = segmenter.process(
            _detection(ended=True, active=False),
            now=0.02,
        )

        self.assertTrue(decision.should_commit)
        self.assertEqual(decision.reason, "silence")
        self.assertEqual(segmenter.statistics.silence_boundaries, 1)

    def test_stable_punctuation_commits_after_minimum_duration(self) -> None:
        segmenter = _segmenter()
        segmenter.process(_detection(started=True), now=0)
        segmenter.observe_partial("A complete clause.", now=0)

        decision = segmenter.process(_detection(), now=0.02)

        self.assertTrue(decision.should_commit)
        self.assertEqual(decision.reason, "punctuation")

    def test_unstable_or_ellipsis_partial_does_not_commit(self) -> None:
        segmenter = _segmenter()
        segmenter.process(_detection(started=True), now=0)
        segmenter.observe_partial("Still thinking...", now=0.015)

        decision = segmenter.process(_detection(), now=0.02)

        self.assertFalse(decision.should_commit)

    def test_short_pause_commits_only_after_preferred_duration(self) -> None:
        segmenter = _segmenter()
        segmenter.process(_detection(started=True), now=0)
        early = segmenter.process(_detection(silence_ms=20), now=0.02)
        decision = segmenter.process(_detection(silence_ms=20), now=0.04)

        self.assertFalse(early.should_commit)
        self.assertTrue(decision.should_commit)
        self.assertEqual(decision.reason, "short_pause")

    def test_maximum_duration_commits_without_ending_voice_activity(self) -> None:
        segmenter = _segmenter()
        segmenter.process(_detection(started=True), now=0)
        for index in range(3):
            self.assertFalse(
                segmenter.process(_detection(), now=(index + 1) * 0.02).should_commit
            )

        decision = segmenter.process(_detection(), now=0.08)

        self.assertTrue(decision.should_commit)
        self.assertEqual(decision.reason, "maximum_duration")
        self.assertTrue(decision.should_commit)

    def test_short_pause_does_not_create_a_second_empty_silence_commit(self) -> None:
        segmenter = _segmenter()
        segmenter.process(_detection(started=True), now=0)
        segmenter.process(_detection(), now=0.02)
        first = segmenter.process(_detection(silence_ms=20), now=0.04)
        ended = segmenter.process(
            _detection(ended=True, active=False, silence_ms=40),
            now=0.06,
        )

        self.assertTrue(first.should_commit)
        self.assertFalse(ended.should_commit)

    def test_configuration_requires_ordered_boundaries_and_short_pause(self) -> None:
        settings = {
            "minimum_segment_duration_ms": 2000,
            "preferred_segment_duration_ms": 5000,
            "short_pause_duration_ms": 150,
            "partial_boundary_stability_ms": 250,
            "maximum_segment_duration_ms": 8000,
        }
        validate_speech_segmentation_settings(
            settings,
            voice_end_silence_duration_ms=350,
        )
        invalid = dict(settings, maximum_segment_duration_ms=4000)
        with self.assertRaisesRegex(ValueError, "Maximum"):
            validate_speech_segmentation_settings(invalid)
        invalid = dict(settings, short_pause_duration_ms=350)
        with self.assertRaisesRegex(ValueError, "shorter"):
            validate_speech_segmentation_settings(
                invalid,
                voice_end_silence_duration_ms=350,
            )


if __name__ == "__main__":
    unittest.main()
