"""ElevenLabs streaming text-to-speech provider."""

from collections.abc import AsyncIterator
import logging
import time

from elevenlabs import VoiceSettings
from elevenlabs.client import AsyncElevenLabs
import httpx

from providers.tts import (
    TTSAuthenticationError,
    TTSChunk,
    TTSConfigurationError,
    TTSNetworkError,
    TTSResult,
    TTSServiceError,
)
from providers.retry import ProviderRetryPolicy

logger = logging.getLogger(__name__)


class ElevenLabsTTS:
    """Synthesize raw mono PCM using the official asynchronous SDK."""

    def __init__(
        self,
        api_key: str,
        voice_id: str,
        model: str = "eleven_flash_v2_5",
        output_format: str = "pcm_16000",
        sample_rate: int = 16_000,
        speed: float = 1.0,
        base_url: str = "https://api.elevenlabs.io",
        timeout_seconds: float = 30,
        language_voices: dict[str, dict[str, str]] | None = None,
        language_models: dict[str, str] | None = None,
        retry_policy: ProviderRetryPolicy | None = None,
    ) -> None:
        if not api_key.strip():
            raise TTSConfigurationError("ELEVENLABS_API_KEY is missing or empty.")
        if not voice_id.strip():
            raise TTSConfigurationError("ElevenLabs TTS voice ID is missing or empty.")
        if output_format != f"pcm_{sample_rate}":
            raise TTSConfigurationError(
                "TTS output_format must be raw PCM matching the configured sample_rate."
            )
        if timeout_seconds <= 0:
            raise TTSConfigurationError("TTS timeout must be greater than zero.")
        if not 0.7 <= speed <= 1.2:
            raise TTSConfigurationError("ElevenLabs TTS speed must be between 0.7 and 1.2.")
        self._api_key = api_key.strip()
        self.voice_id = voice_id.strip()
        self.model = model
        self.output_format = output_format
        self.sample_rate = sample_rate
        self.speed = speed
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.language_voices = dict(language_voices or {})
        self.language_models = dict(language_models or {})
        self.retry_policy = retry_policy or ProviderRetryPolicy()
        self._http_client: httpx.AsyncClient | None = None
        self._client: AsyncElevenLabs | None = None

    def connect(self) -> None:
        """Create reusable asynchronous SDK resources."""
        if self._client is not None:
            return
        self._http_client = httpx.AsyncClient(timeout=self.timeout_seconds)
        self._client = AsyncElevenLabs(
            api_key=self._api_key,
            base_url=self.base_url,
            httpx_client=self._http_client,
        )
        logger.info(
            "event=provider_ready provider=elevenlabs_tts model=%s audio_format=%s speed=%.2f",
            self.model,
            self.output_format,
            self.speed,
        )

    async def synthesize(
        self, text: str, language: str, voice_gender: str = "male"
    ) -> TTSResult:
        """Collect a streaming response for callers that need a complete segment."""
        chunks: list[bytes] = []
        first_byte_latency = 0.0
        total_latency = 0.0
        async for chunk in self.synthesize_stream(text, language, voice_gender):
            if chunk.audio:
                chunks.append(chunk.audio)
            if chunk.first_byte_latency_seconds is not None:
                first_byte_latency = chunk.first_byte_latency_seconds
            if chunk.total_latency_seconds is not None:
                total_latency = chunk.total_latency_seconds
        return TTSResult(
            audio=b"".join(chunks),
            sample_rate=self.sample_rate,
            channels=1,
            sample_width_bytes=2,
            first_byte_latency_seconds=first_byte_latency,
            total_latency_seconds=total_latency,
        )

    async def synthesize_stream(
        self,
        text: str,
        language: str,
        voice_gender: str = "male",
    ) -> AsyncIterator[TTSChunk]:
        """Yield raw PCM from ElevenLabs' streaming endpoint as it arrives."""
        source_text = text.strip()
        language = language.strip()
        if not source_text:
            raise TTSConfigurationError("Cannot synthesize empty text.")
        if not language:
            raise TTSConfigurationError("TTS language is missing.")
        if voice_gender not in {"male", "female"}:
            raise TTSConfigurationError("TTS voice gender must be 'male' or 'female'.")
        if self._client is None:
            self.connect()
        assert self._client is not None

        started_at = time.monotonic()
        selected_voice_id = self.language_voices.get(language, {}).get(
            voice_gender, self.voice_id
        )
        selected_model = self.language_models.get(language, self.model)
        for attempt in range(1, self.retry_policy.max_attempts + 1):
            first_byte_latency: float | None = None
            received_audio = False
            try:
                async for chunk in self._client.text_to_speech.stream(
                    voice_id=selected_voice_id,
                    text=source_text,
                    model_id=selected_model,
                    output_format=self.output_format,
                    language_code=language,
                    voice_settings=VoiceSettings(speed=self.speed),
                ):
                    if not chunk:
                        continue
                    chunk_first_byte_latency: float | None = None
                    if first_byte_latency is None:
                        first_byte_latency = time.monotonic() - started_at
                        chunk_first_byte_latency = first_byte_latency
                    received_audio = True
                    yield TTSChunk(
                        audio=chunk,
                        sample_rate=self.sample_rate,
                        channels=1,
                        sample_width_bytes=2,
                        first_byte_latency_seconds=chunk_first_byte_latency,
                    )
            except httpx.HTTPStatusError as error:
                status_code = error.response.status_code
                if status_code in {401, 403}:
                    raise TTSAuthenticationError(
                        "ElevenLabs rejected the API key or voice permissions."
                    ) from error
                classified: Exception = (
                    TTSNetworkError(f"ElevenLabs TTS is temporarily unavailable (HTTP {status_code}).")
                    if status_code == 408 or status_code >= 500
                    else TTSServiceError(f"ElevenLabs rejected TTS synthesis (HTTP {status_code}).")
                )
            except (httpx.TimeoutException, httpx.TransportError) as error:
                classified = TTSNetworkError("ElevenLabs TTS could not be reached.")
            except Exception as error:
                status_code = getattr(error, "status_code", None)
                if status_code in {401, 403}:
                    raise TTSAuthenticationError(
                        "ElevenLabs rejected the API key or voice permissions."
                    ) from error
                classified = TTSServiceError(
                    f"ElevenLabs TTS synthesis failed: {error.__class__.__name__}."
                )
            else:
                if not received_audio:
                    raise TTSServiceError("ElevenLabs returned no TTS audio.")
                yield TTSChunk(
                    audio=b"",
                    sample_rate=self.sample_rate,
                    channels=1,
                    sample_width_bytes=2,
                    first_byte_latency_seconds=first_byte_latency,
                    total_latency_seconds=time.monotonic() - started_at,
                    final=True,
                )
                return
            if received_audio or not isinstance(classified, TTSNetworkError) or attempt == self.retry_policy.max_attempts:
                raise classified
            delay = self.retry_policy.delay_seconds(attempt)
            logger.warning(
                "event=provider_request_retry provider=elevenlabs_tts attempt=%s "
                "next_attempt=%s delay_seconds=%.2f error=%s",
                attempt,
                attempt + 1,
                delay,
                classified,
            )
            await asyncio.sleep(delay)

    async def close(self) -> None:
        """Close the owned HTTP transport."""
        self._client = None
        client = self._http_client
        self._http_client = None
        if client is not None:
            await client.aclose()
