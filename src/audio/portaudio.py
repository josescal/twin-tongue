"""PortAudio device discovery, resolution, and stream validation."""

from dataclasses import dataclass
from typing import Any, Literal

import sounddevice as sd

DeviceDirection = Literal["input", "output"]
DeviceReference = int | str | None


class AudioDeviceError(RuntimeError):
    """Raised when an audio device cannot be queried, selected, or configured."""


@dataclass(frozen=True)
class AudioDeviceInfo:
    """Display-friendly audio device information."""

    identifier: int
    name: str
    host_api: str
    max_input_channels: int
    max_output_channels: int
    default_sample_rate: float
    default_input: bool
    default_output: bool


def query_devices() -> list[dict[str, Any]]:
    """Return all devices reported by PortAudio."""
    try:
        return [dict(device) for device in sd.query_devices()]
    except (sd.PortAudioError, OSError) as error:
        raise AudioDeviceError(f"Could not query audio devices through PortAudio: {error}") from error


def query_host_apis() -> list[dict[str, Any]]:
    """Return all host APIs reported by PortAudio."""
    try:
        return [dict(host_api) for host_api in sd.query_hostapis()]
    except (sd.PortAudioError, OSError) as error:
        raise AudioDeviceError(f"Could not query PortAudio host APIs: {error}") from error


def get_default_device(direction: DeviceDirection) -> int:
    """Return the default device identifier for a direction."""
    try:
        identifier = sd.default.device[0 if direction == "input" else 1]
    except (sd.PortAudioError, OSError, TypeError, ValueError) as error:
        raise AudioDeviceError(f"Could not determine the default {direction} device: {error}") from error
    if identifier is None or int(identifier) < 0:
        raise AudioDeviceError(f"No default {direction} device is configured.")
    return int(identifier)


def describe_devices() -> list[AudioDeviceInfo]:
    """Return devices enriched with host API and default information."""
    devices = query_devices()
    host_apis = query_host_apis()
    default_input = _optional_default_device("input")
    default_output = _optional_default_device("output")
    result: list[AudioDeviceInfo] = []
    for identifier, device in enumerate(devices):
        host_api_index = int(device["hostapi"])
        host_api_name = str(host_apis[host_api_index]["name"])
        result.append(
            AudioDeviceInfo(
                identifier=identifier,
                name=_clean_name(device["name"]),
                host_api=host_api_name,
                max_input_channels=int(device["max_input_channels"]),
                max_output_channels=int(device["max_output_channels"]),
                default_sample_rate=float(device["default_samplerate"]),
                default_input=identifier == default_input,
                default_output=identifier == default_output,
            )
        )
    return result


def resolve_device(requested: DeviceReference, direction: DeviceDirection) -> int:
    """Resolve a default, numeric, or optionally host-qualified name reference."""
    devices = query_devices()
    if not devices:
        raise AudioDeviceError(f"No audio devices are available for requested direction {direction}.")
    if requested is None:
        identifier = get_default_device(direction)
        if identifier >= len(devices):
            raise AudioDeviceError(f"The default {direction} device identifier {identifier} is not available.")
        _ensure_capability(identifier, devices[identifier], direction, "default")
        return identifier

    numeric_identifier = _parse_identifier(requested)
    if numeric_identifier is not None:
        if numeric_identifier < 0 or numeric_identifier >= len(devices):
            raise AudioDeviceError(
                f"Requested {direction} device {requested!r} does not exist; "
                f"valid identifiers are 0 to {len(devices) - 1}."
            )
        _ensure_capability(numeric_identifier, devices[numeric_identifier], direction, requested)
        return numeric_identifier

    requested_name = str(requested).strip()
    if not requested_name:
        raise AudioDeviceError(f"Requested {direction} device name is empty.")
    host_api_fragment, name_fragment = _parse_name_selector(requested_name)
    host_apis = query_host_apis() if host_api_fragment is not None else []
    matches = [
        (identifier, device)
        for identifier, device in enumerate(devices)
        if name_fragment in str(device["name"]).casefold()
        and (
            host_api_fragment is None
            or host_api_fragment
            in str(host_apis[int(device["hostapi"])]["name"]).casefold()
        )
    ]
    if not matches:
        raise AudioDeviceError(f"Requested {direction} device {requested!r} was not found.")
    if len(matches) > 1:
        choices = ", ".join(
            _format_match(identifier, device, host_apis)
            for identifier, device in matches
        )
        raise AudioDeviceError(
            f"Requested {direction} device {requested!r} is ambiguous. Matches: {choices}. "
            "Use 'Host API::device name' or a numeric identifier."
        )
    identifier, device = matches[0]
    _ensure_capability(identifier, device, direction, requested)
    return identifier


def supports_input(device: int | dict[str, Any]) -> bool:
    """Return whether a device has input channels."""
    info = _device_info(device)
    return int(info["max_input_channels"]) > 0


def supports_output(device: int | dict[str, Any]) -> bool:
    """Return whether a device has output channels."""
    info = _device_info(device)
    return int(info["max_output_channels"]) > 0


def validate_input_settings(device: int, sample_rate: int, channels: int, dtype: str) -> None:
    """Validate an input stream configuration."""
    _validate_settings(device, "input", sample_rate, channels, dtype)


def validate_output_settings(device: int, sample_rate: int, channels: int, dtype: str) -> None:
    """Validate an output stream configuration."""
    _validate_settings(device, "output", sample_rate, channels, dtype)


def device_name(identifier: int) -> str:
    """Return a device name for logging and diagnostics."""
    devices = query_devices()
    if identifier < 0 or identifier >= len(devices):
        raise AudioDeviceError(f"Audio device identifier {identifier} does not exist.")
    return _clean_name(devices[identifier]["name"])


def format_device(identifier: int) -> str:
    """Return the standard display label for a selected device."""
    return f"{device_name(identifier)} (ID {identifier})"


def _validate_settings(device: int, direction: DeviceDirection, sample_rate: int, channels: int, dtype: str) -> None:
    checker = sd.check_input_settings if direction == "input" else sd.check_output_settings
    try:
        checker(device=device, samplerate=sample_rate, channels=channels, dtype=dtype)
    except (sd.PortAudioError, ValueError, TypeError) as error:
        raise AudioDeviceError(
            f"Invalid {direction} configuration for device {device!r}: "
            f"format={dtype}, sample_rate={sample_rate} Hz, channels={channels}. {error}"
        ) from error


def _optional_default_device(direction: DeviceDirection) -> int | None:
    try:
        return get_default_device(direction)
    except AudioDeviceError:
        return None


def _parse_identifier(requested: int | str) -> int | None:
    if isinstance(requested, int):
        return requested
    value = requested.strip()
    if value.lstrip("+-").isdigit():
        return int(value)
    return None


def _parse_name_selector(requested: str) -> tuple[str | None, str]:
    if "::" not in requested:
        return None, requested.casefold()
    host_api, device_name_fragment = requested.split("::", 1)
    host_api = host_api.strip().casefold()
    device_name_fragment = device_name_fragment.strip().casefold()
    if not host_api or not device_name_fragment:
        raise AudioDeviceError(
            "A host-qualified device must use 'Host API::device name' with both parts present."
        )
    return host_api, device_name_fragment


def _format_match(
    identifier: int,
    device: dict[str, Any],
    host_apis: list[dict[str, Any]],
) -> str:
    if host_apis:
        host_api = _clean_name(host_apis[int(device["hostapi"])]["name"])
        return f"{identifier}: {host_api}::{_clean_name(device['name'])}"
    return f"{identifier}: {_clean_name(device['name'])}"


def _ensure_capability(identifier: int, device: dict[str, Any], direction: DeviceDirection, requested: object) -> None:
    supported = supports_input(device) if direction == "input" else supports_output(device)
    if not supported:
        raise AudioDeviceError(
            f"Requested {direction} device {requested!r} resolved to {identifier}: {_clean_name(device['name'])}, "
            f"but it has no {direction} channels."
        )


def _device_info(device: int | dict[str, Any]) -> dict[str, Any]:
    if isinstance(device, dict):
        return device
    devices = query_devices()
    if device < 0 or device >= len(devices):
        raise AudioDeviceError(f"Audio device identifier {device} does not exist.")
    return devices[device]


def _clean_name(value: object) -> str:
    return " ".join(str(value).split())
