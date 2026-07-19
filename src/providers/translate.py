"""Provider-independent text translation contracts and result types."""

from dataclasses import dataclass
from typing import Protocol

from providers.errors import ProviderError


class TranslationError(ProviderError):
    """Base error for text translation."""


class TranslationConfigurationError(TranslationError):
    """Raised for missing or invalid local translation settings."""


class TranslationAuthenticationError(TranslationError):
    """Raised when Google Cloud credentials are unavailable or rejected."""


class TranslationQuotaError(TranslationError):
    """Raised when Google Cloud quota or rate limits are exceeded."""


class TranslationNetworkError(TranslationError):
    """Raised for transient connectivity failures."""


class TranslationServiceError(TranslationError):
    """Raised when a translation service rejects a request."""


@dataclass(frozen=True)
class TranslationResult:
    """One translated text and its measured request latency."""

    source_text: str
    translated_text: str
    source_language: str
    target_language: str
    latency_seconds: float


@dataclass
class TranslationStatistics:
    """Aggregate translation request metrics."""

    requests: int = 0
    successful_translations: int = 0
    failed_translations: int = 0
    source_characters: int = 0
    translated_characters: int = 0
    total_latency_seconds: float = 0

    @property
    def average_latency_seconds(self) -> float:
        """Return average latency across successful translations."""
        if self.successful_translations == 0:
            return 0
        return self.total_latency_seconds / self.successful_translations


class Translator(Protocol):
    """Contract consumed by the pipeline, independent of vendor and transport."""

    @property
    def statistics(self) -> TranslationStatistics:
        """Return aggregate provider statistics."""
        ...

    def connect(self) -> None:
        """Prepare provider resources without translating text."""
        ...

    async def translate(
        self,
        text: str,
        source_language: str,
        target_language: str,
    ) -> TranslationResult:
        """Translate one text segment."""
        ...

    async def close(self) -> None:
        """Release provider resources."""
        ...
