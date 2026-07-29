"""Tests for streaming voice-onset latency correlation."""

import unittest

from audio.speech_latency import StreamingSpeechLatencyTracker


LOUD_PCM = (10_000).to_bytes(2, "little", signed=True) * 480
SILENCE_PCM = bytes(960)


class StreamingSpeechLatencyTrackerTests(unittest.TestCase):
    def test_pairs_one_intervention_across_all_pipeline_stages(self) -> None:
        tracker = StreamingSpeechLatencyTracker(
            threshold_dbfs=-40.0,
            release_duration_ms=100.0,
        )

        self.assertIsNone(
            tracker.observe("accepted", LOUD_PCM, observed_at=1.000)
        )
        self.assertIsNone(tracker.observe("sent", LOUD_PCM, observed_at=1.025))
        self.assertIsNone(
            tracker.observe("received", LOUD_PCM, observed_at=3.000)
        )
        measurement = tracker.observe("played", LOUD_PCM, observed_at=3.080)

        assert measurement is not None
        self.assertEqual(1, measurement.utterance_id)
        self.assertAlmostEqual(25.0, measurement.accepted_to_sent_ms)
        self.assertAlmostEqual(1975.0, measurement.sent_to_received_ms)
        self.assertAlmostEqual(80.0, measurement.received_to_played_ms)
        self.assertAlmostEqual(2080.0, measurement.accepted_to_played_ms)
        snapshot = tracker.snapshot()
        self.assertEqual(1, snapshot["completed_utterances"])
        self.assertEqual(0, snapshot["pending_utterances"])
        self.assertEqual(2080.0, snapshot["last_accepted_to_played_ms"])

    def test_digital_silence_does_not_create_an_intervention(self) -> None:
        tracker = StreamingSpeechLatencyTracker()

        for stage in ("accepted", "sent", "received", "played"):
            self.assertIsNone(
                tracker.observe(stage, SILENCE_PCM, observed_at=1.0)
            )

        self.assertEqual(0, tracker.snapshot()["completed_utterances"])
        self.assertEqual(0, tracker.snapshot()["pending_utterances"])

    def test_stream_reset_discards_incomplete_pairing(self) -> None:
        tracker = StreamingSpeechLatencyTracker()
        tracker.observe("accepted", LOUD_PCM, observed_at=1.0)

        tracker.reset_stream()
        tracker.observe("sent", LOUD_PCM, observed_at=2.0)
        tracker.observe("received", LOUD_PCM, observed_at=3.0)
        self.assertIsNone(tracker.observe("played", LOUD_PCM, observed_at=3.1))

        self.assertEqual(0, tracker.snapshot()["completed_utterances"])
        self.assertEqual(0, tracker.snapshot()["pending_utterances"])


if __name__ == "__main__":
    unittest.main()
