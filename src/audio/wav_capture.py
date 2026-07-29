"""Diagnostic WAV capture for audio sent to speech-to-text."""

from __future__ import annotations

from datetime import datetime
import json
import logging
from pathlib import Path
from queue import Empty, Full, Queue
import re
import struct
from threading import Lock, Thread
import time
from typing import TextIO
import wave

logger = logging.getLogger(__name__)
_CLOSE_SESSION = object()
_SHUTDOWN = object()
DEFAULT_WRITE_BUFFER_BYTES = 64 * 1024
DEFAULT_FLUSH_INTERVAL_SECONDS = 0.5


def repair_incomplete_wav_headers(directory: Path) -> list[Path]:
    """Repair stale PCM WAV sizes left behind by an interrupted process."""
    repaired: list[Path] = []
    if not directory.exists():
        return repaired
    for path in directory.glob("*.wav"):
        try:
            if _repair_wav_header(path):
                repaired.append(path)
                logger.warning(
                    "event=diagnostic_wav_header_repaired file=%s",
                    path,
                )
        except (OSError, ValueError):
            logger.exception(
                "event=diagnostic_wav_header_repair_failed file=%s",
                path,
            )
    return repaired


def _repair_wav_header(path: Path) -> bool:
    size = path.stat().st_size
    if size < 44:
        return False
    with path.open("r+b") as wav_file:
        header = wav_file.read(min(size, 4096))
        if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            raise ValueError(f"{path} is not a RIFF/WAVE file.")
        offset = 12
        data_size_offset: int | None = None
        data_start: int | None = None
        declared_data_size = 0
        while offset + 8 <= len(header):
            chunk_id = header[offset : offset + 4]
            chunk_size = struct.unpack_from("<I", header, offset + 4)[0]
            if chunk_id == b"data":
                data_size_offset = offset + 4
                data_start = offset + 8
                declared_data_size = chunk_size
                break
            offset += 8 + chunk_size + (chunk_size % 2)
        if data_size_offset is None or data_start is None:
            raise ValueError(f"{path} has no readable data chunk.")
        actual_data_size = size - data_start
        declared_riff_size = struct.unpack_from("<I", header, 4)[0]
        actual_riff_size = size - 8
        if (
            declared_data_size == actual_data_size
            and declared_riff_size == actual_riff_size
        ):
            return False
        wav_file.seek(4)
        wav_file.write(struct.pack("<I", actual_riff_size))
        wav_file.seek(data_size_offset)
        wav_file.write(struct.pack("<I", actual_data_size))
        wav_file.flush()
        return True


class DiagnosticWavCapture:
    """Write PCM int16 blocks to rotating WAV files when diagnostics are enabled."""

    def __init__(
        self,
        *,
        enabled: bool,
        directory: Path,
        pipeline_name: str,
        stream_name: str = "stt",
        sample_rate: int,
        channels: int = 1,
        sample_width_bytes: int = 2,
        max_seconds_per_file: float = 120.0,
        write_timing_marks: bool = True,
        write_buffer_bytes: int = DEFAULT_WRITE_BUFFER_BYTES,
        flush_interval_seconds: float = DEFAULT_FLUSH_INTERVAL_SECONDS,
        session_id: str | None = None,
        log_file_creation: bool = True,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("WAV capture sample rate must be greater than zero.")
        if channels <= 0:
            raise ValueError("WAV capture channels must be greater than zero.")
        if sample_width_bytes <= 0:
            raise ValueError("WAV capture sample width must be greater than zero.")
        if max_seconds_per_file <= 0:
            raise ValueError("WAV capture max file duration must be greater than zero.")
        if write_buffer_bytes <= 0:
            raise ValueError("WAV capture write buffer must be greater than zero.")
        if flush_interval_seconds <= 0:
            raise ValueError("WAV capture flush interval must be greater than zero.")
        self._enabled = enabled
        self._directory = directory
        self._pipeline_name = _safe_filename(pipeline_name)
        self._stream_name = _safe_filename(stream_name)
        self._sample_rate = sample_rate
        self._channels = channels
        self._sample_width_bytes = sample_width_bytes
        self._max_frames_per_file = int(sample_rate * max_seconds_per_file)
        self._write_timing_marks = write_timing_marks
        self._write_buffer_bytes = write_buffer_bytes
        self._flush_interval_seconds = flush_interval_seconds
        self._timestamp = session_id or datetime.now().strftime("%Y%m%d-%H%M%S")
        self._log_file_creation = log_file_creation
        self._file_index = 0
        self._frames_written = 0
        self._writer: wave.Wave_write | None = None
        self._marks_writer: TextIO | None = None
        self._pcm_buffer = bytearray()
        self._marks_buffer: list[str] = []
        self._last_buffer_flush_monotonic = time.monotonic()
        self._last_write_monotonic: float | None = None
        self._files: list[Path] = []

    @property
    def enabled(self) -> bool:
        """Return whether the capture will write audio."""
        return self._enabled

    @property
    def files(self) -> tuple[Path, ...]:
        return tuple(self._files)

    def write(self, pcm: bytes) -> None:
        """Append one PCM block to the current WAV file."""
        if not self._enabled or not pcm:
            return
        bytes_per_frame = self._channels * self._sample_width_bytes
        if len(pcm) % bytes_per_frame:
            raise ValueError("WAV capture received an incomplete PCM frame.")
        frames = len(pcm) // bytes_per_frame
        if self._writer is None or self._frames_written + frames > self._max_frames_per_file:
            self._rotate_file()
        assert self._writer is not None
        write_monotonic = time.monotonic()
        frame_start = self._frames_written
        self._pcm_buffer.extend(pcm)
        self._frames_written += frames
        if self._write_timing_marks:
            self._write_mark(
                {
                    "event": "audio_block",
                    "captured_at": datetime.now()
                    .astimezone()
                    .isoformat(timespec="milliseconds"),
                    "wav_frame_start": frame_start,
                    "wav_frame_end": self._frames_written,
                    "wav_offset_start_seconds": frame_start / self._sample_rate,
                    "wav_offset_end_seconds": self._frames_written / self._sample_rate,
                    "frames": frames,
                    "wall_gap_ms": (
                        None
                        if self._last_write_monotonic is None
                        else round(
                            (write_monotonic - self._last_write_monotonic) * 1000,
                            3,
                        )
                    ),
                }
            )
            self._last_write_monotonic = write_monotonic
        if (
            len(self._pcm_buffer) >= self._write_buffer_bytes
            or write_monotonic - self._last_buffer_flush_monotonic
            >= self._flush_interval_seconds
        ):
            self.flush()

    def flush(self) -> None:
        """Write accumulated PCM and timing marks in filesystem-friendly batches."""
        if self._writer is not None and self._pcm_buffer:
            # writeframes() patches RIFF/data lengths on every flush. A file
            # therefore remains readable even if the process exits before close().
            self._writer.writeframes(bytes(self._pcm_buffer))
            file_handle = getattr(self._writer, "_file", None)
            if file_handle is not None:
                file_handle.flush()
            self._pcm_buffer.clear()
        if self._marks_writer is not None and self._marks_buffer:
            self._marks_writer.writelines(self._marks_buffer)
            self._marks_writer.flush()
            self._marks_buffer.clear()
        self._last_buffer_flush_monotonic = time.monotonic()

    def close(self) -> None:
        """Close the current WAV file, if one was opened."""
        if self._writer is not None:
            if self._write_timing_marks:
                self._write_mark(
                    {
                        "event": "capture_closed",
                        "captured_at": datetime.now()
                        .astimezone()
                        .isoformat(timespec="milliseconds"),
                        "wav_frames": self._frames_written,
                        "wav_duration_seconds": self._frames_written
                        / self._sample_rate,
                    }
                )
            self.flush()
            self._writer.close()
            self._writer = None
        if self._marks_writer is not None:
            self._marks_writer.close()
            self._marks_writer = None
        self._last_write_monotonic = None

    def _rotate_file(self) -> None:
        self.close()
        self._directory.mkdir(parents=True, exist_ok=True)
        suffix = f"-{self._file_index:03d}" if self._file_index else ""
        path = self._directory / (
            f"{self._pipeline_name}-{self._stream_name}-{self._timestamp}{suffix}.wav"
        )
        self._file_index += 1
        writer = wave.open(str(path), "wb")
        writer.setnchannels(self._channels)
        writer.setsampwidth(self._sample_width_bytes)
        writer.setframerate(self._sample_rate)
        self._writer = writer
        self._files.append(path)
        self._frames_written = 0
        marks_path = path.with_suffix(".jsonl") if self._write_timing_marks else None
        if marks_path is not None:
            self._marks_writer = marks_path.open("w", encoding="utf-8")
        if self._write_timing_marks:
            self._write_mark(
                {
                    "event": "capture_started",
                    "captured_at": datetime.now()
                    .astimezone()
                    .isoformat(timespec="milliseconds"),
                    "wav_file": path.name,
                    "sample_rate": self._sample_rate,
                    "channels": self._channels,
                    "sample_width_bytes": self._sample_width_bytes,
                }
            )
        log = logger.info if self._log_file_creation else logger.debug
        log(
            "event=diagnostic_wav_capture_started stream=%s file=%s marks_file=%s "
            "sample_rate=%s channels=%s",
            self._stream_name,
            path,
            marks_path or "disabled",
            self._sample_rate,
            self._channels,
        )

    def _write_mark(self, payload: dict[str, object]) -> None:
        if self._marks_writer is None:
            return
        self._marks_buffer.append(json.dumps(payload, ensure_ascii=False) + "\n")


class QueuedDiagnosticWavCapture:
    """Write diagnostic audio on a dedicated thread without blocking audio routing."""

    def __init__(
        self,
        *,
        enabled: bool,
        directory: Path,
        pipeline_name: str,
        stream_name: str,
        sample_rate: int,
        channels: int,
        sample_width_bytes: int = 2,
        max_seconds_per_file: float = 120.0,
        queue_capacity_blocks: int = 500,
        write_timing_marks: bool = True,
        write_buffer_bytes: int = DEFAULT_WRITE_BUFFER_BYTES,
        flush_interval_seconds: float = DEFAULT_FLUSH_INTERVAL_SECONDS,
        session_id: str | None = None,
        log_file_creation: bool = True,
    ) -> None:
        if queue_capacity_blocks <= 0:
            raise ValueError("WAV capture queue capacity must be greater than zero.")
        self._enabled = enabled
        self._capture = DiagnosticWavCapture(
            enabled=enabled,
            directory=directory,
            pipeline_name=pipeline_name,
            stream_name=stream_name,
            sample_rate=sample_rate,
            channels=channels,
            sample_width_bytes=sample_width_bytes,
            max_seconds_per_file=max_seconds_per_file,
            write_timing_marks=write_timing_marks,
            write_buffer_bytes=write_buffer_bytes,
            flush_interval_seconds=flush_interval_seconds,
            session_id=session_id,
            log_file_creation=log_file_creation,
        )
        self._flush_interval_seconds = flush_interval_seconds
        self._queue: Queue[bytes | object] = Queue(maxsize=queue_capacity_blocks)
        self._thread: Thread | None = None
        self._thread_lock = Lock()
        self.dropped_blocks = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def files(self) -> tuple[Path, ...]:
        return self._capture.files

    def start(self) -> None:
        if not self._enabled:
            return
        with self._thread_lock:
            if self._thread is not None:
                return
            self._thread = Thread(
                target=self._run,
                name="diagnostic WAV writer",
                daemon=True,
            )
            self._thread.start()

    def write(self, pcm: bytes) -> None:
        """Queue a PCM block without waiting on filesystem I/O."""
        if not self._enabled or not pcm:
            return
        self.start()
        try:
            self._queue.put_nowait(bytes(pcm))
        except Full:
            self.dropped_blocks += 1

    def close_session(self) -> None:
        """Drain queued blocks and close the current file."""
        if (
            not self._enabled
            or self._thread is None
            or not self._thread.is_alive()
        ):
            return
        self._queue.put(_CLOSE_SESSION)

    def shutdown(self) -> None:
        """Drain queued blocks, close files, and stop the writer thread."""
        if not self._enabled:
            return
        thread = self._thread
        if thread is None:
            self._capture.close()
            return
        if not thread.is_alive():
            self._thread = None
            self._capture.close()
            return
        self._queue.put(_SHUTDOWN)
        thread.join()
        self._thread = None

    def _run(self) -> None:
        try:
            while True:
                try:
                    item = self._queue.get(timeout=self._flush_interval_seconds)
                except Empty:
                    self._capture.flush()
                    continue
                try:
                    if item is _SHUTDOWN:
                        return
                    if item is _CLOSE_SESSION:
                        self._capture.close()
                    else:
                        self._capture.write(item)  # type: ignore[arg-type]
                finally:
                    self._queue.task_done()
        except Exception:
            logger.exception(
                "event=diagnostic_wav_capture_failed action=recording_stopped"
            )
        finally:
            self._capture.close()


def _safe_filename(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return safe.strip("-") or "pipeline"
