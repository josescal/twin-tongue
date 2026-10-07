"""Persistent user choices independent from runtime configuration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal


PipelineName = Literal["remote_to_agent", "agent_to_remote"]
DeviceDirection = Literal["input", "output"]
PIPELINE_NAMES: tuple[PipelineName, ...] = (
    "remote_to_agent",
    "agent_to_remote",
)
DEVICE_DIRECTIONS: tuple[DeviceDirection, ...] = ("input", "output")
VOICE_GENDERS = frozenset({"male", "female"})
UI_LANGUAGES = frozenset({"en", "es"})
CONVERSATION_LANGUAGES = frozenset({"ca", "en", "es", "fr"})


class UserPreferences:
    """Load and atomically persist user-controlled application choices."""

    def __init__(
        self,
        path: Path,
        *,
        default_voice_genders: dict[str, str] | None = None,
        default_ui_language: str = "en",
        default_participant_languages: dict[str, str] | None = None,
    ) -> None:
        voice_defaults = {
            "remote_to_agent": "male",
            "agent_to_remote": "male",
            **(default_voice_genders or {}),
        }
        if any(voice_defaults[name] not in VOICE_GENDERS for name in PIPELINE_NAMES):
            raise ValueError("Default voice genders must be 'male' or 'female'.")
        if default_ui_language not in UI_LANGUAGES:
            raise ValueError("Default UI language must be 'en' or 'es'.")
        language_defaults = {
            "agent": "es",
            "remote": "en",
            **(default_participant_languages or {}),
        }
        if any(value not in CONVERSATION_LANGUAGES for value in language_defaults.values()):
            raise ValueError("Default participant languages are invalid.")

        self.path = path
        payload = self._read()
        stored_voice_genders = payload.get("voice_genders", {})
        if not isinstance(stored_voice_genders, dict):
            stored_voice_genders = {}
        self._voice_genders: dict[PipelineName, str] = {
            name: (
                stored_voice_genders.get(name)
                if stored_voice_genders.get(name) in VOICE_GENDERS
                else voice_defaults[name]
            )
            for name in PIPELINE_NAMES
        }
        ui_language = payload.get("ui_language", default_ui_language)
        self._ui_language = (
            ui_language if ui_language in UI_LANGUAGES else default_ui_language
        )
        stored_languages = payload.get("languages", {})
        if not isinstance(stored_languages, dict):
            stored_languages = {}
        self._participant_languages = dict(language_defaults)
        for role in ("agent", "remote"):
            language = stored_languages.get(role)
            if language in CONVERSATION_LANGUAGES:
                self._participant_languages[role] = language

        stored_devices = payload.get("audio_devices", {})
        if not isinstance(stored_devices, dict):
            stored_devices = {}
        self._audio_devices: dict[DeviceDirection, dict[str, str | None]] = {}
        self._last_working_audio_devices: dict[
            DeviceDirection, dict[str, str | None]
        ] = {}
        for direction in DEVICE_DIRECTIONS:
            stored = stored_devices.get(direction, {})
            if not isinstance(stored, dict):
                stored = {}
            stable_id = stored.get("stable_id")
            name = stored.get("name")
            if stored.get("mode") != "manual" or not isinstance(stable_id, str):
                stable_id = None
                name = None
            self._audio_devices[direction] = {
                "stable_id": stable_id,
                "name": name if isinstance(name, str) else None,
            }
            last_working = payload.get("last_working_audio_devices", {})
            if not isinstance(last_working, dict):
                last_working = {}
            stored_last = last_working.get(direction, {})
            if not isinstance(stored_last, dict):
                stored_last = {}
            last_id = stored_last.get("stable_id")
            last_name = stored_last.get("name")
            self._last_working_audio_devices[direction] = {
                "stable_id": last_id if isinstance(last_id, str) else None,
                "name": last_name if isinstance(last_name, str) else None,
            }
        self._save()

    @property
    def voice_genders(self) -> dict[PipelineName, str]:
        return dict(self._voice_genders)

    @property
    def ui_language(self) -> str:
        return self._ui_language

    @property
    def participant_languages(self) -> dict[str, str]:
        return dict(self._participant_languages)

    def set_voice_gender(self, pipeline: PipelineName, gender: str) -> None:
        if pipeline not in PIPELINE_NAMES:
            raise ValueError(f"Unknown pipeline '{pipeline}'.")
        if gender not in VOICE_GENDERS:
            raise ValueError("Voice gender must be 'male' or 'female'.")
        if self._voice_genders[pipeline] != gender:
            self._voice_genders[pipeline] = gender
            self._save()

    def set_ui_language(self, language: str) -> None:
        if language not in UI_LANGUAGES:
            raise ValueError("UI language must be 'en' or 'es'.")
        if self._ui_language != language:
            self._ui_language = language
            self._save()

    def set_participant_language(self, role: str, language: str) -> None:
        if role not in {"agent", "remote"}:
            raise ValueError(f"Unknown participant language role '{role}'.")
        if language not in CONVERSATION_LANGUAGES:
            raise ValueError("Participant language must be one of: ca, en, es, fr.")
        if self._participant_languages[role] != language:
            self._participant_languages[role] = language
            self._save()

    def audio_device(self, direction: DeviceDirection) -> dict[str, str | None]:
        return dict(self._audio_devices[direction])

    def set_audio_device(
        self,
        direction: DeviceDirection,
        stable_id: str | None,
        name: str | None,
    ) -> None:
        self._audio_devices[direction] = {"stable_id": stable_id, "name": name}
        self._save()

    def last_working_audio_device(
        self, direction: DeviceDirection
    ) -> dict[str, str | None]:
        return dict(self._last_working_audio_devices[direction])

    def set_last_working_audio_device(
        self,
        direction: DeviceDirection,
        stable_id: str,
        name: str,
    ) -> None:
        value = {"stable_id": stable_id, "name": name}
        if self._last_working_audio_devices[direction] != value:
            self._last_working_audio_devices[direction] = value
            self._save()

    def _read(self) -> dict[str, object]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save(self) -> None:
        payload = {
            "ui_language": self._ui_language,
            "languages": dict(self._participant_languages),
            "voice_genders": dict(self._voice_genders),
            "audio_devices": {
                direction: {
                    "mode": "manual" if selected["stable_id"] else "automatic",
                    **selected,
                }
                for direction, selected in self._audio_devices.items()
            },
            "last_working_audio_devices": {
                direction: dict(selected)
                for direction, selected in self._last_working_audio_devices.items()
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.path)
