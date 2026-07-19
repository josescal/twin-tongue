"""Tests for stable host-qualified audio device selection."""

import unittest
from unittest.mock import patch

from audio.portaudio import AudioDeviceError, resolve_device


DEVICES = [
    {
        "name": "CABLE-A Output (VB-Audio Virtual Cable A)",
        "hostapi": 0,
        "max_input_channels": 16,
        "max_output_channels": 0,
    },
    {
        "name": "CABLE-A Output (VB-Audio Virtual Cable A)",
        "hostapi": 1,
        "max_input_channels": 2,
        "max_output_channels": 0,
    },
]
HOST_APIS = [{"name": "MME"}, {"name": "Windows WASAPI"}]


class AudioDeviceResolutionTests(unittest.TestCase):
    def test_host_qualified_name_selects_wasapi_device(self) -> None:
        with (
            patch("audio.portaudio.query_devices", return_value=DEVICES),
            patch("audio.portaudio.query_host_apis", return_value=HOST_APIS),
        ):
            identifier = resolve_device(
                "Windows WASAPI::CABLE-A Output (VB-Audio Virtual Cable A)",
                "input",
            )

        self.assertEqual(identifier, 1)

    def test_unqualified_duplicate_name_is_ambiguous(self) -> None:
        with patch("audio.portaudio.query_devices", return_value=DEVICES):
            with self.assertRaisesRegex(AudioDeviceError, "ambiguous"):
                resolve_device("CABLE-A Output", "input")

    def test_rejects_incomplete_host_qualified_name(self) -> None:
        with patch("audio.portaudio.query_devices", return_value=DEVICES):
            with self.assertRaisesRegex(AudioDeviceError, "both parts"):
                resolve_device("Windows WASAPI::", "input")


if __name__ == "__main__":
    unittest.main()
