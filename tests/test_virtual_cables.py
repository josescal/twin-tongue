from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from audio.device_manager import AudioDeviceManager
from audio.virtual_cables import (
    VirtualCablePair,
    discover_virtual_cables,
)
from audio.windows_audio import CoreAudioEndpoint, CoreAudioSession


def cable(letter: str, capture: int, render: int) -> VirtualCablePair:
    return VirtualCablePair(
        stable_id=f"cable:{letter}",
        name=f"CABLE {letter}",
        capture_index=capture,
        render_index=render,
        capture_sessions=(CoreAudioSession(20, "Teams", "active"),),
        render_sessions=(CoreAudioSession(10, "Edge", "active"),),
    )


class AudioDeviceManagerCableTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.manager = AudioDeviceManager(
            Path(self.temporary_directory.name) / "preferences.json",
            60,
        )
        with patch(
            "audio.device_manager.discover_audio_topology",
            return_value=([], [cable("A", 31, 24), cable("B", 30, 29)]),
        ):
            await self.manager.refresh(force_sessions=True)

    async def asyncTearDown(self) -> None:
        await self.manager.close()
        self.temporary_directory.cleanup()

    async def test_defaults_keep_cable_a_and_b_on_separate_routes(self) -> None:
        self.assertEqual(31, self.manager.active_index("remote_input"))
        self.assertEqual(29, self.manager.active_index("translated_output"))
        snapshot = self.manager.snapshot()["virtual_cables"]
        self.assertEqual("Edge", snapshot["remote_input"]["applications"][0]["name"])
        self.assertEqual("Teams", snapshot["translated_output"]["applications"][0]["name"])

    async def test_disconnected_cable_keeps_existing_stream_index_as_fallback(self) -> None:
        with patch(
            "audio.device_manager.discover_audio_topology",
            return_value=([], [cable("B", 30, 29)]),
        ):
            await self.manager.refresh(force_sessions=True)
        self.assertTrue(
            self.manager.snapshot()["virtual_cables"]["remote_input"]["unavailable"]
        )
        self.assertEqual(31, self.manager.active_index("remote_input", fallback=31))

    async def test_inactive_applications_are_not_exposed(self) -> None:
        with patch(
            "audio.device_manager.discover_audio_topology",
            return_value=([], [
                VirtualCablePair(
                    "cable:A", "CABLE A", 31, 24,
                    render_sessions=(CoreAudioSession(10, "Edge", "inactive"),),
                ),
                cable("B", 30, 29),
            ]),
        ):
            await self.manager.refresh(force_sessions=True)
        self.assertEqual(
            [],
            self.manager.snapshot()["virtual_cables"]["remote_input"]["applications"],
        )

    async def test_topology_refresh_preserves_the_latest_session_observation(self) -> None:
        with patch(
            "audio.device_manager.discover_audio_topology",
            return_value=([], [
                VirtualCablePair("cable:A", "CABLE A", 31, 24),
                VirtualCablePair("cable:B", "CABLE B", 30, 29),
            ]),
        ):
            await self.manager.refresh()

        applications = self.manager.snapshot()["virtual_cables"]["remote_input"][
            "applications"
        ]
        self.assertEqual("Edge", applications[0]["name"])


class VirtualCableDiscoveryTests(unittest.TestCase):
    def test_pairs_render_and_capture_endpoints_and_ignores_16_channel_variant(self) -> None:
        devices = [
            {"name": "CABLE-A Input (VB-Audio Virtual Cable A)", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
            {"name": "CABLE-A Output (VB-Audio Virtual Cable A)", "hostapi": 0, "max_input_channels": 2, "max_output_channels": 0},
            {"name": "CABLE-A In 16ch (VB-Audio Virtual Cable A)", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 16},
        ]
        endpoints = [
            CoreAudioEndpoint("render-a", devices[0]["name"], "output"),
            CoreAudioEndpoint("capture-a", devices[1]["name"], "input"),
        ]
        with patch("audio.virtual_cables.query_audio_sessions", return_value=[]) as sessions:
            result = discover_virtual_cables(
                devices,
                [{"name": "Windows WASAPI"}],
                endpoints,
                include_sessions=True,
            )

        self.assertEqual(1, len(result))
        self.assertEqual((1, 0), (result[0].capture_index, result[0].render_index))
        self.assertEqual(
            {"capture-a", "render-a"},
            {call.args[0] for call in sessions.call_args_list},
        )


if __name__ == "__main__":
    unittest.main()
