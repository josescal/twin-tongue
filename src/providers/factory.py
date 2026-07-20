"""Construction of configured provider clients for translation pipelines."""

from collections.abc import Callable
from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

from providers.elevenlabs_stt import ElevenLabsRealtimeSTT
from providers.elevenlabs_tts import ElevenLabsTTS
from providers.errors import ProviderConfigurationError
from providers.google_translate import GoogleTranslateBasicV2
from providers.retry import ProviderRetryPolicy
from providers.stt import RealtimeTranscript, SpeechToText
from providers.translate import Translator
from providers.tts import TextToSpeech


DEFAULT_ELEVENLABS_BASE_URL = "https://api.elevenlabs.io"
SUPPORTED_PROVIDERS = {
    "stt": "elevenlabs",
    "translation": "google_translate_basic_v2",
    "tts": "elevenlabs",
}


@dataclass(frozen=True)
class ProviderCredentials:
    """Secrets and service locations needed by configured providers."""

    elevenlabs_api_key: str
    google_translate_api_key: str
    elevenlabs_base_url: str = DEFAULT_ELEVENLABS_BASE_URL


class ProviderFactory:
    """Create independent provider clients without exposing vendors to pipelines."""

    def __init__(
        self,
        config: dict[str, object],
        credentials: ProviderCredentials,
    ) -> None:
        self._config = config
        self._credentials = credentials
        self._validate_selection()
        self._validate_credentials()

    @classmethod
    def from_environment(
        cls,
        config: dict[str, object],
        env_path: Path,
    ) -> "ProviderFactory":
        """Load provider secrets from an environment file and process environment."""
        load_dotenv(env_path, override=False)
        return cls(
            config,
            ProviderCredentials(
                elevenlabs_api_key=os.getenv("ELEVENLABS_API_KEY", "").strip(),
                google_translate_api_key=os.getenv(
                    "GOOGLE_TRANSLATE_API_KEY", ""
                ).strip(),
                elevenlabs_base_url=os.getenv(
                    "ELEVENLABS_BASE_URL", DEFAULT_ELEVENLABS_BASE_URL
                ).strip(),
            ),
        )

    def create_translator(self) -> Translator:
        """Create the configured text translation provider."""
        translation = self._section("translation")
        return GoogleTranslateBasicV2(
            api_key=self._credentials.google_translate_api_key,
            endpoint=str(translation["endpoint"]),
            text_format=str(translation["format"]),
            retry_policy=self._retry_policy(),
        )

    def create_tts(self) -> TextToSpeech:
        """Create the configured text-to-speech provider."""
        tts = self._section("tts")
        return ElevenLabsTTS(
            api_key=self._credentials.elevenlabs_api_key,
            voice_id=str(tts["voice_id"]),
            model=str(tts["model"]),
            output_format=str(tts["output_format"]),
            sample_rate=int(tts["sample_rate"]),
            speed=float(tts["speed"]),
            base_url=self._credentials.elevenlabs_base_url,
            language_voices={
                str(language): {
                    str(gender): str(voice_id)
                    for gender, voice_id in dict(voices).items()
                }
                for language, voices in dict(
                    tts.get("language_voices", {})
                ).items()
            },
            language_models={
                str(language): str(model)
                for language, model in dict(
                    tts.get("language_models", {})
                ).items()
            },
            retry_policy=self._retry_policy(),
        )

    def create_stt(
        self,
        *,
        language: str,
        on_partial: Callable[[str], None] | None = None,
        on_final: Callable[[RealtimeTranscript], None] | None = None,
    ) -> SpeechToText:
        """Create the configured realtime speech-to-text provider."""
        stt = self._section("stt")
        return ElevenLabsRealtimeSTT(
            api_key=self._credentials.elevenlabs_api_key,
            base_url=self._credentials.elevenlabs_base_url,
            model=str(stt["model"]),
            audio_format=str(stt["audio_format"]),
            sample_rate=int(stt["sample_rate"]),
            language=language,
            include_timestamps=bool(stt["include_timestamps"]),
            no_verbatim=bool(stt["no_verbatim"]),
            keyterms=[str(term) for term in list(stt["keyterms"])],
            send_chunk_duration_ms=int(stt["send_chunk_duration_ms"]),
            reconnect_on_close=True,
            retry_policy=self._retry_policy(),
            on_partial=on_partial,
            on_final=on_final,
        )

    def _section(self, name: str) -> dict[str, object]:
        section = self._config.get(name)
        if not isinstance(section, dict):
            raise ProviderConfigurationError(
                f"Provider configuration section '{name}' must be a mapping."
            )
        return section

    def _retry_policy(self) -> ProviderRetryPolicy:
        providers = self._section("providers")
        retry = providers.get("retry", {})
        if not isinstance(retry, dict):
            raise ProviderConfigurationError("Provider retry configuration must be a mapping.")
        try:
            return ProviderRetryPolicy(
                max_attempts=int(retry.get("max_attempts", 2)),
                base_delay_seconds=float(retry.get("base_delay_seconds", 0.75)),
            )
        except ValueError as error:
            raise ProviderConfigurationError(str(error)) from error

    def _validate_selection(self) -> None:
        providers = self._section("providers")
        selected = {name: providers.get(name) for name in SUPPORTED_PROVIDERS}
        if selected != SUPPORTED_PROVIDERS:
            expected = ", ".join(
                f"{name}={provider}" for name, provider in SUPPORTED_PROVIDERS.items()
            )
            raise ProviderConfigurationError(
                f"Unsupported provider selection; expected: {expected}."
            )

    def _validate_credentials(self) -> None:
        if not self._credentials.elevenlabs_api_key:
            raise ProviderConfigurationError("ELEVENLABS_API_KEY is missing from .env.")
        if not self._credentials.google_translate_api_key:
            raise ProviderConfigurationError(
                "GOOGLE_TRANSLATE_API_KEY is missing from .env."
            )
        parsed_url = urlparse(self._credentials.elevenlabs_base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ProviderConfigurationError(
                "ELEVENLABS_BASE_URL must be a valid HTTP or HTTPS URL."
            )
