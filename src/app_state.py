"""Live application state shared by the user interface and audio pipelines."""

import asyncio
from contextlib import asynccontextmanager
from enum import StrEnum
import time
from typing import AsyncIterator, Literal, TypeAlias, cast

from audio.device_manager import AudioDeviceManager
from twin_tongue_version import __version__


PipelineName: TypeAlias = Literal["remote_to_agent", "agent_to_remote"]
LanguageRole: TypeAlias = Literal["agent", "remote"]
UiLanguage: TypeAlias = Literal["en", "es"]

PIPELINE_NAMES: tuple[PipelineName, ...] = (
    "remote_to_agent",
    "agent_to_remote",
)
LANGUAGE_ROLES: tuple[LanguageRole, ...] = ("agent", "remote")
SUPPORTED_LANGUAGES = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "ca": "Catalan",
}
SUPPORTED_UI_LANGUAGES: tuple[UiLanguage, ...] = ("en", "es")
TRANSCRIPTION_HISTORY_LIMIT = 50


class PipelineMode(StrEnum):
    """Available runtime behaviours for one audio direction."""

    TRANSLATE = "translate"
    PASSTHROUGH = "passthrough"


class VoiceGender(StrEnum):
    """Available synthesized speaker voices."""

    MALE = "male"
    FEMALE = "female"


class PipelineStatus(StrEnum):
    """Operational states reported by a directional audio pipeline."""

    STARTING = "starting"
    SYSTEM_LOADING = "system_loading"
    SWITCHING = "switching"
    INITIALIZING = "initializing"
    TRANSLATION_UNAVAILABLE = "translation_unavailable"
    TRANSLATION_READY = "translation_ready"
    PASSTHROUGH = "passthrough"
    ERROR = "error"


class ApplicationState:
    """Store live application state and publish coherent snapshots."""

    def __init__(
        self,
        initial_modes: dict[str, str] | None = None,
        initial_languages: dict[str, str] | None = None,
        initial_voice_genders: dict[str, str] | None = None,
        initial_ui_language: str = "en",
        audio_recording_enabled: bool = False,
        active_pipelines: tuple[str, ...] = PIPELINE_NAMES,
        barge_in_enabled: bool = True,
        barge_in_resume_delay_ms: float = 300,
    ) -> None:
        requested_modes = initial_modes or {}
        self._modes: dict[PipelineName, PipelineMode] = {
            name: PipelineMode(requested_modes.get(name, PipelineMode.PASSTHROUGH))
            for name in PIPELINE_NAMES
        }
        self._statuses: dict[PipelineName, PipelineStatus] = {
            name: PipelineStatus.STARTING for name in PIPELINE_NAMES
        }
        unknown = set(active_pipelines) - set(PIPELINE_NAMES)
        if unknown:
            raise ValueError(f"Unknown pipeline names: {', '.join(sorted(unknown))}")
        self._active_pipelines = frozenset(
            cast(PipelineName, name) for name in active_pipelines
        )
        requested_languages = initial_languages or {"agent": "es", "remote": "en"}
        self._languages: dict[LanguageRole, str] = {}
        for role in LANGUAGE_ROLES:
            language = requested_languages.get(role)
            if language not in SUPPORTED_LANGUAGES:
                raise ValueError(f"Unsupported language '{language}' for {role}")
            self._languages[role] = language
        requested_voice_genders = initial_voice_genders or {}
        self._voice_genders: dict[PipelineName, VoiceGender] = {
            name: VoiceGender(requested_voice_genders.get(name, VoiceGender.MALE))
            for name in PIPELINE_NAMES
        }
        self._ui_language = self._parse_ui_language(initial_ui_language)
        self._audio_recording_enabled = bool(audio_recording_enabled)
        self._manual_audio_recording = False
        if barge_in_resume_delay_ms < 0:
            raise ValueError("Barge-in resume delay must not be negative.")
        self._barge_in_enabled = bool(barge_in_enabled)
        self._barge_in_resume_delay_seconds = barge_in_resume_delay_ms / 1000
        self._translated_playback_until: dict[PipelineName, float] = {
            name: 0.0 for name in PIPELINE_NAMES
        }
        self._transcripts: list[dict[str, object]] = []
        self._revision = 0
        self._lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue[dict[str, object]]] = set()
        self._device_manager: AudioDeviceManager | None = None

    def attach_device_manager(self, manager: AudioDeviceManager) -> None:
        """Expose physical devices, virtual routes, and persisted preferences."""
        self._device_manager = manager
        self._voice_genders = {
            name: VoiceGender(gender)
            for name, gender in manager.voice_genders.items()
        }
        self._ui_language = self._parse_ui_language(manager.ui_language)
        for role, language in manager.participant_languages.items():
            if role in LANGUAGE_ROLES and language in SUPPORTED_LANGUAGES:
                self._languages[cast(LanguageRole, role)] = language
        manager.set_change_callback(self._publish_external_change)

    def get_mode(self, name: str) -> PipelineMode:
        """Return the current mode for a pipeline."""
        parsed_name = self._parse_pipeline_name(name)
        return self._modes[parsed_name]

    def snapshot(self) -> dict[str, object]:
        """Return a JSON-serializable view of the current state."""
        result: dict[str, object] = {
            "application_version": __version__,
            "revision": self._revision,
            "ui_language": self._ui_language,
            "supported_ui_languages": list(SUPPORTED_UI_LANGUAGES),
            "languages": dict(self._languages),
            "supported_languages": [
                {"code": code, "label": label}
                for code, label in SUPPORTED_LANGUAGES.items()
            ],
            "audio_recording": {
                "enabled": self._audio_recording_enabled,
                "active": self.is_audio_recording_active(),
                "manual": self._manual_audio_recording,
                "automatic_pipelines": list(self._automatic_recording_pipelines()),
            },
            "barge_in": {
                "enabled": self._barge_in_enabled,
                "pipeline": "agent_to_remote",
                "resume_delay_ms": self._barge_in_resume_delay_seconds * 1000,
            },
            "transcription": {
                "entries": [dict(entry) for entry in self._transcripts],
            },
            "pipelines": {
                name: {
                    "mode": self._modes[name].value,
                    "voice_gender": self._voice_genders[name].value,
                    "running": name in self._active_pipelines,
                    "status": self._statuses[name].value,
                }
                for name in PIPELINE_NAMES
            },
        }
        if self._device_manager is not None:
            result.update(self._device_manager.snapshot())
        return result

    async def _publish_external_change(self) -> None:
        """Publish a physical-device or virtual-route change."""
        async with self._lock:
            self._publish_change()

    def get_language(self, role: str) -> str:
        """Return the language code currently assigned to a participant."""
        parsed_role = self._parse_language_role(role)
        return self._languages[parsed_role]

    async def set_language(self, role: str, language: str) -> dict[str, object]:
        """Change a participant language and notify realtime subscribers."""
        parsed_role = self._parse_language_role(role)
        if language not in SUPPORTED_LANGUAGES:
            choices = ", ".join(SUPPORTED_LANGUAGES)
            raise ValueError(
                f"Unsupported language '{language}'; expected one of: {choices}"
            )
        async with self._lock:
            if self._languages[parsed_role] == language:
                return self.snapshot()
            if self._device_manager is not None:
                self._device_manager.set_participant_language(parsed_role, language)
            self._languages[parsed_role] = language
            return self._publish_change()

    def get_ui_language(self) -> UiLanguage:
        """Return the current interface language."""
        return self._ui_language

    async def set_ui_language(self, language: str) -> dict[str, object]:
        """Change the interface language and notify realtime subscribers."""
        parsed_language = self._parse_ui_language(language)
        async with self._lock:
            if self._ui_language == parsed_language:
                return self.snapshot()
            if self._device_manager is not None:
                self._device_manager.set_ui_language(parsed_language)
            self._ui_language = parsed_language
            return self._publish_change()

    def get_voice_gender(self, name: str) -> VoiceGender:
        """Return the voice gender used for new synthesis requests."""
        return self._voice_genders[self._parse_pipeline_name(name)]

    async def set_voice_gender(
        self, name: str, gender: str | VoiceGender
    ) -> dict[str, object]:
        """Change the synthesized voice and notify realtime subscribers."""
        parsed_name = self._parse_pipeline_name(name)
        try:
            parsed_gender = VoiceGender(gender)
        except ValueError as error:
            choices = ", ".join(item.value for item in VoiceGender)
            raise ValueError(
                f"Invalid voice gender '{gender}'; expected one of: {choices}"
            ) from error
        async with self._lock:
            if self._voice_genders[parsed_name] is parsed_gender:
                return self.snapshot()
            if self._device_manager is not None:
                self._device_manager.set_voice_gender(parsed_name, parsed_gender.value)
            self._voice_genders[parsed_name] = parsed_gender
            return self._publish_change()

    async def set_audio_device(
        self, direction: str, selection: str
    ) -> dict[str, object]:
        """Select a physical input/output endpoint and notify the live UI."""
        if direction not in {"input", "output"}:
            raise ValueError(f"Unknown physical audio direction '{direction}'.")
        if self._device_manager is None:
            raise ValueError("Audio device management is not available.")
        async with self._lock:
            changed = await self._device_manager.set_physical_device(
                cast(Literal["input", "output"], direction), selection
            )
            return self._publish_change() if changed else self.snapshot()

    def is_audio_recording_active(self) -> bool:
        """Return whether any active pipeline is being recorded."""
        return self._audio_recording_enabled and (
            self._manual_audio_recording
            or bool(self._automatic_recording_pipelines())
        )

    def is_audio_recording_active_for(self, name: str) -> bool:
        """Return whether one pipeline should write diagnostic audio."""
        parsed_name = self._parse_pipeline_name(name)
        return self._audio_recording_enabled and parsed_name in self._active_pipelines and (
            self._manual_audio_recording
            or self._modes[parsed_name] is PipelineMode.TRANSLATE
        )

    async def set_manual_audio_recording(self, active: bool) -> dict[str, object]:
        """Start or stop user-requested recording without persisting it."""
        if active and not self._audio_recording_enabled:
            raise ValueError("Audio recording is disabled in configuration.")
        async with self._lock:
            if self._manual_audio_recording is active:
                return self.snapshot()
            self._manual_audio_recording = active
            return self._publish_change()

    def note_translated_playback(
        self,
        name: str,
        buffered_audio_ms: float,
        *,
        now: float | None = None,
    ) -> None:
        """Extend the interval that blocks barge-in from the opposite microphone."""
        parsed_name = self._parse_pipeline_name(name)
        if self._barge_in_enabled:
            return
        if buffered_audio_ms < 0:
            raise ValueError("Buffered translated playback must not be negative.")
        current_time = time.monotonic() if now is None else now
        blocked_until = current_time + (
            buffered_audio_ms / 1000 + self._barge_in_resume_delay_seconds
        )
        self._translated_playback_until[parsed_name] = max(
            self._translated_playback_until[parsed_name], blocked_until
        )

    def is_capture_suppressed_by_barge_in(
        self, name: str, *, now: float | None = None
    ) -> bool:
        """Return whether opposite translated playback currently owns the turn."""
        parsed_name = self._parse_pipeline_name(name)
        if self._barge_in_enabled or parsed_name != "agent_to_remote":
            return False
        current_time = time.monotonic() if now is None else now
        return current_time < self._translated_playback_until["remote_to_agent"]

    def publish_transcript_final(
        self,
        pipeline_name: str,
        transcript_id: int,
        text: str,
        source_language: str,
        target_language: str,
    ) -> None:
        """Append one committed utterance from either conversation direction."""
        parsed_pipeline_name = self._parse_pipeline_name(pipeline_name)
        self._transcripts.append(
            {
                "id": transcript_id,
                "pipeline": parsed_pipeline_name,
                "source_text": text,
                "translated_text": None,
                "translation_status": "pending",
                "source_language": source_language,
                "target_language": target_language,
            }
        )
        del self._transcripts[:-TRANSCRIPTION_HISTORY_LIMIT]
        self._publish_change()

    def publish_transcript_translation(
        self, pipeline_name: str, transcript_id: int, text: str
    ) -> None:
        """Attach translated text to its utterance and conversation direction."""
        parsed_pipeline_name = self._parse_pipeline_name(pipeline_name)
        for index, entry in enumerate(self._transcripts):
            if entry["pipeline"] == parsed_pipeline_name and entry["id"] == transcript_id:
                updated = dict(entry)
                updated["translated_text"] = text
                updated["translation_status"] = "complete"
                self._transcripts[index] = updated
                self._publish_change()
                return

    def mark_transcript_translation_unavailable(
        self, pipeline_name: str, transcript_id: int
    ) -> None:
        """Mark a final segment that will not reach translated synthesis."""
        parsed_pipeline_name = self._parse_pipeline_name(pipeline_name)
        for index, entry in enumerate(self._transcripts):
            if (
                entry["pipeline"] == parsed_pipeline_name
                and entry["id"] == transcript_id
                and entry["translation_status"] == "pending"
            ):
                updated = dict(entry)
                updated["translation_status"] = "unavailable"
                self._transcripts[index] = updated
                self._publish_change()
                return

    async def clear_transcription(self) -> dict[str, object]:
        """Clear committed transcription text for both conversation directions."""
        async with self._lock:
            if not self._transcripts:
                return self.snapshot()
            self._transcripts.clear()
            return self._publish_change()

    async def set_mode(self, name: str, mode: str | PipelineMode) -> dict[str, object]:
        """Change a pipeline mode and notify realtime subscribers."""
        parsed_name = self._parse_pipeline_name(name)
        try:
            parsed_mode = PipelineMode(mode)
        except ValueError as error:
            choices = ", ".join(item.value for item in PipelineMode)
            raise ValueError(f"Invalid mode '{mode}'; expected one of: {choices}") from error

        async with self._lock:
            if self._modes[parsed_name] is parsed_mode:
                return self.snapshot()
            self._modes[parsed_name] = parsed_mode
            self._statuses[parsed_name] = PipelineStatus.SWITCHING
            return self._publish_change()

    async def set_all_modes(self, mode: str | PipelineMode) -> dict[str, object]:
        """Change every active conversation direction in one state update."""
        try:
            parsed_mode = PipelineMode(mode)
        except ValueError as error:
            choices = ", ".join(item.value for item in PipelineMode)
            raise ValueError(f"Invalid mode '{mode}'; expected one of: {choices}") from error

        async with self._lock:
            changed = False
            for name in self._active_pipelines:
                if self._modes[name] is parsed_mode:
                    continue
                self._modes[name] = parsed_mode
                self._statuses[name] = PipelineStatus.SWITCHING
                changed = True
            return self._publish_change() if changed else self.snapshot()

    async def set_pipeline_status(
        self, name: str, status: str | PipelineStatus
    ) -> dict[str, object]:
        """Publish the operational status confirmed by an audio pipeline."""
        parsed_name = self._parse_pipeline_name(name)
        try:
            parsed_status = PipelineStatus(status)
        except ValueError as error:
            choices = ", ".join(item.value for item in PipelineStatus)
            raise ValueError(
                f"Invalid pipeline status '{status}'; expected one of: {choices}"
            ) from error
        async with self._lock:
            if self._statuses[parsed_name] is parsed_status:
                return self.snapshot()
            self._statuses[parsed_name] = parsed_status
            return self._publish_change()

    def _publish_change(self) -> dict[str, object]:
        """Increment the revision and publish one current-state snapshot."""
        self._revision += 1
        snapshot = self.snapshot()
        self._publish(snapshot)
        return snapshot

    def _publish(self, snapshot: dict[str, object]) -> None:
        for queue in tuple(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(snapshot)

    def _automatic_recording_pipelines(self) -> tuple[PipelineName, ...]:
        if not self._audio_recording_enabled:
            return ()
        return tuple(
            name
            for name in PIPELINE_NAMES
            if name in self._active_pipelines
            and self._modes[name] is PipelineMode.TRANSLATE
        )

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[dict[str, object]]]:
        """Subscribe to state changes with a bounded queue."""
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    @staticmethod
    def _parse_pipeline_name(name: str) -> PipelineName:
        if name not in PIPELINE_NAMES:
            raise ValueError(f"Unknown pipeline '{name}'")
        return cast(PipelineName, name)

    @staticmethod
    def _parse_language_role(role: str) -> LanguageRole:
        if role not in LANGUAGE_ROLES:
            raise ValueError(f"Unknown language role '{role}'")
        return cast(LanguageRole, role)

    @staticmethod
    def _parse_ui_language(language: str) -> UiLanguage:
        if language not in SUPPORTED_UI_LANGUAGES:
            raise ValueError("Unsupported UI language; expected one of: en, es")
        return cast(UiLanguage, language)
