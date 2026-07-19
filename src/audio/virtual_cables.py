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
        result.append(
            VirtualCablePair(
                stable_id=stable_id,
                name=f"CABLE {part['letter'] or 'estándar'}",
                capture_index=int(part["capture_index"]),
                render_index=int(part["render_index"]),
                capture_sessions=(
                    _safe_sessions(capture_endpoint_id) if include_sessions else ()
                ),
                render_sessions=(
                    _safe_sessions(render_endpoint_id) if include_sessions else ()
                ),
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
        )
        for cable in cables
    ]


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


def _safe_sessions(endpoint_id: object) -> tuple[CoreAudioSession, ...]:
    if not endpoint_id:
        return ()
    try:
        return tuple(
            session
            for session in query_audio_sessions(str(endpoint_id))
            if session.process_id != 0
            and session.process_id != os.getpid()
            and session.state == "active"
            and not session.application_name.casefold().startswith("cable-")
        )
    except OSError:
        return ()


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _friendly_application_name(value: str) -> str:
    return {
        "ms-teams": "Microsoft Teams",
        "msedge": "Microsoft Edge",
        "msedgewebview2": "Microsoft Edge WebView",
        "windowsterminal": "Terminal de Windows",
    }.get(value.casefold(), value)
