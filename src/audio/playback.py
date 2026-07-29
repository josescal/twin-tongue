"""Blocking PCM audio playback using SoundDevice raw streams."""

from collections.abc import Callable, Iterable
import logging
import time

import sounddevice as sd

from audio.buffering import (
    PcmBlockBuffer,
    WRITE_DROPPED_OLDEST,
    WRITE_INVALID_SIZE,
    WRITE_STORED,
)
from audio.pcm import RecordedAudio, calculate_block_bytes
from audio.portaudio import DeviceReference, device_name, resolve_device, validate_output_settings

logger = logging.getLogger(__name__)

ANTI_CLICK_FADE_MS = 5.0


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
        played_observer: Callable[[bytes], None] | None = None,
        exclusive: bool = False,
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
        self.played_observer = played_observer
        self.exclusive = exclusive
        self._blocks = PcmBlockBuffer(queue_capacity_blocks, self.block_bytes)
        self._silence = bytes(self.block_bytes)
        self._fade_frames = min(
            frames_per_block,
            max(2, round(sample_rate * ANTI_CLICK_FADE_MS / 1000)),
        )
        self._last_frame = [0] * channels
        self._recovering_from_empty_buffer = False
        self._discontinuity_pending = False
        self.stream: sd.RawOutputStream | None = None
        self.dropped_blocks = 0
        self.played_blocks = 0
        self.output_underflows = 0
        self.empty_buffer_events = 0
        self.invalid_block_sizes = 0
        self.maximum_buffered_blocks = 0
        self.maximum_callback_gap_ms = 0.0
        self._last_callback_at: float | None = None
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
        self._last_callback_at = None
        self._last_frame[:] = [0] * self.channels
        self._recovering_from_empty_buffer = False
        self._discontinuity_pending = False
        for block in initial_blocks:
            self._enqueue(block)
        self.stream = sd.RawOutputStream(
            device=self.output_device,
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype=self.dtype,
            blocksize=self.frames_per_block,
            extra_settings=(
                sd.WasapiSettings(exclusive=True)
                if self.exclusive
                else None
            ),
            callback=self._output_callback,
        )
        try:
            self.stream.start()
            logger.info(
                "event=audio_output_stream_opened device_id=%s mode=%s "
                "sample_rate_hz=%s channels=%s",
                self.output_device,
                "exclusive" if self.exclusive else "shared",
                self.sample_rate,
                self.channels,
            )
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

    def discard_pending_blocks(self) -> int:
        """Drop buffered audio, for example after an output-mode switch."""
        return self._blocks.clear()

    def _enqueue(self, block: bytes) -> None:
        result = self._blocks.write(block)
        self.maximum_buffered_blocks = max(
            self.maximum_buffered_blocks,
            len(self._blocks),
        )
        if result != WRITE_STORED:
            self.dropped_blocks += 1
        if result == WRITE_DROPPED_OLDEST and self.played_blocks > 0:
            self._discontinuity_pending = True
        if result == WRITE_INVALID_SIZE:
            self.invalid_block_sizes += 1

    def _output_callback(
        self,
        outdata: object,
        frames: int,
        time_info: object,
        status: sd.CallbackFlags,
    ) -> None:
        callback_at = time.perf_counter()
        if self._last_callback_at is not None:
            self.maximum_callback_gap_ms = max(
                self.maximum_callback_gap_ms,
                (callback_at - self._last_callback_at) * 1000,
            )
        self._last_callback_at = callback_at
        if status is not None and status.output_underflow:
            self.output_underflows += 1
        if len(outdata) != self.block_bytes:  # type: ignore[arg-type]
            self.invalid_block_sizes += 1
            raise sd.CallbackAbort
        if self._blocks.read_into(outdata):
            if self._recovering_from_empty_buffer:
                self._fade_in(outdata)
                self._recovering_from_empty_buffer = False
                self._discontinuity_pending = False
            elif self._discontinuity_pending:
                self._crossfade_from_last_frame(outdata)
                self._discontinuity_pending = False
            self._remember_last_frame(outdata)
            self.played_blocks += 1
        else:
            outdata[:] = self._silence  # type: ignore[index]
            self._fade_out(outdata)
            self._recovering_from_empty_buffer = True
            self.empty_buffer_events += 1
        if self.played_observer is not None:
            try:
                # AEC3 requires the complete render timeline, including
                # callbacks that physically emitted silence.
                self.played_observer(bytes(outdata))  # type: ignore[arg-type]
            except Exception:
                logger.exception(
                    "event=audio_played_observer_failed action=observer_ignored"
                )

    def _fade_out(self, outdata: object) -> None:
        """Reach digital silence smoothly after the queued audio runs dry."""
        if self.dtype != "int16" or not any(self._last_frame):
            for channel in range(self.channels):
                self._last_frame[channel] = 0
            return
        samples = memoryview(outdata).cast("h")
        denominator = max(1, self._fade_frames - 1)
        for frame in range(self._fade_frames):
            remaining = denominator - frame
            offset = frame * self.channels
            for channel in range(self.channels):
                samples[offset + channel] = (
                    self._last_frame[channel] * remaining // denominator
                )
        for channel in range(self.channels):
            self._last_frame[channel] = 0

    def _fade_in(self, outdata: object) -> None:
        """Resume queued audio smoothly after one or more silent callbacks."""
        if self.dtype != "int16":
            return
        samples = memoryview(outdata).cast("h")
        denominator = max(1, self._fade_frames - 1)
        for frame in range(self._fade_frames):
            offset = frame * self.channels
            for channel in range(self.channels):
                samples[offset + channel] = (
                    samples[offset + channel] * frame // denominator
                )

    def _crossfade_from_last_frame(self, outdata: object) -> None:
        """Smooth the first block after old queued audio had to be discarded."""
        if self.dtype != "int16":
            return
        samples = memoryview(outdata).cast("h")
        denominator = max(1, self._fade_frames - 1)
        for frame in range(self._fade_frames):
            previous_weight = denominator - frame
            offset = frame * self.channels
            for channel in range(self.channels):
                current = samples[offset + channel]
                samples[offset + channel] = (
                    self._last_frame[channel] * previous_weight
                    + current * frame
                ) // denominator

    def _remember_last_frame(self, outdata: object) -> None:
        if self.dtype != "int16":
            return
        samples = memoryview(outdata).cast("h")
        offset = (self.frames_per_block - 1) * self.channels
        for channel in range(self.channels):
            self._last_frame[channel] = samples[offset + channel]


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
