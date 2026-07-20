"""Tests for ElevenLabs realtime STT connection and audio lifecycle."""

import base64
import unittest

from providers.elevenlabs_stt import ElevenLabsRealtimeSTT


class ElevenLabsConnectionLifecycleTests(unittest.TestCase):
    def test_idle_close_is_not_fatal_when_reconnect_is_enabled(self) -> None:
        provider = ElevenLabsRealtimeSTT(
            api_key="test",
            reconnect_on_close=True,
        )
        provider.connection = object()  # type: ignore[assignment]

        provider._handle_close()

        self.assertIsNone(provider.connection)
        self.assertIsNone(provider.last_error)
        self.assertFalse(provider.error_event.is_set())
        self.assertEqual(provider.statistics.idle_disconnects, 1)

    def test_idle_activity_message_is_not_fatal_when_reconnect_is_enabled(self) -> None:
        provider = ElevenLabsRealtimeSTT(
            api_key="test",
            reconnect_on_close=True,
        )

        provider._handle_error(
            {
                "message_type": "insufficient_audio_activity",
                "message": "No audio activity",
            }
        )

        self.assertIsNone(provider.last_error)
        self.assertFalse(provider.error_event.is_set())
        self.assertEqual(provider.statistics.connection_errors, 0)

    def test_normal_close_error_notification_is_not_fatal(self) -> None:
        provider = ElevenLabsRealtimeSTT(
            api_key="test",
            reconnect_on_close=True,
        )

        provider._handle_error(
            {
                "message_type": "error",
                "message": "received 1000 (OK); then sent 1000 (OK)",
            }
        )

        self.assertIsNone(provider.last_error)
        self.assertFalse(provider.error_event.is_set())
        self.assertEqual(provider.statistics.connection_errors, 0)


class _Connection:
    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []
        self.commits = 0

    async def send(self, message: dict[str, str]) -> None:
        self.messages.append(message)

    async def commit(self) -> None:
        self.commits += 1


class _NormalCloseConnection(_Connection):
    def __init__(self, *, fail_send: bool = False, fail_commit: bool = False) -> None:
        super().__init__()
        self.fail_send = fail_send
        self.fail_commit = fail_commit

    async def send(self, message: dict[str, str]) -> None:
        if self.fail_send:
            raise RuntimeError("received 1000 (OK); then sent 1000 (OK)")
        await super().send(message)

    async def commit(self) -> None:
        if self.fail_commit:
            raise RuntimeError("received 1000 (OK); then sent 1000 (OK)")
        await super().commit()


class ElevenLabsKeepaliveTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_close_during_keepalive_waits_for_speech_to_reconnect(self) -> None:
        provider = ElevenLabsRealtimeSTT(api_key="test", reconnect_on_close=True)
        provider.connection = _NormalCloseConnection(fail_send=True)  # type: ignore[assignment]

        await provider.send_keepalive(1920)

        self.assertIsNone(provider.connection)
        self.assertFalse(provider.error_event.is_set())
        self.assertIsNone(provider.last_error)
        self.assertEqual(0, provider.statistics.keepalive_blocks)

    async def test_normal_close_during_speech_reconnects_and_retries_audio(self) -> None:
        provider = ElevenLabsRealtimeSTT(
            api_key="test",
            reconnect_on_close=True,
            sample_rate=16_000,
            send_chunk_duration_ms=100,
        )
        provider.connection = _NormalCloseConnection(fail_send=True)  # type: ignore[assignment]
        recovered = _Connection()

        async def reconnect() -> None:
            provider.connection = recovered  # type: ignore[assignment]

        provider.connect = reconnect  # type: ignore[method-assign]

        await provider.send_audio(bytes(3_200))

        self.assertEqual(1, len(recovered.messages))
        self.assertFalse(provider.error_event.is_set())
        self.assertIsNone(provider.last_error)

    async def test_normal_close_during_commit_is_not_fatal(self) -> None:
        provider = ElevenLabsRealtimeSTT(
            api_key="test",
            reconnect_on_close=True,
            sample_rate=16_000,
        )
        connection = _NormalCloseConnection(fail_commit=True)
        provider.connection = connection  # type: ignore[assignment]
        await provider.send_audio(bytes(16_000 * 2))

        await provider.commit_final()

        self.assertIsNone(provider.connection)
        self.assertFalse(provider.error_event.is_set())
        self.assertIsNone(provider.last_error)

    async def test_late_close_callback_does_not_clear_reconnected_session(self) -> None:
        provider = ElevenLabsRealtimeSTT(api_key="test", reconnect_on_close=True)
        previous = _Connection()
        recovered = _Connection()
        provider.connection = recovered  # type: ignore[assignment]

        provider._handle_close(previous)  # type: ignore[arg-type]

        self.assertIs(provider.connection, recovered)

    async def test_audio_is_grouped_into_configured_100ms_chunks(self) -> None:
        provider = ElevenLabsRealtimeSTT(
            api_key="test",
            sample_rate=16_000,
            send_chunk_duration_ms=100,
        )
        connection = _Connection()
        provider.connection = connection  # type: ignore[assignment]
        twenty_ms = bytes(16_000 * 2 * 20 // 1000)

        for _ in range(4):
            await provider.send_audio(twenty_ms)
        self.assertEqual([], connection.messages)

        await provider.send_audio(twenty_ms)

        self.assertEqual(1, len(connection.messages))
        payload = base64.b64decode(connection.messages[0]["audio_base_64"])
        self.assertEqual(16_000 * 2 * 100 // 1000, len(payload))

    async def test_commit_flushes_the_last_partial_chunk(self) -> None:
        provider = ElevenLabsRealtimeSTT(
            api_key="test",
            sample_rate=16_000,
            send_chunk_duration_ms=100,
        )
        connection = _Connection()
        provider.connection = connection  # type: ignore[assignment]
        twenty_ms = bytes(16_000 * 2 * 20 // 1000)

        for _ in range(16):
            await provider.send_audio(twenty_ms)
        await provider.commit_final()

        sizes = [
            len(base64.b64decode(message["audio_base_64"]))
            for message in connection.messages
        ]
        self.assertEqual([3200, 3200, 3200, 640], sizes)
        self.assertEqual(1, connection.commits)

    async def test_voice_boundary_commits_audio_without_waiting_for_partial_text(self) -> None:
        provider = ElevenLabsRealtimeSTT(
            api_key="test",
            sample_rate=16_000,
            reconnect_on_close=True,
        )
        connection = _Connection()
        provider.connection = connection  # type: ignore[assignment]

        await provider.send_audio(bytes(16_000 * 2))
        await provider.commit_final()

        self.assertEqual(connection.commits, 1)

    async def test_silent_keepalive_does_not_count_as_uncommitted_speech(self) -> None:
        provider = ElevenLabsRealtimeSTT(api_key="test", reconnect_on_close=True)
        connection = _Connection()
        provider.connection = connection  # type: ignore[assignment]

        await provider.send_keepalive(1920)
        await provider.commit_final()

        self.assertEqual(len(connection.messages), 1)
        self.assertEqual(connection.commits, 0)
        self.assertEqual(provider.statistics.keepalive_blocks, 1)
        self.assertEqual(provider.statistics.sent_blocks, 1)


class ElevenLabsConfidenceTests(unittest.TestCase):
    def test_timestamped_transcript_preserves_and_logs_word_logprobs(self) -> None:
        received = []
        provider = ElevenLabsRealtimeSTT(
            api_key="test",
            include_timestamps=True,
            on_final=received.append,
        )

        with self.assertLogs("providers.elevenlabs_stt", level="DEBUG") as captured:
            provider._handle_committed_transcript_with_timestamps(
                {
                    "text": "Hello world",
                    "words": [
                        {
                            "text": "Hello",
                            "type": "word",
                            "start": 0.1,
                            "end": 0.4,
                            "logprob": -0.1,
                        },
                        {"text": " ", "type": "spacing", "start": 0.4, "end": 0.4},
                        {
                            "text": "world",
                            "type": "word",
                            "start": 0.4,
                            "end": 0.8,
                            "logprob": -0.3,
                        },
                    ],
                }
            )

        self.assertEqual(1, len(received))
        transcript = received[0]
        self.assertEqual(0.1, transcript.start_seconds)
        self.assertEqual(0.8, transcript.end_seconds)
        self.assertEqual(-0.2, transcript.average_logprob)
        self.assertEqual(-0.3, transcript.minimum_logprob)
        self.assertEqual(["Hello", "world"], [word.text for word in transcript.words])
        messages = "\n".join(captured.output)
        self.assertIn("event=stt_confidence", messages)
        self.assertIn("average_logprob=-0.2000", messages)
        self.assertIn('"logprob":-0.3', messages)


if __name__ == "__main__":
    unittest.main()
