"""Atomic per-call diagnostic metadata."""

from datetime import datetime
import json
from pathlib import Path


class DiagnosticCallManifest:
    def __init__(
        self,
        *,
        enabled: bool,
        directory: Path,
        pipeline_name: str,
        call_id: str,
        devices: dict[str, object],
        formats: dict[str, object],
    ) -> None:
        self.enabled = enabled
        self.directory = directory
        self.pipeline_name = pipeline_name
        self.call_id = call_id
        self.started_at = _now()
        self.devices = devices
        self.formats = formats
        self.events: list[dict[str, object]] = []

    def event(self, name: str, **values: object) -> None:
        if self.enabled:
            self.events.append({"event": name, "at": _now(), **values})

    def close(
        self,
        *,
        tracks: dict[str, list[str]],
        statistics: dict[str, object],
    ) -> Path | None:
        if not self.enabled:
            return None
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / (
            f"{self.pipeline_name}-call-{self.call_id}-manifest.json"
        )
        payload = {
            "schema_version": 1,
            "call_id": self.call_id,
            "pipeline": self.pipeline_name,
            "started_at": self.started_at,
            "closed_at": _now(),
            "devices": self.devices,
            "formats": self.formats,
            "tracks": tracks,
            "events": self.events,
            "statistics": statistics,
        }
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
        return path


def diagnostic_call_id() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")
