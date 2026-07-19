import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from audio.device_manager import AudioDeviceManager
from audio.physical_devices import PhysicalDevice
from audio.virtual_cables import VirtualCablePair
from app_state import ApplicationState
from ui import LocalControlServer


class LocalControlServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.state = ApplicationState()
        self.temporary_directory = TemporaryDirectory()
        self.preference_path = Path(self.temporary_directory.name) / "preferences.json"
        self.device_manager = AudioDeviceManager(
            self.preference_path, 60
        )
        self.state.attach_device_manager(self.device_manager)
        with patch(
            "audio.device_manager.discover_audio_topology",
            return_value=(
                [
                    PhysicalDevice("mic-1", "Micrófono integrado", 1, "input", True),
                    PhysicalDevice("speaker-1", "Altavoces del equipo", 2, "output", True),
                ],
                [
                    VirtualCablePair("cable:A", "CABLE A", 3, 4),
                    VirtualCablePair("cable:B", "CABLE B", 5, 6),
                ],
            ),
        ):
            await self.device_manager.refresh(force_sessions=True)
        self.server = LocalControlServer(self.state, "127.0.0.1", 0)
        await self.server.start()

    async def asyncTearDown(self) -> None:
        await self.server.close()
        await self.device_manager.close()
        self.temporary_directory.cleanup()

    async def request(
        self, method: str, path: str, body: dict[str, object] | None = None
    ) -> tuple[int, str, bytes]:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.server.port)
        encoded_body = json.dumps(body).encode("utf-8") if body is not None else b""
        request = (
            f"{method} {path} HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            f"Content-Length: {len(encoded_body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii") + encoded_body
        writer.write(request)
        await writer.drain()
        status_line = (await reader.readline()).decode("ascii")
        status = int(status_line.split(" ", 2)[1])
        headers: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if line == b"\r\n":
                break
            key, value = line.decode("latin-1").split(":", 1)
            headers[key.lower()] = value.strip()
        response_body = await reader.readexactly(int(headers["content-length"]))
        writer.close()
        await writer.wait_closed()
        return status, headers["content-type"], response_body

    async def test_serves_control_page_and_state(self) -> None:
        page_status, page_type, page = await self.request("GET", "/")
        state_status, state_type, body = await self.request("GET", "/api/state")

        self.assertEqual(200, page_status)
        self.assertIn("text/html", page_type)
        decoded_page = page.decode("utf-8")
        self.assertIn('data-pipeline="remote_to_agent"', decoded_page)
        self.assertIn("💬 Idioma de la otra persona", decoded_page)
        self.assertIn("🗣️ Tu idioma (agente)", decoded_page)
        self.assertIn('data-audio-direction="input"', decoded_page)
        self.assertIn('class="device-fixed"', decoded_page)
        self.assertNotIn('select id="agent-input-device"', decoded_page)
        self.assertNotIn("setAudioDevice", decoded_page)
        self.assertIn('data-cable-route="remote_input"', decoded_page)
        self.assertIn("Altavoz de la llamada", decoded_page)
        self.assertIn("Micrófono de la llamada", decoded_page)
        self.assertIn('name="voice-gender"', decoded_page)
        self.assertIn("Hombre", decoded_page)
        self.assertIn("Mujer", decoded_page)
        self.assertIn('id="transcription-tab"', decoded_page)
        self.assertIn("Transcripción del agente", decoded_page)
        self.assertIn("source-block", decoded_page)
        self.assertIn("translated-block", decoded_page)
        self.assertIn('id="help-tab"', decoded_page)
        self.assertIn('id="help-panel"', decoded_page)
        self.assertIn("Cómo usar Twin Tongue", decoded_page)
        self.assertIn('id="application-version"', decoded_page)
        self.assertIn("state.application_version", decoded_page)
        self.assertIn("Inicia la llamada. Cuando esté activa", decoded_page)
        self.assertIn("Habla con un volumen alto y claro", decoded_page)
        self.assertIn("La transcripción y la traducción pueden contener errores", decoded_page)
        self.assertIn("La traducción tampoco es instantánea", decoded_page)
        self.assertLess(
            decoded_page.index("Altavoz de la llamada"),
            decoded_page.index("Micrófono de la llamada"),
        )
        self.assertIn("Traducción sin conexión", decoded_page)
        self.assertNotIn("routing-help", decoded_page)
        self.assertNotIn("applications-title", decoded_page)
        self.assertEqual(200, state_status)
        self.assertIn("application/json", state_type)
        self.assertEqual("passthrough", json.loads(body)["pipelines"]["remote_to_agent"]["mode"])

    async def test_put_changes_pipeline_mode(self) -> None:
        status, _, body = await self.request(
            "PUT", "/api/pipelines/agent_to_remote", {"mode": "translate"}
        )

        self.assertEqual(200, status)
        self.assertEqual(
            "translate",
            json.loads(body)["pipelines"]["agent_to_remote"]["mode"],
        )
        self.assertEqual("translate", self.state.get_mode("agent_to_remote").value)

    async def test_shutdown_closes_an_active_event_stream_promptly(self) -> None:
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", self.server.port
        )
        writer.write(
            b"GET /api/events HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Connection: keep-alive\r\n\r\n"
        )
        await writer.drain()
        response_headers = await reader.readuntil(b"\r\n\r\n")
        self.assertIn(b"text/event-stream", response_headers)
        await reader.readuntil(b"\n\n")

        await asyncio.wait_for(self.server.close(), timeout=1.0)

        self.assertEqual(b"", await asyncio.wait_for(reader.read(), timeout=1.0))
        writer.close()
        await writer.wait_closed()

    async def test_rejects_invalid_mode(self) -> None:
        status, _, body = await self.request(
            "PUT", "/api/pipelines/remote_to_agent", {"mode": "disabled"}
        )

        self.assertEqual(400, status)
        self.assertIn("Invalid mode", json.loads(body)["error"])

    async def test_put_changes_participant_language(self) -> None:
        status, _, body = await self.request(
            "PUT", "/api/languages/agent", {"language": "ca"}
        )

        self.assertEqual(200, status)
        self.assertEqual("ca", json.loads(body)["languages"]["agent"])
        self.assertEqual("ca", self.state.get_language("agent"))

    async def test_put_changes_voice_gender(self) -> None:
        status, _, body = await self.request(
            "PUT", "/api/voice-gender", {"gender": "female"}
        )

        self.assertEqual(200, status)
        self.assertEqual("female", json.loads(body)["voice_gender"])
        self.assertEqual("female", self.state.get_voice_gender().value)
        saved = json.loads(
            self.preference_path.read_text(encoding="utf-8")
        )
        self.assertEqual("female", saved["voice_gender"])

    async def test_put_rejects_invalid_voice_gender(self) -> None:
        status, _, body = await self.request(
            "PUT", "/api/voice-gender", {"gender": "other"}
        )

        self.assertEqual(400, status)
        self.assertIn("Invalid voice gender", json.loads(body)["error"])

    async def test_delete_clears_agent_transcription(self) -> None:
        self.state.publish_agent_final(1, "Hola", "es", "en")

        status, _, body = await self.request("DELETE", "/api/transcription")

        self.assertEqual(200, status)
        self.assertEqual([], json.loads(body)["transcription"]["entries"])

    async def test_physical_audio_selection_endpoint_is_not_available(self) -> None:
        status, _, body = await self.request(
            "PUT", "/api/audio-devices/input", {"selection": "mic-1"}
        )

        self.assertEqual(404, status)
        self.assertIn("Route not found", json.loads(body)["error"])

    async def test_virtual_cable_routes_are_read_only(self) -> None:
        status, _, body = await self.request(
            "PUT", "/api/virtual-cables/remote_input", {"selection": "cable:B"}
        )

        self.assertEqual(404, status)
        self.assertIn("Route not found", json.loads(body)["error"])
