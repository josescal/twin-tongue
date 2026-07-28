"""Shared contracts for streaming speech-to-speech translation engines."""

from dataclasses import dataclass
import math

import numpy as np


@dataclass
class RealtimeTranslationStatistics:
    """Comparable duration, latency, and reliability counters for one session."""

    connections: int = 0
    reconnections: int = 0
    errors: int = 0
    input_pcm_bytes: int = 0
    output_pcm_bytes: int = 0
    input_transcript_characters: int = 0
    output_transcript_characters: int = 0
    first_audio_latency_ms: float | None = None
    total_latency_ms: float | None = None
    input_level_sample_count: int = 0
    input_level_square_sum: float = 0.0
    input_peak_amplitude: int = 0
    output_level_sample_count: int = 0
    output_level_square_sum: float = 0.0
    output_peak_amplitude: int = 0

    def input_duration_seconds(self, sample_rate: int) -> float:
        return self.input_pcm_bytes / (sample_rate * 2)

    def output_duration_seconds(self, sample_rate: int) -> float:
        return self.output_pcm_bytes / (sample_rate * 2)

    def reset_audio_levels(self) -> None:
        """Reset in-memory level measurements for a new connection."""
        self.input_level_sample_count = 0
        self.input_level_square_sum = 0.0
        self.input_peak_amplitude = 0
        self.output_level_sample_count = 0
        self.output_level_square_sum = 0.0
        self.output_peak_amplitude = 0

    def record_input_level(self, pcm16: bytes) -> None:
        self._record_level(pcm16, output=False)

    def record_output_level(self, pcm16: bytes) -> None:
        self._record_level(pcm16, output=True)

    def input_rms_dbfs(self) -> float | None:
        return self._rms_dbfs(
            self.input_level_square_sum,
            self.input_level_sample_count,
        )

    def output_rms_dbfs(self) -> float | None:
        return self._rms_dbfs(
            self.output_level_square_sum,
            self.output_level_sample_count,
        )

    def _record_level(self, pcm16: bytes, *, output: bool) -> None:
        if not pcm16:
            return
        samples = np.frombuffer(pcm16, dtype="<i2").astype(np.float64)
        square_sum = float(np.dot(samples, samples))
        peak = int(np.max(np.abs(samples)))
        if output:
            self.output_level_sample_count += int(samples.size)
            self.output_level_square_sum += square_sum
            self.output_peak_amplitude = max(self.output_peak_amplitude, peak)
        else:
            self.input_level_sample_count += int(samples.size)
            self.input_level_square_sum += square_sum
            self.input_peak_amplitude = max(self.input_peak_amplitude, peak)

    @staticmethod
    def _rms_dbfs(square_sum: float, sample_count: int) -> float | None:
        if sample_count <= 0:
            return None
        rms = math.sqrt(square_sum / sample_count)
        if rms <= 0:
            return float("-inf")
        return 20.0 * math.log10(rms / 32768.0)
