"""Tests for provider selection, credentials, and client construction."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from providers.elevenlabs_stt import ElevenLabsRealtimeSTT
from providers.elevenlabs_tts import ElevenLabsTTS
from providers.errors import ProviderConfigurationError, ProviderError
from providers.factory import ProviderCredentials, ProviderFactory
from providers.google_translate import GoogleTranslateBasicV2
from providers.stt import STTError
from providers.translate import TranslationError
from providers.tts import TTSError


def _config() -> dict[str, object]:
    return {
        "providers": {
            "stt": "elevenlabs",
            "translation": "google_translate_basic_v2",
            "tts": "elevenlabs",
        },
        "stt": {
            "model": "scribe_v2_realtime",
            "audio_format": "pcm_16000",
            "sample_rate": 16_000,
            "include_timestamps": False,
        },
        "translation": {
            "endpoint": "https://translation.googleapis.com/language/translate/v2",
            "format": "text",
        },
        "tts": {
            "model": "eleven_flash_v2_5",
            "voice_id": "default-voice",
            "voice_gender": "male",
            "output_format": "pcm_16000",
            "sample_rate": 16_000,
            "speed": 1.1,
            "language_voices": {
                "es": {"male": "spanish-male", "female": "spanish-female"}
            },
            "language_models": {},
        },
    }


def _credentials() -> ProviderCredentials:
    return ProviderCredentials(
        elevenlabs_api_key="elevenlabs-test",
        google_translate_api_key="google-test",
        elevenlabs_base_url="https://example.invalid",
    )


class ProviderFactoryTests(unittest.TestCase):
    def test_loads_credentials_from_the_environment_file(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            env_path = Path(temporary_directory) / ".env"
            env_path.write_text(
                "ELEVENLABS_API_KEY=elevenlabs-from-env\n"
                "GOOGLE_TRANSLATE_API_KEY=google-from-env\n"
                "ELEVENLABS_BASE_URL=https://providers.example.invalid\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                factory = ProviderFactory.from_environment(_config(), env_path)

        stt = factory.create_stt(language="fr")
        self.assertEqual("https://providers.example.invalid", stt.base_url)

    def test_constructs_the_configured_provider_clients(self) -> None:
        factory = ProviderFactory(_config(), _credentials())

        stt = factory.create_stt(language="en")
        translator = factory.create_translator()
        tts = factory.create_tts()

        self.assertIsInstance(stt, ElevenLabsRealtimeSTT)
        self.assertEqual("en", stt.language)
        self.assertIsInstance(translator, GoogleTranslateBasicV2)
        self.assertIsInstance(tts, ElevenLabsTTS)
        self.assertEqual("spanish-female", tts.language_voices["es"]["female"])

    def test_rejects_an_unsupported_provider_selection(self) -> None:
        config = _config()
        config["providers"]["stt"] = "other"  # type: ignore[index]

        with self.assertRaisesRegex(
            ProviderConfigurationError, "Unsupported provider selection"
        ):
            ProviderFactory(config, _credentials())

    def test_rejects_missing_credentials_and_invalid_url(self) -> None:
        with self.assertRaisesRegex(
            ProviderConfigurationError, "ELEVENLABS_API_KEY"
        ):
            ProviderFactory(
                _config(),
                ProviderCredentials("", "google-test"),
            )
        with self.assertRaisesRegex(
            ProviderConfigurationError, "ELEVENLABS_BASE_URL"
        ):
            ProviderFactory(
                _config(),
                ProviderCredentials("elevenlabs-test", "google-test", "invalid"),
            )

    def test_provider_specific_errors_share_one_base_class(self) -> None:
        self.assertTrue(issubclass(STTError, ProviderError))
        self.assertTrue(issubclass(TranslationError, ProviderError))
        self.assertTrue(issubclass(TTSError, ProviderError))


if __name__ == "__main__":
    unittest.main()
