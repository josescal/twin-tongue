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
UI_LANGUAGES = frozenset({"en", "es"})
CONVERSATION_LANGUAGES = frozenset({"ca", "en", "es", "fr"})


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
        preference_path: Path,
        default_voice_gender: str = "male",
        default_ui_language: str = "en",
        default_participant_languages: dict[str, str] | None = None,
    ) -> None:
        if default_voice_gender not in VOICE_GENDERS:
            raise ValueError("Default voice gender must be 'male' or 'female'.")
        if default_ui_language not in UI_LANGUAGES:
            raise ValueError("Default UI language must be 'en' or 'es'.")
        self.preference_path = preference_path
        self._default_voice_gender = default_voice_gender
        self._default_ui_language = default_ui_language
        self._default_participant_languages = {
            "agent": "es",
            "remote": "en",
            **(default_participant_languages or {}),
        }
        preferences = self._load_preferences()
        self._voice_gender = preferences["voice_gender"]
        self._ui_language = preferences["ui_language"]
        self._participant_languages = preferences["languages"]
        self._preferred_ids: dict[SelectionDirection, str | None] = {
            "input": preferences["input"]["stable_id"],
            "output": preferences["output"]["stable_id"],
        }
        self._preferred_names: dict[SelectionDirection, str | None] = {
            "input": preferences["input"]["name"],
            "output": preferences["output"]["name"],
        }
        self._devices: dict[SelectionDirection, list[PhysicalDevice]] = {
            "input": [], "output": []
        }
        self._active: dict[SelectionDirection, PhysicalDevice | None] = {
            "input": None, "output": None
        }
        self._save_preferences()

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

    @property
    def ui_language(self) -> str:
        return self._ui_language

    @property
    def participant_languages(self) -> dict[str, str]:
        return dict(self._participant_languages)

    def set_voice_gender(self, gender: str) -> None:
        if gender not in VOICE_GENDERS:
            raise ValueError("Voice gender must be 'male' or 'female'.")
        if self._voice_gender == gender:
            return
        self._voice_gender = gender
        self._save_preferences()

    def set_ui_language(self, language: str) -> None:
        if language not in UI_LANGUAGES:
            raise ValueError("UI language must be 'en' or 'es'.")
        if self._ui_language == language:
            return
        self._ui_language = language
        self._save_preferences()

    def set_participant_language(self, role: str, language: str) -> None:
        if role not in {"agent", "remote"}:
            raise ValueError(f"Unknown participant language role '{role}'.")
        if language not in CONVERSATION_LANGUAGES:
            raise ValueError("Participant language must be one of: ca, en, es, fr.")
        if self._participant_languages[role] == language:
            return
        self._participant_languages[role] = language
        self._save_preferences()

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
        self._resolve_active(direction)
        self._save_preferences()
        return old_signature != self._direction_signature(direction)

    def snapshot(self) -> dict[str, object]:
        return {
            direction: self._snapshot_direction(direction)
            for direction in ("input", "output")
        }

    def _resolve_active(self, direction: SelectionDirection) -> None:
        devices = self._devices[direction]
        preferred_id = self._preferred_ids[direction]
        preferred = next(
            (item for item in devices if item.stable_id == preferred_id),
            None,
        )
        self._active[direction] = preferred or select_automatic_physical_device(
            devices, direction, required=False
        )

    def _snapshot_direction(self, direction: SelectionDirection) -> dict[str, object]:
        active = self._active[direction]
        preferred_id = self._preferred_ids[direction]
        return {
            "selection": preferred_id or "automatic",
            "mode": "manual" if preferred_id else "automatic",
            "preferred_name": self._preferred_names[direction],
            "preferred_available": preferred_id is None or any(
                item.stable_id == preferred_id for item in self._devices[direction]
            ),
            "active_id": active.stable_id if active else None,
            "active_name": active.name if active else None,
            "devices": [
                {
                    "id": item.stable_id,
                    "name": item.name,
                    "default_communications": item.default_communications,
                }
                for item in self._devices[direction]
            ],
        }

    def _direction_signature(self, direction: SelectionDirection) -> tuple[object, ...]:
        active = self._active[direction]
        return (
            tuple((item.stable_id, item.name, item.portaudio_index, item.default_communications) for item in self._devices[direction]),
            self._preferred_ids[direction],
            active.stable_id if active else None,
            active.portaudio_index if active else None,
        )

    def _load_preferences(self) -> dict[str, object]:
        try:
            payload = json.loads(self.preference_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        voice_gender = (
            payload.get("voice_gender", self._default_voice_gender)
        )
        if voice_gender not in VOICE_GENDERS:
            voice_gender = self._default_voice_gender
        ui_language = payload.get("ui_language", self._default_ui_language)
        if ui_language not in UI_LANGUAGES:
            ui_language = self._default_ui_language
        stored_languages = payload.get("languages", {})
        if not isinstance(stored_languages, dict):
            stored_languages = {}
        languages = dict(self._default_participant_languages)
        for role in ("agent", "remote"):
            language = stored_languages.get(role, languages[role])
            if language in CONVERSATION_LANGUAGES:
                languages[role] = language
        device_preferences: dict[str, dict[str, str | None]] = {}
        for direction in ("input", "output"):
            stored_device = payload.get(direction, {})
            if not isinstance(stored_device, dict):
                stored_device = {}
            stable_id = stored_device.get("stable_id")
            name = stored_device.get("name")
            if stored_device.get("mode") != "manual" or not isinstance(stable_id, str):
                stable_id = None
                name = None
            device_preferences[direction] = {
                "stable_id": stable_id,
                "name": name if isinstance(name, str) else None,
            }
        return {
            "voice_gender": voice_gender,
            "ui_language": ui_language,
            "languages": languages,
            "input": device_preferences["input"],
            "output": device_preferences["output"],
        }

    def _save_preferences(self) -> None:
        payload = {
            "voice_gender": self._voice_gender,
            "ui_language": self._ui_language,
            "languages": dict(self._participant_languages),
            **{
                direction: {
                    "mode": "manual" if self._preferred_ids[direction] else "automatic",
                    "stable_id": self._preferred_ids[direction],
                    "name": self._preferred_names[direction],
                }
                for direction in ("input", "output")
            },
        }
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
