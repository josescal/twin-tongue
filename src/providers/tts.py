"""Provider-independent text-to-speech contracts."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from providers.errors import ProviderError


class TTSError(ProviderError):
    """Base error for speech synthesis."""


class TTSConfigurationError(TTSError):
    """Raised for invalid local TTS configuration."""


class TTSAuthenticationError(TTSError):
    """Raised when a TTS provider rejects its credential."""


class TTSNetworkError(TTSError):
    """Raised when a TTS provider cannot be reached."""


class TTSServiceError(TTSError):
    """Raised when a TTS provider rejects synthesis."""


@dataclass(frozen=True)
class TTSResult:
    """One synthesized raw PCM segment and timing information."""

    audio: bytes
    sample_rate: int
    channels: int
    sample_width_bytes: int
    first_byte_latency_seconds: float
    total_latency_seconds: float


@dataclass(frozen=True)
class TTSChunk:
    """One incremental raw PCM chunk from a streaming synthesis request."""

    audio: bytes
    sample_rate: int
    channels: int
    sample_width_bytes: int
    first_byte_latency_seconds: float | None = None
    total_latency_seconds: float | None = None
    final: bool = False


class TextToSpeech(Protocol):
    """Contract consumed by translation pipelines, independent of TTS vendor."""

    def connect(self) -> None:
        """Prepare provider resources."""
        ...

    async def synthesize(
        self, text: str, language: str, voice_gender: str = "male"
    ) -> TTSResult:
        """Synthesize one text segment as raw PCM."""
        ...

    def synthesize_stream(
        self, text: str, language: str, voice_gender: str = "male"
    ) -> AsyncIterator[TTSChunk]:
        """Yield raw PCM incrementally as the provider generates it."""
        ...

    async def close(self) -> None:
        """Release provider resources."""
        ...
