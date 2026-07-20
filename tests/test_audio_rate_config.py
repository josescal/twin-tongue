"""Configuration tests for the 48 kHz device / 16 kHz processing boundary."""

from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
from config import load_config


class AudioRateConfigurationTests(unittest.TestCase):
    def test_default_rates_are_explicit_and_consistent(self) -> None:
        config = load_config(ROOT / "config" / "default.toml")

        self.assertNotIn("languages", config)
        self.assertEqual(config["audio"]["processing_sample_rate"], 16_000)
        self.assertEqual(config["stt"]["sample_rate"], 16_000)
        self.assertEqual(config["stt"]["audio_format"], "pcm_16000")
        self.assertTrue(config["stt"]["include_timestamps"])
        self.assertTrue(config["stt"]["no_verbatim"])
        self.assertEqual([], config["stt"]["keyterms"])
        self.assertEqual(100, config["stt"]["send_chunk_duration_ms"])
        self.assertEqual(
            {
                "enabled": True,
                "directory": "logs/stt-audio",
                "max_seconds_per_file": 120.0,
                "write_timing_marks": False,
                "write_buffer_kb": 64,
                "flush_interval_seconds": 1.0,
            },
            config["stt"]["audio_capture"],
        )
        self.assertEqual(config["tts"]["sample_rate"], 16_000)
        self.assertEqual(config["tts"]["output_format"], "pcm_16000")
        self.assertEqual(config["tts"]["voice_gender"], "male")
        for language in ("ca", "en", "es", "fr"):
            voices = config["tts"]["language_voices"][language]
            self.assertEqual({"male", "female"}, set(voices))
            self.assertEqual("LudcwvHIZaqQOcQfVZSY", voices["female"])
        self.assertEqual(
            config["pipelines"]["remote_to_agent"]["input_sample_rate"],
            48_000,
        )
        self.assertEqual(
            config["pipelines"]["agent_to_remote"]["input_sample_rate"],
            48_000,
        )
        self.assertEqual(
            config["pipelines"]["remote_to_agent"]["input_channels"],
            2,
        )
        self.assertEqual(
            config["pipelines"]["agent_to_remote"]["input_channels"],
            1,
        )
        self.assertEqual(
            config["pipelines"]["remote_to_agent"]["output_sample_rate"],
            48_000,
        )
        self.assertEqual(
            config["pipelines"]["agent_to_remote"]["output_sample_rate"],
            48_000,
        )
        self.assertEqual(
            {"enabled": False, "resume_delay_ms": 300},
            config["pipelines"]["agent_to_remote"]["barge_in"],
        )
        expected_latency_protection = {
            "enabled": True,
            "playback_backlog_discard_age_seconds": 8.0,
            "segment_discard_age_seconds": 15.0,
            "playback_backlog_segments_to_keep": 2,
        }
        self.assertEqual(
            config["pipelines"]["remote_to_agent"]["latency_protection"],
            expected_latency_protection,
        )
        self.assertEqual(
            config["pipelines"]["agent_to_remote"]["latency_protection"],
            expected_latency_protection,
        )
        expected_remote_segmentation = {
            "minimum_segment_duration_ms": 1500,
            "preferred_segment_duration_ms": 2500,
            "short_pause_duration_ms": 150,
            "partial_boundary_stability_ms": 200,
            "maximum_segment_duration_ms": 5000,
        }
        expected_agent_segmentation = {
            **expected_remote_segmentation,
            "minimum_segment_duration_ms": 2200,
            "partial_boundary_stability_ms": 350,
        }
        self.assertEqual(
            config["pipelines"]["remote_to_agent"]["segmentation"],
            expected_remote_segmentation,
        )
        self.assertEqual(
            config["pipelines"]["agent_to_remote"]["segmentation"],
            expected_agent_segmentation,
        )


if __name__ == "__main__":
    unittest.main()
