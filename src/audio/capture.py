"""Blocking PCM audio capture using SoundDevice raw streams."""

import asyncio
from collections.abc import Iterable
from collections import deque
from dataclasses import dataclass
import logging
import math
from threading import Event, Lock, RLock
import time

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
    query_devices,
    resolve_device,
    validate_input_settings,
)
from audio.pcm import RecordedAudio, calculate_block_bytes, calculate_block_frames

logger = logging.getLogger(__name__)
DEFAULT_FIRST_CALLBACK_TIMEOUT_SECONDS = 0.9
DIAGNOSTIC_CAPTURE_TIMEOUT_GRACE_SECONDS = 3.0
COMMON_INPUT_SAMPLE_RATES = (48_000, 44_100, 32_000, 16_000)
_ACTIVE_INPUTS_LOCK = Lock()
_ACTIVE_INPUTS: dict[int, "QueuedAudioInput"] = {}


class AudioInputUnresponsiveError(RuntimeError):
    """Raised when an input stream opens but never delivers audio callbacks."""


@dataclass(frozen=True)
class InputStreamFormat:
    """A concrete PCM format accepted by an input endpoint."""

    sample_rate: int
    channels: int


@dataclass
class QueuedAudioInputStatistics:
    """Counters produced by non-blocking audio input."""

    captured_blocks: int = 0
    dropped_blocks: int = 0
    input_overflows: int = 0
    captured_pcm_bytes: int = 0
    invalid_block_sizes: int = 0
    maximum_buffered_blocks: int = 0
    maximum_callback_gap_ms: float = 0


@dataclass
class _DiagnosticCaptureTap:
    """Bounded copy of live callback audio for a user-triggered loopback test."""

    target_frames: int
    blocks: list[bytes]
    completed: Event
    captured_frames: int = 0
    cancelled: bool = False


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
        *,
        negotiate_format: bool = False,
        exclusive: bool = False,
        first_callback_timeout_seconds: float = DEFAULT_FIRST_CALLBACK_TIMEOUT_SECONDS,
    ) -> None:
        if channels <= 0:
            raise ValueError("Channel count must be greater than zero.")
        if queue_capacity_blocks <= 0:
            raise ValueError("Queue capacity must be greater than zero.")
        if first_callback_timeout_seconds <= 0:
            raise ValueError("First callback timeout must be greater than zero.")
        self.input_device_reference = input_device
        self.sample_rate = sample_rate
        self.channels = channels
        self.dtype = dtype
        self.frame_duration_ms = frame_duration_ms
        self.frames_per_block = calculate_block_frames(sample_rate, frame_duration_ms)
        self.block_bytes = calculate_block_bytes(self.frames_per_block, channels, dtype)
        self.queue_capacity_blocks = queue_capacity_blocks
        self.negotiate_format = negotiate_format
        self.exclusive = exclusive
        self.first_callback_timeout_seconds = first_callback_timeout_seconds
        self._blocks = PcmBlockBuffer(queue_capacity_blocks, self.block_bytes)
        self.statistics = QueuedAudioInputStatistics()
        self.input_device: int | None = None
        self.stream: sd.RawInputStream | None = None
        self._stream_lock = RLock()
        self._last_callback_at: float | None = None
        self._first_callback = Event()
        self._prepared_device: int | None = None
        self._data_loop: asyncio.AbstractEventLoop | None = None
        self._data_event: asyncio.Event | None = None
        self._level_lock = Lock()
        self._recent_levels: deque[tuple[float, float, float]] = deque()
        self._diagnostic_tap_lock = Lock()
        self._diagnostic_tap: _DiagnosticCaptureTap | None = None

    def start(self) -> None:
        """Resolve, validate, and start the raw input stream."""
        with self._stream_lock:
            if self.stream is not None:
                raise RuntimeError("Queued audio capture is already started.")
            self._blocks = PcmBlockBuffer(self.queue_capacity_blocks, self.block_bytes)
            self.statistics = QueuedAudioInputStatistics()
            self._last_callback_at = None
            self.input_device = resolve_device(self.input_device_reference, "input")
            if self._prepared_device != self.input_device:
                selected_format = self._select_working_format(self.input_device)
                self._configure_format(selected_format)
            self._open_stream(confirm_callback=True)
            self._prepared_device = None

    def prepare(self) -> InputStreamFormat:
        """Negotiate a responsive format before downstream audio is configured."""
        with self._stream_lock:
            if self.stream is not None:
                raise RuntimeError("Cannot prepare audio capture while it is running.")
            device = resolve_device(self.input_device_reference, "input")
            selected_format = self._select_working_format(device)
            self.input_device = device
            self._configure_format(selected_format)
            self._prepared_device = device
            return selected_format

    def switch_device(self, input_device: DeviceReference) -> bool:
        """Probe and switch atomically, reopening the previous endpoint on failure."""
        with self._stream_lock:
            resolved = resolve_device(input_device, "input")
            if self.stream is not None and resolved == self.input_device:
                return False
            selected_format = self._select_working_format(resolved)
            old_reference = self.input_device_reference
            old_device = self.input_device
            old_format = InputStreamFormat(self.sample_rate, self.channels)
            old_stream = self.stream
            self.stream = None
            _unregister_active_input(self, old_device)
            self._cancel_diagnostic_tap()
            self._close_stream(old_stream)
            try:
                self.input_device_reference = input_device
                self.input_device = resolved
                self._configure_format(selected_format)
                self._open_stream(confirm_callback=True)
                self._prepared_device = None
            except Exception as switch_error:
                self.input_device_reference = old_reference
                self.input_device = old_device
                self._configure_format(old_format)
                try:
                    if old_device is not None:
                        self._open_stream(confirm_callback=True)
                    self._prepared_device = None
                except Exception as rollback_error:
                    raise RuntimeError(
                        f"Input switch failed and rollback also failed: "
                        f"switch={switch_error}; rollback={rollback_error}"
                    ) from switch_error
                raise
            logger.info("event=audio_device_changed direction=input device=%s", format_device(resolved))
            return True

    def _open_stream(self, *, confirm_callback: bool = False) -> None:
        assert self.input_device is not None
        validate_input_settings(self.input_device, self.sample_rate, self.channels, self.dtype)
        self._first_callback.clear()
        try:
            self.stream = sd.RawInputStream(
                device=self.input_device,
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype=self.dtype,
                blocksize=self.frames_per_block,
                extra_settings=(
                    sd.WasapiSettings(exclusive=True)
                    if self.exclusive
                    else None
                ),
                callback=self._input_callback,
            )
            self.stream.start()
            if confirm_callback and not self._first_callback.wait(
                self.first_callback_timeout_seconds
            ):
                raise AudioInputUnresponsiveError(
                    f"Input device {format_device(self.input_device)} opened at "
                    f"{self.sample_rate} Hz/{self.channels} ch but delivered no "
                    f"callback within {self.first_callback_timeout_seconds:.2f}s."
                )
            logger.info(
                "event=audio_input_stream_opened device=%s mode=%s "
                "sample_rate_hz=%s channels=%s",
                format_device(self.input_device),
                "exclusive" if self.exclusive else "shared",
                self.sample_rate,
                self.channels,
            )
            _register_active_input(self)
        except Exception:
            self.stop()
            raise

    def _select_working_format(self, device: int) -> InputStreamFormat:
        candidates = self._format_candidates(device)
        if not self.negotiate_format:
            candidates = candidates[:1]
        errors: list[str] = []
        for candidate in candidates:
            try:
                self._probe_format(device, candidate)
                if candidate != InputStreamFormat(self.sample_rate, self.channels):
                    logger.info(
                        "event=audio_input_format_negotiated device=%s "
                        "sample_rate_hz=%s channels=%s",
                        format_device(device),
                        candidate.sample_rate,
                        candidate.channels,
                    )
                return candidate
            except Exception as error:
                errors.append(
                    f"{candidate.sample_rate}Hz/{candidate.channels}ch: {error}"
                )
        detail = "; ".join(errors)
        raise AudioInputUnresponsiveError(
            f"No responsive input format found for {format_device(device)}. {detail}"
        )

    def _format_candidates(self, device: int) -> list[InputStreamFormat]:
        info = query_devices()[device]
        maximum_channels = max(1, int(info["max_input_channels"]))
        default_rate = int(round(float(info["default_samplerate"])))
        rates = _unique((self.sample_rate, default_rate, *COMMON_INPUT_SAMPLE_RATES))
        channels = _unique(
            channel
            for channel in (self.channels, 1, 2)
            if channel <= maximum_channels
        )
        return [
            InputStreamFormat(rate, channel)
            for rate in rates
            for channel in channels
        ]

    def _probe_format(self, device: int, stream_format: InputStreamFormat) -> None:
        validate_input_settings(
            device,
            stream_format.sample_rate,
            stream_format.channels,
            self.dtype,
        )
        callback_received = Event()

        def callback(
            indata: object,
            frames: int,
            time_info: object,
            status: sd.CallbackFlags,
        ) -> None:
            callback_received.set()

        stream: sd.RawInputStream | None = None
        try:
            stream = sd.RawInputStream(
                device=device,
                samplerate=stream_format.sample_rate,
                channels=stream_format.channels,
                dtype=self.dtype,
                blocksize=calculate_block_frames(
                    stream_format.sample_rate, self.frame_duration_ms
                ),
                extra_settings=(
                    sd.WasapiSettings(exclusive=True)
                    if self.exclusive
                    else None
                ),
                callback=callback,
            )
            stream.start()
            if not callback_received.wait(self.first_callback_timeout_seconds):
                raise AudioInputUnresponsiveError(
                    "stream opened but produced no callback"
                )
        finally:
            self._close_stream(stream)

    def _configure_format(self, stream_format: InputStreamFormat) -> None:
        self.sample_rate = stream_format.sample_rate
        self.channels = stream_format.channels
        self.frames_per_block = calculate_block_frames(
            self.sample_rate, self.frame_duration_ms
        )
        self.block_bytes = calculate_block_bytes(
            self.frames_per_block, self.channels, self.dtype
        )
        self._blocks = PcmBlockBuffer(self.queue_capacity_blocks, self.block_bytes)
        self._last_callback_at = None

    def stop(self) -> None:
        """Stop and close the input stream if it is open."""
        with self._stream_lock:
            stream = self.stream
            self.stream = None
            _unregister_active_input(self, self.input_device)
            self._close_stream(stream)
        self._cancel_diagnostic_tap()

    def _cancel_diagnostic_tap(self) -> None:
        """Wake and detach any diagnostic recording interrupted by stream changes."""
        with self._diagnostic_tap_lock:
            tap = self._diagnostic_tap
            self._diagnostic_tap = None
            if tap is not None:
                tap.cancelled = True
                tap.completed.set()

    @staticmethod
    def _close_stream(stream: sd.RawInputStream | None) -> None:
        if stream is None:
            return
        try:
            try:
                stream.abort()
            except (AttributeError, sd.PortAudioError):
                stream.stop()
        finally:
            stream.close()

    def pop_block(self) -> bytes | None:
        """Return the oldest captured block without blocking."""
        return self._blocks.pop_bytes()

    async def wait_for_block(self, timeout_seconds: float) -> bytes | None:
        """Wait efficiently for the PortAudio callback to provide one block."""
        loop = asyncio.get_running_loop()
        if self._data_loop is not loop or self._data_event is None:
            self._data_loop = loop
            self._data_event = asyncio.Event()
        block = self.pop_block()
        if block is not None:
            return block
        self._data_event.clear()
        # Close the clear/write race before sleeping.
        block = self.pop_block()
        if block is not None:
            return block
        try:
            await asyncio.wait_for(
                self._data_event.wait(),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            return None
        return self.pop_block()

    def has_pending_blocks(self) -> bool:
        """Return whether captured audio remains queued."""
        return bool(len(self._blocks))

    @property
    def buffered_blocks(self) -> int:
        """Return the approximate number of blocks awaiting consumption."""
        return len(self._blocks)

    def record_diagnostic_audio(
        self,
        duration: float,
        *,
        stop_event: Event | None = None,
    ) -> RecordedAudio:
        """Copy live callback PCM until full, manually stopped, or timed out."""
        if duration <= 0:
            raise ValueError("Recording duration must be greater than zero.")
        if self.dtype != "int16":
            raise AudioDeviceError(
                "The physical audio test currently requires int16 capture."
            )
        with self._stream_lock:
            if self.stream is None:
                raise AudioDeviceError("The active microphone stream is not running.")
            target_frames = math.ceil(duration * self.sample_rate)
            tap = _DiagnosticCaptureTap(target_frames, [], Event())
            with self._diagnostic_tap_lock:
                if self._diagnostic_tap is not None:
                    raise AudioDeviceError(
                        "A physical audio test is already recording."
                    )
                self._diagnostic_tap = tap
        manual_recording = stop_event is not None
        if manual_recording:
            deadline = time.monotonic() + duration
            while not tap.completed.is_set() and not stop_event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                tap.completed.wait(min(0.05, remaining))
            completed = tap.completed.is_set()
            timeout_seconds = duration
        else:
            timeout_seconds = duration + max(
                self.first_callback_timeout_seconds,
                DIAGNOSTIC_CAPTURE_TIMEOUT_GRACE_SECONDS,
            )
            completed = tap.completed.wait(timeout_seconds)
        with self._diagnostic_tap_lock:
            if self._diagnostic_tap is tap:
                self._diagnostic_tap = None
        incomplete_fixed_recording = (
            not manual_recording
            and (not completed or tap.captured_frames < target_frames)
        )
        if tap.cancelled or incomplete_fixed_recording or tap.captured_frames == 0:
            captured_duration = tap.captured_frames / self.sample_rate
            diagnostic_device = (
                format_device(self.input_device)
                if self.input_device is not None
                else str(self.input_device_reference)
            )
            logger.warning(
                "event=diagnostic_audio_capture_incomplete device=%s "
                "captured_frames=%s target_frames=%s captured_duration_ms=%.1f "
                "requested_duration_ms=%.1f timeout_seconds=%.1f "
                "maximum_callback_gap_ms=%.1f",
                diagnostic_device,
                tap.captured_frames,
                target_frames,
                captured_duration * 1000,
                duration * 1000,
                timeout_seconds,
                self.statistics.maximum_callback_gap_ms,
            )
            raise AudioInputUnresponsiveError(
                "The active microphone stream provided only "
                f"{captured_duration:.2f} of {duration:.2f} seconds within "
                f"{timeout_seconds:.1f} seconds. Please retry the test."
            )
        return RecordedAudio(
            blocks=tuple(tap.blocks),
            sample_rate=self.sample_rate,
            channels=self.channels,
            dtype=self.dtype,
            frames_per_block=self.frames_per_block,
            frame_count=tap.captured_frames,
        )

    def _input_callback(
        self,
        indata: object,
        frames: int,
        time_info: object,
        status: sd.CallbackFlags,
    ) -> None:
        self.statistics.captured_blocks += 1
        self.statistics.captured_pcm_bytes += len(indata)  # type: ignore[arg-type]
        callback_at = time.perf_counter()
        if self._last_callback_at is not None:
            self.statistics.maximum_callback_gap_ms = max(
                self.statistics.maximum_callback_gap_ms,
                (callback_at - self._last_callback_at) * 1000,
            )
        self._last_callback_at = callback_at
        self._copy_to_diagnostic_tap(indata, frames)
        if self.statistics.captured_blocks % 5 == 0:
            samples = memoryview(indata).cast("h")
            if samples:
                peak = max(abs(sample) for sample in samples)
                rms = math.sqrt(
                    sum(sample * sample for sample in samples) / len(samples)
                )
                with self._level_lock:
                    self._recent_levels.append(
                        (callback_at, _dbfs(peak), _dbfs(rms))
                    )
                    cutoff = callback_at - 3.0
                    while (
                        self._recent_levels
                        and self._recent_levels[0][0] < cutoff
                    ):
                        self._recent_levels.popleft()
        self._first_callback.set()
        if status.input_overflow:
            self.statistics.input_overflows += 1
        result = self._blocks.write(indata)
        self.statistics.maximum_buffered_blocks = max(
            self.statistics.maximum_buffered_blocks,
            len(self._blocks),
        )
        if result != WRITE_STORED:
            self.statistics.dropped_blocks += 1
        if result == WRITE_INVALID_SIZE:
            self.statistics.invalid_block_sizes += 1
        loop = self._data_loop
        event = self._data_event
        if loop is not None and event is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass

    def _copy_to_diagnostic_tap(self, indata: object, frames: int) -> None:
        """Copy at most the requested frames while leaving pipeline buffering intact."""
        with self._diagnostic_tap_lock:
            tap = self._diagnostic_tap
            if tap is None:
                return
            remaining_frames = tap.target_frames - tap.captured_frames
            if remaining_frames <= 0:
                return
            copied_frames = min(frames, remaining_frames)
            copied_bytes = calculate_block_bytes(
                copied_frames, self.channels, self.dtype
            )
            tap.blocks.append(bytes(indata)[:copied_bytes])
            tap.captured_frames += copied_frames
            if tap.captured_frames >= tap.target_frames:
                tap.completed.set()

    def recent_level_summary(
        self, since_monotonic: float
    ) -> tuple[float, float] | None:
        """Return peak and strongest block RMS observed since one instant."""
        with self._level_lock:
            levels = [
                item
                for item in self._recent_levels
                if item[0] >= since_monotonic
            ]
        if not levels:
            return None
        return (
            max(item[1] for item in levels),
            max(item[2] for item in levels),
        )


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


def _unique(values: Iterable[int | float]) -> list[int]:
    result: list[int] = []
    for value in values:
        integer = int(value)
        if integer > 0 and integer not in result:
            result.append(integer)
    return result


def active_audio_input(device: int) -> QueuedAudioInput | None:
    """Return the live capture owning one endpoint in this process."""
    with _ACTIVE_INPUTS_LOCK:
        return _ACTIVE_INPUTS.get(device)


def _register_active_input(capture: QueuedAudioInput) -> None:
    if capture.input_device is None:
        return
    with _ACTIVE_INPUTS_LOCK:
        _ACTIVE_INPUTS[capture.input_device] = capture


def _unregister_active_input(
    capture: QueuedAudioInput, device: int | None
) -> None:
    if device is None:
        return
    with _ACTIVE_INPUTS_LOCK:
        if _ACTIVE_INPUTS.get(device) is capture:
            del _ACTIVE_INPUTS[device]


def _dbfs(amplitude: float) -> float:
    if amplitude <= 0:
        return -96.0
    return max(-96.0, 20.0 * math.log10(amplitude / 32_768.0))
