"""Provider-independent realtime speech-to-text contracts."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from providers.errors import ProviderError


class STTError(ProviderError):
    """Base error for realtime speech recognition."""


class STTConfigurationError(STTError):
    """Raised for invalid local STT configuration."""


class STTAuthenticationError(STTError):
    """Raised when an STT provider rejects its credential."""


class STTNetworkError(STTError):
    """Raised when an STT provider cannot be reached or disconnects."""


class STTServiceError(STTError):
    """Raised when an STT provider rejects a request."""


@dataclass(frozen=True)
class RealtimeTranscriptWord:
    """One timestamped realtime word with optional model confidence."""

    text: str
    start_seconds: float | None = None
    end_seconds: float | None = None
    logprob: float | None = None


@dataclass(frozen=True)
class RealtimeTranscript:
    """A committed transcript with optional segment timestamps."""

    text: str
    start_seconds: float | None = None
    end_seconds: float | None = None
    words: tuple[RealtimeTranscriptWord, ...] = ()

    @property
    def average_logprob(self) -> float | None:
        values = [word.logprob for word in self.words if word.logprob is not None]
        return sum(values) / len(values) if values else None

    @property
    def minimum_logprob(self) -> float | None:
        values = [word.logprob for word in self.words if word.logprob is not None]
        return min(values) if values else None


@dataclass
class RealtimeSTTStatistics:
    """Aggregate realtime STT counters."""

    sent_blocks: int = 0
    sent_mono_pcm_bytes: int = 0
    partial_transcripts: int = 0
    final_transcripts: int = 0
    connection_errors: int = 0
    idle_disconnects: int = 0
    reconnections: int = 0
    keepalive_blocks: int = 0


class SpeechToText(Protocol):
    """Contract consumed by the pipeline, independent of STT vendor."""

    statistics: RealtimeSTTStatistics
    last_error: STTError | None
    on_partial: Callable[[str], None] | None
    on_final: Callable[[RealtimeTranscript], None] | None
    error_event: asyncio.Event
    sample_rate: int
    language: str
    reconnect_on_close: bool

    @property
    def connection(self) -> object | None:
        """Return the active provider connection, if any."""
        ...

    async def connect(self) -> None:
        """Open a realtime recognition session."""
        ...

    async def send_audio(self, mono_pcm: bytes) -> None:
        """Send one raw mono PCM block for recognition."""
        ...

    async def send_keepalive(self, pcm_bytes: int) -> None:
        """Keep an idle realtime session open when supported."""
        ...

    async def commit_final(self) -> None:
        """Request a final transcript for buffered audio."""
        ...

    async def close(self) -> None:
        """Close the realtime recognition session."""
        ...
