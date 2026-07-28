"""Runtime selection and persistence for the agent's physical audio endpoints."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Any, Literal

from audio.portaudio import AudioDeviceError, get_default_device
from audio.windows_audio import CoreAudioEndpoint
from preferences import UserPreferences

SelectionDirection = Literal["input", "output"]
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PhysicalDevice:
    stable_id: str
    name: str
    portaudio_index: int
    direction: SelectionDirection
    default_communications: bool = False


class PhysicalDeviceSelection:
    """Resolve automatic physical endpoints and persist user preferences."""

    def __init__(
        self,
        preferences: UserPreferences,
    ) -> None:
        self._preferences = preferences
        self._preferred_ids: dict[SelectionDirection, str | None] = {
            direction: preferences.audio_device(direction)["stable_id"]
            for direction in ("input", "output")
        }
        self._preferred_names: dict[SelectionDirection, str | None] = {
            direction: preferences.audio_device(direction)["name"]
            for direction in ("input", "output")
        }
        self._devices: dict[SelectionDirection, list[PhysicalDevice]] = {
            "input": [], "output": []
        }
        self._active: dict[SelectionDirection, PhysicalDevice | None] = {
            "input": None, "output": None
        }
        self._unavailable_preferences: set[SelectionDirection] = set()
        self._unhealthy_ids: dict[SelectionDirection, set[str]] = {
            "input": set(), "output": set()
        }
        self._last_working_ids: dict[SelectionDirection, str | None] = {
            direction: preferences.last_working_audio_device(direction)["stable_id"]
            for direction in ("input", "output")
        }

    def update(self, devices: list[PhysicalDevice]) -> tuple[SelectionDirection, ...]:
        """Replace discovered endpoints and return directions whose state changed."""
        changed_directions: list[SelectionDirection] = []
        for direction in ("input", "output"):
            old_signature = self._direction_signature(direction)
            self._devices[direction] = [item for item in devices if item.direction == direction]
            self._resolve_active(direction)
            self._report_preference_status(direction)
            if old_signature != self._direction_signature(direction):
                changed_directions.append(direction)
        return tuple(changed_directions)

    def active_index(self, direction: SelectionDirection) -> int:
        self._validate_direction(direction)
        active = self._active[direction]
        if active is None:
            raise AudioDeviceError(f"No physical {direction} audio device is available.")
        return active.portaudio_index

    def set_device(self, direction: SelectionDirection, selection: str) -> bool:
        """Select one available device by stable ID, or restore automatic mode."""
        self._validate_direction(direction)
        old_signature = self._direction_signature(direction)
        if selection == "automatic":
            self._preferred_ids[direction] = None
            self._preferred_names[direction] = None
        else:
            selected = next(
                (item for item in self._devices[direction] if item.stable_id == selection),
                None,
            )
            if selected is None:
                raise ValueError(
                    f"Unknown or unavailable physical {direction} device '{selection}'."
                )
            self._preferred_ids[direction] = selected.stable_id
            self._preferred_names[direction] = selected.name
            self._unhealthy_ids[direction].discard(selected.stable_id)
        self._resolve_active(direction)
        self._preferences.set_audio_device(
            direction,
            self._preferred_ids[direction],
            self._preferred_names[direction],
        )
        return old_signature != self._direction_signature(direction)

    def mark_healthy(self, direction: SelectionDirection, portaudio_index: int) -> bool:
        """Remember a working endpoint and remove its temporary quarantine."""
        self._validate_direction(direction)
        device = self._find_by_index(direction, portaudio_index)
        old_signature = self._direction_signature(direction)
        self._unhealthy_ids[direction].discard(device.stable_id)
        self._last_working_ids[direction] = device.stable_id
        self._preferences.set_last_working_audio_device(
            direction, device.stable_id, device.name
        )
        self._resolve_active(direction)
        return old_signature != self._direction_signature(direction)

    def mark_unhealthy(
        self, direction: SelectionDirection, portaudio_index: int
    ) -> bool:
        """Quarantine a failed endpoint until it is selected again or disappears."""
        self._validate_direction(direction)
        device = self._find_by_index(direction, portaudio_index)
        old_signature = self._direction_signature(direction)
        replace_manual_preference = (
            self._preferred_ids[direction] == device.stable_id
        )
        self._unhealthy_ids[direction].add(device.stable_id)
        self._resolve_active(direction)
        replacement = self._active[direction]
        if replace_manual_preference and replacement is not None:
            self._preferred_ids[direction] = replacement.stable_id
            self._preferred_names[direction] = replacement.name
            self._preferences.set_audio_device(
                direction,
                replacement.stable_id,
                replacement.name,
            )
            LOGGER.warning(
                "event=preferred_audio_device_replaced direction=%s "
                "discarded=%s replacement=%s",
                direction,
                device.name,
                replacement.name,
            )
        return old_signature != self._direction_signature(direction)

    def snapshot(self) -> dict[str, object]:
        return {
            direction: self._snapshot_direction(direction)
            for direction in ("input", "output")
        }

    def _resolve_active(self, direction: SelectionDirection) -> None:
        devices = self._devices[direction]
        available = [
            item for item in devices
            if item.stable_id not in self._unhealthy_ids[direction]
        ]
        preferred_id = self._preferred_ids[direction]
        preferred = next(
            (item for item in available if item.stable_id == preferred_id),
            None,
        )
        last_working = next(
            (
                item for item in available
                if item.stable_id == self._last_working_ids[direction]
            ),
            None,
        )
        communications_default = next(
            (item for item in available if item.default_communications),
            None,
        )
        first_available = available[0] if available else None
        if preferred_id is not None:
            self._active[direction] = (
                preferred or last_working or communications_default or first_available
            )
        else:
            self._active[direction] = (
                communications_default or last_working or first_available
            )

    def _snapshot_direction(self, direction: SelectionDirection) -> dict[str, object]:
        active = self._active[direction]
        preferred_id = self._preferred_ids[direction]
        expected = next(
            (
                item
                for item in self._devices[direction]
                if item.stable_id == preferred_id
            ),
            None,
        )
        if preferred_id is None:
            expected = next(
                (
                    item
                    for item in self._devices[direction]
                    if item.default_communications
                ),
                None,
            )
        fallback_active = bool(
            active
            and expected
            and expected.stable_id in self._unhealthy_ids[direction]
            and active.stable_id != expected.stable_id
        )
        return {
            "selection": preferred_id or "automatic",
            "mode": "manual" if preferred_id else "automatic",
            "preferred_name": self._preferred_names[direction],
            "preferred_available": preferred_id is None or any(
                item.stable_id == preferred_id for item in self._devices[direction]
            ),
            "active_id": active.stable_id if active else None,
            "active_name": active.name if active else None,
            "fallback_active": fallback_active,
            "discarded_devices": [
                {"id": item.stable_id, "name": item.name}
                for item in self._devices[direction]
                if item.stable_id in self._unhealthy_ids[direction]
            ],
            "last_working_id": self._last_working_ids[direction],
            "devices": [
                {
                    "id": item.stable_id,
                    "name": item.name,
                    "default_communications": item.default_communications,
                    "healthy": item.stable_id not in self._unhealthy_ids[direction],
                }
                for item in self._devices[direction]
            ],
        }

    def _report_preference_status(self, direction: SelectionDirection) -> None:
        preferred_id = self._preferred_ids[direction]
        preferred_available = preferred_id is not None and any(
            item.stable_id == preferred_id for item in self._devices[direction]
        )
        if preferred_id is not None and not preferred_available:
            if direction not in self._unavailable_preferences:
                active = self._active[direction]
                LOGGER.warning(
                    "event=preferred_audio_device_unavailable direction=%s "
                    "preferred=%s fallback=%s",
                    direction,
                    self._preferred_names[direction] or preferred_id,
                    active.name if active else "none",
                )
            self._unavailable_preferences.add(direction)
            return
        if preferred_available and direction in self._unavailable_preferences:
            LOGGER.info(
                "event=preferred_audio_device_recovered direction=%s device=%s",
                direction,
                self._preferred_names[direction] or preferred_id,
            )
        self._unavailable_preferences.discard(direction)

    def _direction_signature(self, direction: SelectionDirection) -> tuple[object, ...]:
        active = self._active[direction]
        return (
            tuple((item.stable_id, item.name, item.portaudio_index, item.default_communications) for item in self._devices[direction]),
            self._preferred_ids[direction],
            active.stable_id if active else None,
            active.portaudio_index if active else None,
            tuple(sorted(self._unhealthy_ids[direction])),
            self._last_working_ids[direction],
        )

    def _find_by_index(
        self, direction: SelectionDirection, portaudio_index: int
    ) -> PhysicalDevice:
        device = next(
            (
                item for item in self._devices[direction]
                if item.portaudio_index == portaudio_index
            ),
            None,
        )
        if device is None:
            raise AudioDeviceError(
                f"Physical {direction} device {portaudio_index} is unavailable."
            )
        return device

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
