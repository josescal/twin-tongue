"""Blocking PCM audio playback using SoundDevice raw streams."""

from collections.abc import Iterable
import logging

import sounddevice as sd

from audio.buffering import PcmBlockBuffer, WRITE_INVALID_SIZE, WRITE_STORED
from audio.pcm import RecordedAudio, calculate_block_bytes
from audio.portaudio import DeviceReference, device_name, resolve_device, validate_output_settings

logger = logging.getLogger(__name__)


class QueuedAudioOutput:
    """Continuously play captured PCM blocks through a callback-backed stream."""

    def __init__(
        self,
        output_device: DeviceReference,
        sample_rate: int,
        channels: int,
        dtype: str,
        frames_per_block: int,
        queue_capacity_blocks: int = 10,
    ) -> None:
        if queue_capacity_blocks <= 0:
            raise ValueError("Queue capacity must be greater than zero.")
        self.output_device = resolve_device(output_device, "output")
        self.sample_rate = sample_rate
        self.channels = channels
        self.dtype = dtype
        self.frames_per_block = frames_per_block
        self.block_bytes = calculate_block_bytes(frames_per_block, channels, dtype)
        self.queue_capacity_blocks = queue_capacity_blocks
        self._blocks = PcmBlockBuffer(queue_capacity_blocks, self.block_bytes)
        self._silence = bytes(self.block_bytes)
        self.stream: sd.RawOutputStream | None = None
        self.dropped_blocks = 0
        self.played_blocks = 0
        self.output_underflows = 0
        self.empty_buffer_events = 0
        self.invalid_block_sizes = 0
        validate_output_settings(self.output_device, sample_rate, channels, dtype)

    @property
    def started(self) -> bool:
        return self.stream is not None

    @property
    def buffered_blocks(self) -> int:
        """Return the approximate number of blocks awaiting playback."""
        return len(self._blocks)

    def start(self, initial_blocks: Iterable[bytes] = ()) -> None:
        if self.stream is not None:
            return
        self._blocks = PcmBlockBuffer(self.queue_capacity_blocks, self.block_bytes)
        self.dropped_blocks = 0
        self.played_blocks = 0
        self.output_underflows = 0
        self.empty_buffer_events = 0
        self.invalid_block_sizes = 0
        for block in initial_blocks:
            self._enqueue(block)
        self.stream = sd.RawOutputStream(
            device=self.output_device,
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype=self.dtype,
            blocksize=self.frames_per_block,
            callback=self._output_callback,
        )
        try:
            self.stream.start()
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        stream = self.stream
        self.stream = None
        if stream is None:
            return
        try:
            stream.stop()
        finally:
            stream.close()

    def switch_device(self, output_device: DeviceReference) -> bool:
        """Safely reopen a running live output on a different device."""
        resolved = resolve_device(output_device, "output")
        if resolved == self.output_device:
            return False
        was_started = self.started
        self.stop()
        validate_output_settings(resolved, self.sample_rate, self.channels, self.dtype)
        self.output_device = resolved
        if was_started:
            self.start()
        logger.info("event=audio_device_changed direction=output device=%s", device_name(resolved))
        return True

    def push_block(self, block: bytes) -> None:
        if self.stream is None:
            raise RuntimeError("Queued audio output is not started.")
        self._enqueue(block)

    def _enqueue(self, block: bytes) -> None:
        result = self._blocks.write(block)
        if result != WRITE_STORED:
            self.dropped_blocks += 1
        if result == WRITE_INVALID_SIZE:
            self.invalid_block_sizes += 1

    def _output_callback(
        self,
        outdata: object,
        frames: int,
        time_info: object,
        status: sd.CallbackFlags,
    ) -> None:
        if status is not None and status.output_underflow:
            self.output_underflows += 1
        if len(outdata) != self.block_bytes:  # type: ignore[arg-type]
            self.invalid_block_sizes += 1
            raise sd.CallbackAbort
        if self._blocks.read_into(outdata):
            self.played_blocks += 1
            return
        outdata[:] = self._silence  # type: ignore[index]
        self.empty_buffer_events += 1


def play_audio(audio: RecordedAudio, output_device: DeviceReference) -> None:
    """Play recorded PCM blocks without conversion or resampling."""
    identifier = resolve_device(output_device, "output")
    try:
        validate_output_settings(identifier, audio.sample_rate, audio.channels, audio.dtype)
    except RuntimeError as error:
        raise RuntimeError(f"{error} Resampling and format conversion are not implemented.") from error

    logger.info(
        "event=audio_playback_started device_id=%s device=%s sample_rate_hz=%s "
        "channels=%s format=%s frames_per_block=%s",
        identifier,
        device_name(identifier),
        audio.sample_rate,
        audio.channels,
        audio.dtype,
        audio.frames_per_block,
    )
    try:
        with sd.RawOutputStream(
            device=identifier,
            samplerate=audio.sample_rate,
            channels=audio.channels,
            dtype=audio.dtype,
            blocksize=audio.frames_per_block,
        ) as stream:
            for block in audio.blocks:
                underflowed = stream.write(block)
                if underflowed:
                    logger.warning(
                        "event=audio_output_underflow device_id=%s consequence=audio_glitch_possible",
                        identifier,
                    )
    finally:
        logger.info("event=audio_playback_stopped device_id=%s", identifier)
