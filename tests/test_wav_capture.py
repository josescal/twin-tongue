"""Tests for diagnostic WAV capture."""

from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import MagicMock, patch
import wave

from audio.wav_capture import (
    DiagnosticWavCapture,
    QueuedDiagnosticWavCapture,
    repair_incomplete_wav_headers,
)


def _first_nonzero_int16_frame(pcm: bytes) -> int:
    for index in range(0, len(pcm), 2):
        if pcm[index : index + 2] != b"\x00\x00":
            return index // 2
    raise AssertionError("PCM contains only silence")


class DiagnosticWavCaptureTests(unittest.TestCase):
    def test_writes_pcm_int16_wav_file(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            capture = DiagnosticWavCapture(
                enabled=True,
                directory=directory,
                pipeline_name="remote_to_agent",
                sample_rate=16_000,
                max_seconds_per_file=60.0,
            )

            capture.write(bytes(3_200))
            capture.close()

            files = list(directory.glob("remote_to_agent-stt-*.wav"))
            self.assertEqual(1, len(files))
            with wave.open(str(files[0]), "rb") as wav_file:
                self.assertEqual(1, wav_file.getnchannels())
                self.assertEqual(2, wav_file.getsampwidth())
                self.assertEqual(16_000, wav_file.getframerate())
                self.assertEqual(1_600, wav_file.getnframes())
            marks = [
                json.loads(line)
                for line in files[0].with_suffix(".jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                ["capture_started", "audio_block", "capture_closed"],
                [mark["event"] for mark in marks],
            )
            self.assertEqual(0, marks[1]["wav_frame_start"])
            self.assertEqual(1_600, marks[1]["wav_frame_end"])
            self.assertIsNone(marks[1]["wall_gap_ms"])

    def test_disabled_capture_does_not_create_files(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            capture = DiagnosticWavCapture(
                enabled=False,
                directory=directory,
                pipeline_name="remote_to_agent",
                sample_rate=16_000,
            )

            capture.write(bytes(3_200))
            capture.close()

            self.assertEqual([], list(directory.glob("*.wav")))
            self.assertEqual([], list(directory.glob("*.jsonl")))

    def test_timing_marks_can_be_disabled_to_reduce_filesystem_activity(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            capture = DiagnosticWavCapture(
                enabled=True,
                directory=directory,
                pipeline_name="agent_to_remote",
                sample_rate=16_000,
                write_timing_marks=False,
            )

            for _ in range(20):
                capture.write(bytes(640))
            capture.close()

            files = list(directory.glob("agent_to_remote-stt-*.wav"))
            self.assertEqual(1, len(files))
            self.assertEqual([], list(directory.glob("*.jsonl")))
            with wave.open(str(files[0]), "rb") as wav_file:
                self.assertEqual(6_400, wav_file.getnframes())

    def test_tracks_share_sample_zero_and_pad_their_first_observation(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            origin = 100.0
            captured = DiagnosticWavCapture(
                enabled=True,
                directory=directory,
                pipeline_name="remote_to_agent",
                stream_name="captured",
                sample_rate=1_000,
                session_id="aligned",
                write_timing_marks=False,
            )
            played = DiagnosticWavCapture(
                enabled=True,
                directory=directory,
                pipeline_name="remote_to_agent",
                stream_name="played",
                sample_rate=1_000,
                session_id="aligned",
                write_timing_marks=False,
            )

            captured.start_session(origin)
            played.start_session(origin)
            captured.write(
                b"\x01\x00" * 20,
                observed_at_monotonic=100.100,
            )
            played.write(
                b"\x02\x00" * 20,
                observed_at_monotonic=100.300,
            )
            captured.close()
            played.close()

            captured_path = directory / "remote_to_agent-captured-aligned.wav"
            played_path = directory / "remote_to_agent-played-aligned.wav"
            with wave.open(str(captured_path), "rb") as wav_file:
                captured_pcm = wav_file.readframes(wav_file.getnframes())
            with wave.open(str(played_path), "rb") as wav_file:
                played_pcm = wav_file.readframes(wav_file.getnframes())

            self.assertEqual(80, _first_nonzero_int16_frame(captured_pcm))
            self.assertEqual(280, _first_nonzero_int16_frame(played_pcm))

    def test_pcm_blocks_are_batched_before_writing_to_disk(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            writer = MagicMock()
            with patch("audio.wav_capture.wave.open", return_value=writer):
                capture = DiagnosticWavCapture(
                    enabled=True,
                    directory=Path(temporary_directory),
                    pipeline_name="agent_to_remote",
                    sample_rate=16_000,
                    write_timing_marks=False,
                    write_buffer_bytes=6_400,
                    flush_interval_seconds=60.0,
                )

                for _ in range(9):
                    capture.write(bytes(640))
                writer.writeframes.assert_not_called()

                capture.write(bytes(640))
                writer.writeframes.assert_called_once()
                self.assertEqual(6_400, len(writer.writeframes.call_args.args[0]))
                capture.close()

    def test_queued_capture_writes_named_stream_without_blocking_caller(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            capture = QueuedDiagnosticWavCapture(
                enabled=True,
                directory=directory,
                pipeline_name="remote_to_agent",
                stream_name="input",
                sample_rate=48_000,
                channels=2,
            )

            capture.write(bytes(3_840))
            capture.shutdown()

            files = list(directory.glob("remote_to_agent-input-*.wav"))
            self.assertEqual(1, len(files))
            with wave.open(str(files[0]), "rb") as wav_file:
                self.assertEqual(2, wav_file.getnchannels())
                self.assertEqual(48_000, wav_file.getframerate())
                self.assertEqual(960, wav_file.getnframes())

    def test_flush_keeps_header_readable_before_graceful_close(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            capture = DiagnosticWavCapture(
                enabled=True,
                directory=directory,
                pipeline_name="agent_to_remote",
                stream_name="sent",
                sample_rate=24_000,
                write_buffer_bytes=960,
            )

            try:
                capture.write(bytes(960))
                path = next(directory.glob("agent_to_remote-sent-*.wav"))
                with wave.open(str(path), "rb") as wav_file:
                    self.assertEqual(480, wav_file.getnframes())
            finally:
                capture.close()

    def test_repairs_an_interrupted_wav_header_from_payload_size(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            path = directory / "agent_to_remote-sent-interrupted.wav"
            with wave.open(str(path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(24_000)
                wav_file.writeframes(bytes(4_800))
            with path.open("r+b") as wav_file:
                wav_file.seek(4)
                wav_file.write((996).to_bytes(4, "little"))
                wav_file.seek(40)
                wav_file.write((960).to_bytes(4, "little"))

            repaired = repair_incomplete_wav_headers(directory)

            self.assertEqual([path], repaired)
            with wave.open(str(path), "rb") as wav_file:
                self.assertEqual(2_400, wav_file.getnframes())


if __name__ == "__main__":
    unittest.main()
