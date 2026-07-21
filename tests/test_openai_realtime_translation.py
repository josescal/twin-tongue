import asyncio
import base64
import json
import unittest

from engines.openai_realtime import OpenAIRealtimeTranslationSession


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
        self.assertEqual("session.update", self.socket.sent[0]["type"])
        self.assertEqual(
            "es", self.socket.sent[0]["session"]["audio"]["output"]["language"]
        )
        self.assertEqual("session.input_audio_buffer.append", self.socket.sent[1]["type"])
        self.assertEqual("session.close", self.socket.sent[-1]["type"])
        self.assertEqual([b"\x03\x00"], self.audio)
        self.assertEqual(["Hola"], self.input_text)
        self.assertEqual(["Hello"], self.output_text)
        self.assertEqual(4, self.session.statistics.input_pcm_bytes)
        self.assertEqual(2, self.session.statistics.output_pcm_bytes)
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


if __name__ == "__main__":
    unittest.main()
