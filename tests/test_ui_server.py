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
        self.state = ApplicationState(audio_recording_enabled=True)
        self.temporary_directory = TemporaryDirectory()
        self.preference_path = Path(self.temporary_directory.name) / "preferences.json"
        self.device_manager = AudioDeviceManager(self.preference_path, 60)
        self.state.attach_device_manager(self.device_manager)
        with patch(
            "audio.device_manager.discover_audio_topology",
            return_value=(
                [
                    PhysicalDevice("mic-1", "Integrated microphone", 1, "input", True),
                    PhysicalDevice("speaker-1", "Computer speakers", 2, "output", True),
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

    async def test_serves_bilingual_control_page_and_state(self) -> None:
        page_status, page_type, page = await self.request("GET", "/")
        state_status, state_type, body = await self.request("GET", "/api/state")

        self.assertEqual(200, page_status)
        self.assertIn("text/html", page_type)
        decoded_page = page.decode("utf-8")
        self.assertIn('data-pipeline="remote_to_agent"', decoded_page)
        self.assertIn('data-ui-language="es"', decoded_page)
        self.assertIn('data-ui-language="en"', decoded_page)
        self.assertIn("🇪🇸 ES", decoded_page)
        self.assertIn("🇬🇧 EN", decoded_page)
        self.assertIn("Other person's language", decoded_page)
        self.assertIn("Idioma de la otra persona", decoded_page)
        self.assertIn("Your language", decoded_page)
        self.assertIn("Tu idioma", decoded_page)
        self.assertIn('data-audio-direction="input"', decoded_page)
        self.assertIn('select id="agent-input-device"', decoded_page)
        self.assertIn('select id="agent-output-device"', decoded_page)
        self.assertIn("setAudioDevice", decoded_page)
        self.assertIn('data-cable-route="remote_input"', decoded_page)
        self.assertIn("Call speaker", decoded_page)
        self.assertIn("Altavoz de la llamada", decoded_page)
        self.assertIn("Call microphone", decoded_page)
        self.assertIn("Micrófono de la llamada", decoded_page)
        self.assertIn('name="voice-gender"', decoded_page)
        self.assertIn('class="settings-panel"', decoded_page)
        self.assertIn('id="all-translation-button"', decoded_page)
        self.assertNotIn('data-i18n="youHear"', decoded_page)
        self.assertNotIn('data-i18n="theyHear"', decoded_page)
        self.assertNotIn("You currently hear", decoded_page)
        self.assertNotIn("Ahora escuchas", decoded_page)
        self.assertNotIn("Por ahora se está usando", decoded_page)
        self.assertNotIn("recuperará la selección guardada", decoded_page)
        self.assertIn('id="offline-notice"', decoded_page)
        self.assertIn("El proceso de Twin Tongue está desconectado", decoded_page)
        self.assertIn('recording.classList.remove("active")', decoded_page)
        self.assertIn("Male", decoded_page)
        self.assertIn("Female", decoded_page)
        self.assertIn("Hombre", decoded_page)
        self.assertIn("Mujer", decoded_page)
        self.assertIn('id="transcription-tab"', decoded_page)
        self.assertIn("Conversation transcription", decoded_page)
        self.assertIn("Transcripción de la conversación", decoded_page)
        self.assertIn("Other person said", decoded_page)
        self.assertIn("La otra persona ha dicho", decoded_page)
        self.assertIn("You hear", decoded_page)
        self.assertIn("Tú escuchas", decoded_page)
        self.assertIn("remote-turn", decoded_page)
        self.assertIn("agent-turn", decoded_page)
        self.assertIn("source-block", decoded_page)
        self.assertIn("translated-block", decoded_page)
        self.assertIn("softSegmentLimit", decoded_page)
        self.assertIn("endsAtSentenceBoundary", decoded_page)
        self.assertIn('id="help-tab"', decoded_page)
        self.assertIn('id="help-panel"', decoded_page)
        self.assertIn("How to use Twin Tongue", decoded_page)
        self.assertNotIn("The saved device is unavailable", decoded_page)
        self.assertIn("Cómo usar Twin Tongue", decoded_page)
        self.assertIn('id="application-version"', decoded_page)
        self.assertIn("state.application_version", decoded_page)
        self.assertIn('id="audio-recording"', decoded_page)
        self.assertIn("Diagnostic recording", decoded_page)
        self.assertIn("setAudioRecording", decoded_page)
        self.assertIn("Start the call and check", decoded_page)
        self.assertIn("Inicia la llamada y comprueba", decoded_page)
        self.assertIn("Speak clearly and keep a steady volume", decoded_page)
        self.assertIn("Habla claro y con volumen estable", decoded_page)
        self.assertIn("Transcription and translation can contain errors", decoded_page)
        self.assertIn("La transcripción y la traducción pueden contener errores", decoded_page)
        self.assertLess(
            decoded_page.index("Call speaker"),
            decoded_page.index("Call microphone"),
        )
        self.assertIn("No call application connected", decoded_page)
        self.assertIn("Ninguna aplicación de llamada conectada", decoded_page)
        self.assertNotIn("Translation not connected", decoded_page)
        self.assertNotIn("Traducción sin conexión", decoded_page)
        self.assertNotIn("routing-help", decoded_page)
        self.assertNotIn("applications-title", decoded_page)
        self.assertEqual(200, state_status)
        self.assertIn("application/json", state_type)
        state = json.loads(body)
        self.assertEqual("passthrough", state["pipelines"]["remote_to_agent"]["mode"])
        self.assertEqual("en", state["ui_language"])
        self.assertEqual(["en", "es"], state["supported_ui_languages"])
        self.assertTrue(state["audio_recording"]["enabled"])
        self.assertFalse(state["audio_recording"]["active"])

    async def test_put_starts_and_stops_manual_audio_recording(self) -> None:
        started_status, _, started_body = await self.request(
            "PUT", "/api/audio-recording", {"active": True}
        )
        stopped_status, _, stopped_body = await self.request(
            "PUT", "/api/audio-recording", {"active": False}
        )

        self.assertEqual(200, started_status)
        self.assertTrue(json.loads(started_body)["audio_recording"]["manual"])
        self.assertEqual(200, stopped_status)
        self.assertFalse(json.loads(stopped_body)["audio_recording"]["active"])

    async def test_put_rejects_non_boolean_audio_recording_state(self) -> None:
        status, _, body = await self.request(
            "PUT", "/api/audio-recording", {"active": "yes"}
        )

        self.assertEqual(400, status)
        self.assertIn("boolean 'active'", json.loads(body)["error"])

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

    async def test_put_changes_both_pipeline_modes(self) -> None:
        status, _, body = await self.request(
            "PUT", "/api/pipelines", {"mode": "translate"}
        )

        self.assertEqual(200, status)
        pipelines = json.loads(body)["pipelines"]
        self.assertEqual("translate", pipelines["remote_to_agent"]["mode"])
        self.assertEqual("translate", pipelines["agent_to_remote"]["mode"])

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
        saved = json.loads(self.preference_path.read_text(encoding="utf-8"))
        self.assertEqual("ca", saved["languages"]["agent"])

    async def test_put_changes_ui_language(self) -> None:
        status, _, body = await self.request(
            "PUT", "/api/ui-language", {"language": "es"}
        )

        self.assertEqual(200, status)
        self.assertEqual("es", json.loads(body)["ui_language"])
        self.assertEqual("es", self.state.get_ui_language())
        saved = json.loads(self.preference_path.read_text(encoding="utf-8"))
        self.assertEqual("es", saved["ui_language"])

    async def test_put_rejects_invalid_ui_language(self) -> None:
        status, _, body = await self.request(
            "PUT", "/api/ui-language", {"language": "fr"}
        )

        self.assertEqual(400, status)
        self.assertIn("Unsupported UI language", json.loads(body)["error"])

    async def test_put_changes_voice_gender(self) -> None:
        status, _, body = await self.request(
            "PUT", "/api/voice-gender", {"gender": "female"}
        )

        self.assertEqual(200, status)
        self.assertEqual("female", json.loads(body)["voice_gender"])
        self.assertEqual("female", self.state.get_voice_gender().value)
        saved = json.loads(self.preference_path.read_text(encoding="utf-8"))
        self.assertEqual("female", saved["voice_gender"])

    async def test_put_rejects_invalid_voice_gender(self) -> None:
        status, _, body = await self.request(
            "PUT", "/api/voice-gender", {"gender": "other"}
        )

        self.assertEqual(400, status)
        self.assertIn("Invalid voice gender", json.loads(body)["error"])

    async def test_delete_clears_conversation_transcription(self) -> None:
        self.state.publish_transcript_final(
            "agent_to_remote", 1, "Hola", "es", "en"
        )

        status, _, body = await self.request("DELETE", "/api/transcription")

        self.assertEqual(200, status)
        self.assertEqual([], json.loads(body)["transcription"]["entries"])

    async def test_put_changes_and_persists_physical_audio_selection(self) -> None:
        status, _, body = await self.request(
            "PUT", "/api/audio-devices/input", {"selection": "mic-1"}
        )

        self.assertEqual(200, status)
        selected = json.loads(body)["audio_devices"]["input"]
        self.assertEqual("manual", selected["mode"])
        self.assertEqual("mic-1", selected["selection"])
        saved = json.loads(self.preference_path.read_text(encoding="utf-8"))
        self.assertEqual("mic-1", saved["input"]["stable_id"])

    async def test_put_rejects_unknown_physical_audio_selection(self) -> None:
        status, _, body = await self.request(
            "PUT", "/api/audio-devices/input", {"selection": "missing"}
        )

        self.assertEqual(400, status)
        self.assertIn("Unknown or unavailable", json.loads(body)["error"])

    async def test_virtual_cable_routes_are_read_only(self) -> None:
        status, _, body = await self.request(
            "PUT", "/api/virtual-cables/remote_input", {"selection": "cable:B"}
        )

        self.assertEqual(404, status)
        self.assertIn("Route not found", json.loads(body)["error"])
