"""OpenAI dedicated Realtime translation WebSocket session."""

import asyncio
import base64
from collections.abc import Awaitable, Callable
import inspect
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Protocol
from urllib.parse import urlencode

import websockets
from dotenv import load_dotenv

from engines.realtime_translation import RealtimeTranslationStatistics
from providers.errors import ProviderConfigurationError, ProviderError


LOGGER = logging.getLogger(__name__)
MAX_WEBSOCKET_MESSAGE_BYTES = 4 * 1024 * 1024
WEBSOCKET_SEND_TIMEOUT_SECONDS = 2.0
LEVEL_MEASUREMENT_INTERVAL_BLOCKS = 5
AudioCallback = Callable[[bytes], Awaitable[None] | None]
TranscriptCallback = Callable[[str], Awaitable[None] | None]


class RealtimeTranslationError(ProviderError):
    """Raised when a Realtime translation session cannot continue."""


def is_realtime_rate_limit_error(error: BaseException | None) -> bool:
    """Return whether an exception chain represents an OpenAI rate limit."""
    current = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        status_code = getattr(current, "status_code", None)
        response = getattr(current, "response", None)
        response_status = getattr(response, "status_code", None)
        if status_code == 429 or response_status == 429:
            return True
        message = str(current).casefold()
        if any(
            marker in message
            for marker in (
                "rate limit",
                "rate_limit",
                "rate limited",
                "too many requests",
                "status 429",
                "http 429",
            )
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


class RealtimeRateLimitCoordinator:
    """Share a provider rate-limit cooldown across directional sessions."""

    def __init__(self) -> None:
        self._retry_not_before = 0.0

    def defer(self, delay_seconds: float, *, now: float) -> float:
        """Extend the shared cooldown and return its remaining duration."""
        self._retry_not_before = max(
            self._retry_not_before,
            now + max(0.0, delay_seconds),
        )
        return self.remaining_delay(now=now)

    def remaining_delay(self, *, now: float) -> float:
        """Return how long every direction must wait before reconnecting."""
        return max(0.0, self._retry_not_before - now)


class WebSocketConnection(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


ConnectCallable = Callable[..., Awaitable[WebSocketConnection]]


class OpenAIRealtimeTranslationSession:
    """Stream PCM16 audio to one target language and emit translated PCM16."""

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str,
        model: str,
        sample_rate: int,
        safety_identifier: str = "",
        input_transcription_model: str | None = None,
        session_setup_timeout_seconds: float = 10.0,
        close_timeout_seconds: float = 10.0,
        on_audio: AudioCallback | None = None,
        on_input_transcript: TranscriptCallback | None = None,
        on_output_transcript: TranscriptCallback | None = None,
        connect_callable: ConnectCallable | None = None,
    ) -> None:
        if not api_key.strip():
            raise ProviderConfigurationError("OPENAI_API_KEY is missing from .env.")
        if not endpoint.startswith("wss://"):
            raise ProviderConfigurationError(
                "OpenAI Realtime translation endpoint must use wss://."
            )
        if not model.strip():
            raise ProviderConfigurationError(
                "OpenAI Realtime translation model must not be empty."
            )
        if sample_rate != 24_000:
            raise ProviderConfigurationError(
                "OpenAI Realtime translation requires 24000 Hz PCM16 audio."
            )
        if (
            input_transcription_model is not None
            and not input_transcription_model.strip()
        ):
            raise ProviderConfigurationError(
                "OpenAI Realtime input transcription model must not be empty."
            )
        if session_setup_timeout_seconds <= 0 or close_timeout_seconds <= 0:
            raise ProviderConfigurationError(
                "OpenAI Realtime setup and close timeouts must be greater than zero."
            )
        self.api_key = api_key.strip()
        self.endpoint = endpoint.rstrip("?")
        self.model = model.strip()
        self.sample_rate = sample_rate
        self.safety_identifier = safety_identifier.strip()
        self.input_transcription_model = (
            input_transcription_model.strip()
            if input_transcription_model is not None
            else None
        )
        self.session_setup_timeout_seconds = session_setup_timeout_seconds
        self.close_timeout_seconds = close_timeout_seconds
        self.on_audio = on_audio
        self.on_input_transcript = on_input_transcript
        self.on_output_transcript = on_output_transcript
        self.statistics = RealtimeTranslationStatistics()
        self.error_event = asyncio.Event()
        self.last_error: BaseException | None = None
        self._connect_callable = connect_callable or websockets.connect
        self._connection: WebSocketConnection | None = None
        self._receiver_task: asyncio.Task[None] | None = None
        self._session_ready = asyncio.Event()
        self._session_closed = asyncio.Event()
        self._closing = False
        self._send_lock = asyncio.Lock()
        self._first_input_at: float | None = None
        self._last_input_at: float | None = None
        self._last_output_at: float | None = None
        self._observed_event_types: set[str] = set()
        self._input_level_block_count = 0
        self._output_level_block_count = 0

    @property
    def connected(self) -> bool:
        return self._connection is not None and self._session_ready.is_set()

    async def connect(self, target_language: str, *, reconnecting: bool = False) -> None:
        """Open and configure one dedicated target-language translation session."""
        if self._connection is not None:
            return
        if not self.api_key:
            raise ProviderConfigurationError("OPENAI_API_KEY is missing from .env.")
        if not target_language.strip():
            raise ProviderConfigurationError("Target language must not be empty.")
        self._session_ready.clear()
        self._session_closed.clear()
        self._closing = False
        self.error_event.clear()
        self.last_error = None
        self._first_input_at = None
        self._last_input_at = None
        self._last_output_at = None
        self._observed_event_types.clear()
        self.statistics.first_audio_latency_ms = None
        self.statistics.total_latency_ms = None
        self.statistics.reset_audio_levels()
        self._input_level_block_count = 0
        self._output_level_block_count = 0
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if self.safety_identifier:
            headers["OpenAI-Safety-Identifier"] = self.safety_identifier
        url = f"{self.endpoint}?{urlencode({'model': self.model})}"
        setup_started_at = time.monotonic()
        try:
            connection = await asyncio.wait_for(
                self._connect_callable(
                    url,
                    additional_headers=headers,
                    compression=None,
                    max_size=MAX_WEBSOCKET_MESSAGE_BYTES,
                    close_timeout=self.close_timeout_seconds,
                ),
                timeout=self.session_setup_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError as error:
            self._record_error(error)
            raise RealtimeTranslationError(
                "OpenAI Realtime translation connection timed out."
            ) from error
        except Exception as error:
            self._record_error(error)
            raise RealtimeTranslationError(
                f"Could not connect to OpenAI Realtime translation: {error}"
            ) from error
        self._connection = connection
        self.statistics.connections += 1
        if reconnecting:
            self.statistics.reconnections += 1
        self._receiver_task = asyncio.create_task(
            self._receive_events(), name="openai realtime translation receiver"
        )
        input_audio: dict[str, object] = {
            "noise_reduction": {"type": "near_field"}
        }
        if self.input_transcription_model is not None:
            input_audio["transcription"] = {
                "model": self.input_transcription_model
            }
        await self._send_json(
            {
                "type": "session.update",
                "session": {
                    "audio": {
                        "input": input_audio,
                        "output": {"language": target_language},
                    }
                },
            }
        )
        ready_wait = asyncio.create_task(self._session_ready.wait())
        error_wait = asyncio.create_task(self.error_event.wait())
        try:
            remaining_setup_seconds = max(
                0.0,
                self.session_setup_timeout_seconds
                - (time.monotonic() - setup_started_at),
            )
            done, pending = await asyncio.wait(
                {ready_wait, error_wait},
                timeout=remaining_setup_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if error_wait in done or self.last_error is not None:
                raise RealtimeTranslationError(
                    f"OpenAI Realtime translation setup failed: {self.last_error}"
                ) from self.last_error
            if ready_wait not in done:
                raise TimeoutError
        except asyncio.CancelledError:
            await self.abort()
            raise
        except TimeoutError as error:
            self._record_error(error)
            await self.abort()
            raise RealtimeTranslationError(
                "OpenAI Realtime translation session configuration timed out."
            ) from error
        except RealtimeTranslationError:
            await self.abort()
            raise

    async def send_audio(self, pcm16: bytes) -> None:
        """Append one mono 24 kHz PCM16 block to the continuous source stream."""
        if not pcm16:
            return
        if len(pcm16) % 2:
            raise ValueError("PCM16 audio blocks must contain complete samples.")
        if not self.connected:
            raise RealtimeTranslationError("Realtime translation session is not ready.")
        await self._send_json(
            {
                "type": "session.input_audio_buffer.append",
                "audio": base64.b64encode(pcm16).decode("ascii"),
            }
        )
        now = time.monotonic()
        if self._first_input_at is None:
            self._first_input_at = now
        self._last_input_at = now
        self.statistics.input_pcm_bytes += len(pcm16)
        self._input_level_block_count += 1
        if self._input_level_block_count % LEVEL_MEASUREMENT_INTERVAL_BLOCKS == 1:
            self.statistics.record_input_level(pcm16)

    async def close(self) -> None:
        """Flush translated output with session.close before closing the socket."""
        connection = self._connection
        if connection is None:
            return
        self._closing = True
        try:
            await self._send_json({"type": "session.close"})
            await asyncio.wait_for(
                self._session_closed.wait(), timeout=self.close_timeout_seconds
            )
        except (TimeoutError, RealtimeTranslationError) as error:
            LOGGER.warning(
                "event=realtime_translation_close_incomplete action=abort error=%s", error
            )
        finally:
            await self.abort()

    async def abort(self) -> None:
        """Close immediately, cancelling the receiver when graceful drain is impossible."""
        self._closing = True
        connection, self._connection = self._connection, None
        receiver, self._receiver_task = self._receiver_task, None
        if connection is not None:
            try:
                await connection.close()
            except Exception:
                pass
        current = asyncio.current_task()
        if receiver is not None and receiver is not current and not receiver.done():
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
        self._session_ready.clear()
        self._closing = False

    async def _send_json(self, payload: dict[str, object]) -> None:
        connection = self._connection
        if connection is None:
            raise RealtimeTranslationError("Realtime translation socket is closed.")
        try:
            async with self._send_lock:
                await asyncio.wait_for(
                    connection.send(json.dumps(payload, separators=(",", ":"))),
                    timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS,
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._record_error(error)
            raise RealtimeTranslationError(
                f"Could not send Realtime translation audio: {error}"
            ) from error

    async def _receive_events(self) -> None:
        connection = self._connection
        if connection is None:
            return
        try:
            while True:
                raw = await connection.recv()
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                event = json.loads(raw)
                if not isinstance(event, dict):
                    continue
                event_type = event.get("type")
                if isinstance(event_type, str) and event_type not in self._observed_event_types:
                    self._observed_event_types.add(event_type)
                    LOGGER.info(
                        "event=realtime_translation_event_observed type=%s",
                        event_type,
                    )
                if event_type == "session.updated":
                    self._session_ready.set()
                elif event_type == "session.output_audio.delta":
                    await self._handle_audio_delta(event)
                elif event_type == "session.input_transcript.delta":
                    delta = str(event.get("delta", ""))
                    self.statistics.input_transcript_characters += len(delta)
                    await self._invoke(self.on_input_transcript, delta)
                elif event_type == "session.output_transcript.delta":
                    delta = str(event.get("delta", ""))
                    self.statistics.output_transcript_characters += len(delta)
                    await self._invoke(self.on_output_transcript, delta)
                elif event_type == "session.closed":
                    self._session_closed.set()
                    self._session_ready.clear()
                    if not self._closing:
                        self._record_error(
                            RealtimeTranslationError(
                                "OpenAI Realtime translation session closed unexpectedly."
                            )
                        )
                    return
                elif event_type == "error":
                    detail = event.get("error")
                    raise RealtimeTranslationError(f"OpenAI Realtime error: {detail}")
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            if self._connection is connection:
                self._record_error(error)

    async def _handle_audio_delta(self, event: dict[str, Any]) -> None:
        encoded = event.get("delta")
        if not isinstance(encoded, str):
            raise RealtimeTranslationError("Translated audio delta is not base64 text.")
        try:
            pcm16 = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as error:
            raise RealtimeTranslationError("Translated audio delta is invalid base64.") from error
        if len(pcm16) % 2:
            raise RealtimeTranslationError("Translated audio delta is not complete PCM16.")
        now = time.monotonic()
        if self.statistics.first_audio_latency_ms is None and self._first_input_at is not None:
            self.statistics.first_audio_latency_ms = (now - self._first_input_at) * 1000
        self._last_output_at = now
        if self._last_input_at is not None:
            self.statistics.total_latency_ms = (now - self._last_input_at) * 1000
        self.statistics.output_pcm_bytes += len(pcm16)
        self._output_level_block_count += 1
        if self._output_level_block_count % LEVEL_MEASUREMENT_INTERVAL_BLOCKS == 1:
            self.statistics.record_output_level(pcm16)
        await self._invoke(self.on_audio, pcm16)

    def _record_error(self, error: BaseException) -> None:
        self.statistics.errors += 1
        self.last_error = error
        self.error_event.set()
        LOGGER.warning(
            "event=realtime_translation_session_error error_type=%s error=%r",
            type(error).__name__,
            error,
        )

    @staticmethod
    async def _invoke(
        callback: Callable[[Any], Awaitable[None] | None] | None, value: Any
    ) -> None:
        if callback is None or value == "":
            return
        result = callback(value)
        if inspect.isawaitable(result):
            await result


class OpenAIRealtimeTranslationFactory:
    """Create independent directional sessions from config and environment secrets."""

    def __init__(
        self,
        config: dict[str, object],
        *,
        api_key: str,
        safety_identifier: str = "",
    ) -> None:
        realtime = config.get("realtime_translation")
        if not isinstance(realtime, dict) or not isinstance(realtime.get("openai"), dict):
            raise ProviderConfigurationError(
                "Missing realtime_translation.openai configuration."
            )
        self._settings = dict(realtime["openai"])
        self._config = config
        self._api_key = api_key
        self._safety_identifier = safety_identifier
        self.rate_limit_coordinator = RealtimeRateLimitCoordinator()

    @classmethod
    def from_environment(
        cls, config: dict[str, object], env_path: Path
    ) -> "OpenAIRealtimeTranslationFactory":
        load_dotenv(env_path, override=False)
        return cls(
            config,
            api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            safety_identifier=os.getenv("OPENAI_SAFETY_IDENTIFIER", "").strip(),
        )

    def create(
        self,
        *,
        pipeline_name: str | None = None,
        on_audio: AudioCallback | None = None,
        on_input_transcript: TranscriptCallback | None = None,
        on_output_transcript: TranscriptCallback | None = None,
    ) -> OpenAIRealtimeTranslationSession:
        settings = self._settings
        if pipeline_name is not None:
            realtime = self._config.get("realtime_translation")
            if isinstance(realtime, dict):
                directions = realtime.get("directions")
                if isinstance(directions, dict):
                    direction_settings = directions.get(pipeline_name)
                    if isinstance(direction_settings, dict):
                        settings = direction_settings
        input_transcription = settings["input_transcription"]
        if not isinstance(input_transcription, dict):
            raise ProviderConfigurationError(
                "OpenAI Realtime input_transcription configuration must be a mapping."
            )
        input_transcription_model = (
            str(input_transcription["model"])
            if bool(input_transcription["enabled"])
            else None
        )
        return OpenAIRealtimeTranslationSession(
            api_key=self._api_key,
            endpoint=str(settings["endpoint"]),
            model=str(settings["model"]),
            sample_rate=int(settings["sample_rate"]),
            safety_identifier=self._safety_identifier,
            input_transcription_model=input_transcription_model,
            session_setup_timeout_seconds=float(
                settings["session_setup_timeout_seconds"]
            ),
            close_timeout_seconds=float(settings["close_timeout_seconds"]),
            on_audio=on_audio,
            on_input_transcript=on_input_transcript,
            on_output_transcript=on_output_transcript,
        )
