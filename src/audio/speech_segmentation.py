"""Adaptive STT segmentation independent of the voice-detection backend."""

from dataclasses import dataclass
import time
from collections.abc import Mapping

from audio.voice_detection import VoiceDetectionResult

@dataclass
class SpeechSegmentationStatistics:
    """Count the boundaries selected by each adaptive rule."""

    silence_boundaries: int = 0
    punctuation_boundaries: int = 0
    short_pause_boundaries: int = 0
    maximum_duration_boundaries: int = 0


@dataclass(frozen=True)
class SpeechSegmentationDecision:
    """Describe whether the audio accumulated in STT should be committed."""

    should_commit: bool = False
    reason: str | None = None


class AdaptiveSpeechSegmenter:
    """Select stable STT boundaries while allowing VAD to remain active."""

    def __init__(
        self,
        *,
        frame_duration_ms: float,
        minimum_segment_duration_ms: float,
        preferred_segment_duration_ms: float,
        short_pause_duration_ms: float,
        partial_boundary_stability_ms: float,
        maximum_segment_duration_ms: float,
    ) -> None:
        settings = {
            "minimum_segment_duration_ms": minimum_segment_duration_ms,
            "preferred_segment_duration_ms": preferred_segment_duration_ms,
            "short_pause_duration_ms": short_pause_duration_ms,
            "partial_boundary_stability_ms": partial_boundary_stability_ms,
            "maximum_segment_duration_ms": maximum_segment_duration_ms,
        }
        validate_speech_segmentation_settings(settings)
        if frame_duration_ms <= 0:
            raise ValueError("Speech segmentation frame duration must be greater than zero.")
        self.frame_duration_ms = frame_duration_ms
        self.minimum_segment_duration_ms = minimum_segment_duration_ms
        self.preferred_segment_duration_ms = preferred_segment_duration_ms
        self.short_pause_duration_ms = short_pause_duration_ms
        self.partial_boundary_stability_ms = partial_boundary_stability_ms
        self.maximum_segment_duration_ms = maximum_segment_duration_ms
        self.statistics = SpeechSegmentationStatistics()
        self._segment_duration_ms = 0.0
        self._has_speech_since_commit = False
        self._partial_text = ""
        self._partial_updated_at: float | None = None

    @property
    def segment_duration_ms(self) -> float:
        return self._segment_duration_ms

    def observe_partial(self, text: str, *, now: float | None = None) -> None:
        """Record the latest provisional transcript and when it last changed."""
        partial = text.strip()
        if not partial or partial == self._partial_text:
            return
        self._partial_text = partial
        self._partial_updated_at = time.monotonic() if now is None else now

    def process(
        self,
        detection: VoiceDetectionResult,
        *,
        now: float | None = None,
    ) -> SpeechSegmentationDecision:
        """Evaluate one VAD result after its forwarded PCM has been sent to STT."""
        current_time = time.monotonic() if now is None else now
        if detection.speech_started:
            self._reset_segment()

        self._segment_duration_ms += (
            len(detection.forwarded_blocks) * self.frame_duration_ms
        )
        if (detection.active or detection.speech_started) and detection.silence_duration_ms <= 0:
            self._has_speech_since_commit = True

        reason: str | None = None
        if detection.speech_ended:
            if self._has_speech_since_commit:
                reason = "silence"
        elif detection.active:
            stable_partial = self._partial_is_stable_boundary(current_time)
            if (
                self._segment_duration_ms >= self.minimum_segment_duration_ms
                and stable_partial
            ):
                reason = "punctuation"
            elif (
                self._segment_duration_ms >= self.preferred_segment_duration_ms
                and detection.silence_duration_ms >= self.short_pause_duration_ms
            ):
                reason = "short_pause"
            elif self._segment_duration_ms >= self.maximum_segment_duration_ms:
                reason = "maximum_duration"

        if reason is None:
            if detection.speech_ended:
                self._reset_segment()
            return SpeechSegmentationDecision()

        self._record_boundary(reason)
        self._reset_segment()
        return SpeechSegmentationDecision(True, reason)

    def reset(self) -> None:
        """Clear the active segment while preserving aggregate statistics."""
        self._reset_segment()

    def _partial_is_stable_boundary(self, now: float) -> bool:
        if self._partial_updated_at is None or not _ends_at_linguistic_boundary(
            self._partial_text
        ):
            return False
        stable_ms = (now - self._partial_updated_at) * 1000
        return stable_ms >= self.partial_boundary_stability_ms

    def _reset_segment(self) -> None:
        self._segment_duration_ms = 0.0
        self._has_speech_since_commit = False
        self._partial_text = ""
        self._partial_updated_at = None

    def _record_boundary(self, reason: str) -> None:
        if reason == "silence":
            self.statistics.silence_boundaries += 1
        elif reason == "punctuation":
            self.statistics.punctuation_boundaries += 1
        elif reason == "short_pause":
            self.statistics.short_pause_boundaries += 1
        elif reason == "maximum_duration":
            self.statistics.maximum_duration_boundaries += 1


def create_speech_segmenter(
    settings: Mapping[str, object],
    *,
    frame_duration_ms: float,
) -> AdaptiveSpeechSegmenter:
    """Build the configured adaptive segmenter."""
    validate_speech_segmentation_settings(settings)
    return AdaptiveSpeechSegmenter(
        frame_duration_ms=frame_duration_ms,
        minimum_segment_duration_ms=float(settings["minimum_segment_duration_ms"]),
        preferred_segment_duration_ms=float(settings["preferred_segment_duration_ms"]),
        short_pause_duration_ms=float(settings["short_pause_duration_ms"]),
        partial_boundary_stability_ms=float(
            settings["partial_boundary_stability_ms"]
        ),
        maximum_segment_duration_ms=float(settings["maximum_segment_duration_ms"]),
    )


def validate_speech_segmentation_settings(
    settings: Mapping[str, object],
    *,
    voice_end_silence_duration_ms: float | None = None,
) -> None:
    """Validate adaptive boundary timings and their ordering."""
    keys = (
        "minimum_segment_duration_ms",
        "preferred_segment_duration_ms",
        "short_pause_duration_ms",
        "partial_boundary_stability_ms",
        "maximum_segment_duration_ms",
    )
    values: dict[str, float] = {}
    for key in keys:
        value = settings.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"Speech segmentation {key} must be numeric.")
        values[key] = float(value)
    if values["minimum_segment_duration_ms"] <= 0:
        raise ValueError("Minimum segment duration must be greater than zero.")
    if values["preferred_segment_duration_ms"] < values["minimum_segment_duration_ms"]:
        raise ValueError(
            "Preferred segment duration must be greater than or equal to minimum segment duration."
        )
    if values["maximum_segment_duration_ms"] < values["preferred_segment_duration_ms"]:
        raise ValueError(
            "Maximum segment duration must be greater than or equal to preferred segment duration."
        )
    if values["short_pause_duration_ms"] <= 0:
        raise ValueError("Short pause duration must be greater than zero.")
    if values["partial_boundary_stability_ms"] < 0:
        raise ValueError("Partial boundary stability must not be negative.")
    if (
        voice_end_silence_duration_ms is not None
        and values["short_pause_duration_ms"] >= voice_end_silence_duration_ms
    ):
        raise ValueError(
            "Short pause duration must be shorter than voice detection minimum silence duration."
        )


def _ends_at_linguistic_boundary(text: str) -> bool:
    stripped = text.rstrip()
    if not stripped or stripped.endswith(("...", "…")):
        return False
    return stripped[-1] in ".?!;:"
