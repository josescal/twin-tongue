"""Process-friendly CSV metrics output."""

import csv
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock


METRIC_FIELDS = (
    "timestamp",
    "pipeline",
    "mode",
    "capture_blocks",
    "capture_dropped_blocks",
    "capture_overflows",
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
            write_header = not daily_path.exists() or daily_path.stat().st_size == 0
            with daily_path.open("a", newline="", encoding="utf-8") as output:
                writer = csv.DictWriter(output, fieldnames=METRIC_FIELDS)
                if write_header:
                    writer.writeheader()
                writer.writerow(row)

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
