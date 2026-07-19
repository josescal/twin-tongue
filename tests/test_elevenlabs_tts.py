"""Unit tests for ElevenLabs TTS request settings."""

import asyncio
import unittest

from providers.elevenlabs_tts import ElevenLabsTTS
from providers.tts import TTSConfigurationError


class _FakeTextToSpeech:
    def __init__(self) -> None:
        self.arguments: dict[str, object] = {}

    def stream(self, **kwargs: object):
        self.arguments = kwargs

        async def chunks():
            yield b"\x00\x00"

        return chunks()


class _FakeClient:
    def __init__(self) -> None:
        self.text_to_speech = _FakeTextToSpeech()


class ElevenLabsTTSSpeedTests(unittest.TestCase):
    def test_rejects_speed_outside_supported_range(self) -> None:
        with self.assertRaises(TTSConfigurationError):
            ElevenLabsTTS(api_key="test", voice_id="voice", speed=1.21)

    def test_passes_configured_speed_as_voice_setting(self) -> None:
        provider = ElevenLabsTTS(api_key="test", voice_id="voice", speed=1.1)
        client = _FakeClient()
        provider._client = client  # type: ignore[assignment]

        asyncio.run(provider.synthesize("Hola", "es"))

        voice_settings = client.text_to_speech.arguments["voice_settings"]
        self.assertEqual(voice_settings.speed, 1.1)
        self.assertEqual(
            client.text_to_speech.arguments["output_format"],
            "pcm_16000",
        )

    def test_uses_configured_model_fallback_for_catalan(self) -> None:
        provider = ElevenLabsTTS(
            api_key="test",
            voice_id="voice",
            model="eleven_flash_v2_5",
            language_models={"ca": "eleven_v3"},
        )
        client = _FakeClient()
        provider._client = client  # type: ignore[assignment]

        asyncio.run(provider.synthesize("Bon dia", "ca"))

        self.assertEqual(
            "eleven_v3",
            client.text_to_speech.arguments["model_id"],
        )

    def test_uses_configured_voice_for_target_language(self) -> None:
        provider = ElevenLabsTTS(
            api_key="test",
            voice_id="spanish-voice",
            language_voices={
                "en": {"male": "english-male", "female": "spanish-female"},
                "fr": {"male": "french-male", "female": "spanish-female"},
                "ca": {"male": "spanish-male", "female": "spanish-female"},
            },
        )
        client = _FakeClient()
        provider._client = client  # type: ignore[assignment]

        asyncio.run(provider.synthesize("Good morning", "en", "male"))

        self.assertEqual(
            "english-male",
            client.text_to_speech.arguments["voice_id"],
        )

    def test_uses_same_female_voice_for_each_configured_language(self) -> None:
        provider = ElevenLabsTTS(
            api_key="test",
            voice_id="default-voice",
            language_voices={
                language: {"male": f"{language}-male", "female": "clara"}
                for language in ("ca", "en", "es", "fr")
            },
        )
        client = _FakeClient()
        provider._client = client  # type: ignore[assignment]

        asyncio.run(provider.synthesize("Bonjour", "fr", "female"))

        self.assertEqual("clara", client.text_to_speech.arguments["voice_id"])

    def test_uses_default_voice_without_language_override(self) -> None:
        provider = ElevenLabsTTS(
            api_key="test",
            voice_id="default-voice",
            language_voices={"en": {"male": "english-voice"}},
        )
        client = _FakeClient()
        provider._client = client  # type: ignore[assignment]

        asyncio.run(provider.synthesize("Hola", "es"))

        self.assertEqual(
            "default-voice",
            client.text_to_speech.arguments["voice_id"],
        )

    def test_rejects_invalid_voice_gender(self) -> None:
        provider = ElevenLabsTTS(api_key="test", voice_id="voice")

        with self.assertRaisesRegex(TTSConfigurationError, "voice gender"):
            asyncio.run(provider.synthesize("Hola", "es", "other"))

    def test_stream_yields_audio_before_final_timing_marker(self) -> None:
        provider = ElevenLabsTTS(api_key="test", voice_id="voice")
        client = _FakeClient()
        provider._client = client  # type: ignore[assignment]

        async def collect():
            return [chunk async for chunk in provider.synthesize_stream("Hello", "en")]

        chunks = asyncio.run(collect())

        self.assertEqual(b"\x00\x00", chunks[0].audio)
        self.assertFalse(chunks[0].final)
        self.assertIsNotNone(chunks[0].first_byte_latency_seconds)
        self.assertTrue(chunks[-1].final)
        self.assertIsNotNone(chunks[-1].total_latency_seconds)


if __name__ == "__main__":
    unittest.main()
