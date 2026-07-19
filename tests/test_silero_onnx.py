"""Tests for the lightweight Silero ONNX Runtime adapter."""

import unittest

import numpy as np

from audio.silero_onnx import (
    CONTEXT_SAMPLES,
    SAMPLE_RATE,
    STATE_SHAPE,
    WINDOW_SAMPLES,
    SileroOnnxModel,
    SileroVadIterator,
    model_path,
)


class FakeSession:
    def __init__(self, score: float = 0.75) -> None:
        self.score = score
        self.inputs: dict[str, np.ndarray] | None = None

    def run(
        self,
        _outputs: object,
        inputs: dict[str, np.ndarray],
    ) -> tuple[np.ndarray, np.ndarray]:
        self.inputs = inputs
        return np.array([[self.score]], dtype=np.float32), np.ones(
            STATE_SHAPE, dtype=np.float32
        )


class ScoreModel:
    def __init__(self, scores: list[float]) -> None:
        self._scores = iter(scores)
        self.resets = 0

    def infer(self, _samples: np.ndarray) -> float:
        return next(self._scores)

    def reset_states(self) -> None:
        self.resets += 1


class SileroOnnxModelTests(unittest.TestCase):
    def test_bundled_models_exist_for_supported_opsets(self) -> None:
        self.assertTrue(model_path(15).is_file())
        self.assertTrue(model_path(16).is_file())

    def test_inference_uses_context_state_and_sample_rate(self) -> None:
        session = FakeSession()
        model = SileroOnnxModel(model_path(16), session=session)

        score = model.infer(np.ones(WINDOW_SAMPLES, dtype=np.float32))

        self.assertAlmostEqual(0.75, score)
        assert session.inputs is not None
        self.assertEqual(
            (1, CONTEXT_SAMPLES + WINDOW_SAMPLES),
            session.inputs["input"].shape,
        )
        self.assertEqual(STATE_SHAPE, session.inputs["state"].shape)
        self.assertEqual(SAMPLE_RATE, int(session.inputs["sr"]))
        self.assertEqual(np.int64, session.inputs["sr"].dtype)

    def test_invalid_window_size_is_rejected(self) -> None:
        model = SileroOnnxModel(model_path(16), session=FakeSession())
        with self.assertRaisesRegex(ValueError, "512 samples"):
            model.infer(np.zeros(100, dtype=np.float32))

    def test_iterator_matches_silero_boundary_policy(self) -> None:
        model = ScoreModel([0.8, 0.2, 0.2, 0.2])
        iterator = SileroVadIterator(
            model, threshold=0.5, min_silence_duration_ms=64
        )
        window = np.zeros(WINDOW_SAMPLES, dtype=np.float32)

        self.assertEqual({"start": 0}, iterator.process(window))
        self.assertIsNone(iterator.process(window))
        self.assertIsNone(iterator.process(window))
        self.assertEqual({"end": 512}, iterator.process(window))
        self.assertFalse(iterator.triggered)


if __name__ == "__main__":
    unittest.main()
