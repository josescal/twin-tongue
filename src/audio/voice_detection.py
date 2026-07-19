"""Backend-independent streaming voice detection and PCM routing."""

import asyncio
from collections import deque
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
import importlib
import logging
import math
import time
from typing import Protocol

import numpy as np

from audio.resampling import pcm_int16le_to_float32
from audio.silero_onnx import (
    SileroOnnxModel,
    SileroVadIterator,
    model_path as silero_model_path,
)

logger = logging.getLogger(__name__)


def preload_voice_detection_package(settings: Mapping[str, object]) -> None:
    """Import the configured backend before asynchronous runtime services start."""
    validate_voice_detection_settings(settings)
    backend_name = str(settings["backend"]).strip().lower()
    if backend_name != "silero":
        return
    logger.info(
        "event=application_startup_waiting phase=voice_detection_package_load "
        "backend=%s estimated_max_wait_seconds=30",
        backend_name,
    )
    started_at = time.perf_counter()
    try:
        importlib.import_module("onnxruntime")
        silero_model_path(int(settings["silero"].get("opset_version", 16)))
    except ImportError as error:
        raise RuntimeError(
            "Silero voice detection requires the 'onnxruntime' package."
        ) from error
    logger.info(
        "event=voice_detection_package_loaded backend=%s duration_ms=%.0f",
        backend_name,
        (time.perf_counter() - started_at) * 1000,
    )


@dataclass(frozen=True)
class VoiceActivityEvent:
    """Backend result for one input block."""

    active: bool
    started: bool = False
    ended: bool = False
    score: float | None = None
    silence_duration_ms: float = 0.0


@dataclass(frozen=True)
class VoiceDetectionLoadMetrics:
    """One-time backend initialization timings."""

    package_import_ms: float
    model_load_ms: float
    iterator_initialization_ms: float
    total_ms: float


class VoiceDetectionBackend(Protocol):
    """Contract implemented by external voice-detection libraries."""

    name: str
    sample_rate: int
    last_inference_ms: float
    load_metrics: VoiceDetectionLoadMetrics

    def process(self, mono_pcm: bytes) -> VoiceActivityEvent: ...

    def reset(self) -> None: ...


class SileroVoiceDetectionBackend:
    """Streaming adapter around the bundled Silero ONNX model."""

    name = "silero"
    sample_rate = 16_000
    window_samples = 512

    def __init__(
        self,
        *,
        input_rate: int,
        threshold: float,
        min_silence_duration_ms: float,
        runtime: str = "onnx",
        opset_version: int = 16,
    ) -> None:
        total_started = time.perf_counter()
        if input_rate != self.sample_rate:
            raise ValueError("Silero voice detection requires mono PCM at 16000 Hz.")
        if runtime.strip().lower() != "onnx":
            raise ValueError("Silero runtime must be 'onnx'.")
        package_import_started = time.perf_counter()
        try:
            importlib.import_module("onnxruntime")
        except ImportError as error:
            raise RuntimeError(
                "Silero voice detection requires the 'onnxruntime' package."
            ) from error
        package_import_ms = (time.perf_counter() - package_import_started) * 1000
        model_load_started = time.perf_counter()
        model = SileroOnnxModel(silero_model_path(opset_version))
        model_load_ms = (time.perf_counter() - model_load_started) * 1000
        iterator_started = time.perf_counter()
        self._iterator = SileroVadIterator(
            model,
            threshold=threshold,
            min_silence_duration_ms=min_silence_duration_ms,
        )
        iterator_initialization_ms = (time.perf_counter() - iterator_started) * 1000
        self._pending = np.empty(0, dtype=np.float32)
        self.last_inference_ms = 0.0
        self.load_metrics = VoiceDetectionLoadMetrics(
            package_import_ms=package_import_ms,
            model_load_ms=model_load_ms,
            iterator_initialization_ms=iterator_initialization_ms,
            total_ms=(time.perf_counter() - total_started) * 1000,
        )

    def process(self, mono_pcm: bytes) -> VoiceActivityEvent:
        samples = pcm_int16le_to_float32(mono_pcm)
        self._pending = np.concatenate((self._pending, samples))
        started = False
        ended = False
        while self._pending.size >= self.window_samples:
            window = self._pending[: self.window_samples]
            self._pending = self._pending[self.window_samples :]
            inference_started = time.perf_counter()
            event = self._iterator.process(window)
            self.last_inference_ms = (time.perf_counter() - inference_started) * 1000
            if event:
                started = started or "start" in event
                ended = ended or "end" in event
        tentative_silence_samples = 0
        if self._iterator.temp_end:
            tentative_silence_samples = max(
                0,
                self._iterator.current_sample - self._iterator.temp_end,
            )
        return VoiceActivityEvent(
            active=bool(self._iterator.triggered),
            started=started,
            ended=ended,
            silence_duration_ms=(
                tentative_silence_samples * 1000 / self.sample_rate
            ),
        )

    def reset(self) -> None:
        self._pending = np.empty(0, dtype=np.float32)
        self.last_inference_ms = 0.0
        self._iterator.reset_states()


BackendFactory = Callable[[int, Mapping[str, object], Mapping[str, object]], VoiceDetectionBackend]


def _create_silero(
    input_rate: int,
    backend_settings: Mapping[str, object],
    pipeline_settings: Mapping[str, object],
) -> VoiceDetectionBackend:
    return SileroVoiceDetectionBackend(
        input_rate=input_rate,
        threshold=float(pipeline_settings["threshold"]),
        min_silence_duration_ms=float(
            pipeline_settings["min_silence_duration_ms"]
        ),
        runtime=str(backend_settings.get("runtime", "onnx")),
        opset_version=int(backend_settings.get("opset_version", 16)),
    )


_BACKEND_FACTORIES: dict[str, BackendFactory] = {"silero": _create_silero}


@dataclass
class VoiceDetectionStatistics:
    """Aggregate routing and backend timing counters."""

    activations: int = 0
    endings: int = 0
    maximum_inference_ms: float = 0.0
    interval_max_score: float | None = None


@dataclass(frozen=True)
class VoiceDetectionResult:
    """PCM and boundary events emitted for one captured block."""

    forwarded_blocks: tuple[bytes, ...]
    speech_started: bool
    speech_ended: bool
    active: bool
    silence_duration_ms: float = 0.0


class StreamingVoiceDetector:
    """Backend-neutral facade that routes detected speech with pre-roll."""

    def __init__(
        self,
        backend: VoiceDetectionBackend,
        *,
        frame_duration_ms: float,
        preroll_ms: float,
    ) -> None:
        if frame_duration_ms <= 0:
            raise ValueError("Voice detection frame duration must be greater than zero.")
        if preroll_ms < 0:
            raise ValueError("Voice detection pre-roll must not be negative.")
        self.backend = backend
        self.frame_duration_ms = frame_duration_ms
        self.preroll_blocks = max(1, math.ceil(preroll_ms / frame_duration_ms))
        self.statistics = VoiceDetectionStatistics()
        self.active = False
        self._preroll: deque[bytes] = deque(maxlen=self.preroll_blocks)

    def process(self, mono_pcm: bytes) -> VoiceDetectionResult:
        """Detect and return the PCM blocks that should be sent to STT."""
        event = self.backend.process(mono_pcm)
        self.statistics.maximum_inference_ms = max(
            self.statistics.maximum_inference_ms,
            self.backend.last_inference_ms,
        )
        if event.score is not None:
            peak = self.statistics.interval_max_score
            self.statistics.interval_max_score = max(
                event.score, peak if peak is not None else event.score
            )

        speech_started = event.started and not self.active
        speech_ended = False
        if not self.active:
            self._preroll.append(mono_pcm)
            if not speech_started:
                return VoiceDetectionResult(
                    (), False, False, False, event.silence_duration_ms
                )
            self.active = True
            self.statistics.activations += 1
            blocks = tuple(self._preroll)
            self._preroll.clear()
            return VoiceDetectionResult(
                blocks, True, False, True, event.silence_duration_ms
            )

        if event.ended:
            self.active = False
            self._preroll.clear()
            self.statistics.endings += 1
            speech_ended = True
        return VoiceDetectionResult(
            (mono_pcm,),
            False,
            speech_ended,
            self.active,
            event.silence_duration_ms,
        )

    def reset(self) -> None:
        """Clear transient state while preserving aggregate statistics."""
        self.active = False
        self._preroll.clear()
        self.backend.reset()


def create_voice_detector(
    settings: Mapping[str, object],
    pipeline_settings: Mapping[str, object],
    input_rate: int,
    *,
    frame_duration_ms: float,
) -> StreamingVoiceDetector:
    """Build the configured detector without exposing its backend to callers."""
    validate_voice_detection_settings(settings, pipeline_settings)
    backend_name = str(settings["backend"]).strip().lower()
    backend_settings = settings.get(backend_name, {})
    if not isinstance(backend_settings, Mapping):
        raise ValueError(f"voice_detection.{backend_name} must be a mapping.")
    backend = _BACKEND_FACTORIES[backend_name](
        input_rate, backend_settings, pipeline_settings
    )
    return StreamingVoiceDetector(
        backend,
        frame_duration_ms=frame_duration_ms,
        preroll_ms=float(pipeline_settings["preroll_ms"]),
    )


class VoiceDetectorLoader:
    """Serialize expensive detector creation on one dedicated worker thread."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="twin-tongue-voice-detector-loader",
        )
        self._closed = False

    async def load(
        self,
        settings: Mapping[str, object],
        pipeline_settings: Mapping[str, object],
        input_rate: int,
        *,
        frame_duration_ms: float,
    ) -> StreamingVoiceDetector:
        """Create one independent detector without using the general executor."""
        if self._closed:
            raise RuntimeError("Voice detector loader is closed.")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor,
            partial(
                create_voice_detector,
                settings,
                pipeline_settings,
                input_rate,
                frame_duration_ms=frame_duration_ms,
            ),
        )

    async def close(self) -> None:
        """Wait for pending detector creation and stop the dedicated worker."""
        if self._closed:
            return
        self._closed = True
        await asyncio.to_thread(
            self._executor.shutdown,
            wait=True,
            cancel_futures=False,
        )


def validate_voice_detection_settings(
    settings: Mapping[str, object],
    pipeline_settings: Mapping[str, object] | None = None,
) -> None:
    """Validate global backend selection and optional pipeline policy."""
    backend_name = str(settings.get("backend", "")).strip().lower()
    if backend_name not in _BACKEND_FACTORIES:
        supported = ", ".join(sorted(_BACKEND_FACTORIES))
        raise ValueError(f"Voice detection backend must be one of: {supported}.")
    backend_settings = settings.get(backend_name, {})
    if not isinstance(backend_settings, Mapping):
        raise ValueError(f"voice_detection.{backend_name} must be a mapping.")
    if backend_name == "silero":
        if str(backend_settings.get("runtime", "onnx")).strip().lower() != "onnx":
            raise ValueError("Silero runtime must be 'onnx'.")
        opset = backend_settings.get("opset_version", 16)
        if not isinstance(opset, int) or isinstance(opset, bool) or opset not in {15, 16}:
            raise ValueError("Silero opset_version must be 15 or 16.")
    if pipeline_settings is None:
        return
    numeric = (
        "threshold",
        "min_silence_duration_ms",
        "preroll_ms",
    )
    for key in numeric:
        value = pipeline_settings.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"Voice detection {key} must be numeric.")
    threshold = float(pipeline_settings["threshold"])
    if not 0 <= threshold <= 1:
        raise ValueError("Voice detection threshold must be between 0 and 1.")
    for key in ("min_silence_duration_ms", "preroll_ms"):
        if float(pipeline_settings[key]) < 0:
            raise ValueError(f"Voice detection {key} must not be negative.")
