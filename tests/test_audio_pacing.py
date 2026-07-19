"""Tests for realtime PCM send pacing."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from audio.pacing import RealtimeAudioPacer


class RealtimeAudioPacerTests(unittest.IsolatedAsyncioTestCase):
    async def test_continuous_mode_waits_for_realtime_interval(self) -> None:
        pacer = RealtimeAudioPacer(frame_duration_ms=20, max_burst_ms=0)
        sleep = AsyncMock()
        with (
            patch("audio.pacing.time.monotonic", side_effect=(10.0, 10.0, 10.02)),
            patch("audio.pacing.asyncio.sleep", sleep),
        ):
            await pacer.wait()
            await pacer.wait()

        sleep.assert_awaited_once()
        self.assertAlmostEqual(sleep.await_args.args[0], 0.02)

    async def test_burst_mode_allows_bounded_catch_up(self) -> None:
        pacer = RealtimeAudioPacer(frame_duration_ms=20, max_burst_ms=100)
        sleep = AsyncMock()
        with (
            patch(
                "audio.pacing.time.monotonic",
                side_effect=(10.0, 11.0, 11.0, 11.0, 11.0, 11.0, 11.0, 11.0, 11.02),
            ),
            patch("audio.pacing.asyncio.sleep", sleep),
        ):
            for _ in range(8):
                await pacer.wait()

        sleep.assert_awaited_once()
        self.assertAlmostEqual(sleep.await_args.args[0], 0.02)

    def test_rejects_negative_burst(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be negative"):
            RealtimeAudioPacer(frame_duration_ms=20, max_burst_ms=-1)


if __name__ == "__main__":
    unittest.main()
