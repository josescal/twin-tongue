"""Tests for ElevenLabs realtime STT connection and audio lifecycle."""

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


class _Connection:
    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []
        self.commits = 0

    async def send(self, message: dict[str, str]) -> None:
        self.messages.append(message)

    async def commit(self) -> None:
        self.commits += 1


class ElevenLabsKeepaliveTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
