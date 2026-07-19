"""List audio devices reported by SoundDevice and PortAudio."""

import argparse
import logging

from audio.portaudio import AudioDeviceError, AudioDeviceInfo, describe_devices
from config import configure_logging


def parse_args() -> argparse.Namespace:
    """Parse command-line filters."""
    parser = argparse.ArgumentParser(description="List available PortAudio devices.")
    parser.add_argument("--inputs", action="store_true", help="Show devices with input channels only.")
    parser.add_argument("--outputs", action="store_true", help="Show devices with output channels only.")
    parser.add_argument("--host-api", help="Filter by host API name, for example WASAPI.")
    return parser.parse_args()


def filter_devices(devices: list[AudioDeviceInfo], args: argparse.Namespace) -> list[AudioDeviceInfo]:
    """Apply the requested capability and host API filters."""
    result = devices
    if args.inputs:
        result = [device for device in result if device.max_input_channels > 0]
    if args.outputs:
        result = [device for device in result if device.max_output_channels > 0]
    if args.host_api:
        fragment = args.host_api.casefold()
        result = [device for device in result if fragment in device.host_api.casefold()]
    return result


def print_table(devices: list[AudioDeviceInfo]) -> None:
    """Print a dependency-free text table."""
    headers = ("ID", "Name", "Host API", "Inputs", "Outputs", "Default Hz", "Default")
    rows = [
        (
            str(device.identifier),
            device.name,
            device.host_api,
            str(device.max_input_channels),
            str(device.max_output_channels),
            f"{device.default_sample_rate:g}",
            _default_marker(device),
        )
        for device in devices
    ]
    widths = [max(len(header), *(len(row[index]) for row in rows)) for index, header in enumerate(headers)]
    print("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def _default_marker(device: AudioDeviceInfo) -> str:
    markers: list[str] = []
    if device.default_input:
        markers.append("INPUT")
    if device.default_output:
        markers.append("OUTPUT")
    return ", ".join(markers) or "-"


def main() -> int:
    """List devices and return a process exit code."""
    configure_logging()
    args = parse_args()
    try:
        devices = filter_devices(describe_devices(), args)
    except AudioDeviceError as error:
        logging.error("%s", error)
        return 1
    if not devices:
        print("No audio devices matched the requested filters.")
        return 0
    print_table(devices)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
