"""Unified runtime management for physical endpoints and VB-CABLE routes."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import time
from typing import Awaitable, Callable

from audio.physical_devices import (
    PhysicalDevice,
    PhysicalDeviceSelection,
    SelectionDirection,
    discover_physical_devices,
)
from audio.portaudio import AudioDeviceError, query_devices, query_host_apis
from audio.virtual_cables import (
    CABLE_ROUTES,
    CableRoute,
    VirtualCablePair,
    active_cable_index,
    discover_virtual_cables,
    retain_sessions,
    virtual_cable_signature,
    virtual_cable_snapshot,
)
from audio.windows_audio import query_core_audio_endpoints
from preferences import PipelineName, UserPreferences

LOGGER = logging.getLogger(__name__)
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_SESSION_POLL_INTERVAL_SECONDS = 7.5
DeviceKey = SelectionDirection | CableRoute


class AudioDeviceManager:
    """Poll audio topology once and coordinate every runtime device choice."""

    def __init__(
        self,
        preference_path: Path,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        session_poll_interval_seconds: float = DEFAULT_SESSION_POLL_INTERVAL_SECONDS,
        default_voice_genders: dict[str, str] | None = None,
        default_ui_language: str = "en",
        default_participant_languages: dict[str, str] | None = None,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("Audio device poll interval must be greater than zero.")
        if session_poll_interval_seconds <= 0:
            raise ValueError("Audio session poll interval must be greater than zero.")
        self.poll_interval_seconds = poll_interval_seconds
        self.session_poll_interval_seconds = session_poll_interval_seconds
        self._preferences = UserPreferences(
            preference_path,
            default_voice_genders=default_voice_genders,
            default_ui_language=default_ui_language,
            default_participant_languages=default_participant_languages,
        )
        self._physical = PhysicalDeviceSelection(self._preferences)
        self._cables: list[VirtualCablePair] = []
        self._revisions: dict[DeviceKey, int] = {
            "input": 0,
            "output": 0,
            "remote_input": 0,
            "translated_output": 0,
        }
        self._condition = asyncio.Condition()
        self._task: asyncio.Task[None] | None = None
        self._on_change: Callable[[], Awaitable[None]] | None = None
        self._last_session_refresh_at: float | None = None
        self._refresh_error: str | None = None

    def set_change_callback(self, callback: Callable[[], Awaitable[None]]) -> None:
        self._on_change = callback

    async def start(self) -> None:
        if self._task is not None:
            return
        await self.refresh(force_sessions=True)
        self.active_index("remote_input")
        self.active_index("translated_output")
        self._task = asyncio.create_task(self._poll(), name="audio device monitor")

    async def close(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def refresh(self, *, force_sessions: bool = False) -> None:
        now = time.monotonic()
        include_sessions = force_sessions or self._sessions_due(now)
        try:
            physical, cables = await asyncio.to_thread(
                discover_audio_topology,
                include_sessions,
            )
        except (AudioDeviceError, OSError, RuntimeError) as error:
            detail = str(error)
            if detail != self._refresh_error:
                LOGGER.warning(
                    "event=audio_device_refresh_failed consequence=topology_stale "
                    "action=retrying error=%s",
                    detail,
                )
                self._refresh_error = detail
            return

        if self._refresh_error is not None:
            LOGGER.info("event=audio_device_refresh_recovered")
            self._refresh_error = None

        if include_sessions:
            self._last_session_refresh_at = now
        else:
            cables = retain_sessions(cables, self._cables)

        old_cable_signature = virtual_cable_signature(self._cables)
        old_route_signature = _route_signature(self._cables)
        changed_keys: list[DeviceKey] = list(self._physical.update(physical))
        self._cables = cables
        if old_route_signature != _route_signature(cables):
            changed_keys.extend(CABLE_ROUTES)

        for key in changed_keys:
            self._revisions[key] += 1
        if changed_keys:
            async with self._condition:
                self._condition.notify_all()

        if (
            changed_keys
            or old_cable_signature != virtual_cable_signature(cables)
        ) and self._on_change is not None:
            await self._on_change()

    @property
    def voice_genders(self) -> dict[PipelineName, str]:
        return self._preferences.voice_genders

    @property
    def ui_language(self) -> str:
        return self._preferences.ui_language

    @property
    def participant_languages(self) -> dict[str, str]:
        return self._preferences.participant_languages

    def set_voice_gender(self, pipeline: PipelineName, gender: str) -> None:
        self._preferences.set_voice_gender(pipeline, gender)

    def set_ui_language(self, language: str) -> None:
        self._preferences.set_ui_language(language)

    def set_participant_language(self, role: str, language: str) -> None:
        self._preferences.set_participant_language(role, language)

    async def set_physical_device(
        self, direction: SelectionDirection, selection: str
    ) -> bool:
        """Persist a physical endpoint selection and wake the affected pipeline."""
        changed = self._physical.set_device(direction, selection)
        if not changed:
            return False
        self._revisions[direction] += 1
        async with self._condition:
            self._condition.notify_all()
        return True

    def active_index(self, key: DeviceKey, fallback: int | None = None) -> int:
        if key in {"input", "output"}:
            return self._physical.active_index(key)
        return active_cable_index(self._cables, key, fallback)

    def revision(self, key: DeviceKey) -> int:
        return self._revisions[key]

    async def wait_for_change(self, key: DeviceKey, revision: int) -> int:
        async with self._condition:
            await self._condition.wait_for(lambda: self._revisions[key] != revision)
        return self._revisions[key]

    def snapshot(self) -> dict[str, object]:
        return {
            "audio_devices": self._physical.snapshot(),
            "virtual_cables": virtual_cable_snapshot(self._cables),
        }

    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval_seconds)
            await self.refresh()

    def _sessions_due(self, now: float) -> bool:
        return (
            self._last_session_refresh_at is None
            or now - self._last_session_refresh_at >= self.session_poll_interval_seconds
        )


def discover_audio_topology(
    include_sessions: bool,
) -> tuple[list[PhysicalDevice], list[VirtualCablePair]]:
    devices = query_devices()
    host_apis = query_host_apis()
    try:
        core_endpoints = query_core_audio_endpoints()
    except OSError as error:
        core_endpoints = []
    return (
        discover_physical_devices(devices, host_apis, core_endpoints),
        discover_virtual_cables(
            devices,
            host_apis,
            core_endpoints,
            include_sessions=include_sessions,
        ),
    )


def _route_signature(cables: list[VirtualCablePair]) -> tuple[object, ...]:
    return tuple(
        (cable.stable_id, cable.capture_index, cable.render_index)
        for cable in cables
    )
