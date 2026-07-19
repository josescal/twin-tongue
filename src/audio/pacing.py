"""Realtime pacing for PCM blocks sent to streaming providers."""

import asyncio
import time


class RealtimeAudioPacer:
    """Prevent queued PCM blocks from being flushed faster than realtime."""

    def __init__(self, frame_duration_ms: float, max_burst_ms: float = 0) -> None:
        if frame_duration_ms <= 0:
            raise ValueError("Pacer frame duration must be greater than zero.")
        if max_burst_ms < 0:
            raise ValueError("Pacer maximum burst must not be negative.")
        self.frame_seconds = frame_duration_ms / 1000
        self.max_burst_seconds = max_burst_ms / 1000
        self._next_send_at: float | None = None

    async def wait(self) -> None:
        """Wait until the next block may be sent, allowing a bounded catch-up burst."""
        now = time.monotonic()
        if self._next_send_at is None:
            self._next_send_at = now
        scheduled_at = max(
            self._next_send_at,
            now - self.max_burst_seconds,
        )
        delay = scheduled_at - now
        if delay > 0:
            await asyncio.sleep(delay)
            now = time.monotonic()
            scheduled_at = max(scheduled_at, now)
        self._next_send_at = scheduled_at + self.frame_seconds

    def reset(self) -> None:
        """Forget accumulated timing after a provider reconnection."""
        self._next_send_at = None
