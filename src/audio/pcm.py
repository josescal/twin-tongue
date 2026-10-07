"""Common PCM data structures, block geometry, and channel conversion."""

from dataclasses import dataclass
import math

import numpy as np

RAW_PCM_SAMPLE_WIDTH_BYTES = {
    "float32": 4,
    "int32": 4,
    "int24": 3,
    "int16": 2,
    "int8": 1,
    "uint8": 1,
}


@dataclass(frozen=True)
class RecordedAudio:
    """PCM blocks and the stream settings required to play them."""

    blocks: tuple[bytes, ...]
    sample_rate: int
    channels: int
    dtype: str
    frames_per_block: int
    frame_count: int


def calculate_block_frames(sample_rate: int, frame_duration_ms: float) -> int:
    """Calculate a positive whole number of frames per audio block."""
    if sample_rate <= 0:
        raise ValueError("Sample rate must be greater than zero.")
    if frame_duration_ms <= 0:
        raise ValueError("Frame duration must be greater than zero.")
    return max(1, round(sample_rate * frame_duration_ms / 1000))


def calculate_block_bytes(frames: int, channels: int, dtype: str) -> int:
    """Calculate the byte size of one raw PCM block."""
    if frames <= 0:
        raise ValueError("Frame count must be greater than zero.")
    if channels <= 0:
        raise ValueError("Channel count must be greater than zero.")
    normalized_dtype = dtype.strip().lower()
    try:
        sample_width = RAW_PCM_SAMPLE_WIDTH_BYTES[normalized_dtype]
    except KeyError as error:
        supported = ", ".join(RAW_PCM_SAMPLE_WIDTH_BYTES)
        raise ValueError(f"Raw PCM dtype must be one of: {supported}.") from error
    return frames * channels * sample_width


def convert_int16_channels(
    pcm_data: bytes,
    input_channels: int,
    output_channels: int,
) -> bytes:
    """Adapt mono/stereo int16 PCM while preserving the number of frames."""
    if input_channels == output_channels and input_channels in (1, 2):
        return pcm_data
    if input_channels == 1 and output_channels == 2:
        return _mono_int16_to_stereo(pcm_data)
    if input_channels == 2 and output_channels == 1:
        return _stereo_int16_to_mono(pcm_data)
    raise ValueError(
        f"Unsupported int16 channel conversion: {input_channels} -> {output_channels}."
    )


def apply_int16_gain(pcm_data: bytes, gain_db: float) -> tuple[bytes, int]:
    """Apply gain to PCM16, saturating safely and reporting clipped samples."""
    if len(pcm_data) % 2 != 0:
        raise ValueError("PCM16 gain input must contain complete 2-byte samples.")
    if not math.isfinite(gain_db):
        raise ValueError("PCM16 gain must be finite.")
    if not pcm_data or gain_db == 0:
        return pcm_data, 0
    factor = 10.0 ** (gain_db / 20.0)
    scaled = np.rint(
        np.frombuffer(pcm_data, dtype="<i2").astype(np.float64) * factor
    )
    clipped = int(np.count_nonzero((scaled < -32768.0) | (scaled > 32767.0)))
    return np.clip(scaled, -32768.0, 32767.0).astype("<i2").tobytes(), clipped


def _mono_int16_to_stereo(pcm_data: bytes) -> bytes:
    """Upmix little-endian mono int16 PCM by duplicating each sample."""
    if len(pcm_data) % 2 != 0:
        raise ValueError("Mono int16 PCM data must contain complete 2-byte samples.")

    samples = np.frombuffer(pcm_data, dtype="<i2")
    return np.repeat(samples, 2).tobytes()


def _stereo_int16_to_mono(pcm_data: bytes) -> bytes:
    """Downmix little-endian stereo int16 PCM to mono by averaging channels."""
    if len(pcm_data) % 4 != 0:
        raise ValueError(
            "Stereo int16 PCM data must contain complete 4-byte left/right sample pairs."
        )

    stereo = np.frombuffer(pcm_data, dtype="<i2").reshape(-1, 2)
    channel_sum = stereo.sum(axis=1, dtype=np.int32)
    return np.trunc(channel_sum / 2).astype("<i2").tobytes()
