"""Configurable streaming resampling and its PCM conversions."""

from collections.abc import Callable, Mapping
from typing import Protocol

import numpy as np
import numpy.typing as npt
import soxr

Float32Samples = npt.NDArray[np.float32]
SUPPORTED_SOXR_QUALITIES = frozenset({"QQ", "LQ", "MQ", "HQ", "VHQ"})


def pcm_int16le_to_float32(mono_pcm: bytes) -> Float32Samples:
    """Convert little-endian mono PCM int16 bytes to normalized float32 samples."""
    if len(mono_pcm) % 2:
        raise ValueError("PCM int16 data must contain complete samples.")
    samples = np.frombuffer(mono_pcm, dtype="<i2")
    return samples.astype(np.float32) / 32768.0


def float32_to_pcm_int16le(samples: npt.ArrayLike) -> bytes:
    """Convert normalized floating-point samples to clipped little-endian PCM int16."""
    values = np.asarray(samples, dtype=np.float32)
    clipped = np.clip(values, -1.0, 1.0)
    scaled = np.rint(clipped * 32768.0)
    return np.clip(scaled, -32768, 32767).astype("<i2").tobytes()


class StreamingFloatResampler(Protocol):
    """Backend contract for continuous mono float32 resampling."""

    input_rate: int
    output_rate: int

    def process(
        self,
        samples: Float32Samples,
        *,
        final: bool = False,
    ) -> Float32Samples: ...

    def reset(self) -> None: ...


class SoxrStreamingFloatResampler:
    """Streaming mono resampler backed by libsoxr through python-soxr."""

    def __init__(self, input_rate: int, output_rate: int, *, quality: str = "HQ") -> None:
        _validate_sample_rates(input_rate, output_rate)
        normalized_quality = quality.strip().upper()
        if normalized_quality not in SUPPORTED_SOXR_QUALITIES:
            supported = ", ".join(sorted(SUPPORTED_SOXR_QUALITIES))
            raise ValueError(f"SoXR quality must be one of: {supported}.")
        self.input_rate = input_rate
        self.output_rate = output_rate
        self.quality = normalized_quality
        self._stream = soxr.ResampleStream(
            input_rate,
            output_rate,
            1,
            dtype="float32",
            quality=normalized_quality,
        )

    def process(
        self,
        samples: Float32Samples,
        *,
        final: bool = False,
    ) -> Float32Samples:
        if samples.ndim != 1:
            raise ValueError("Streaming mono resampling requires a one-dimensional array.")
        return self._stream.resample_chunk(samples, last=final)

    def reset(self) -> None:
        self._stream.clear()


BackendFactory = Callable[[int, int, Mapping[str, object]], StreamingFloatResampler]


def _create_soxr(
    input_rate: int,
    output_rate: int,
    settings: Mapping[str, object],
) -> StreamingFloatResampler:
    return SoxrStreamingFloatResampler(
        input_rate,
        output_rate,
        quality=str(settings.get("quality", "HQ")),
    )


_BACKEND_FACTORIES: dict[str, BackendFactory] = {"soxr": _create_soxr}


class StreamingPcmInt16Resampler:
    """PCM-facing facade that keeps format conversion out of audio consumers."""

    def __init__(
        self,
        backend: StreamingFloatResampler,
        *,
        output_block_frames: int | None = None,
    ) -> None:
        if output_block_frames is not None and output_block_frames <= 0:
            raise ValueError("Output block frames must be greater than zero.")
        self._backend = backend
        self.input_rate = backend.input_rate
        self.output_rate = backend.output_rate
        self.output_block_frames = output_block_frames
        self._pending = bytearray()

    def process(self, mono_pcm: bytes, *, final: bool = False) -> tuple[bytes, ...]:
        samples = self._backend.process(
            pcm_int16le_to_float32(mono_pcm),
            final=final,
        )
        output = float32_to_pcm_int16le(samples)
        if self.output_block_frames is None:
            return (output,) if output else ()
        self._pending.extend(output)
        block_bytes = self.output_block_frames * 2
        complete = len(self._pending) // block_bytes
        blocks = tuple(
            bytes(self._pending[offset : offset + block_bytes])
            for offset in range(0, complete * block_bytes, block_bytes)
        )
        del self._pending[: complete * block_bytes]
        if final and self._pending:
            blocks += (bytes(self._pending),)
            self._pending.clear()
        return blocks

    def reset(self) -> None:
        self._backend.reset()
        self._pending.clear()


def create_resampler(
    settings: Mapping[str, object],
    input_rate: int,
    output_rate: int,
    *,
    output_block_frames: int | None = None,
) -> StreamingPcmInt16Resampler:
    """Build the configured PCM resampler without exposing its backend to callers."""
    _validate_sample_rates(input_rate, output_rate)
    validate_resampling_settings(settings)
    backend_name = str(settings.get("backend", "soxr")).strip().lower()
    factory = _BACKEND_FACTORIES[backend_name]
    return StreamingPcmInt16Resampler(
        factory(input_rate, output_rate, settings),
        output_block_frames=output_block_frames,
    )


def resample_pcm_int16(
    settings: Mapping[str, object],
    mono_pcm: bytes,
    input_rate: int,
    output_rate: int,
) -> bytes:
    """Resample one complete mono PCM segment and flush the backend tail."""
    if input_rate == output_rate:
        if len(mono_pcm) % 2:
            raise ValueError("PCM int16 data must contain complete samples.")
        return mono_pcm
    return b"".join(
        create_resampler(settings, input_rate, output_rate).process(
            mono_pcm,
            final=True,
        )
    )


def validate_resampling_settings(settings: Mapping[str, object]) -> None:
    """Validate backend selection and backend-specific configurable options."""
    backend_name = str(settings.get("backend", "")).strip().lower()
    if backend_name not in _BACKEND_FACTORIES:
        supported = ", ".join(sorted(_BACKEND_FACTORIES))
        raise ValueError(f"Resampling backend must be one of: {supported}.")
    if backend_name == "soxr":
        quality = str(settings.get("quality", "HQ")).strip().upper()
        if quality not in SUPPORTED_SOXR_QUALITIES:
            supported = ", ".join(sorted(SUPPORTED_SOXR_QUALITIES))
            raise ValueError(f"SoXR quality must be one of: {supported}.")


def _validate_sample_rates(input_rate: int, output_rate: int) -> None:
    if input_rate <= 0 or output_rate <= 0:
        raise ValueError("Sample rates must be greater than zero.")
