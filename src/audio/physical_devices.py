"""Runtime selection and persistence for the agent's physical audio endpoints."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Literal

from audio.portaudio import AudioDeviceError, get_default_device
from audio.windows_audio import CoreAudioEndpoint

SelectionDirection = Literal["input", "output"]
VOICE_GENDERS = frozenset({"male", "female"})


@dataclass(frozen=True)
class PhysicalDevice:
    stable_id: str
    name: str
    portaudio_index: int
    direction: SelectionDirection
    default_communications: bool = False


class PhysicalDeviceSelection:
    """Resolve automatic physical endpoints and persist the voice preference."""

    def __init__(self, preference_path: Path, default_voice_gender: str = "male") -> None:
        if default_voice_gender not in VOICE_GENDERS:
            raise ValueError("Default voice gender must be 'male' or 'female'.")
        self.preference_path = preference_path
        self._default_voice_gender = default_voice_gender
        self._voice_gender = self._load_voice_gender()
        self._save_preferences()
        self._devices: dict[SelectionDirection, list[PhysicalDevice]] = {
            "input": [], "output": []
        }
        self._active: dict[SelectionDirection, PhysicalDevice | None] = {
            "input": None, "output": None
        }

    def update(self, devices: list[PhysicalDevice]) -> tuple[SelectionDirection, ...]:
        """Replace discovered endpoints and return directions whose state changed."""
        changed_directions: list[SelectionDirection] = []
        for direction in ("input", "output"):
            old_signature = self._direction_signature(direction)
            self._devices[direction] = [item for item in devices if item.direction == direction]
            self._resolve_active(direction)
            if old_signature != self._direction_signature(direction):
                changed_directions.append(direction)
        return tuple(changed_directions)

    def active_index(self, direction: SelectionDirection) -> int:
        self._validate_direction(direction)
        active = self._active[direction]
        if active is None:
            raise AudioDeviceError(f"No physical {direction} audio device is available.")
        return active.portaudio_index

    @property
    def voice_gender(self) -> str:
        return self._voice_gender

    def set_voice_gender(self, gender: str) -> None:
        if gender not in VOICE_GENDERS:
            raise ValueError("Voice gender must be 'male' or 'female'.")
        if self._voice_gender == gender:
            return
        self._voice_gender = gender
        self._save_preferences()

    def snapshot(self) -> dict[str, object]:
        return {
            direction: self._snapshot_direction(direction)
            for direction in ("input", "output")
        }

    def _resolve_active(self, direction: SelectionDirection) -> None:
        devices = self._devices[direction]
        self._active[direction] = select_automatic_physical_device(
            devices, direction, required=False
        )

    def _snapshot_direction(self, direction: SelectionDirection) -> dict[str, object]:
        active = self._active[direction]
        return {
            "active_id": active.stable_id if active else None,
            "active_name": active.name if active else None,
        }

    def _direction_signature(self, direction: SelectionDirection) -> tuple[object, ...]:
        active = self._active[direction]
        return (
            tuple((item.stable_id, item.name, item.portaudio_index, item.default_communications) for item in self._devices[direction]),
            active.stable_id if active else None,
            active.portaudio_index if active else None,
        )

    def _load_voice_gender(self) -> str:
        try:
            payload = json.loads(self.preference_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return self._default_voice_gender
        voice_gender = (
            payload.get("voice_gender", self._default_voice_gender)
            if isinstance(payload, dict)
            else self._default_voice_gender
        )
        if voice_gender not in VOICE_GENDERS:
            voice_gender = self._default_voice_gender
        return voice_gender

    def _save_preferences(self) -> None:
        payload = {"voice_gender": self._voice_gender}
        self.preference_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.preference_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.preference_path)

    @staticmethod
    def _validate_direction(direction: str) -> None:
        if direction not in {"input", "output"}:
            raise ValueError(f"Unknown physical audio direction '{direction}'.")


def discover_physical_devices(
    devices: list[dict[str, Any]],
    host_apis: list[dict[str, Any]],
    core_endpoints: list[CoreAudioEndpoint],
) -> list[PhysicalDevice]:
    """Combine Core Audio endpoint identity/default role with PortAudio indexes."""
    result: list[PhysicalDevice] = []
    for index, device in enumerate(devices):
        host_api = str(host_apis[int(device["hostapi"])]["name"])
        if "wasapi" not in host_api.casefold() or _is_virtual(str(device["name"])):
            continue
        for direction, channel_key in (("input", "max_input_channels"), ("output", "max_output_channels")):
            if int(device[channel_key]) <= 0:
                continue
            endpoint = _match_core_endpoint(str(device["name"]), direction, core_endpoints)
            stable_id = (
                f"coreaudio:{endpoint.endpoint_id}"
                if endpoint is not None
                else f"portaudio:wasapi:{direction}:{_normalize_name(str(device['name']))}"
            )
            default = endpoint.default_communications if endpoint is not None else False
            result.append(
                PhysicalDevice(
                    stable_id=stable_id,
                    name=_friendly_name(endpoint.name if endpoint else str(device["name"]), direction),
                    portaudio_index=index,
                    direction=direction,
                    default_communications=default,
                )
            )
    for direction in ("input", "output"):
        directional = [item for item in result if item.direction == direction]
        if directional and not any(item.default_communications for item in directional):
            try:
                default_index = get_default_device(direction)
            except AudioDeviceError:
                continue
            result = [
                PhysicalDevice(item.stable_id, item.name, item.portaudio_index, item.direction, item.portaudio_index == default_index)
                if item.direction == direction else item
                for item in result
            ]
    return _deduplicate(result)


def select_automatic_physical_device(
    devices: list[PhysicalDevice],
    direction: SelectionDirection,
    *,
    required: bool = True,
) -> PhysicalDevice | None:
    """Select the communications default, matching automatic application mode."""
    directional = [item for item in devices if item.direction == direction]
    selected = next(
        (item for item in directional if item.default_communications),
        directional[0] if directional else None,
    )
    if selected is None and required:
        raise AudioDeviceError(f"No physical WASAPI {direction} device is available.")
    return selected


def _match_core_endpoint(name: str, direction: str, endpoints: list[CoreAudioEndpoint]) -> CoreAudioEndpoint | None:
    needle = _normalize_name(name)
    candidates = [item for item in endpoints if item.direction == direction]
    exact = next((item for item in candidates if _normalize_name(item.name) == needle), None)
    if exact:
        return exact
    return next(
        (item for item in candidates if needle in _normalize_name(item.name) or _normalize_name(item.name) in needle),
        None,
    )


def _normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _friendly_name(value: str, direction: str) -> str:
    name = " ".join(value.split())
    name = re.sub(r"\s*\((?:Windows )?WASAPI\)\s*$", "", name, flags=re.I)
    if direction == "input" and re.match(r"^(microphone|micrófono)( array)?\s*\(", name, re.I):
        detail = name[name.find("(") + 1 :].rstrip(")")
        return f"Micrófono integrado — {detail}"
    if direction == "output" and re.match(r"^(speakers|altavoces)\s*\(", name, re.I):
        detail = name[name.find("(") + 1 :].rstrip(")")
        return f"Altavoces del equipo — {detail}"
    return name


def _is_virtual(name: str) -> bool:
    folded = name.casefold()
    return "vb-audio" in folded or "vb-cable" in folded or "virtual cable" in folded


def _deduplicate(devices: list[PhysicalDevice]) -> list[PhysicalDevice]:
    unique: dict[tuple[str, str], PhysicalDevice] = {}
    for item in devices:
        key = (item.direction, item.stable_id)
        current = unique.get(key)
        if current is None or (item.default_communications and not current.default_communications):
            unique[key] = item
    return sorted(unique.values(), key=lambda item: (item.direction, item.name.casefold()))
