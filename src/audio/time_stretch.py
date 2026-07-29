"""Streaming, pitch-preserving time-scale compression for mono PCM16 audio."""

from __future__ import annotations

import numpy as np


class StreamingWsola:
    """Compress mono speech in time with waveform-similarity overlap-add.

    The implementation deliberately uses short speech-oriented windows and keeps
    one overlap in memory, so it can accept the 20 ms blocks used by the realtime
    pipeline without accumulating an entire utterance.
    """

    def __init__(
        self,
        sample_rate: int,
        *,
        frame_ms: float = 40.0,
        overlap_ms: float = 20.0,
        search_ms: float = 5.0,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("Sample rate must be greater than zero.")
        self.sample_rate = sample_rate
        self.frame_length = max(4, round(sample_rate * frame_ms / 1000))
        self.overlap_length = max(2, round(sample_rate * overlap_ms / 1000))
        if self.overlap_length * 2 != self.frame_length:
            raise ValueError("WSOLA currently requires a 50% frame overlap.")
        self.synthesis_hop = self.frame_length - self.overlap_length
        self.search_radius = max(1, round(sample_rate * search_ms / 1000))
        self._fade_in = np.linspace(
            0.0, 1.0, self.overlap_length, dtype=np.float32
        )
        self._fade_out = 1.0 - self._fade_in
        self.reset()

    def reset(self) -> None:
        self._input = np.empty(0, dtype=np.float32)
        self._analysis_position = 0
        self._previous_overlap: np.ndarray | None = None

    def process(
        self,
        pcm16: bytes,
        *,
        speed: float = 1.0,
        final: bool = False,
    ) -> list[bytes]:
        """Return zero or more PCM16 chunks, shortening duration by ``speed``."""
        if speed < 1.0 or speed > 1.25:
            raise ValueError("WSOLA speed must be between 1.0 and 1.25.")
        if len(pcm16) % 2:
            raise ValueError("PCM16 audio must contain complete samples.")
        if pcm16:
            incoming = np.frombuffer(pcm16, dtype="<i2").astype(np.float32)
            self._input = np.concatenate((self._input, incoming))

        output: list[bytes] = []
        if self._previous_overlap is None and len(self._input) >= self.frame_length:
            first = self._input[: self.frame_length]
            output.append(self._to_pcm16(first[: self.synthesis_hop]))
            self._previous_overlap = first[self.synthesis_hop :].copy()
            self._analysis_position = round(self.synthesis_hop * speed)

        while self._previous_overlap is not None:
            if (
                not final
                and len(self._input) - self.frame_length
                < self._analysis_position
            ):
                break
            earliest = max(0, self._analysis_position - self.search_radius)
            latest = min(
                len(self._input) - self.frame_length,
                self._analysis_position + self.search_radius,
            )
            if latest < earliest:
                break
            position = self._best_match(earliest, latest)
            frame = self._input[position : position + self.frame_length]
            blended = (
                self._previous_overlap * self._fade_out
                + frame[: self.overlap_length] * self._fade_in
            )
            output.append(self._to_pcm16(blended))
            self._previous_overlap = frame[self.synthesis_hop :].copy()
            # Advance the nominal analysis timeline independently from the
            # correlation correction. Otherwise periodic speech can repeatedly
            # choose an earlier, phase-identical candidate and cancel the
            # requested compression.
            self._analysis_position += round(self.synthesis_hop * speed)
            self._trim_consumed_input()

        if final:
            if self._previous_overlap is not None:
                output.append(self._to_pcm16(self._previous_overlap))
            elif len(self._input):
                output.append(self._to_pcm16(self._input))
            self.reset()
        return output

    def _best_match(self, earliest: int, latest: int) -> int:
        assert self._previous_overlap is not None
        reference = self._previous_overlap
        reference_energy = float(np.dot(reference, reference))
        if reference_energy < 1.0:
            return min(max(self._analysis_position, earliest), latest)
        preferred = min(max(self._analysis_position, earliest), latest)
        best_position = preferred
        best_score = -2.0
        # A four-sample stride is accurate enough for speech alignment and keeps
        # the callback worker inexpensive even at the maximum search radius.
        reference_view = reference[::4]
        reference_norm = float(np.linalg.norm(reference_view)) + 1e-9
        positions = list(range(earliest, latest + 1, 4))
        positions.sort(key=lambda position: abs(position - preferred))
        for position in positions:
            candidate = self._input[
                position : position + self.overlap_length : 4
            ]
            score = float(np.dot(reference_view, candidate)) / (
                reference_norm * (float(np.linalg.norm(candidate)) + 1e-9)
            )
            if score > best_score + 1e-5:
                best_score = score
                best_position = position
        return best_position

    def _trim_consumed_input(self) -> None:
        retain_from = max(0, self._analysis_position - self.search_radius)
        if retain_from < self.frame_length * 4:
            return
        self._input = self._input[retain_from:]
        self._analysis_position -= retain_from

    @staticmethod
    def _to_pcm16(samples: np.ndarray) -> bytes:
        return np.clip(np.rint(samples), -32768, 32767).astype("<i2").tobytes()
