"""Blocking PCM audio capture using SoundDevice raw streams."""

from dataclasses import dataclass
import logging
import math
from threading import RLock

import sounddevice as sd

from audio.buffering import (
    PcmBlockBuffer,
    WRITE_INVALID_SIZE,
    WRITE_STORED,
)
from audio.portaudio import (
    DeviceReference,
    device_name,
    format_device,
    resolve_device,
    validate_input_settings,
)
from audio.pcm import RecordedAudio, calculate_block_bytes, calculate_block_frames

logger = logging.getLogger(__name__)


@dataclass
class QueuedAudioInputStatistics:
    """Counters produced by non-blocking audio input."""

    captured_blocks: int = 0
    dropped_blocks: int = 0
    input_overflows: int = 0
    captured_pcm_bytes: int = 0
    invalid_block_sizes: int = 0


class QueuedAudioInput:
    """Capture raw PCM into a bounded preallocated handoff buffer."""

    def __init__(
        self,
        input_device: DeviceReference,
        sample_rate: int,
        channels: int,
        dtype: str,
        frame_duration_ms: float,
        queue_capacity_blocks: int,
    ) -> None:
        if channels <= 0:
            raise ValueError("Channel count must be greater than zero.")
        if queue_capacity_blocks <= 0:
            raise ValueError("Queue capacity must be greater than zero.")
        self.input_device_reference = input_device
        self.sample_rate = sample_rate
        self.channels = channels
        self.dtype = dtype
        self.frame_duration_ms = frame_duration_ms
        self.frames_per_block = calculate_block_frames(sample_rate, frame_duration_ms)
        self.block_bytes = calculate_block_bytes(self.frames_per_block, channels, dtype)
        self.queue_capacity_blocks = queue_capacity_blocks
        self._blocks = PcmBlockBuffer(queue_capacity_blocks, self.block_bytes)
        self.statistics = QueuedAudioInputStatistics()
        self.input_device: int | None = None
        self.stream: sd.RawInputStream | None = None
        self._stream_lock = RLock()

    def start(self) -> None:
        """Resolve, validate, and start the raw input stream."""
        with self._stream_lock:
            if self.stream is not None:
                raise RuntimeError("Queued audio capture is already started.")
            self._blocks = PcmBlockBuffer(self.queue_capacity_blocks, self.block_bytes)
            self.statistics = QueuedAudioInputStatistics()
            self.input_device = resolve_device(self.input_device_reference, "input")
            self._open_stream()

    def switch_device(self, input_device: DeviceReference) -> bool:
        """Safely reopen capture on another current PortAudio device index."""
        with self._stream_lock:
            resolved = resolve_device(input_device, "input")
            if self.stream is not None and resolved == self.input_device:
                return False
            self.stop()
            self.input_device_reference = input_device
            self.input_device = resolved
            self._open_stream()
            logger.info("event=audio_device_changed direction=input device=%s", format_device(resolved))
            return True

    def _open_stream(self) -> None:
        assert self.input_device is not None
        validate_input_settings(self.input_device, self.sample_rate, self.channels, self.dtype)
        try:
            self.stream = sd.RawInputStream(
                device=self.input_device,
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype=self.dtype,
                blocksize=self.frames_per_block,
                callback=self._input_callback,
            )
            self.stream.start()
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        """Stop and close the input stream if it is open."""
        with self._stream_lock:
            stream = self.stream
            self.stream = None
            if stream is None:
                return
            try:
                stream.stop()
            finally:
                stream.close()

    def pop_block(self) -> bytes | None:
        """Return the oldest captured block without blocking."""
        return self._blocks.pop_bytes()

    def has_pending_blocks(self) -> bool:
        """Return whether captured audio remains queued."""
        return bool(len(self._blocks))

    @property
    def buffered_blocks(self) -> int:
        """Return the approximate number of blocks awaiting consumption."""
        return len(self._blocks)

    def _input_callback(
        self,
        indata: object,
        frames: int,
        time_info: object,
        status: sd.CallbackFlags,
    ) -> None:
        self.statistics.captured_blocks += 1
        self.statistics.captured_pcm_bytes += len(indata)  # type: ignore[arg-type]
        if status.input_overflow:
            self.statistics.input_overflows += 1
        result = self._blocks.write(indata)
        if result != WRITE_STORED:
            self.statistics.dropped_blocks += 1
        if result == WRITE_INVALID_SIZE:
            self.statistics.invalid_block_sizes += 1


def record_audio(
    input_device: DeviceReference,
    duration: float,
    sample_rate: int,
    channels: int,
    dtype: str,
    frame_duration_ms: float,
) -> RecordedAudio:
    """Record PCM audio into memory for a fixed duration."""
    if duration <= 0:
        raise ValueError("Recording duration must be greater than zero.")
    if channels <= 0:
        raise ValueError("Channel count must be greater than zero.")

    identifier = resolve_device(input_device, "input")
    validate_input_settings(identifier, sample_rate, channels, dtype)
    block_frames = calculate_block_frames(sample_rate, frame_duration_ms)
    total_frames = math.ceil(duration * sample_rate)
    blocks: list[bytes] = []
    captured_frames = 0

    logger.info(
        "event=audio_recording_started device_id=%s device=%s sample_rate_hz=%s "
        "channels=%s format=%s frames_per_block=%s duration_seconds=%s",
        identifier,
        device_name(identifier),
        sample_rate,
        channels,
        dtype,
        block_frames,
        duration,
    )
    try:
        with sd.RawInputStream(
            device=identifier,
            samplerate=sample_rate,
            channels=channels,
            dtype=dtype,
            blocksize=block_frames,
        ) as stream:
            while captured_frames < total_frames:
                requested_frames = min(block_frames, total_frames - captured_frames)
                data, overflowed = stream.read(requested_frames)
                if overflowed:
                    logger.warning(
                        "event=audio_input_overflow device_id=%s consequence=audio_loss_possible",
                        identifier,
                    )
                blocks.append(bytes(data))
                captured_frames += requested_frames
    finally:
        logger.info("event=audio_recording_stopped device_id=%s", identifier)
    return RecordedAudio(
        blocks=tuple(blocks),
        sample_rate=sample_rate,
        channels=channels,
        dtype=dtype,
        frames_per_block=block_frames,
        frame_count=captured_frames,
    )
