"""Compare a played reference WAV with a physical-microphone capture.

This is an offline diagnostic.  It never opens an audio device and does not
modify the recordings.  Positive delay means that the signal reaches the
microphone after it was played.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import wave

import numpy as np


def _read_start(path: Path) -> datetime:
    marks = path.with_suffix(".jsonl")
    with marks.open("r", encoding="utf-8") as handle:
        first = json.loads(handle.readline())
    if first.get("event") != "capture_started":
        raise ValueError(f"{marks} does not start with capture_started")
    return datetime.fromisoformat(str(first["captured_at"]))


def _read_mono(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2:
            raise ValueError(f"{path} is not PCM16")
        rate = handle.getframerate()
        channels = handle.getnchannels()
        samples = np.frombuffer(
            handle.readframes(handle.getnframes()), dtype="<i2"
        ).astype(np.float64)
    samples = samples.reshape(-1, channels).mean(axis=1)
    return samples / 32768.0, rate


def _energy(signal: np.ndarray, frames: int) -> np.ndarray:
    usable = len(signal) // frames * frames
    if not usable:
        return np.empty(0)
    blocks = signal[:usable].reshape(-1, frames)
    rms = np.sqrt(np.mean(blocks * blocks, axis=1))
    return 20.0 * np.log10(np.maximum(rms, 10 ** (-96 / 20)))


def _pearson(left: np.ndarray, right: np.ndarray) -> float:
    left = left - np.mean(left)
    right = right - np.mean(right)
    denominator = math.sqrt(float(left @ left) * float(right @ right))
    return float(left @ right) / denominator if denominator else 0.0


def _aligned_energy_correlation(
    reference_db: np.ndarray,
    microphone_db: np.ndarray,
    start_delta_seconds: float,
    hop_seconds: float,
    minimum_delay_ms: int,
    maximum_delay_ms: int,
) -> tuple[float, int, int]:
    best = (-2.0, 0, 0)
    for delay_ms in range(minimum_delay_ms, maximum_delay_ms + 1, 10):
        # microphone local time = reference local time + reference_start
        #                         + path_delay - microphone_start
        shift = round(
            (start_delta_seconds + delay_ms / 1000.0) / hop_seconds
        )
        ref_start = max(0, -shift)
        mic_start = max(0, shift)
        count = min(
            len(reference_db) - ref_start,
            len(microphone_db) - mic_start,
        )
        if count < 100:
            continue
        correlation = _pearson(
            reference_db[ref_start : ref_start + count],
            microphone_db[mic_start : mic_start + count],
        )
        if correlation > best[0]:
            best = (correlation, delay_ms, count)
    return best


def _aligned_samples(
    reference: np.ndarray,
    microphone: np.ndarray,
    rate: int,
    start_delta_seconds: float,
    delay_seconds: float,
) -> tuple[np.ndarray, np.ndarray]:
    shift = round((start_delta_seconds + delay_seconds) * rate)
    ref_start = max(0, -shift)
    mic_start = max(0, shift)
    count = min(len(reference) - ref_start, len(microphone) - mic_start)
    return (
        reference[ref_start : ref_start + count],
        microphone[mic_start : mic_start + count],
    )


def _windowed_coherence(
    reference: np.ndarray,
    microphone: np.ndarray,
    rate: int,
) -> tuple[float, float, int]:
    fft_size = 4096
    hop = fft_size // 2
    window = np.hanning(fft_size)
    pxx = np.zeros(fft_size // 2 + 1)
    pyy = np.zeros_like(pxx)
    pxy = np.zeros(fft_size // 2 + 1, dtype=np.complex128)
    used = 0
    for offset in range(0, len(reference) - fft_size + 1, hop):
        ref = reference[offset : offset + fft_size]
        mic = microphone[offset : offset + fft_size]
        ref_rms = math.sqrt(float(np.mean(ref * ref)))
        if ref_rms < 10 ** (-45 / 20):
            continue
        x = np.fft.rfft(ref * window)
        y = np.fft.rfft(mic * window)
        pxx += np.abs(x) ** 2
        pyy += np.abs(y) ** 2
        pxy += y * np.conj(x)
        used += 1
    if not used:
        return 0.0, 0.0, 0
    coherence = np.abs(pxy) ** 2 / np.maximum(pxx * pyy, 1e-30)
    frequencies = np.fft.rfftfreq(fft_size, 1 / rate)
    speech = coherence[(frequencies >= 300) & (frequencies <= 4000)]
    return float(np.mean(speech)), float(np.median(speech)), used


def _active_attenuation(
    reference: np.ndarray,
    microphone: np.ndarray,
    rate: int,
) -> tuple[float, float, int]:
    block = round(rate * 0.02)
    ref_db = _energy(reference, block)
    mic_db = _energy(microphone, block)
    count = min(len(ref_db), len(mic_db))
    active = ref_db[:count] >= -45.0
    if not np.any(active):
        return -96.0, -96.0, 0
    return (
        float(np.median(ref_db[:count][active])),
        float(np.median(mic_db[:count][active])),
        int(np.sum(active)),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("reference", type=Path)
    parser.add_argument("microphone", type=Path)
    parser.add_argument("--min-delay-ms", type=int, default=0)
    parser.add_argument("--max-delay-ms", type=int, default=1500)
    args = parser.parse_args()

    reference, ref_rate = _read_mono(args.reference)
    microphone, mic_rate = _read_mono(args.microphone)
    if ref_rate != mic_rate:
        raise ValueError(f"sample rates differ: {ref_rate} != {mic_rate}")

    ref_start = _read_start(args.reference)
    mic_start = _read_start(args.microphone)
    start_delta = (ref_start - mic_start).total_seconds()
    hop_seconds = 0.01
    frame_size = round(ref_rate * hop_seconds)
    ref_db = _energy(reference, frame_size)
    mic_db = _energy(microphone, frame_size)
    correlation, delay_ms, compared = _aligned_energy_correlation(
        ref_db,
        mic_db,
        start_delta,
        hop_seconds,
        args.min_delay_ms,
        args.max_delay_ms,
    )
    aligned_ref, aligned_mic = _aligned_samples(
        reference,
        microphone,
        ref_rate,
        start_delta,
        delay_ms / 1000.0,
    )
    waveform_correlation = _pearson(aligned_ref, aligned_mic)
    coherence_mean, coherence_median, coherence_windows = _windowed_coherence(
        aligned_ref, aligned_mic, ref_rate
    )
    ref_active_db, mic_active_db, active_blocks = _active_attenuation(
        aligned_ref, aligned_mic, ref_rate
    )

    print(json.dumps(
        {
            "reference": str(args.reference),
            "microphone": str(args.microphone),
            "sample_rate": ref_rate,
            "reference_start": ref_start.isoformat(),
            "microphone_start": mic_start.isoformat(),
            "best_path_delay_ms": delay_ms,
            "log_energy_correlation": round(correlation, 4),
            "energy_frames_compared": compared,
            "waveform_correlation_at_energy_delay": round(
                waveform_correlation, 4
            ),
            "speech_band_coherence_mean": round(coherence_mean, 4),
            "speech_band_coherence_median": round(coherence_median, 4),
            "coherence_windows": coherence_windows,
            "reference_active_median_dbfs": round(ref_active_db, 2),
            "microphone_at_reference_active_median_dbfs": round(
                mic_active_db, 2
            ),
            "median_path_attenuation_db": round(
                mic_active_db - ref_active_db, 2
            ),
            "reference_active_blocks": active_blocks,
        },
        indent=2,
    ))


if __name__ == "__main__":
    main()
