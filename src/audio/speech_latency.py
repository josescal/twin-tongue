"""Voice-onset latency tracking across a streaming audio pipeline."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from threading import Lock
import time

import numpy as np


STAGES = ("accepted", "sent", "received", "played")


@dataclass(frozen=True)
class SpeechLatencyMeasurement:
    """Latency values for one translated speech intervention."""

    utterance_id: int
    accepted_to_sent_ms: float
    sent_to_received_ms: float
    received_to_played_ms: float
    accepted_to_received_ms: float
    accepted_to_played_ms: float

    def as_dict(self) -> dict[str, int | float]:
        return {
            "utterance_id": self.utterance_id,
            "accepted_to_sent_ms": round(self.accepted_to_sent_ms, 3),
            "sent_to_received_ms": round(self.sent_to_received_ms, 3),
            "received_to_played_ms": round(self.received_to_played_ms, 3),
            "accepted_to_received_ms": round(
                self.accepted_to_received_ms, 3
            ),
            "accepted_to_played_ms": round(self.accepted_to_played_ms, 3),
        }


class _VoiceOnsetDetector:
    def __init__(
        self,
        *,
        threshold_dbfs: float,
        release_duration_ms: float,
    ) -> None:
        self.threshold_dbfs = threshold_dbfs
        self.release_seconds = release_duration_ms / 1000
        self.active = False
        self.silence_started_at: float | None = None

    def reset(self) -> None:
        self.active = False
        self.silence_started_at = None

    def observe(self, pcm16: bytes, *, observed_at: float) -> bool:
        if not pcm16 or len(pcm16) % 2:
            return False
        samples = np.frombuffer(pcm16, dtype="<i2").astype(np.float64)
        if samples.size == 0:
            return False
        rms = float(np.sqrt(np.mean(np.square(samples))))
        dbfs = (
            -96.0
            if rms <= 0
            else max(-96.0, 20 * math.log10(rms / 32768.0))
        )
        if dbfs >= self.threshold_dbfs:
            onset = not self.active
            self.active = True
            self.silence_started_at = None
            return onset
        if not self.active:
            return False
        if self.silence_started_at is None:
            self.silence_started_at = observed_at
        elif observed_at - self.silence_started_at >= self.release_seconds:
            self.active = False
            self.silence_started_at = None
        return False


class StreamingSpeechLatencyTracker:
    """Pair sequential voice onsets at accepted, sent, received and played."""

    def __init__(
        self,
        *,
        threshold_dbfs: float = -50.0,
        release_duration_ms: float = 600.0,
        maximum_pending_utterances: int = 20,
        maximum_recent_measurements: int = 20,
    ) -> None:
        if release_duration_ms <= 0:
            raise ValueError("Voice release duration must be greater than zero.")
        if maximum_pending_utterances <= 0:
            raise ValueError("Maximum pending utterances must be greater than zero.")
        self._detectors = {
            stage: _VoiceOnsetDetector(
                threshold_dbfs=threshold_dbfs,
                release_duration_ms=release_duration_ms,
            )
            for stage in STAGES
        }
        self._maximum_pending = maximum_pending_utterances
        self._pending: deque[dict[str, float | int]] = deque()
        self._recent: deque[SpeechLatencyMeasurement] = deque(
            maxlen=maximum_recent_measurements
        )
        self._next_utterance_id = 1
        self._completed_utterances = 0
        self._lock = Lock()

    def reset_stream(self) -> None:
        """Discard incomplete pairings while retaining completed measurements."""
        with self._lock:
            self._pending.clear()
            for detector in self._detectors.values():
                detector.reset()

    def observe(
        self,
        stage: str,
        pcm16: bytes,
        *,
        observed_at: float | None = None,
    ) -> SpeechLatencyMeasurement | None:
        if stage not in self._detectors:
            raise ValueError(f"Unsupported speech latency stage: {stage}")
        at = time.monotonic() if observed_at is None else observed_at
        with self._lock:
            if not self._detectors[stage].observe(pcm16, observed_at=at):
                return None
            if stage == "accepted":
                self._pending.append(
                    {
                        "utterance_id": self._next_utterance_id,
                        "accepted": at,
                    }
                )
                self._next_utterance_id += 1
                while len(self._pending) > self._maximum_pending:
                    self._pending.popleft()
                return None

            utterance = next(
                (
                    item
                    for item in self._pending
                    if stage not in item
                    and self._previous_stages_present(item, stage)
                ),
                None,
            )
            if utterance is None:
                return None
            utterance[stage] = at
            if stage != "played":
                return None

            measurement = self._measurement(utterance)
            self._pending.remove(utterance)
            self._recent.append(measurement)
            self._completed_utterances += 1
            return measurement

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            recent = list(self._recent)
            total_values = [
                measurement.accepted_to_played_ms for measurement in recent
            ]
            return {
                "completed_utterances": self._completed_utterances,
                "pending_utterances": len(self._pending),
                "recent_measurements": [
                    measurement.as_dict() for measurement in recent
                ],
                "last_accepted_to_played_ms": (
                    round(total_values[-1], 3) if total_values else None
                ),
                "average_accepted_to_played_ms": (
                    round(sum(total_values) / len(total_values), 3)
                    if total_values
                    else None
                ),
                "minimum_accepted_to_played_ms": (
                    round(min(total_values), 3) if total_values else None
                ),
                "maximum_accepted_to_played_ms": (
                    round(max(total_values), 3) if total_values else None
                ),
            }

    @staticmethod
    def _previous_stages_present(
        utterance: dict[str, float | int],
        stage: str,
    ) -> bool:
        stage_index = STAGES.index(stage)
        return all(previous in utterance for previous in STAGES[:stage_index])

    @staticmethod
    def _measurement(
        utterance: dict[str, float | int],
    ) -> SpeechLatencyMeasurement:
        accepted = float(utterance["accepted"])
        sent = float(utterance["sent"])
        received = float(utterance["received"])
        played = float(utterance["played"])
        return SpeechLatencyMeasurement(
            utterance_id=int(utterance["utterance_id"]),
            accepted_to_sent_ms=(sent - accepted) * 1000,
            sent_to_received_ms=(received - sent) * 1000,
            received_to_played_ms=(played - received) * 1000,
            accepted_to_received_ms=(received - accepted) * 1000,
            accepted_to_played_ms=(played - accepted) * 1000,
        )
