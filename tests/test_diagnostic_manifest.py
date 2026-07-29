"""Tests for atomic call diagnostic manifests."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from audio.diagnostic_manifest import DiagnosticCallManifest


class DiagnosticCallManifestTests(unittest.TestCase):
    def test_writes_devices_formats_tracks_and_timeline(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            manifest = DiagnosticCallManifest(
                enabled=True,
                directory=Path(temporary_directory),
                pipeline_name="agent_to_remote",
                call_id="call-1",
                devices={"input": "Microphone (ID 1)", "output": "Cable (ID 2)"},
                formats={"captured": {"sample_rate": 48_000, "channels": 1}},
            )
            manifest.event("call_audio_gate_changed", active=True)

            path = manifest.close(
                tracks={"captured": ["captured.wav"]},
                statistics={"aec3": {"capture_frames": 4}},
            )

            self.assertIsNotNone(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("call-1", payload["call_id"])
            self.assertEqual("Microphone (ID 1)", payload["devices"]["input"])
            self.assertEqual(["captured.wav"], payload["tracks"]["captured"])
            self.assertTrue(payload["events"][0]["active"])
            self.assertEqual(
                4, payload["statistics"]["aec3"]["capture_frames"]
            )
