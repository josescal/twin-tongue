"""ElevenLabs realtime Speech-to-Text provider."""

import asyncio
import base64
from collections.abc import Callable
import logging
import time
from typing import Any

from elevenlabs import (
    AudioFormat,
    CommitStrategy,
    ElevenLabs,
    RealtimeAudioOptions,
    RealtimeConnection,
    RealtimeEvents,
)

from providers.stt import (
    RealtimeSTTStatistics,
    RealtimeTranscript,
    STTAuthenticationError,
    STTConfigurationError,
    STTError,
    STTNetworkError,
    STTServiceError,
)

logger = logging.getLogger(__name__)


class ElevenLabsRealtimeSTT:
    """Send prepared mono PCM blocks through the official ElevenLabs SDK."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.elevenlabs.io",
        model: str = "scribe_v2_realtime",
        audio_format: str = "pcm_16000",
        sample_rate: int = 16_000,
        language: str = "en",
        include_timestamps: bool = True,
        reconnect_on_close: bool = False,
        on_partial: Callable[[str], None] | None = None,
        on_final: Callable[[RealtimeTranscript], None] | None = None,
    ) -> None:
        if not api_key.strip():
            raise STTConfigurationError("ELEVENLABS_API_KEY is missing or empty.")
        if sample_rate <= 0:
            raise STTConfigurationError("STT sample rate must be greater than zero.")
        try:
            self.audio_format = AudioFormat(audio_format)
        except ValueError as error:
            raise STTConfigurationError(f"Unsupported ElevenLabs audio format: {audio_format}") from error
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.sample_rate = sample_rate
        self.language = language
        self.include_timestamps = include_timestamps
        self.reconnect_on_close = reconnect_on_close
        self.on_partial = on_partial
        self.on_final = on_final
        self.statistics = RealtimeSTTStatistics()
        self.partial_transcripts: list[str] = []
        self.final_transcripts: list[RealtimeTranscript] = []
        self.last_error: STTError | None = None
        self.error_event = asyncio.Event()
        self.closed_event = asyncio.Event()
        self.client: ElevenLabs | None = None
        self.connection: RealtimeConnection | None = None
        self._closing = False
        self._pending_finals: dict[str, asyncio.Task[None]] = {}
        self._recent_finals: dict[str, float] = {}
        self._uncommitted_audio_seconds = 0.0
        self._successful_connections = 0

    async def connect(self) -> None:
        """Create the SDK client and open a realtime manual-audio connection."""
        if self.connection is not None:
            raise STTConfigurationError("ElevenLabs realtime STT is already connected.")
        self._closing = False
        self.closed_event.clear()
        options: RealtimeAudioOptions = {
            "model_id": self.model,
            "audio_format": self.audio_format,
            "sample_rate": self.sample_rate,
            "commit_strategy": CommitStrategy.MANUAL,
            "language_code": self.language,
            "include_timestamps": self.include_timestamps,
        }
        logger.info(
            "event=provider_connection_started provider=elevenlabs_stt model=%s "
            "audio_format=%s language=%s",
            self.model,
            self.audio_format.value,
            self.language,
        )
        try:
            self.client = ElevenLabs(api_key=self.api_key, base_url=self.base_url)
            self.connection = await self.client.speech_to_text.realtime.connect(options)
            self._register_event_handlers()
            if self._successful_connections:
                self.statistics.reconnections += 1
                logger.info("event=provider_reconnected provider=elevenlabs_stt")
            self._successful_connections += 1
        except Exception as error:
            self.statistics.connection_errors += 1
            raise self._classify_exception(error) from error

    async def send_audio(self, mono_pcm: bytes) -> None:
        """Base64-encode and send one mono PCM block using the SDK public API."""
        await self._send_audio(mono_pcm, count_as_uncommitted=True)

    async def send_keepalive(self, pcm_bytes: int) -> None:
        """Send one silent PCM block to keep an idle realtime session warm."""
        if pcm_bytes <= 0:
            return
        await self._send_audio(bytes(pcm_bytes), count_as_uncommitted=False)
        self.statistics.keepalive_blocks += 1

    async def _send_audio(self, mono_pcm: bytes, *, count_as_uncommitted: bool) -> None:
        if self.connection is None:
            raise STTConfigurationError("ElevenLabs realtime STT is not connected.")
        if not mono_pcm:
            return
        encoded_audio = base64.b64encode(mono_pcm).decode("ascii")
        try:
            await self.connection.send({"audio_base_64": encoded_audio})
        except Exception as error:
            self.statistics.connection_errors += 1
            classified = self._classify_exception(error)
            self.last_error = classified
            self.error_event.set()
            raise classified from error
        self.statistics.sent_blocks += 1
        self.statistics.sent_mono_pcm_bytes += len(mono_pcm)
        if count_as_uncommitted:
            self._uncommitted_audio_seconds += len(mono_pcm) / (self.sample_rate * 2)

    async def commit_final(self) -> None:
        """Request a final commit through the SDK public connection method."""
        if self.connection is None or self.statistics.sent_blocks == 0:
            return
        if self._uncommitted_audio_seconds < 0.3:
            return
        committed_audio_seconds = self._uncommitted_audio_seconds
        try:
            await self.connection.commit()
        except Exception as error:
            self.statistics.connection_errors += 1
            raise self._classify_exception(error) from error
        self._uncommitted_audio_seconds = max(
            0,
            self._uncommitted_audio_seconds - committed_audio_seconds,
        )

    async def close(self) -> None:
        """Flush pending handlers and close the realtime connection cleanly."""
        self._closing = True
        await self._flush_pending_finals()
        connection = self.connection
        self.connection = None
        if connection is None:
            return
        try:
            await connection.close()
        except Exception as error:
            self.statistics.connection_errors += 1
            raise self._classify_exception(error) from error
        finally:
            self.closed_event.set()

    def _register_event_handlers(self) -> None:
        assert self.connection is not None
        self.connection.on(RealtimeEvents.SESSION_STARTED, self._handle_session_started)
        self.connection.on(RealtimeEvents.PARTIAL_TRANSCRIPT, self._handle_partial_transcript)
        self.connection.on(RealtimeEvents.COMMITTED_TRANSCRIPT, self._handle_committed_transcript)
        self.connection.on(
            RealtimeEvents.COMMITTED_TRANSCRIPT_WITH_TIMESTAMPS,
            self._handle_committed_transcript_with_timestamps,
        )
        self.connection.on(RealtimeEvents.ERROR, self._handle_error)
        self.connection.on(RealtimeEvents.CLOSE, self._handle_close)

    def _handle_session_started(self, data: dict[str, Any]) -> None:
        session_id = data.get("session_id")
        if session_id:
            logger.info(
                "event=provider_connected provider=elevenlabs_stt session_id=%s",
                session_id,
            )
        else:
            logger.info("event=provider_connected provider=elevenlabs_stt")

    def _handle_partial_transcript(self, data: dict[str, Any]) -> None:
        text = str(data.get("text", "")).strip()
        if not text:
            return
        self.statistics.partial_transcripts += 1
        self.partial_transcripts.append(text)
        if self.on_partial is not None:
            self.on_partial(text)

    def _handle_committed_transcript(self, data: dict[str, Any]) -> None:
        text = str(data.get("text", "")).strip()
        if not text:
            return
        if not self.include_timestamps:
            self._emit_final(RealtimeTranscript(text=text))
            return
        key = _transcript_key(text)
        previous = self._pending_finals.pop(key, None)
        if previous is not None:
            previous.cancel()
        self._pending_finals[key] = asyncio.create_task(
            self._emit_final_after_timestamp_grace(text, key)
        )

    def _handle_committed_transcript_with_timestamps(self, data: dict[str, Any]) -> None:
        text = str(data.get("text", "")).strip()
        if not text:
            return
        key = _transcript_key(text)
        pending = self._pending_finals.pop(key, None)
        if pending is not None:
            pending.cancel()
        if self._was_recently_emitted(text):
            return
        start_seconds, end_seconds = _timestamp_range(data.get("words"))
        self._emit_final(
            RealtimeTranscript(
                text=text,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
            )
        )

    def _handle_error(self, data: dict[str, Any]) -> None:
        message_type = str(data.get("message_type", "error"))
        detail = str(data.get("error") or data.get("message") or "ElevenLabs realtime STT error")
        safe_detail = self._redact(detail)
        if self._closing and _is_normal_close_notification(safe_detail):
            return
        if message_type == "commit_throttled":
            return
        if message_type == "insufficient_audio_activity" and self.reconnect_on_close:
            return
        if message_type == RealtimeEvents.AUTH_ERROR.value:
            error: STTError = STTAuthenticationError(
                f"ElevenLabs authentication failed: {safe_detail}"
            )
        else:
            error = STTServiceError(f"ElevenLabs error ({message_type}): {safe_detail}")
        self.statistics.connection_errors += 1
        self.last_error = error
        self.error_event.set()
        logger.error(
            "event=provider_failed provider=elevenlabs_stt operation=realtime_session error=%s",
            error,
        )

    def _handle_close(self) -> None:
        self.closed_event.set()
        if self._closing:
            return
        if self.reconnect_on_close:
            self.connection = None
            self._uncommitted_audio_seconds = 0.0
            self.statistics.idle_disconnects += 1
            logger.info(
                "event=provider_disconnected provider=elevenlabs_stt reason=idle "
                "consequence=local_capture_active"
            )
            return
        error = STTNetworkError("ElevenLabs realtime connection closed unexpectedly.")
        self.statistics.connection_errors += 1
        self.last_error = error
        self.error_event.set()
        logger.error(
            "event=provider_failed provider=elevenlabs_stt operation=realtime_session "
            "consequence=pipeline_unavailable error=%s",
            error,
        )

    async def _emit_final_after_timestamp_grace(self, text: str, key: str) -> None:
        try:
            await asyncio.sleep(0.25)
            self._emit_final(RealtimeTranscript(text=text))
        finally:
            current_task = asyncio.current_task()
            if self._pending_finals.get(key) is current_task:
                self._pending_finals.pop(key, None)

    def _emit_final(self, transcript: RealtimeTranscript) -> None:
        if self._was_recently_emitted(transcript.text):
            return
        self._recent_finals[_transcript_key(transcript.text)] = time.monotonic()
        self.statistics.final_transcripts += 1
        self.final_transcripts.append(transcript)
        if self.on_final is not None:
            self.on_final(transcript)

    def _was_recently_emitted(self, text: str) -> bool:
        emitted_at = self._recent_finals.get(_transcript_key(text))
        return emitted_at is not None and time.monotonic() - emitted_at < 2

    async def _flush_pending_finals(self) -> None:
        pending = list(self._pending_finals.values())
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)
        self._pending_finals.clear()

    def _classify_exception(self, error: Exception) -> STTError:
        detail = self._redact(str(error)) or error.__class__.__name__
        lowered = detail.casefold()
        if "auth" in lowered or "api key" in lowered or "401" in lowered or "403" in lowered:
            return STTAuthenticationError(f"ElevenLabs authentication failed: {detail}")
        if any(token in lowered for token in ("dns", "connect", "network", "websocket", "url", "timeout")):
            return STTNetworkError(f"Could not connect to ElevenLabs: {detail}")
        return STTServiceError(f"ElevenLabs request failed: {detail}")

    def _redact(self, text: str) -> str:
        return text.replace(self.api_key, "[REDACTED]") if self.api_key else text


def _timestamp_range(words: object) -> tuple[float | None, float | None]:
    if not isinstance(words, list):
        return None, None
    starts = [word.get("start") for word in words if isinstance(word, dict)]
    ends = [word.get("end") for word in words if isinstance(word, dict)]
    numeric_starts = [float(value) for value in starts if isinstance(value, (int, float))]
    numeric_ends = [float(value) for value in ends if isinstance(value, (int, float))]
    return (
        min(numeric_starts) if numeric_starts else None,
        max(numeric_ends) if numeric_ends else None,
    )


def _is_normal_close_notification(detail: str) -> bool:
    lowered = detail.casefold()
    return "1000" in lowered and (
        "user ended conversation" in lowered
        or "normal closure" in lowered
        or "ok" in lowered
    )


def _transcript_key(text: str) -> str:
    return " ".join(text.split()).casefold()
