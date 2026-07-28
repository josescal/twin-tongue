"""Unified runtime management for physical endpoints and VB-CABLE routes."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from threading import Event
from typing import Awaitable, Callable

from audio.physical_devices import (
    PhysicalDevice,
    PhysicalDeviceSelection,
    SelectionDirection,
    discover_physical_devices,
)
from audio.diagnostics import (
    test_microphone,
    test_physical_audio_path,
    test_speaker,
)
from audio.portaudio import AudioDeviceError, query_devices, query_host_apis
from audio.virtual_cables import (
    CABLE_ROUTES,
    CableRoute,
    VirtualCablePair,
    active_cable_index,
    application_session_active,
    discover_virtual_cables,
    refresh_sessions as refresh_virtual_cable_sessions,
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
        self._session_task: asyncio.Task[None] | None = None
        self._refresh_lock = asyncio.Lock()
        self._on_change: Callable[[], Awaitable[None]] | None = None
        self._refresh_error: str | None = None
        self._physical_runtime: dict[SelectionDirection, dict[str, object]] = {
            "input": {"status": "unknown"},
            "output": {"status": "unknown"},
        }
        self._diagnostic_lock = asyncio.Lock()
        self._physical_path_stop_event: Event | None = None

    def set_change_callback(self, callback: Callable[[], Awaitable[None]]) -> None:
        self._on_change = callback

    async def start(self) -> None:
        if self._task is not None:
            return
        await self.refresh(force_sessions=True)
        self.active_index("remote_input")
        self.active_index("translated_output")
        self._task = asyncio.create_task(self._poll(), name="audio device monitor")
        self._session_task = asyncio.create_task(
            self._poll_sessions(), name="audio application session monitor"
        )

    async def close(self) -> None:
        if self._task is None:
            return
        tasks = tuple(
            task for task in (self._task, self._session_task) if task is not None
        )
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None
        self._session_task = None

    async def refresh(self, *, force_sessions: bool = False) -> None:
        async with self._refresh_lock:
            try:
                physical, cables = await asyncio.to_thread(
                    discover_audio_topology,
                    force_sessions,
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

            if not force_sessions:
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

            state_changed = (
                bool(changed_keys)
                or old_cable_signature != virtual_cable_signature(cables)
            )
        if state_changed and self._on_change is not None:
            await self._on_change()

    async def refresh_sessions(self) -> None:
        """Refresh connected applications without querying PortAudio topology."""
        async with self._refresh_lock:
            previous_signature = virtual_cable_signature(self._cables)
            cables = await asyncio.to_thread(
                refresh_virtual_cable_sessions, self._cables
            )
            self._cables = cables
            state_changed = (
                previous_signature != virtual_cable_signature(cables)
            )
        if state_changed and self._on_change is not None:
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

    async def report_physical_device_healthy(
        self,
        direction: SelectionDirection,
        portaudio_index: int,
        *,
        sample_rate: int | None = None,
        channels: int | None = None,
    ) -> None:
        """Persist a proven endpoint as the preferred automatic fallback."""
        changed = self._physical.mark_healthy(direction, portaudio_index)
        self._physical_runtime[direction] = {
            "status": "healthy",
            "device_index": portaudio_index,
            "sample_rate": sample_rate,
            "channels": channels,
        }
        if changed and self._on_change is not None:
            await self._on_change()

    async def report_physical_device_failed(
        self,
        direction: SelectionDirection,
        portaudio_index: int,
        error: BaseException,
    ) -> bool:
        """Quarantine a failed endpoint and wake a pipeline for its fallback."""
        changed = self._physical.mark_unhealthy(direction, portaudio_index)
        self._physical_runtime[direction] = {
            "status": "failed",
            "device_index": portaudio_index,
            "error": str(error),
        }
        try:
            fallback: int | str = self._physical.active_index(direction)
        except AudioDeviceError:
            fallback = "none"
        LOGGER.warning(
            "event=physical_audio_device_quarantined direction=%s device=%s "
            "fallback=%s error=%s",
            direction,
            portaudio_index,
            fallback,
            error,
        )
        if not changed:
            return False
        self._revisions[direction] += 1
        async with self._condition:
            self._condition.notify_all()
        if self._on_change is not None:
            await self._on_change()
        return True

    async def test_physical_device(
        self, direction: SelectionDirection
    ) -> dict[str, object]:
        """Run one short endpoint check without blocking the control server."""
        if direction not in {"input", "output"}:
            raise ValueError(f"Unknown physical audio direction '{direction}'.")
        async with self._diagnostic_lock:
            device = self.active_index(direction)
            diagnostic = test_microphone if direction == "input" else test_speaker
            return await asyncio.to_thread(diagnostic, device)

    async def test_physical_audio_path(self) -> dict[str, object]:
        """Validate capture, pipeline downmix, and physical playback together."""
        async with self._diagnostic_lock:
            input_device = self.active_index("input")
            output_device = self.active_index("output")
            stop_event = Event()
            self._physical_path_stop_event = stop_event
            try:
                return await asyncio.to_thread(
                    test_physical_audio_path,
                    input_device,
                    output_device,
                    stop_event,
                )
            finally:
                if self._physical_path_stop_event is stop_event:
                    self._physical_path_stop_event = None

    def stop_physical_audio_path_test(self) -> bool:
        """Stop the current manual path recording before its safety limit."""
        stop_event = self._physical_path_stop_event
        if stop_event is None:
            return False
        stop_event.set()
        return True

    def active_index(self, key: DeviceKey, fallback: int | None = None) -> int:
        if key in {"input", "output"}:
            return self._physical.active_index(key)
        return active_cable_index(self._cables, key, fallback)

    def revision(self, key: DeviceKey) -> int:
        return self._revisions[key]

    def application_session_active(self, route: CableRoute) -> bool | None:
        """Return cached external application presence for one cable route."""
        return application_session_active(self._cables, route)

    async def wait_for_change(self, key: DeviceKey, revision: int) -> int:
        async with self._condition:
            await self._condition.wait_for(lambda: self._revisions[key] != revision)
        return self._revisions[key]

    def snapshot(self) -> dict[str, object]:
        physical = self._physical.snapshot()
        for direction in ("input", "output"):
            direction_snapshot = physical.get(direction)
            if isinstance(direction_snapshot, dict):
                direction_snapshot["runtime"] = dict(
                    self._physical_runtime[direction]
                )
        return {
            "audio_devices": physical,
            "virtual_cables": virtual_cable_snapshot(self._cables),
        }

    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval_seconds)
            await self.refresh()

    async def _poll_sessions(self) -> None:
        while True:
            await asyncio.sleep(self.session_poll_interval_seconds)
            await self.refresh_sessions()


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
