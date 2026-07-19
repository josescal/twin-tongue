"""Minimal NumPy/ONNX Runtime adapter for the bundled Silero VAD models."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np


SAMPLE_RATE = 16_000
WINDOW_SAMPLES = 512
CONTEXT_SAMPLES = 64
STATE_SHAPE = (2, 1, 128)
MODEL_FILENAMES = {
    15: "silero_vad_16k_op15.onnx",
    16: "silero_vad.onnx",
}


def model_path(opset_version: int) -> Path:
    """Return the bundled model path for one supported ONNX opset."""
    try:
        filename = MODEL_FILENAMES[opset_version]
    except KeyError as error:
        raise ValueError("Silero opset_version must be 15 or 16.") from error
    path = Path(__file__).with_name("models") / filename
    if not path.is_file():
        raise RuntimeError(f"Bundled Silero ONNX model is missing: {path}")
    return path


class SileroOnnxModel:
    """Run the streaming Silero model without importing PyTorch or Torchaudio."""

    def __init__(self, path: Path, *, session: Any | None = None) -> None:
        if session is None:
            try:
                import onnxruntime as ort
            except ImportError as error:
                raise RuntimeError(
                    "Silero voice detection requires the 'onnxruntime' package."
                ) from error
            options = ort.SessionOptions()
            options.inter_op_num_threads = 1
            options.intra_op_num_threads = 1
            session = ort.InferenceSession(
                str(path),
                providers=["CPUExecutionProvider"],
                sess_options=options,
            )
        self._session = session
        self.reset_states()

    def reset_states(self) -> None:
        self._state = np.zeros(STATE_SHAPE, dtype=np.float32)
        self._context = np.zeros((1, CONTEXT_SAMPLES), dtype=np.float32)

    def infer(self, samples: np.ndarray) -> float:
        window = np.asarray(samples, dtype=np.float32).reshape(1, -1)
        if window.shape[1] != WINDOW_SAMPLES:
            raise ValueError(
                f"Silero expects {WINDOW_SAMPLES} samples per inference, "
                f"received {window.shape[1]}."
            )
        model_input = np.concatenate((self._context, window), axis=1)
        output, state = self._session.run(
            None,
            {
                "input": model_input,
                "state": self._state,
                "sr": np.array(SAMPLE_RATE, dtype=np.int64),
            },
        )
        self._state = np.asarray(state, dtype=np.float32)
        self._context = model_input[:, -CONTEXT_SAMPLES:]
        return float(np.asarray(output).reshape(-1)[0])


class SileroVadIterator:
    """Apply the official Silero streaming boundary policy to ONNX scores."""

    def __init__(
        self,
        model: SileroOnnxModel,
        *,
        threshold: float,
        min_silence_duration_ms: float,
    ) -> None:
        self.model = model
        self.threshold = threshold
        self.min_silence_samples = SAMPLE_RATE * min_silence_duration_ms / 1000
        self.reset_states()

    def reset_states(self) -> None:
        self.model.reset_states()
        self.triggered = False
        self.temp_end = 0
        self.current_sample = 0

    def process(self, samples: np.ndarray) -> dict[str, int] | None:
        window_samples = int(np.asarray(samples).size)
        self.current_sample += window_samples
        speech_probability = self.model.infer(samples)

        if speech_probability >= self.threshold and self.temp_end:
            self.temp_end = 0

        if speech_probability >= self.threshold and not self.triggered:
            self.triggered = True
            return {"start": max(0, self.current_sample - window_samples)}

        if speech_probability < self.threshold - 0.15 and self.triggered:
            if not self.temp_end:
                self.temp_end = self.current_sample
            if self.current_sample - self.temp_end < self.min_silence_samples:
                return None
            speech_end = self.temp_end - window_samples
            self.temp_end = 0
            self.triggered = False
            return {"end": int(speech_end)}

        return None
