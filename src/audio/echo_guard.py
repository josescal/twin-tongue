"""Reference-driven echo guard shared by the two audio directions."""

from array import array
from collections import deque
from dataclasses import dataclass
import math
from threading import Lock
import time


@dataclass
class EchoGuardStatistics:
    reference_blocks: int = 0
    processed_blocks: int = 0
    suppressed_blocks: int = 0
    near_end_override_blocks: int = 0
    correlated_echo_blocks: int = 0
    maximum_reference_correlation: float = 0.0


class EchoReferenceBus:
    """Publish recent far-end activity for the local microphone pipeline."""

    def __init__(self, reference_activity_dbfs: float = -48.0) -> None:
        self.reference_activity_dbfs = reference_activity_dbfs
        self._lock = Lock()
        self._last_active_at: float | None = None
        self._last_rms_dbfs = -96.0
        self.reference_blocks = 0
        self._history: deque[tuple[float, float]] = deque()

    def observe(self, pcm16_mono: bytes, observed_at: float | None = None) -> None:
        rms_dbfs = pcm16_rms_dbfs(pcm16_mono)
        now = time.monotonic() if observed_at is None else observed_at
        with self._lock:
            self.reference_blocks += 1
            self._last_rms_dbfs = rms_dbfs
            self._history.append((now, rms_dbfs))
            history_cutoff = now - 3.0
            while self._history and self._history[0][0] < history_cutoff:
                self._history.popleft()
            if rms_dbfs >= self.reference_activity_dbfs:
                self._last_active_at = now

    def snapshot(self) -> tuple[float | None, float]:
        with self._lock:
            return self._last_active_at, self._last_rms_dbfs

    def history_snapshot(self) -> tuple[tuple[float, float], ...]:
        with self._lock:
            return tuple(self._history)


class ReferenceEchoGuard:
    """Suppress microphone audio only when it correlates with far-end playback."""

    def __init__(
        self,
        reference: EchoReferenceBus,
        *,
        enabled: bool = True,
        near_end_override_dbfs: float = -26.0,
        post_reference_override_dbfs: float = -38.0,
        near_end_hold_ms: float = 800.0,
        hangover_ms: float = 350.0,
        correlation_window_ms: float = 200.0,
        reference_delay_max_ms: float = 500.0,
        correlation_threshold: float = 0.72,
        block_duration_ms: float = 20.0,
    ) -> None:
        self.reference = reference
        self.enabled = enabled
        self.near_end_override_dbfs = near_end_override_dbfs
        self.post_reference_override_dbfs = post_reference_override_dbfs
        self.near_end_hold_seconds = near_end_hold_ms / 1000.0
        self.hangover_seconds = hangover_ms / 1000.0
        self.correlation_window_seconds = correlation_window_ms / 1000.0
        self.reference_delay_max_seconds = reference_delay_max_ms / 1000.0
        self.correlation_threshold = correlation_threshold
        self.block_duration_seconds = block_duration_ms / 1000.0
        self._correlation_window_blocks = (
            max(2, math.ceil(correlation_window_ms / block_duration_ms))
            if correlation_window_ms > 0
            else 1
        )
        self._pending_blocks: deque[tuple[bytes, float, float]] = deque()
        self._last_near_end_at: float | None = None
        self.statistics = EchoGuardStatistics()

    def process(self, pcm16_mono: bytes, now: float | None = None) -> bytes:
        self.statistics.processed_blocks += 1
        self.statistics.reference_blocks = self.reference.reference_blocks
        if not self.enabled or not pcm16_mono:
            return pcm16_mono
        current = time.monotonic() if now is None else now
        microphone_rms_dbfs = pcm16_rms_dbfs(pcm16_mono)
        correlation_mode = self._correlation_window_blocks > 1
        window_near_end_active = False
        if correlation_mode:
            self._pending_blocks.append(
                (pcm16_mono, current, microphone_rms_dbfs)
            )
            if len(self._pending_blocks) < self._correlation_window_blocks:
                return bytes(len(pcm16_mono))
            window_near_end_active = (
                sum(
                    level >= self.post_reference_override_dbfs
                    for _, _, level in self._pending_blocks
                )
                >= 3
            )
            correlation = self._best_reference_correlation(
                tuple(self._pending_blocks)
            )
            pcm16_mono, current, microphone_rms_dbfs = (
                self._pending_blocks.popleft()
            )
            if correlation is not None:
                self.statistics.maximum_reference_correlation = max(
                    self.statistics.maximum_reference_correlation,
                    correlation,
                )
                if correlation >= self.correlation_threshold:
                    self.statistics.correlated_echo_blocks += 1
                    self.statistics.suppressed_blocks += 1
                    return bytes(len(pcm16_mono))
            # Correlation mode is an echo detector, not a noise gate. A weak
            # near-end voice can legitimately be below the fixed level
            # overrides (for example, an analogue jack with low gain). If it
            # does not correlate with far-end playback, preserve it and let
            # the downstream VAD decide whether it is speech.
            if window_near_end_active:
                self._last_near_end_at = current
                self.statistics.near_end_override_blocks += 1
            elif (
                self._last_near_end_at is not None
                and current - self._last_near_end_at
                <= self.near_end_hold_seconds
            ):
                self.statistics.near_end_override_blocks += 1
            return pcm16_mono

        last_reference_at, reference_rms_dbfs = self.reference.snapshot()
        effective_reference_delay = (
            self.reference_delay_max_seconds
            if correlation_mode
            else 0.0
        )
        reference_active = (
            last_reference_at is not None
            and current - last_reference_at
            <= self.hangover_seconds + effective_reference_delay
        )
        if not reference_active:
            return pcm16_mono

        reference_currently_active = (
            reference_rms_dbfs >= self.reference.reference_activity_dbfs
        )
        override_dbfs = (
            self.near_end_override_dbfs
            if reference_currently_active
            else self.post_reference_override_dbfs
        )
        if microphone_rms_dbfs >= override_dbfs:
            self._last_near_end_at = current
            self.statistics.near_end_override_blocks += 1
            return pcm16_mono
        if (
            self._last_near_end_at is not None
            and current - self._last_near_end_at <= self.near_end_hold_seconds
        ):
            self.statistics.near_end_override_blocks += 1
            return pcm16_mono
        self.statistics.suppressed_blocks += 1
        return bytes(len(pcm16_mono))

    def _best_reference_correlation(
        self,
        microphone_window: tuple[tuple[bytes, float, float], ...],
    ) -> float | None:
        reference_history = self.reference.history_snapshot()
        if len(reference_history) < len(microphone_window):
            return None
        microphone_levels = [item[2] for item in microphone_window]
        if _standard_deviation(microphone_levels) < 2.0:
            return None

        best: float | None = None
        delay_steps = max(
            0,
            round(self.reference_delay_max_seconds / self.block_duration_seconds),
        )
        tolerance = self.block_duration_seconds * 0.75
        for delay_step in range(delay_steps + 1):
            delay = delay_step * self.block_duration_seconds
            reference_levels: list[float] = []
            matched_microphone_levels: list[float] = []
            reference_index = 0
            for _, microphone_at, microphone_level in microphone_window:
                target_at = microphone_at - delay
                while (
                    reference_index + 1 < len(reference_history)
                    and abs(reference_history[reference_index + 1][0] - target_at)
                    <= abs(reference_history[reference_index][0] - target_at)
                ):
                    reference_index += 1
                reference_at, reference_level = reference_history[reference_index]
                if abs(reference_at - target_at) <= tolerance:
                    matched_microphone_levels.append(microphone_level)
                    reference_levels.append(reference_level)
            if len(reference_levels) < max(6, len(microphone_window) - 2):
                continue
            if (
                max(reference_levels) < self.reference.reference_activity_dbfs
                or _standard_deviation(reference_levels) < 2.0
            ):
                continue
            correlation = _pearson_correlation(
                matched_microphone_levels,
                reference_levels,
            )
            if best is None or correlation > best:
                best = correlation
        return best


def pcm16_rms_dbfs(pcm16: bytes) -> float:
    if not pcm16:
        return -96.0
    samples = array("h")
    samples.frombytes(pcm16)
    if not samples:
        return -96.0
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    if rms <= 0:
        return -96.0
    return max(-96.0, 20.0 * math.log10(rms / 32_768.0))


def _standard_deviation(values: list[float]) -> float:
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))


def _pearson_correlation(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum(
        (left_value - left_mean) * (right_value - right_mean)
        for left_value, right_value in zip(left, right, strict=True)
    )
    denominator = math.sqrt(
        sum((value - left_mean) ** 2 for value in left)
        * sum((value - right_mean) ** 2 for value in right)
    )
    return numerator / denominator if denominator > 0 else 0.0
