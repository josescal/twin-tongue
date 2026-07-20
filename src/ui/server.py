"""Small dependency-free HTTP and SSE server for local runtime control."""

import asyncio
import json
import logging
from pathlib import Path
from urllib.parse import urlsplit

from app_state import ApplicationState

LOGGER = logging.getLogger(__name__)
MAX_REQUEST_BODY_BYTES = 64 * 1024
CONNECTION_SHUTDOWN_TIMEOUT_SECONDS = 2.0
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
INDEX_PATH = Path(__file__).with_name("index.html")


class HttpRequestError(Exception):
    """Represent an HTTP client error with a response status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class LocalControlServer:
    """Serve the local control page and its JSON/SSE API."""

    def __init__(self, state: ApplicationState, host: str, port: int) -> None:
        if host not in LOOPBACK_HOSTS:
            raise ValueError("The control server must bind to a loopback host.")
        if not 0 <= port <= 65535:
            raise ValueError("The control server port must be between 0 and 65535.")
        self._state = state
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None
        self._connection_tasks: set[asyncio.Task[object]] = set()
        self._connection_writers: set[asyncio.StreamWriter] = set()

    @property
    def port(self) -> int:
        """Return the actual bound port, including when port zero was requested."""
        if self._server and self._server.sockets:
            return int(self._server.sockets[0].getsockname()[1])
        return self._port

    @property
    def url(self) -> str:
        """Return the URL users can open locally."""
        host = f"[{self._host}]" if ":" in self._host else self._host
        return f"http://{host}:{self.port}"

    async def start(self) -> None:
        """Start accepting local HTTP connections."""
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._handle_connection,
            self._host,
            self._port,
        )
        LOGGER.info("event=control_server_ready url=%s", self.url)

    async def close(self) -> None:
        """Stop accepting connections and close persistent clients promptly."""
        if self._server is None:
            return
        server = self._server
        self._server = None
        server.close()
        tasks = tuple(self._connection_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=CONNECTION_SHUTDOWN_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                LOGGER.warning(
                    "event=control_server_shutdown_timeout component=connections "
                    "timeout_seconds=%.1f action=abort_sockets",
                    CONNECTION_SHUTDOWN_TIMEOUT_SECONDS,
                )
                for writer in tuple(self._connection_writers):
                    writer.transport.abort()
        try:
            await asyncio.wait_for(
                server.wait_closed(),
                timeout=CONNECTION_SHUTDOWN_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            LOGGER.warning(
                "event=control_server_shutdown_timeout component=listener "
                "timeout_seconds=%.1f action=continue_shutdown",
                CONNECTION_SHUTDOWN_TIMEOUT_SECONDS,
            )
        LOGGER.info("event=control_server_stopped active_connections=%s", len(tasks))

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._connection_tasks.add(task)
        self._connection_writers.add(writer)
        try:
            method, target, headers, body = await self._read_request(reader)
            path = urlsplit(target).path
            if method == "GET" and path == "/":
                await self._write_response(
                    writer,
                    200,
                    INDEX_PATH.read_bytes(),
                    "text/html; charset=utf-8",
                )
            elif method == "GET" and path == "/api/state":
                await self._write_json(writer, 200, self._state.snapshot())
            elif method == "GET" and path == "/api/events":
                await self._serve_events(writer)
                return
            elif method == "PUT" and path == "/api/pipelines":
                payload = self._decode_json(body)
                if not isinstance(payload.get("mode"), str):
                    raise HttpRequestError(400, "The JSON body must contain a string 'mode'.")
                try:
                    snapshot = await self._state.set_all_modes(payload["mode"])
                except ValueError as error:
                    raise HttpRequestError(400, str(error)) from error
                LOGGER.info(
                    "event=control_changed setting=mode pipeline=all value=%s",
                    payload["mode"],
                )
                await self._write_json(writer, 200, snapshot)
            elif method == "PUT" and path.startswith("/api/pipelines/"):
                name = path.removeprefix("/api/pipelines/")
                payload = self._decode_json(body)
                if not isinstance(payload.get("mode"), str):
                    raise HttpRequestError(400, "The JSON body must contain a string 'mode'.")
                try:
                    snapshot = await self._state.set_mode(name, payload["mode"])
                except ValueError as error:
                    raise HttpRequestError(400, str(error)) from error
                LOGGER.info(
                    "event=control_changed setting=mode pipeline=%s value=%s",
                    name,
                    payload["mode"],
                )
                await self._write_json(writer, 200, snapshot)
            elif method == "PUT" and path.startswith("/api/languages/"):
                role = path.removeprefix("/api/languages/")
                payload = self._decode_json(body)
                if not isinstance(payload.get("language"), str):
                    raise HttpRequestError(
                        400, "The JSON body must contain a string 'language'."
                    )
                try:
                    snapshot = await self._state.set_language(role, payload["language"])
                except ValueError as error:
                    raise HttpRequestError(400, str(error)) from error
                LOGGER.info(
                    "event=control_changed setting=language role=%s value=%s",
                    role,
                    payload["language"],
                )
                await self._write_json(writer, 200, snapshot)
            elif method == "PUT" and path == "/api/ui-language":
                payload = self._decode_json(body)
                if not isinstance(payload.get("language"), str):
                    raise HttpRequestError(
                        400, "The JSON body must contain a string 'language'."
                    )
                try:
                    snapshot = await self._state.set_ui_language(payload["language"])
                except ValueError as error:
                    raise HttpRequestError(400, str(error)) from error
                LOGGER.info(
                    "event=control_changed setting=ui_language value=%s",
                    payload["language"],
                )
                await self._write_json(writer, 200, snapshot)
            elif method == "PUT" and path == "/api/voice-gender":
                payload = self._decode_json(body)
                if not isinstance(payload.get("gender"), str):
                    raise HttpRequestError(
                        400, "The JSON body must contain a string 'gender'."
                    )
                try:
                    snapshot = await self._state.set_voice_gender(payload["gender"])
                except ValueError as error:
                    raise HttpRequestError(400, str(error)) from error
                LOGGER.info(
                    "event=control_changed setting=voice_gender value=%s",
                    payload["gender"],
                )
                await self._write_json(writer, 200, snapshot)
            elif method == "PUT" and path.startswith("/api/audio-devices/"):
                direction = path.removeprefix("/api/audio-devices/")
                payload = self._decode_json(body)
                if not isinstance(payload.get("selection"), str):
                    raise HttpRequestError(
                        400, "The JSON body must contain a string 'selection'."
                    )
                try:
                    snapshot = await self._state.set_audio_device(
                        direction, payload["selection"]
                    )
                except ValueError as error:
                    raise HttpRequestError(400, str(error)) from error
                LOGGER.info(
                    "event=control_changed setting=audio_device direction=%s value=%s",
                    direction,
                    payload["selection"],
                )
                await self._write_json(writer, 200, snapshot)
            elif method == "PUT" and path == "/api/audio-recording":
                payload = self._decode_json(body)
                if not isinstance(payload.get("active"), bool):
                    raise HttpRequestError(
                        400, "The JSON body must contain a boolean 'active'."
                    )
                try:
                    snapshot = await self._state.set_manual_audio_recording(
                        payload["active"]
                    )
                except ValueError as error:
                    raise HttpRequestError(400, str(error)) from error
                LOGGER.info(
                    "event=control_changed setting=audio_recording manual=%s",
                    payload["active"],
                )
                await self._write_json(writer, 200, snapshot)
            elif method == "DELETE" and path == "/api/transcription":
                snapshot = await self._state.clear_transcription()
                LOGGER.info("event=control_changed setting=transcription action=cleared")
                await self._write_json(writer, 200, snapshot)
            else:
                raise HttpRequestError(404, "Route not found.")
        except HttpRequestError as error:
            await self._write_json(writer, error.status, {"error": str(error)})
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except OSError:
            pass
        except Exception:
            LOGGER.exception("event=control_server_request_failed action=http_500")
            try:
                await self._write_json(writer, 500, {"error": "Internal server error."})
            except (ConnectionError, OSError):
                pass
        finally:
            if not writer.is_closing():
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
                except (OSError, TimeoutError):
                    writer.transport.abort()
            self._connection_writers.discard(writer)
            if task is not None:
                self._connection_tasks.discard(task)

    async def _read_request(
        self, reader: asyncio.StreamReader
    ) -> tuple[str, str, dict[str, str], bytes]:
        request_line = await reader.readline()
        if not request_line:
            raise ConnectionError("Client disconnected before sending a request.")
        try:
            method, target, version = request_line.decode("ascii").strip().split(" ", 2)
        except (UnicodeDecodeError, ValueError) as error:
            raise HttpRequestError(400, "Malformed HTTP request line.") from error
        if version not in {"HTTP/1.0", "HTTP/1.1"}:
            raise HttpRequestError(400, "Unsupported HTTP version.")
        headers: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if line in {b"\r\n", b"\n"}:
                break
            if not line:
                raise HttpRequestError(400, "Incomplete HTTP headers.")
            try:
                key, value = line.decode("latin-1").split(":", 1)
            except ValueError as error:
                raise HttpRequestError(400, "Malformed HTTP header.") from error
            headers[key.strip().lower()] = value.strip()
        try:
            content_length = int(headers.get("content-length", "0"))
        except ValueError as error:
            raise HttpRequestError(400, "Invalid Content-Length header.") from error
        if not 0 <= content_length <= MAX_REQUEST_BODY_BYTES:
            raise HttpRequestError(413, "Request body is too large.")
        body = await reader.readexactly(content_length) if content_length else b""
        return method.upper(), target, headers, body

    @staticmethod
    def _decode_json(body: bytes) -> dict[str, object]:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HttpRequestError(400, "Request body must be valid JSON.") from error
        if not isinstance(payload, dict):
            raise HttpRequestError(400, "Request body must be a JSON object.")
        return payload

    async def _serve_events(self, writer: asyncio.StreamWriter) -> None:
        headers = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/event-stream; charset=utf-8\r\n"
            "Cache-Control: no-cache\r\n"
            "Connection: keep-alive\r\n\r\n"
        )
        writer.write(headers.encode("ascii"))
        await writer.drain()
        async with self._state.subscribe() as queue:
            await self._write_event(writer, self._state.snapshot())
            while True:
                try:
                    snapshot = await asyncio.wait_for(queue.get(), timeout=15)
                    await self._write_event(writer, snapshot)
                except TimeoutError:
                    writer.write(b": keep-alive\n\n")
                    await writer.drain()

    @staticmethod
    async def _write_event(writer: asyncio.StreamWriter, payload: dict[str, object]) -> None:
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        writer.write(f"data: {data}\n\n".encode("utf-8"))
        await writer.drain()

    @staticmethod
    async def _write_response(
        writer: asyncio.StreamWriter,
        status: int,
        body: bytes,
        content_type: str,
    ) -> None:
        reason = {
            200: "OK",
            400: "Bad Request",
            404: "Not Found",
            413: "Payload Too Large",
            500: "Internal Server Error",
        }.get(status, "Error")
        headers = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n"
        )
        writer.write(headers.encode("ascii") + body)
        await writer.drain()

    async def _write_json(
        self, writer: asyncio.StreamWriter, status: int, payload: dict[str, object]
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        await self._write_response(writer, status, body, "application/json; charset=utf-8")
