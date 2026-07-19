"""Google Cloud Translation Basic v2 REST provider."""

from html import unescape
import logging
import time
from urllib.parse import urlparse

import httpx

from providers.translate import (
    TranslationAuthenticationError,
    TranslationConfigurationError,
    TranslationError,
    TranslationNetworkError,
    TranslationQuotaError,
    TranslationResult,
    TranslationServiceError,
    TranslationStatistics,
)

logger = logging.getLogger(__name__)


class GoogleTranslateBasicV2:
    """Translate text through Basic v2 using an API key in an HTTP header."""

    def __init__(
        self,
        api_key: str,
        endpoint: str = "https://translation.googleapis.com/language/translate/v2",
        text_format: str = "text",
        timeout_seconds: float = 15,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise TranslationConfigurationError("GOOGLE_TRANSLATE_API_KEY is missing or empty.")
        parsed_endpoint = urlparse(endpoint)
        if parsed_endpoint.scheme != "https" or not parsed_endpoint.netloc:
            raise TranslationConfigurationError("Google Translation endpoint must be a valid HTTPS URL.")
        if text_format not in {"text", "html"}:
            raise TranslationConfigurationError("Translation format must be 'text' or 'html'.")
        if timeout_seconds <= 0:
            raise TranslationConfigurationError("Translation timeout must be greater than zero.")
        self._api_key = api_key.strip()
        self.endpoint = endpoint
        self.text_format = text_format
        self.timeout_seconds = timeout_seconds
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._statistics = TranslationStatistics()

    @property
    def statistics(self) -> TranslationStatistics:
        return self._statistics

    def connect(self) -> None:
        """Create a reusable async HTTP client without exposing its credentials."""
        if self._client is not None:
            return
        self._client = httpx.AsyncClient(
            headers={
                "x-goog-api-key": self._api_key,
                "content-type": "application/json; charset=utf-8",
            },
            timeout=self.timeout_seconds,
            transport=self._transport,
        )
        logger.info("event=provider_ready provider=google_translate_v2")

    async def translate(
        self,
        text: str,
        source_language: str,
        target_language: str,
    ) -> TranslationResult:
        """Translate one non-empty text segment through the Basic v2 REST API."""
        source_text = text.strip()
        source_language = source_language.strip()
        target_language = target_language.strip()
        if not source_text:
            raise TranslationConfigurationError("Cannot translate empty text.")
        if not source_language or not target_language:
            raise TranslationConfigurationError("Source and target languages are required.")
        if self._client is None:
            self.connect()
        assert self._client is not None

        self._statistics.requests += 1
        self._statistics.source_characters += len(source_text)
        started_at = time.monotonic()
        try:
            response = await self._client.post(
                self.endpoint,
                json={
                    "q": [source_text],
                    "source": source_language,
                    "target": target_language,
                    "format": self.text_format,
                },
            )
        except (httpx.TimeoutException, httpx.TransportError) as error:
            self._statistics.failed_translations += 1
            raise TranslationNetworkError(
                "Google Cloud Translation Basic v2 could not be reached."
            ) from error

        if response.status_code >= 400:
            self._statistics.failed_translations += 1
            raise self._http_error(response.status_code)

        try:
            payload = response.json()
            translations = payload["data"]["translations"]
            translated_text = str(translations[0]["translatedText"]).strip()
        except (ValueError, KeyError, IndexError, TypeError) as error:
            self._statistics.failed_translations += 1
            raise TranslationServiceError(
                "Google Cloud Translation Basic v2 returned an invalid response."
            ) from error
        if not translated_text:
            self._statistics.failed_translations += 1
            raise TranslationServiceError(
                "Google Cloud Translation Basic v2 returned an empty translation."
            )

        latency_seconds = time.monotonic() - started_at
        if self.text_format == "text":
            translated_text = unescape(translated_text)
        self._statistics.successful_translations += 1
        self._statistics.translated_characters += len(translated_text)
        self._statistics.total_latency_seconds += latency_seconds
        return TranslationResult(
            source_text=source_text,
            translated_text=translated_text,
            source_language=source_language,
            target_language=target_language,
            latency_seconds=latency_seconds,
        )

    async def close(self) -> None:
        """Close the reusable HTTP client."""
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    @staticmethod
    def _http_error(status_code: int) -> TranslationError:
        if status_code in {401, 403}:
            return TranslationAuthenticationError(
                "Google Cloud Translation rejected the API key or its API restrictions."
            )
        if status_code == 429:
            return TranslationQuotaError(
                "Google Cloud Translation quota or rate limit was exceeded."
            )
        if status_code >= 500:
            return TranslationNetworkError(
                f"Google Cloud Translation is temporarily unavailable (HTTP {status_code})."
            )
        return TranslationServiceError(
            f"Google Cloud Translation rejected the request (HTTP {status_code})."
        )
