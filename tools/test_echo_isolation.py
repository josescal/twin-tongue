"""Run a short physical playback-to-capture isolation test on Windows."""

from __future__ import annotations

from array import array
import argparse
import json
import math
from pathlib import Path
import sys
from threading import Lock
import time

import numpy as np
import sounddevice as sd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from audio.portaudio import describe_devices  # noqa: E402


def _device(reference: str, direction: str) -> int:
    requested_identifier = (
        int(reference.strip()) if reference.strip().isdigit() else None
    )
    matches = [
        item
        for item in describe_devices()
        if item.host_api == "Windows WASAPI"
        and (
            item.identifier == requested_identifier
            if requested_identifier is not None
            else reference.casefold() in item.name.casefold()
        )
        and (
            item.max_input_channels > 0
            if direction == "input"
            else item.max_output_channels > 0
        )
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected one WASAPI {direction} matching {reference!r}, "
            f"found {[(item.identifier, item.name) for item in matches]}"
        )
    return matches[0].identifier


def _dbfs(value: float) -> float:
    if value <= 0:
        return -96.0
    return max(-96.0, 20 * math.log10(value / 32768.0))


def _pearson(left: list[float], right: list[float]) -> float:
    x = np.asarray(left) - np.mean(left)
    y = np.asarray(right) - np.mean(right)
    denominator = math.sqrt(float(x @ x) * float(y @ y))
    return float(x @ y) / denominator if denominator else 0.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="Micrófono (Realtek")
    parser.add_argument("--output", default="Realtek HD Audio 2nd output")
    parser.add_argument(
        "--shared",
        action="store_true",
        help="Use shared capture as a controlled comparison.",
    )
    args = parser.parse_args()

    sample_rate = 48_000
    frames = 960
    input_device = _device(args.input, "input")
    output_device = _device(args.output, "output")
    rng = np.random.default_rng(20260728)
    levels = [
        0.0,
        0.0,
        *rng.choice([0.02, 0.04, 0.08, 0.12], size=75).tolist(),
        *([0.0] * 35),
    ]
    captured: list[tuple[float, float]] = []
    writes: list[tuple[float, float]] = []
    capture_lock = Lock()

    def callback(
        indata: object,
        frame_count: int,
        time_info: object,
        status: sd.CallbackFlags,
    ) -> None:
        samples = memoryview(indata).cast("h")
        rms = math.sqrt(
            sum(sample * sample for sample in samples) / max(1, len(samples))
        )
        with capture_lock:
            captured.append((time.perf_counter(), _dbfs(rms)))

    capture = sd.RawInputStream(
        device=input_device,
        samplerate=sample_rate,
        channels=1,
        dtype="int16",
        blocksize=frames,
        extra_settings=(
            None
            if args.shared
            else sd.WasapiSettings(exclusive=True)
        ),
        callback=callback,
    )
    playback = sd.RawOutputStream(
        device=output_device,
        samplerate=sample_rate,
        channels=2,
        dtype="int16",
        blocksize=frames,
    )
    capture.start()
    playback.start()
    try:
        time.sleep(0.4)
        phase = 0.0
        for level in levels:
            pcm = array("h")
            for _ in range(frames):
                # Two incommensurate tones make accidental speech correlation
                # unlikely while remaining quieter than broadband white noise.
                sample = int(
                    32767
                    * level
                    * (
                        0.55 * math.sin(phase)
                        + 0.45 * math.sin(phase * 1.617)
                    )
                )
                phase += 2 * math.pi * 733 / sample_rate
                pcm.extend((sample, sample))
            writes.append((time.perf_counter(), _dbfs(32767 * level)))
            playback.write(pcm.tobytes())
        time.sleep(0.8)
    finally:
        playback.close()
        capture.close()

    best = (-2.0, 0)
    for delay_ms in range(0, 701, 20):
        ref_values: list[float] = []
        mic_values: list[float] = []
        for written_at, reference_dbfs in writes:
            target = written_at + delay_ms / 1000
            nearest = min(captured, key=lambda item: abs(item[0] - target))
            if abs(nearest[0] - target) <= 0.025:
                ref_values.append(reference_dbfs)
                mic_values.append(nearest[1])
        if len(ref_values) < 50:
            continue
        correlation = _pearson(ref_values, mic_values)
        if correlation > best[0]:
            best = (correlation, delay_ms)

    start = writes[0][0]
    end = writes[-1][0] + 0.02
    baseline = [level for at, level in captured if at < start]
    active = [
        level
        for at, level in captured
        if start <= at <= end + 0.7
    ]
    print(json.dumps(
        {
            "input_device": input_device,
            "output_device": output_device,
            "capture_mode": (
                "wasapi_shared" if args.shared else "wasapi_exclusive"
            ),
            "best_delay_ms": best[1],
            "level_envelope_correlation": round(best[0], 4),
            "baseline_median_dbfs": round(float(np.median(baseline)), 2),
            "playback_window_median_dbfs": round(
                float(np.median(active)), 2
            ),
            "playback_window_peak_block_dbfs": round(max(active), 2),
            "blocks_captured": len(captured),
        },
        indent=2,
    ))


if __name__ == "__main__":
    main()
