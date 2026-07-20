import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from audio.device_manager import AudioDeviceManager, discover_audio_topology
from audio.physical_devices import PhysicalDevice, discover_physical_devices
from audio.windows_audio import CoreAudioEndpoint


def device(
    stable_id: str,
    name: str,
    index: int,
    direction: str = "input",
    default: bool = False,
) -> PhysicalDevice:
    return PhysicalDevice(stable_id, name, index, direction, default)  # type: ignore[arg-type]


class AudioDeviceManagerPhysicalSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.preference_path = Path(self.temporary_directory.name) / "preferences.json"
        self.manager = AudioDeviceManager(self.preference_path, 60)

    async def asyncTearDown(self) -> None:
        await self.manager.close()
        self.temporary_directory.cleanup()

    async def refresh_with(self, devices: list[PhysicalDevice]) -> None:
        with patch(
            "audio.device_manager.discover_audio_topology",
            return_value=(devices, []),
        ):
            await self.manager.refresh(force_sessions=True)

    async def test_startup_uses_independent_automatic_communications_defaults(self) -> None:
        await self.refresh_with([
            device("mic-realtek", "Micrófono integrado", 7, default=True),
            device("mic-jabra", "Jabra Evolve2", 11),
            device("speaker-realtek", "Altavoces del equipo", 8, "output"),
            device("speaker-jabra", "Jabra Evolve2", 12, "output", True),
        ])

        snapshot = self.manager.snapshot()["audio_devices"]
        self.assertEqual("Micrófono integrado", snapshot["input"]["active_name"])
        self.assertEqual("Jabra Evolve2", snapshot["output"]["active_name"])

    async def test_usb_or_bluetooth_connection_and_default_change_switch_automatic(self) -> None:
        await self.refresh_with([
            device("mic-realtek", "Micrófono integrado", 7, default=True),
            device("speaker-realtek", "Altavoces", 8, "output", True),
        ])
        initial_revision = self.manager.revision("input")
        change = asyncio.create_task(
            self.manager.wait_for_change("input", initial_revision)
        )
        await asyncio.sleep(0)
        await self.refresh_with([
            device("mic-realtek", "Micrófono integrado", 9),
            device("mic-headset", "Cascos Bluetooth", 13, default=True),
            device("speaker-realtek", "Altavoces", 10, "output"),
            device("speaker-headset", "Cascos Bluetooth", 14, "output", True),
        ])

        self.assertGreater(await asyncio.wait_for(change, 1), initial_revision)
        self.assertEqual(13, self.manager.active_index("input"))
        self.assertEqual(14, self.manager.active_index("output"))

    async def test_preferences_are_persisted_and_survive_restart(self) -> None:
        self.manager.set_voice_gender("remote_to_agent", "female")
        self.manager.set_ui_language("es")
        self.manager.set_participant_language("remote", "fr")
        saved = json.loads(self.preference_path.read_text(encoding="utf-8"))
        self.assertEqual(
            {"remote_to_agent": "female", "agent_to_remote": "male"},
            saved["voice_genders"],
        )
        self.assertEqual("es", saved["ui_language"])
        self.assertEqual({"agent": "es", "remote": "fr"}, saved["languages"])

        restarted = AudioDeviceManager(self.preference_path, 60)
        self.assertEqual("female", restarted.voice_genders["remote_to_agent"])
        self.assertEqual("male", restarted.voice_genders["agent_to_remote"])
        self.assertEqual("es", restarted.ui_language)
        self.assertEqual({"agent": "es", "remote": "fr"}, restarted.participant_languages)

    async def test_manual_devices_are_persisted_and_restored_by_stable_id(self) -> None:
        available = [
            device("mic-integrated", "Integrated microphone", 7, default=True),
            device("mic-headset", "Headset microphone", 11),
            device("speaker-integrated", "Computer speakers", 8, "output", True),
            device("speaker-headset", "Headset headphones", 12, "output"),
        ]
        await self.refresh_with(available)

        await self.manager.set_physical_device("input", "mic-headset")
        await self.manager.set_physical_device("output", "speaker-headset")

        self.assertEqual(11, self.manager.active_index("input"))
        self.assertEqual(12, self.manager.active_index("output"))
        saved = json.loads(self.preference_path.read_text(encoding="utf-8"))
        self.assertEqual(
            {"mode": "manual", "stable_id": "mic-headset", "name": "Headset microphone"},
            saved["audio_devices"]["input"],
        )
        restarted = AudioDeviceManager(self.preference_path, 60)
        with patch(
            "audio.device_manager.discover_audio_topology",
            return_value=(available, []),
        ):
            await restarted.refresh(force_sessions=True)
        self.assertEqual(11, restarted.active_index("input"))
        await restarted.close()

    async def test_automatic_selection_can_be_restored(self) -> None:
        await self.refresh_with([
            device("mic-integrated", "Integrated microphone", 7, default=True),
            device("mic-headset", "Headset microphone", 11),
        ])
        await self.manager.set_physical_device("input", "mic-headset")

        await self.manager.set_physical_device("input", "automatic")

        self.assertEqual(7, self.manager.active_index("input"))
        snapshot = self.manager.snapshot()["audio_devices"]["input"]
        self.assertEqual("automatic", snapshot["selection"])
        saved = json.loads(self.preference_path.read_text(encoding="utf-8"))
        self.assertEqual("automatic", saved["audio_devices"]["input"]["mode"])

    async def test_unavailable_preference_warns_and_uses_the_current_default(self) -> None:
        await self.refresh_with([
            device("mic-default", "Integrated microphone", 7, default=True),
            device("mic-headset", "Headset microphone", 11),
        ])
        await self.manager.set_physical_device("input", "mic-headset")

        with self.assertLogs("audio.physical_devices", level="WARNING") as captured:
            await self.refresh_with([
                device("mic-default", "Integrated microphone", 9, default=True),
            ])

        snapshot = self.manager.snapshot()["audio_devices"]["input"]
        self.assertFalse(snapshot["preferred_available"])
        self.assertEqual("Headset microphone", snapshot["preferred_name"])
        self.assertEqual("Integrated microphone", snapshot["active_name"])
        self.assertEqual(9, self.manager.active_index("input"))
        self.assertIn("fallback=Integrated microphone", captured.output[0])

    async def test_configured_voice_defaults_are_independent(self) -> None:
        preference_path = Path(self.temporary_directory.name) / "voice-defaults.json"
        manager = AudioDeviceManager(
            preference_path,
            60,
            default_voice_genders={
                "remote_to_agent": "female",
                "agent_to_remote": "male",
            },
        )

        self.assertEqual(
            {"remote_to_agent": "female", "agent_to_remote": "male"},
            manager.voice_genders,
        )
        saved = json.loads(preference_path.read_text(encoding="utf-8"))
        self.assertEqual(manager.voice_genders, saved["voice_genders"])
        self.assertEqual("en", saved["ui_language"])
        self.assertEqual({"agent": "es", "remote": "en"}, saved["languages"])

    async def test_application_sessions_are_refreshed_less_often_than_topology(self) -> None:
        with patch(
            "audio.device_manager.discover_audio_topology",
            return_value=([], []),
        ) as discover:
            await self.manager.refresh(force_sessions=True)
            await self.manager.refresh()

        self.assertEqual([True, False], [call.args[0] for call in discover.call_args_list])


class PhysicalDeviceDiscoveryTests(unittest.TestCase):
    def test_vb_cable_is_hidden_and_core_audio_id_is_used(self) -> None:
        portaudio_devices = [
            {"name": "CABLE-A Output (VB-Audio Virtual Cable A)", "hostapi": 0, "max_input_channels": 2, "max_output_channels": 0},
            {"name": "Micrófono (Realtek Audio)", "hostapi": 0, "max_input_channels": 2, "max_output_channels": 0},
        ]
        endpoints = [
            CoreAudioEndpoint("endpoint-cable", "CABLE-A Output (VB-Audio Virtual Cable A)", "input"),
            CoreAudioEndpoint("endpoint-mic", "Micrófono (Realtek Audio)", "input", True),
        ]
        discovered = discover_physical_devices(
            portaudio_devices,
            [{"name": "Windows WASAPI"}],
            endpoints,
        )

        self.assertEqual(1, len(discovered))
        self.assertEqual("coreaudio:endpoint-mic", discovered[0].stable_id)
        self.assertNotIn("VB-Audio", discovered[0].name)

    def test_unified_discovery_enumerates_each_backend_once(self) -> None:
        with (
            patch("audio.device_manager.query_devices", return_value=[]) as devices,
            patch("audio.device_manager.query_host_apis", return_value=[]) as host_apis,
            patch(
                "audio.device_manager.query_core_audio_endpoints",
                return_value=[],
            ) as endpoints,
        ):
            discover_audio_topology(include_sessions=False)

        devices.assert_called_once_with()
        host_apis.assert_called_once_with()
        endpoints.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
