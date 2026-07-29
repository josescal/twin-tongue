"""Tests for user-triggered physical audio checks."""

from array import array
import unittest
from unittest.mock import Mock, patch

from audio.diagnostics import (
    _assess_physical_path,
    test_microphone as run_microphone_test,
    test_physical_audio_path as run_physical_audio_path_test,
    test_speaker as run_speaker_test,
)
from audio.pcm import RecordedAudio


class AudioDiagnosticTests(unittest.TestCase):
    def test_manual_recording_with_clear_peak_and_silent_sections_passes(
        self,
    ) -> None:
        captured = [
            {"channel": 1, "peak_dbfs": -24.1, "rms_dbfs": -44.4},
            {"channel": 2, "peak_dbfs": -24.1, "rms_dbfs": -44.4},
        ]
        mono = [{"channel": 1, "peak_dbfs": -24.1, "rms_dbfs": -44.4}]

        self.assertEqual("passed", _assess_physical_path(captured, mono))

    def test_physical_path_requires_low_peak_and_rms_for_low_signal(
        self,
    ) -> None:
        captured = [
            {"channel": 1, "peak_dbfs": -36.0, "rms_dbfs": -55.0},
            {"channel": 2, "peak_dbfs": -36.0, "rms_dbfs": -55.0},
        ]
        mono = [{"channel": 1, "peak_dbfs": -36.0, "rms_dbfs": -55.0}]

        self.assertEqual("low_signal", _assess_physical_path(captured, mono))

    def test_physical_path_reuses_stereo_pipeline_capture_and_plays_downmix(
        self,
    ) -> None:
        captured = RecordedAudio(
            blocks=(array("h", (8_000, 8_000, -8_000, -8_000)).tobytes(),),
            sample_rate=16_000,
            channels=2,
            dtype="int16",
            frames_per_block=2,
            frame_count=2,
        )
        capture = Mock()
        capture.record_diagnostic_audio.return_value = captured

        with (
            patch("audio.diagnostics.active_audio_input", return_value=capture),
            patch("audio.diagnostics.device_name", side_effect=("Mic", "Speaker")),
            patch(
                "audio.diagnostics.query_devices",
                return_value=[{} for _ in range(8)]
                + [{"max_output_channels": 2}],
            ),
            patch("audio.diagnostics.play_audio") as play,
        ):
            result = run_physical_audio_path_test(7, 8)

        capture.record_diagnostic_audio.assert_called_once()
        played = play.call_args.args[0]
        self.assertEqual(2, played.channels)
        self.assertEqual(
            array("h", (8_000, 8_000, -8_000, -8_000)).tobytes(),
            played.blocks[0],
        )
        self.assertEqual("stereo_to_mono_average", result["conversion"])
        self.assertEqual(1, result["pipeline_channels"])
        self.assertEqual(2, result["playback_channels"])
        self.assertFalse(result["aec3_applied"])
        self.assertEqual("passed", result["assessment"])
        self.assertEqual(2, len(result["channel_levels"]))
        self.assertTrue(result["passed"])

    def test_physical_path_detects_destructive_stereo_downmix(self) -> None:
        captured = RecordedAudio(
            blocks=(array("h", (8_000, -8_000, -8_000, 8_000)).tobytes(),),
            sample_rate=16_000,
            channels=2,
            dtype="int16",
            frames_per_block=2,
            frame_count=2,
        )
        capture = Mock()
        capture.record_diagnostic_audio.return_value = captured

        with (
            patch("audio.diagnostics.active_audio_input", return_value=capture),
            patch("audio.diagnostics.device_name", side_effect=("Mic", "Speaker")),
            patch(
                "audio.diagnostics.query_devices",
                return_value=[{} for _ in range(8)]
                + [{"max_output_channels": 2}],
            ),
            patch("audio.diagnostics.play_audio"),
        ):
            result = run_physical_audio_path_test(7, 8)

        self.assertEqual("downmix_cancellation", result["assessment"])
        self.assertFalse(result["passed"])
        self.assertEqual(-96.0, result["downmixed_level"]["peak_dbfs"])

    def test_microphone_reports_level_and_negotiated_format(self) -> None:
        capture = Mock()
        capture.sample_rate = 16_000
        capture.channels = 2
        capture.statistics.captured_blocks = 2
        blocks = [array("h", (8_000, -8_000, 4_000, -4_000)).tobytes()]
        capture.pop_block.side_effect = lambda: blocks.pop(0) if blocks else None

        with (
            patch("audio.diagnostics.QueuedAudioInput", return_value=capture),
            patch("audio.diagnostics.device_name", return_value="Test microphone"),
            patch("audio.diagnostics.MICROPHONE_TEST_SECONDS", 0.001),
        ):
            result = run_microphone_test(7)

        self.assertTrue(result["signal_detected"])
        self.assertEqual(16_000, result["sample_rate"])
        self.assertEqual(2, result["channels"])
        capture.prepare.assert_called_once_with()
        capture.start.assert_called_once_with()
        capture.stop.assert_called_once_with()

    def test_microphone_reuses_an_exclusive_pipeline_capture(self) -> None:
        capture = Mock()
        capture.sample_rate = 48_000
        capture.channels = 1
        capture.statistics.captured_blocks = 100
        capture.recent_level_summary.return_value = (-18.0, -27.0)

        with (
            patch(
                "audio.diagnostics.active_audio_input",
                return_value=capture,
            ),
            patch("audio.diagnostics.device_name", return_value="Live microphone"),
            patch("audio.diagnostics.MICROPHONE_TEST_SECONDS", 0),
        ):
            result = run_microphone_test(7)

        self.assertEqual("active_pipeline", result["source"])
        self.assertTrue(result["signal_detected"])
        capture.recent_level_summary.assert_called_once()

    def test_speaker_writes_a_short_tone_to_the_selected_device(self) -> None:
        stream = Mock()
        with (
            patch(
                "audio.diagnostics.query_devices",
                return_value=[
                    {},
                    {"max_output_channels": 2, "default_samplerate": 48_000},
                ],
            ),
            patch("audio.diagnostics.validate_output_settings") as validate,
            patch("audio.diagnostics.sd.RawOutputStream", return_value=stream),
            patch("audio.diagnostics.device_name", return_value="Test headphones"),
        ):
            result = run_speaker_test(1)

        validate.assert_called_once_with(1, 48_000, 2, "int16")
        stream.start.assert_called_once_with()
        stream.write.assert_called_once()
        stream.stop.assert_called_once_with()
        stream.close.assert_called_once_with()
        self.assertEqual("output", result["direction"])
        self.assertEqual(660.0, result["tone_hz"])


if __name__ == "__main__":
    unittest.main()
