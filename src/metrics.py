"""Process-friendly CSV metrics output."""

import csv
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock


METRIC_FIELDS = (
    "timestamp",
    "pipeline",
    "mode",
    "engine",
    "capture_blocks",
    "capture_dropped_blocks",
    "capture_overflows",
    "capture_invalid_blocks",
    "capture_maximum_buffered_blocks",
    "capture_maximum_callback_gap_ms",
    "passthrough_output_dropped_blocks",
    "passthrough_output_underflows",
    "passthrough_output_empty_buffer_events",
    "passthrough_output_invalid_blocks",
    "passthrough_output_maximum_buffered_blocks",
    "passthrough_output_maximum_callback_gap_ms",
    "translated_output_underflows",
    "recording_dropped_blocks",
    "event_loop_maximum_lag_ms",
    "final_transcripts",
    "translations",
    "syntheses",
    "played_segments",
    "dropped_transcripts",
    "dropped_translations",
    "dropped_syntheses",
    "stale_before_translation",
    "stale_before_tts",
    "stale_before_playback",
    "superseded_before_playback",
    "maximum_playback_start_latency_ms",
    "vad_peak_probability",
    "vad_activations",
    "vad_endings",
    "vad_max_inference_ms",
    "segmentation_silence_boundaries",
    "segmentation_punctuation_boundaries",
    "segmentation_short_pause_boundaries",
    "segmentation_maximum_duration_boundaries",
    "realtime_first_audio_latency_ms",
    "realtime_total_latency_ms",
    "realtime_input_audio_duration_ms",
    "realtime_output_audio_duration_ms",
    "realtime_send_queue_buffered_blocks",
    "realtime_send_queue_dropped_blocks",
    "realtime_received_queue_buffered_blocks",
    "realtime_received_queue_buffered_ms",
    "realtime_received_queue_maximum_buffered_blocks",
    "realtime_received_queue_maximum_buffered_ms",
    "realtime_received_queue_dropped_blocks",
    "realtime_received_queue_dropped_ms",
    "realtime_received_queue_discontinuities",
    "realtime_playback_buffered_blocks",
    "realtime_playback_buffered_ms",
    "realtime_playback_maximum_buffered_blocks",
    "realtime_playback_maximum_buffered_ms",
    "realtime_playback_dropped_blocks",
    "realtime_playback_empty_buffer_events",
    "realtime_input_transcript_characters",
    "realtime_output_transcript_characters",
    "realtime_voice_latency_completed_utterances",
    "realtime_voice_latency_pending_utterances",
    "realtime_voice_latency_last_ms",
    "realtime_voice_latency_average_ms",
    "realtime_errors",
    "realtime_reconnections",
)


class CsvMetricsWriter:
    """Append metric snapshots with one stable header across both directions."""

    def __init__(self, path: Path | str, retention_days: int = 14) -> None:
        if isinstance(retention_days, bool) or retention_days < 1:
            raise ValueError("Metric retention must be at least one day.")
        self.path = Path(path)
        self.retention_days = retention_days
        self._lock = Lock()
        self._last_cleanup_date: date | None = None

    def write(self, values: dict[str, object]) -> None:
        now = datetime.now().astimezone()
        row = {field: values.get(field, "") for field in METRIC_FIELDS}
        row["timestamp"] = now.isoformat(timespec="milliseconds")
        with self._lock:
            day = now.date()
            daily_path = self._daily_path(day)
            daily_path.parent.mkdir(parents=True, exist_ok=True)
            if self._last_cleanup_date != day:
                self._remove_expired_files(day)
                self._last_cleanup_date = day
            self._ensure_current_header(daily_path)
            write_header = not daily_path.exists() or daily_path.stat().st_size == 0
            with daily_path.open("a", newline="", encoding="utf-8") as output:
                writer = csv.DictWriter(output, fieldnames=METRIC_FIELDS)
                if write_header:
                    writer.writeheader()
                writer.writerow(row)

    def _ensure_current_header(self, path: Path) -> None:
        """Upgrade an existing daily file when diagnostic fields are added."""
        if not path.exists() or path.stat().st_size == 0:
            return
        with path.open(newline="", encoding="utf-8") as source:
            reader = csv.DictReader(source)
            if tuple(reader.fieldnames or ()) == METRIC_FIELDS:
                return
            rows = list(reader)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=METRIC_FIELDS)
            writer.writeheader()
            for existing in rows:
                writer.writerow(
                    {field: existing.get(field, "") for field in METRIC_FIELDS}
                )
        temporary.replace(path)

    def _daily_path(self, day: date) -> Path:
        suffix = self.path.suffix or ".csv"
        return self.path.with_name(f"{self.path.stem}-{day.isoformat()}{suffix}")

    def _remove_expired_files(self, today: date) -> None:
        cutoff = today - timedelta(days=self.retention_days - 1)
        suffix = self.path.suffix or ".csv"
        prefix = f"{self.path.stem}-"
        for candidate in self.path.parent.glob(f"{self.path.stem}-????-??-??{suffix}"):
            encoded_date = candidate.stem.removeprefix(prefix)
            try:
                file_date = date.fromisoformat(encoded_date)
            except ValueError:
                continue
            if file_date < cutoff:
                candidate.unlink(missing_ok=True)
