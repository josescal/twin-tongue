"""Shared contracts for streaming speech-to-speech translation engines."""

from dataclasses import dataclass


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

    def input_duration_seconds(self, sample_rate: int) -> float:
        return self.input_pcm_bytes / (sample_rate * 2)

    def output_duration_seconds(self, sample_rate: int) -> float:
        return self.output_pcm_bytes / (sample_rate * 2)
