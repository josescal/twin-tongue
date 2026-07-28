import asyncio
import base64
import json
import unittest

from engines.openai_realtime import (
    MAX_WEBSOCKET_MESSAGE_BYTES,
    OpenAIRealtimeTranslationSession,
)


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.incoming: asyncio.Queue[str | BaseException] = asyncio.Queue()
        self.closed = False

    async def send(self, message: str) -> None:
        event = json.loads(message)
        self.sent.append(event)
        if event["type"] == "session.update":
            self.incoming.put_nowait(json.dumps({"type": "session.updated"}))
        elif event["type"] == "session.close":
            self.incoming.put_nowait(json.dumps({"type": "session.closed"}))

    async def recv(self) -> str:
        value = await self.incoming.get()
        if isinstance(value, BaseException):
            raise value
        return value

    async def close(self) -> None:
        self.closed = True


class OpenAIRealtimeTranslationSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.socket = FakeWebSocket()
        self.connection_calls: list[tuple[str, dict[str, object]]] = []

        async def connect(url: str, **kwargs: object) -> FakeWebSocket:
            self.connection_calls.append((url, kwargs))
            return self.socket

        self.audio: list[bytes] = []
        self.input_text: list[str] = []
        self.output_text: list[str] = []
        self.session = OpenAIRealtimeTranslationSession(
            api_key="test-key",
            endpoint="wss://api.openai.test/v1/realtime/translations",
            model="gpt-realtime-translate",
            sample_rate=24_000,
            safety_identifier="safe-user",
            on_audio=self.audio.append,
            on_input_transcript=self.input_text.append,
            on_output_transcript=self.output_text.append,
            connect_callable=connect,
        )

    async def asyncTearDown(self) -> None:
        await self.session.abort()

    async def test_connect_configure_stream_receive_and_close(self) -> None:
        await self.session.connect("es")
        await self.session.send_audio(b"\x01\x00\x02\x00")
        self.socket.incoming.put_nowait(
            json.dumps(
                {
                    "type": "session.output_audio.delta",
                    "delta": base64.b64encode(b"\x03\x00").decode("ascii"),
                }
            )
        )
        self.socket.incoming.put_nowait(
            json.dumps({"type": "session.input_transcript.delta", "delta": "Hola"})
        )
        self.socket.incoming.put_nowait(
            json.dumps({"type": "session.output_transcript.delta", "delta": "Hello"})
        )
        await asyncio.sleep(0)

        await self.session.close()

        url, options = self.connection_calls[0]
        self.assertEqual(
            "wss://api.openai.test/v1/realtime/translations?model=gpt-realtime-translate",
            url,
        )
        self.assertEqual(
            "Bearer test-key", options["additional_headers"]["Authorization"]
        )
        self.assertEqual(
            "safe-user",
            options["additional_headers"]["OpenAI-Safety-Identifier"],
        )
        self.assertEqual(MAX_WEBSOCKET_MESSAGE_BYTES, options["max_size"])
        self.assertEqual("session.update", self.socket.sent[0]["type"])
        self.assertEqual(
            "es", self.socket.sent[0]["session"]["audio"]["output"]["language"]
        )
        self.assertEqual(
            {"type": "near_field"},
            self.socket.sent[0]["session"]["audio"]["input"]["noise_reduction"],
        )
        self.assertEqual("session.input_audio_buffer.append", self.socket.sent[1]["type"])
        self.assertEqual("session.close", self.socket.sent[-1]["type"])
        self.assertEqual([b"\x03\x00"], self.audio)
        self.assertEqual(["Hola"], self.input_text)
        self.assertEqual(["Hello"], self.output_text)
        self.assertEqual(4, self.session.statistics.input_pcm_bytes)
        self.assertEqual(2, self.session.statistics.output_pcm_bytes)
        self.assertEqual(2, self.session.statistics.input_level_sample_count)
        self.assertEqual(1, self.session.statistics.output_level_sample_count)
        self.assertEqual(2, self.session.statistics.input_peak_amplitude)
        self.assertEqual(3, self.session.statistics.output_peak_amplitude)
        self.assertIsNotNone(self.session.statistics.input_rms_dbfs())
        self.assertIsNotNone(self.session.statistics.output_rms_dbfs())
        self.assertEqual(4, self.session.statistics.input_transcript_characters)
        self.assertEqual(5, self.session.statistics.output_transcript_characters)
        self.assertIsNotNone(self.session.statistics.first_audio_latency_ms)
        self.assertTrue(self.socket.closed)

    async def test_server_error_is_observable(self) -> None:
        await self.session.connect("fr")
        self.socket.incoming.put_nowait(
            json.dumps({"type": "error", "error": {"message": "rate limited"}})
        )

        await asyncio.wait_for(self.session.error_event.wait(), timeout=1)

        self.assertEqual(1, self.session.statistics.errors)
        self.assertIn("rate limited", str(self.session.last_error))

    async def test_reconnect_counter_is_separate_from_connections(self) -> None:
        await self.session.connect("ca", reconnecting=True)

        self.assertEqual(1, self.session.statistics.connections)
        self.assertEqual(1, self.session.statistics.reconnections)

    async def test_cancelled_connection_is_not_reported_as_provider_failure(self) -> None:
        connection_started = asyncio.Event()

        async def blocked_connect(
            url: str, **kwargs: object
        ) -> FakeWebSocket:
            connection_started.set()
            await asyncio.Event().wait()
            return self.socket

        session = OpenAIRealtimeTranslationSession(
            api_key="test-key",
            endpoint="wss://api.openai.test/v1/realtime/translations",
            model="gpt-realtime-translate",
            sample_rate=24_000,
            connect_callable=blocked_connect,
        )
        task = asyncio.create_task(session.connect("es"))
        await connection_started.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(0, session.statistics.errors)

    async def test_unexpected_server_close_marks_session_unavailable(self) -> None:
        await self.session.connect("es")
        self.socket.incoming.put_nowait(
            json.dumps({"type": "session.closed"})
        )

        await asyncio.wait_for(self.session.error_event.wait(), timeout=1)

        self.assertFalse(self.session.connected)
        self.assertIn("unexpectedly", str(self.session.last_error))

    async def test_connection_timeout_is_bounded_and_observable(self) -> None:
        async def blocked_connect(
            url: str, **kwargs: object
        ) -> FakeWebSocket:
            await asyncio.Event().wait()
            return self.socket

        session = OpenAIRealtimeTranslationSession(
            api_key="test-key",
            endpoint="wss://api.openai.test/v1/realtime/translations",
            model="gpt-realtime-translate",
            sample_rate=24_000,
            session_setup_timeout_seconds=0.01,
            connect_callable=blocked_connect,
        )

        with self.assertRaisesRegex(Exception, "timed out"):
            await session.connect("es")

        self.assertEqual(1, session.statistics.errors)
        self.assertFalse(session.connected)


if __name__ == "__main__":
    unittest.main()
