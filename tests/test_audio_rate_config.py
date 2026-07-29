"""Configuration tests for the 48 kHz device / 16 kHz processing boundary."""

from pathlib import Path
import unittest
import tomllib
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
from config import ConfigurationError, load_config


class AudioRateConfigurationTests(unittest.TestCase):
    def test_toml_uses_namespaced_schema_v2_without_legacy_sections(self) -> None:
        with (ROOT / "config" / "default.toml").open("rb") as config_file:
            raw = tomllib.load(config_file)

        self.assertEqual(2, raw["schema_version"])
        self.assertIn("runtime", raw["pipeline"])
        self.assertIn("classic", raw["pipeline"])
        self.assertIn("speech_to_speech", raw["pipeline"])
        self.assertEqual(
            "speech_to_speech",
            raw["pipeline"]["runtime"]["remote_to_agent"]["type"],
        )
        self.assertEqual(
            "speech_to_speech",
            raw["pipeline"]["runtime"]["agent_to_remote"]["type"],
        )
        self.assertEqual(
            "elevenlabs",
            raw["pipeline"]["classic"]["remote_to_agent"]["stt"]["engine"],
        )
        self.assertEqual(
            "openai_realtime",
            raw["pipeline"]["speech_to_speech"]["remote_to_agent"]
            ["translation"]["engine"],
        )
        for legacy in (
            "pipelines",
            "providers",
            "stt",
            "translation",
            "tts",
            "classic_pipeline",
            "realtime_translation",
            "logging",
            "server",
        ):
            self.assertNotIn(legacy, raw)

    def test_runtime_type_and_direction_override_are_resolved_from_file(self) -> None:
        source = (ROOT / "config" / "default.toml").read_text(encoding="utf-8")
        source = source.replace(
            "[pipeline.runtime.agent_to_remote]\n"
            "enabled = true\n"
            'mode = "passthrough"\n'
            'type = "speech_to_speech"',
            "[pipeline.runtime.agent_to_remote]\n"
            "enabled = true\n"
            'mode = "passthrough"\n'
            'type = "classic"',
        )
        source = source.replace(
            "[pipeline.speech_to_speech.remote_to_agent.translation]\n"
            'engine = "openai_realtime"',
            "[pipeline.speech_to_speech.remote_to_agent.translation]\n"
            'engine = "openai_realtime"\n\n'
            "[pipeline.speech_to_speech.remote_to_agent.translation.openai_realtime]\n"
            "playback_queue_capacity_blocks = 10\n\n"
            "[pipeline.speech_to_speech.remote_to_agent.translation."
            "openai_realtime.input_transcription]\n"
            "enabled = false",
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(source, encoding="utf-8")
            config = load_config(path)

        self.assertEqual(
            "openai_realtime", config["pipelines"]["remote_to_agent"]["engine"]
        )
        self.assertEqual("classic", config["pipelines"]["agent_to_remote"]["engine"])
        self.assertEqual(
            10,
            config["realtime_translation"]["directions"]["remote_to_agent"]
            ["playback_queue_capacity_blocks"],
        )
        self.assertEqual(
            4,
            config["realtime_translation"]["directions"]["agent_to_remote"]
            ["playback_queue_capacity_blocks"],
        )
        self.assertEqual(
            {
                "enabled": False,
                "model": "gpt-realtime-whisper",
            },
            config["realtime_translation"]["directions"]["remote_to_agent"]
            ["input_transcription"],
        )

    def test_default_rates_are_explicit_and_consistent(self) -> None:
        config = load_config(ROOT / "config" / "default.toml")

        self.assertNotIn("languages", config)
        self.assertEqual(config["audio"]["processing_sample_rate"], 16_000)
        self.assertFalse(config["audio"]["physical_capture_exclusive_mode"])
        self.assertEqual(
            {
                "enabled": True,
                "stream_delay_ms": 0,
                "render_queue_capacity_blocks": 100,
            },
            config["audio"]["aec3"],
        )
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
        self.assertNotIn("voice_gender", config["tts"])
        self.assertEqual(
            "male", config["pipelines"]["remote_to_agent"]["voice_gender"]
        )
        self.assertEqual(
            "male", config["pipelines"]["agent_to_remote"]["voice_gender"]
        )
        self.assertEqual(
            "openai_realtime", config["pipelines"]["remote_to_agent"]["engine"]
        )
        self.assertEqual(
            "openai_realtime", config["pipelines"]["agent_to_remote"]["engine"]
        )
        self.assertEqual(
            24_000, config["realtime_translation"]["openai"]["sample_rate"]
        )
        self.assertEqual(
            "gpt-realtime-translate",
            config["realtime_translation"]["openai"]["model"],
        )
        self.assertEqual(
            200,
            config["realtime_translation"]["openai"]["send_chunk_duration_ms"],
        )
        self.assertEqual(
            25,
            config["realtime_translation"]["openai"]["input_queue_capacity_blocks"],
        )
        self.assertEqual(
            3,
            config["realtime_translation"]["openai"]["send_queue_capacity_frames"],
        )
        self.assertEqual(
            25,
            config["realtime_translation"]["openai"][
                "received_queue_capacity_blocks"
            ],
        )
        self.assertEqual(
            4,
            config["realtime_translation"]["openai"]["playback_queue_capacity_blocks"],
        )
        self.assertFalse(
            config["realtime_translation"]["openai"]["log_transcript_deltas"]
        )
        self.assertEqual(
            {
                "enabled": True,
                "model": "gpt-realtime-whisper",
            },
            config["realtime_translation"]["openai"]["input_transcription"],
        )
        self.assertEqual(
            {
                "enabled": True,
                "directory": "logs/realtime-audio",
                "max_seconds_per_file": 120.0,
                "write_timing_marks": True,
                "queue_capacity_blocks": 500,
                "write_buffer_kb": 64,
                "flush_interval_seconds": 1.0,
            },
            config["realtime_translation"]["openai"]["audio_capture"],
        )
        self.assertEqual(
            150,
            config["realtime_translation"]["openai"][
                "transcript_ui_update_interval_ms"
            ],
        )
        self.assertEqual(
            500,
            config["realtime_translation"]["openai"][
                "transcript_segment_idle_ms"
            ],
        )
        self.assertEqual(
            {
                "poll_interval_seconds": 1.0,
                "failure_grace_seconds": 5.0,
            },
            config["audio"]["virtual_cables"]["session_monitor"],
        )
        self.assertEqual(
            {
                "enabled": True,
                "disconnect_grace_seconds": 3.0,
            },
            config["realtime_translation"]["openai"]["session_gate"],
        )
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
            2,
        )
        self.assertEqual(
            config["pipelines"]["remote_to_agent"]["output_sample_rate"],
            48_000,
        )
        self.assertEqual(
            config["pipelines"]["agent_to_remote"]["output_sample_rate"],
            48_000,
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
            config["pipelines"]["remote_to_agent"]["classic"]["segmentation"],
            expected_remote_segmentation,
        )
        self.assertEqual(
            config["pipelines"]["agent_to_remote"]["classic"]["segmentation"],
            expected_agent_segmentation,
        )
        self.assertNotIn("voice_detection", config)
        self.assertEqual(
            "silero", config["classic_pipeline"]["voice_detection"]["backend"]
        )

    def test_realtime_provider_frame_duration_must_be_two_hundred_ms(self) -> None:
        source = (ROOT / "config" / "default.toml").read_text(encoding="utf-8")
        source = source.replace(
            "send_chunk_duration_ms = 200",
            "send_chunk_duration_ms = 20",
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(source, encoding="utf-8")
            with self.assertRaisesRegex(
                ConfigurationError,
                r"send_chunk_duration_ms must be 200",
            ):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
