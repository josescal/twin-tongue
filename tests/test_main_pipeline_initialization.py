"""Tests for engine-specific application startup resources."""

import unittest
from unittest.mock import Mock, patch

import main as application_main


class VoiceDetectorInitializationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.voice_detection_settings = {"model_path": "silero.onnx"}
        self.config: dict[str, object] = {
            "classic_pipeline": {
                "voice_detection": self.voice_detection_settings,
            }
        }

    def test_realtime_only_does_not_initialize_silero(self) -> None:
        with (
            patch.object(
                application_main, "preload_voice_detection_package"
            ) as preload,
            patch.object(application_main, "VoiceDetectorLoader") as loader,
        ):
            result = application_main._create_voice_detector_loader(
                self.config,
                {
                    "remote_to_agent": "openai_realtime",
                    "agent_to_remote": "openai_realtime",
                },
            )

        self.assertIsNone(result)
        preload.assert_not_called()
        loader.assert_not_called()

    def test_classic_only_initializes_silero(self) -> None:
        voice_detector_loader = Mock()
        with (
            patch.object(
                application_main, "preload_voice_detection_package"
            ) as preload,
            patch.object(
                application_main,
                "VoiceDetectorLoader",
                return_value=voice_detector_loader,
            ) as loader,
        ):
            result = application_main._create_voice_detector_loader(
                self.config,
                {"remote_to_agent": "classic"},
            )

        self.assertIs(voice_detector_loader, result)
        preload.assert_called_once_with(self.voice_detection_settings)
        loader.assert_called_once_with()

    def test_mixed_engines_initialize_silero_for_classic_direction(self) -> None:
        voice_detector_loader = Mock()
        with (
            patch.object(
                application_main, "preload_voice_detection_package"
            ) as preload,
            patch.object(
                application_main,
                "VoiceDetectorLoader",
                return_value=voice_detector_loader,
            ),
        ):
            result = application_main._create_voice_detector_loader(
                self.config,
                {
                    "remote_to_agent": "openai_realtime",
                    "agent_to_remote": "classic",
                },
            )

        self.assertIs(voice_detector_loader, result)
        preload.assert_called_once_with(self.voice_detection_settings)


class PipelineSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pipelines: dict[str, object] = {
            "remote_to_agent": {"enabled": True},
            "agent_to_remote": {"enabled": False},
        }

    def test_both_selects_only_enabled_directions(self) -> None:
        self.assertEqual(
            ("remote_to_agent",),
            application_main._select_pipeline_names("both", self.pipelines),
        )

    def test_explicit_disabled_direction_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            application_main.ConfigurationError,
            "agent_to_remote must be enabled",
        ):
            application_main._select_pipeline_names(
                "agent_to_remote",
                self.pipelines,
            )

    def test_both_requires_at_least_one_enabled_direction(self) -> None:
        disabled = {
            "remote_to_agent": {"enabled": False},
            "agent_to_remote": {"enabled": False},
        }
        with self.assertRaisesRegex(
            application_main.ConfigurationError,
            "At least one pipeline",
        ):
            application_main._select_pipeline_names("both", disabled)


if __name__ == "__main__":
    unittest.main()
