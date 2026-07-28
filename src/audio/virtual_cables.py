"""Discovery and runtime selection of the two VB-CABLE pipeline routes."""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Any, Literal

from audio.portaudio import AudioDeviceError
from audio.windows_audio import (
    CoreAudioEndpoint,
    CoreAudioSession,
    query_audio_sessions,
)

CableRoute = Literal["remote_input", "translated_output"]
CABLE_ROUTES = ("remote_input", "translated_output")
DEFAULT_CABLE_SELECTIONS: dict[CableRoute, str] = {
    "remote_input": "cable:A",
    "translated_output": "cable:B",
}


@dataclass(frozen=True)
class VirtualCablePair:
    stable_id: str
    name: str
    capture_index: int
    render_index: int
    capture_sessions: tuple[CoreAudioSession, ...] = ()
    render_sessions: tuple[CoreAudioSession, ...] = ()
    capture_endpoint_id: str | None = None
    render_endpoint_id: str | None = None
    capture_sessions_known: bool = False
    render_sessions_known: bool = False


def discover_virtual_cables(
    devices: list[dict[str, Any]],
    host_apis: list[dict[str, Any]],
    endpoints: list[CoreAudioEndpoint],
    *,
    include_sessions: bool,
) -> list[VirtualCablePair]:
    """Pair the capture and render sides of installed VB-CABLE devices."""
    parts: dict[str, dict[str, object]] = {}
    for index, device in enumerate(devices):
        host_api = str(host_apis[int(device["hostapi"])]["name"])
        name = " ".join(str(device["name"]).split())
        match = re.match(r"^CABLE(?:-([A-Z]))?\s+(Input|Output)\s+\(VB-Audio Virtual Cable", name, re.I)
        if "wasapi" not in host_api.casefold() or match is None or "16ch" in name.casefold():
            continue
        letter = (match.group(1) or "").upper()
        stable_id = f"cable:{letter or 'base'}"
        part = parts.setdefault(stable_id, {"letter": letter})
        endpoint = _match_endpoint(name, endpoints)
        if match.group(2).casefold() == "output" and int(device["max_input_channels"]) > 0:
            part["capture_index"] = index
            part["capture_endpoint_id"] = endpoint.endpoint_id if endpoint else None
        elif match.group(2).casefold() == "input" and int(device["max_output_channels"]) > 0:
            part["render_index"] = index
            part["render_endpoint_id"] = endpoint.endpoint_id if endpoint else None

    result: list[VirtualCablePair] = []
    for stable_id, part in parts.items():
        if "capture_index" not in part or "render_index" not in part:
            continue
        capture_endpoint_id = part.get("capture_endpoint_id")
        render_endpoint_id = part.get("render_endpoint_id")
        capture_sessions, capture_sessions_known = (
            _query_sessions(capture_endpoint_id) if include_sessions else ((), False)
        )
        render_sessions, render_sessions_known = (
            _query_sessions(render_endpoint_id) if include_sessions else ((), False)
        )
        result.append(
            VirtualCablePair(
                stable_id=stable_id,
                name=f"CABLE {part['letter'] or 'estándar'}",
                capture_index=int(part["capture_index"]),
                render_index=int(part["render_index"]),
                capture_sessions=capture_sessions,
                render_sessions=render_sessions,
                capture_endpoint_id=(
                    str(capture_endpoint_id) if capture_endpoint_id else None
                ),
                render_endpoint_id=(
                    str(render_endpoint_id) if render_endpoint_id else None
                ),
                capture_sessions_known=capture_sessions_known,
                render_sessions_known=render_sessions_known,
            )
        )
    return sorted(result, key=lambda cable: cable.name.casefold())


def retain_sessions(
    cables: list[VirtualCablePair],
    previous: list[VirtualCablePair],
) -> list[VirtualCablePair]:
    """Carry session observations across topology-only refreshes."""
    previous_by_id = {cable.stable_id: cable for cable in previous}
    return [
        VirtualCablePair(
            stable_id=cable.stable_id,
            name=cable.name,
            capture_index=cable.capture_index,
            render_index=cable.render_index,
            capture_sessions=(
                previous_by_id[cable.stable_id].capture_sessions
                if cable.stable_id in previous_by_id
                else ()
            ),
            render_sessions=(
                previous_by_id[cable.stable_id].render_sessions
                if cable.stable_id in previous_by_id
                else ()
            ),
            capture_endpoint_id=cable.capture_endpoint_id,
            render_endpoint_id=cable.render_endpoint_id,
            capture_sessions_known=(
                previous_by_id[cable.stable_id].capture_sessions_known
                if cable.stable_id in previous_by_id
                else False
            ),
            render_sessions_known=(
                previous_by_id[cable.stable_id].render_sessions_known
                if cable.stable_id in previous_by_id
                else False
            ),
        )
        for cable in cables
    ]


def refresh_sessions(cables: list[VirtualCablePair]) -> list[VirtualCablePair]:
    """Refresh only application sessions, without re-enumerating audio devices."""
    refreshed: list[VirtualCablePair] = []
    for cable in cables:
        capture_sessions, capture_known = _query_sessions(
            cable.capture_endpoint_id
        )
        render_sessions, render_known = _query_sessions(cable.render_endpoint_id)
        refreshed.append(
            VirtualCablePair(
                stable_id=cable.stable_id,
                name=cable.name,
                capture_index=cable.capture_index,
                render_index=cable.render_index,
                capture_sessions=(
                    capture_sessions if capture_known else cable.capture_sessions
                ),
                render_sessions=(
                    render_sessions if render_known else cable.render_sessions
                ),
                capture_endpoint_id=cable.capture_endpoint_id,
                render_endpoint_id=cable.render_endpoint_id,
                capture_sessions_known=capture_known,
                render_sessions_known=render_known,
            )
        )
    return refreshed


def application_session_active(
    cables: list[VirtualCablePair],
    route: CableRoute,
) -> bool | None:
    """Return active application presence, or None when it cannot be observed."""
    cable = _selected_cable(cables, route)
    if cable is None:
        return None
    if route == "remote_input":
        return bool(cable.render_sessions) if cable.render_sessions_known else None
    return bool(cable.capture_sessions) if cable.capture_sessions_known else None


def active_cable_index(
    cables: list[VirtualCablePair],
    route: CableRoute,
    fallback: int | None = None,
) -> int:
    cable = _selected_cable(cables, route)
    if cable is None:
        if fallback is not None:
            return fallback
        raise AudioDeviceError(f"Selected VB-CABLE route {route!r} is not available.")
    return cable.capture_index if route == "remote_input" else cable.render_index


def virtual_cable_snapshot(cables: list[VirtualCablePair]) -> dict[str, object]:
    return {route: _route_snapshot(cables, route) for route in CABLE_ROUTES}


def virtual_cable_signature(cables: list[VirtualCablePair]) -> tuple[object, ...]:
    return tuple(
        (
            cable.stable_id,
            cable.capture_index,
            cable.render_index,
            cable.capture_sessions,
            cable.render_sessions,
            cable.capture_endpoint_id,
            cable.render_endpoint_id,
            cable.capture_sessions_known,
            cable.render_sessions_known,
        )
        for cable in cables
    )


def _route_snapshot(
    cables: list[VirtualCablePair],
    route: CableRoute,
) -> dict[str, object]:
    cable = _selected_cable(cables, route)
    sessions = ()
    if cable is not None:
        sessions = cable.render_sessions if route == "remote_input" else cable.capture_sessions
    applications: dict[str, dict[str, object]] = {}
    for session in sessions:
        if session.state != "active":
            continue
        name = _friendly_application_name(session.application_name)
        applications[name.casefold()] = {
            "name": name,
            "process_id": session.process_id,
            "state": session.state,
            "audio_active": True,
        }
    return {
        "selection": DEFAULT_CABLE_SELECTIONS[route],
        "active_name": cable.name if cable else None,
        "unavailable": cable is None,
        "applications": sorted(
            applications.values(), key=lambda item: str(item["name"]).casefold()
        ),
    }


def _selected_cable(
    cables: list[VirtualCablePair],
    route: CableRoute,
) -> VirtualCablePair | None:
    selection = DEFAULT_CABLE_SELECTIONS[route]
    return next((cable for cable in cables if cable.stable_id == selection), None)


def _match_endpoint(name: str, endpoints: list[CoreAudioEndpoint]) -> CoreAudioEndpoint | None:
    normalized = _normalize(name)
    return next((endpoint for endpoint in endpoints if _normalize(endpoint.name) == normalized), None)


def _query_sessions(
    endpoint_id: object,
) -> tuple[tuple[CoreAudioSession, ...], bool]:
    if not endpoint_id:
        return (), False
    try:
        sessions = query_audio_sessions(str(endpoint_id))
    except OSError:
        return (), False
    return (
        tuple(
            session
            for session in sessions
            if session.process_id != 0
            and session.process_id != os.getpid()
            and session.state == "active"
            and not session.application_name.casefold().startswith("cable-")
        ),
        True,
    )


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _friendly_application_name(value: str) -> str:
    return {
        "ms-teams": "Microsoft Teams",
        "msedge": "Microsoft Edge",
        "msedgewebview2": "Microsoft Edge WebView",
        "windowsterminal": "Terminal de Windows",
    }.get(value.casefold(), value)
