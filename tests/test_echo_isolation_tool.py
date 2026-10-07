"""Tests for the controlled physical echo-isolation tool."""

import unittest
from unittest.mock import patch

from audio.portaudio import AudioDeviceInfo
from tools.test_echo_isolation import _device


DEVICES = [
    AudioDeviceInfo(
        identifier=25,
        name="Realtek headphones",
        host_api="Windows WASAPI",
        max_input_channels=0,
        max_output_channels=2,
        default_sample_rate=48_000,
        default_input=False,
        default_output=False,
    ),
    AudioDeviceInfo(
        identifier=32,
        name="Realtek microphone",
        host_api="Windows WASAPI",
        max_input_channels=2,
        max_output_channels=0,
        default_sample_rate=48_000,
        default_input=False,
        default_output=False,
    ),
]


class EchoIsolationToolTests(unittest.TestCase):
    def test_numeric_portaudio_identifier_is_accepted(self) -> None:
        with patch(
            "tools.test_echo_isolation.describe_devices",
            return_value=DEVICES,
        ):
            self.assertEqual(32, _device("32", "input"))
            self.assertEqual(25, _device("25", "output"))

    def test_numeric_identifier_must_support_the_requested_direction(self) -> None:
        with (
            patch(
                "tools.test_echo_isolation.describe_devices",
                return_value=DEVICES,
            ),
            self.assertRaisesRegex(RuntimeError, "WASAPI input"),
        ):
            _device("25", "input")


if __name__ == "__main__":
    unittest.main()
