"""Thin streaming adapter around WebRTC Audio Processing Module AEC3."""

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
import logging
from threading import Lock
from typing import Protocol

from aec_audio_processing import AudioProcessor

from audio.pcm import convert_int16_channels
from audio.resampling import StreamingPcmInt16Resampler, create_resampler


LOGGER = logging.getLogger(__name__)
WEBRTC_FRAME_DURATION_MS = 10


class NativeAudioProcessor(Protocol):
    """Subset of the third-party WebRTC binding used by Twin Tongue."""

    def set_stream_format(
        self,
        sample_rate_in: int,
        channel_count_in: int,
        sample_rate_out: int,
        channel_count_out: int,
    ) -> None: ...

    def set_reverse_stream_format(
        self,
        sample_rate_in: int,
        channel_count_in: int,
    ) -> None: ...

    def set_stream_delay(self, delay_ms: int) -> None: ...

    def get_frame_size(self) -> int: ...

    def process_reverse_stream(self, pcm16: bytes) -> bytes: ...

    def process_stream(self, pcm16: bytes) -> bytes: ...


NativeProcessorFactory = Callable[..., NativeAudioProcessor]


@dataclass
class WebRtcAec3Statistics:
    """Operational counters around the native WebRTC processor."""

    render_blocks: int = 0
    render_frames: int = 0
    capture_blocks: int = 0
    capture_frames: int = 0
    bypassed_capture_blocks: int = 0
    dropped_render_blocks: int = 0
    resets: int = 0
    errors: int = 0


class WebRtcAec3:
    """Feed synchronized render and capture PCM to WebRTC AEC3.

    Echo removal itself is implemented by ``webrtc-audio-processing``. This
    class only serializes calls into its native Audio Processing Module,
    converts channels, resamples the render reference when necessary, and
    splits Twin Tongue's stream blocks into WebRTC's required 10 ms frames.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        stream_delay_ms: int = 0,
        render_queue_capacity_blocks: int = 100,
        resampling: Mapping[str, object] | None = None,
        processor_factory: NativeProcessorFactory = AudioProcessor,
    ) -> None:
        if stream_delay_ms < 0:
            raise ValueError("WebRTC AEC3 stream delay must not be negative.")
        if render_queue_capacity_blocks < 1:
            raise ValueError(
                "WebRTC AEC3 render queue capacity must be at least one."
            )
        self.enabled = enabled
        self.stream_delay_ms = stream_delay_ms
        self.render_queue_capacity_blocks = render_queue_capacity_blocks
        self._resampling = dict(resampling or {"backend": "soxr", "quality": "HQ"})
        self._processor_factory = processor_factory
        self._processor: NativeAudioProcessor | None = None
        self._sample_rate: int | None = None
        self._frame_bytes = 0
        self._render_queue: deque[tuple[bytes, int, int]] = deque()
        self._render_resampler: StreamingPcmInt16Resampler | None = None
        self._render_source_rate: int | None = None
        self._render_pending = bytearray()
        self._lock = Lock()
        self.statistics = WebRtcAec3Statistics()

    def observe_render(
        self,
        pcm16: bytes,
        *,
        sample_rate: int,
        channels: int,
    ) -> None:
        """Queue the PCM actually rendered to the local speaker/headphones."""
        if not self.enabled or not pcm16:
            return
        if sample_rate <= 0:
            raise ValueError("WebRTC AEC3 render sample rate must be positive.")
        mono = convert_int16_channels(pcm16, channels, 1)
        with self._lock:
            self.statistics.render_blocks += 1
            if len(self._render_queue) >= self.render_queue_capacity_blocks:
                self._render_queue.popleft()
                self.statistics.dropped_render_blocks += 1
            self._render_queue.append((mono, sample_rate, channels))

    def process_capture(self, pcm16_mono: bytes, *, sample_rate: int) -> bytes:
        """Remove render echo from one mono microphone block with WebRTC AEC3."""
        if not self.enabled or not pcm16_mono:
            return pcm16_mono
        if len(pcm16_mono) % 2:
            raise ValueError("WebRTC AEC3 capture must be complete PCM int16 samples.")
        with self._lock:
            self.statistics.capture_blocks += 1
            try:
                self._ensure_processor(sample_rate)
                self._drain_render_queue()
                if len(pcm16_mono) % self._frame_bytes:
                    self.statistics.bypassed_capture_blocks += 1
                    LOGGER.warning(
                        "event=webrtc_aec3_capture_bypassed reason=non_10ms_frame "
                        "sample_rate_hz=%s bytes=%s expected_multiple=%s",
                        sample_rate,
                        len(pcm16_mono),
                        self._frame_bytes,
                    )
                    return pcm16_mono
                processor = self._processor
                if processor is None:
                    return pcm16_mono
                cleaned = bytearray()
                for offset in range(0, len(pcm16_mono), self._frame_bytes):
                    frame = pcm16_mono[offset : offset + self._frame_bytes]
                    cleaned.extend(processor.process_stream(frame))
                    self.statistics.capture_frames += 1
                return bytes(cleaned)
            except Exception:
                self.statistics.errors += 1
                self._reset_native()
                LOGGER.exception(
                    "event=webrtc_aec3_processing_failed action=capture_bypassed"
                )
                return pcm16_mono

    def reset(self) -> None:
        """Reset native adaptive state and discard reference from the prior call."""
        with self._lock:
            self.statistics.resets += 1
            self._render_queue.clear()
            self._reset_native()

    def statistics_snapshot(self) -> dict[str, int | bool]:
        """Return JSON-serializable AEC3 state for call manifests and metrics."""
        with self._lock:
            return {
                "enabled": self.enabled,
                **asdict(self.statistics),
            }

    def _ensure_processor(self, sample_rate: int) -> None:
        if sample_rate <= 0:
            raise ValueError("WebRTC AEC3 capture sample rate must be positive.")
        if self._processor is not None and self._sample_rate == sample_rate:
            return
        self._reset_native()
        processor = self._processor_factory(
            enable_aec=True,
            enable_ns=False,
            enable_agc=False,
            enable_vad=False,
        )
        processor.set_stream_format(sample_rate, 1, sample_rate, 1)
        processor.set_reverse_stream_format(sample_rate, 1)
        processor.set_stream_delay(self.stream_delay_ms)
        frame_size = processor.get_frame_size()
        if frame_size <= 0:
            raise RuntimeError("WebRTC AEC3 returned an invalid frame size.")
        self._processor = processor
        self._sample_rate = sample_rate
        self._frame_bytes = frame_size * 2
        LOGGER.info(
            "event=webrtc_aec3_initialized sample_rate_hz=%s frame_ms=%s "
            "stream_delay_ms=%s",
            sample_rate,
            WEBRTC_FRAME_DURATION_MS,
            self.stream_delay_ms,
        )

    def _drain_render_queue(self) -> None:
        processor = self._processor
        sample_rate = self._sample_rate
        if processor is None or sample_rate is None:
            return
        while self._render_queue:
            pcm16_mono, source_rate, _ = self._render_queue.popleft()
            if source_rate != self._render_source_rate:
                self._render_source_rate = source_rate
                self._render_pending.clear()
                self._render_resampler = (
                    None
                    if source_rate == sample_rate
                    else create_resampler(
                        self._resampling,
                        source_rate,
                        sample_rate,
                        output_block_frames=self._frame_bytes // 2,
                    )
                )
            if self._render_resampler is None:
                render_blocks = (pcm16_mono,)
            else:
                render_blocks = self._render_resampler.process(pcm16_mono)
            for render_block in render_blocks:
                self._render_pending.extend(render_block)
                while len(self._render_pending) >= self._frame_bytes:
                    frame = bytes(self._render_pending[: self._frame_bytes])
                    del self._render_pending[: self._frame_bytes]
                    processor.process_reverse_stream(frame)
                    self.statistics.render_frames += 1

    def _reset_native(self) -> None:
        self._processor = None
        self._sample_rate = None
        self._frame_bytes = 0
        self._render_resampler = None
        self._render_source_rate = None
        self._render_pending.clear()
