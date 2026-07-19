"""Live application state shared by the user interface and audio pipelines."""

import asyncio
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import AsyncIterator, Literal, TypeAlias, cast

from audio.device_manager import AudioDeviceManager
from twin_tongue_version import __version__


PipelineName: TypeAlias = Literal["remote_to_agent", "agent_to_remote"]
LanguageRole: TypeAlias = Literal["agent", "remote"]

PIPELINE_NAMES: tuple[PipelineName, ...] = (
    "remote_to_agent",
    "agent_to_remote",
)
LANGUAGE_ROLES: tuple[LanguageRole, ...] = ("agent", "remote")
SUPPORTED_LANGUAGES = {
    "en": "Inglés",
    "es": "Español",
    "fr": "Francés",
    "ca": "Catalán",
}
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
        initial_voice_gender: str = VoiceGender.MALE,
        active_pipelines: tuple[str, ...] = PIPELINE_NAMES,
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
        self._voice_gender = VoiceGender(initial_voice_gender)
        self._agent_transcripts: list[dict[str, object]] = []
        self._revision = 0
        self._lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue[dict[str, object]]] = set()
        self._device_manager: AudioDeviceManager | None = None

    def attach_device_manager(self, manager: AudioDeviceManager) -> None:
        """Expose physical devices and virtual routes through realtime state."""
        self._device_manager = manager
        self._voice_gender = VoiceGender(manager.voice_gender)
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
            "languages": dict(self._languages),
            "supported_languages": [
                {"code": code, "label": label}
                for code, label in SUPPORTED_LANGUAGES.items()
            ],
            "voice_gender": self._voice_gender.value,
            "transcription": {
                "entries": [dict(entry) for entry in self._agent_transcripts],
            },
            "pipelines": {
                name: {
                    "mode": self._modes[name].value,
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
            self._languages[parsed_role] = language
            return self._publish_change()

    def get_voice_gender(self) -> VoiceGender:
        """Return the voice gender used for new synthesis requests."""
        return self._voice_gender

    async def set_voice_gender(
        self, gender: str | VoiceGender
    ) -> dict[str, object]:
        """Change the synthesized voice and notify realtime subscribers."""
        try:
            parsed_gender = VoiceGender(gender)
        except ValueError as error:
            choices = ", ".join(item.value for item in VoiceGender)
            raise ValueError(
                f"Invalid voice gender '{gender}'; expected one of: {choices}"
            ) from error
        async with self._lock:
            if self._voice_gender is parsed_gender:
                return self.snapshot()
            if self._device_manager is not None:
                self._device_manager.set_voice_gender(parsed_gender.value)
            self._voice_gender = parsed_gender
            return self._publish_change()

    def publish_agent_final(
        self,
        transcript_id: int,
        text: str,
        source_language: str,
        target_language: str,
    ) -> None:
        """Append one committed agent utterance to the bounded live history."""
        self._agent_transcripts.append(
            {
                "id": transcript_id,
                "source_text": text,
                "translated_text": None,
                "translation_status": "pending",
                "source_language": source_language,
                "target_language": target_language,
            }
        )
        del self._agent_transcripts[:-TRANSCRIPTION_HISTORY_LIMIT]
        self._publish_change()

    def publish_agent_translation(self, transcript_id: int, text: str) -> None:
        """Attach the client-facing translation to its committed utterance."""
        for index, entry in enumerate(self._agent_transcripts):
            if entry["id"] == transcript_id:
                updated = dict(entry)
                updated["translated_text"] = text
                updated["translation_status"] = "complete"
                self._agent_transcripts[index] = updated
                self._publish_change()
                return

    def mark_agent_translation_unavailable(self, transcript_id: int) -> None:
        """Mark a final segment that will not reach translated synthesis."""
        for index, entry in enumerate(self._agent_transcripts):
            if entry["id"] == transcript_id and entry["translation_status"] == "pending":
                updated = dict(entry)
                updated["translation_status"] = "unavailable"
                self._agent_transcripts[index] = updated
                self._publish_change()
                return

    async def clear_agent_transcription(self) -> dict[str, object]:
        """Clear committed transcription text from live memory."""
        async with self._lock:
            if not self._agent_transcripts:
                return self.snapshot()
            self._agent_transcripts.clear()
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
