"""Tests for backend-independent voice detection and routing."""

import asyncio
import struct
import threading
import time
import unittest
from unittest.mock import patch

from audio.voice_detection import (
    StreamingVoiceDetector,
    VoiceActivityEvent,
    VoiceDetectorLoader,
    preload_voice_detection_package,
    validate_voice_detection_settings,
)


def _block(value: int) -> bytes:
    return struct.pack("<8h", *([value] * 8))


class SequenceBackend:
    name = "test"
    sample_rate = 16_000
    last_inference_ms = 0.1

    def __init__(self, events: list[VoiceActivityEvent]) -> None:
        self.events = iter(events)
        self.resets = 0

    def process(self, mono_pcm: bytes) -> VoiceActivityEvent:
        return next(self.events)

    def reset(self) -> None:
        self.resets += 1


class StreamingVoiceDetectorTests(unittest.TestCase):
    def test_preloads_the_configured_package_once(self) -> None:
        settings = {
            "backend": "silero",
            "silero": {"runtime": "onnx", "opset_version": 16},
        }
        with (
            patch("audio.voice_detection.importlib.import_module") as import_module,
            self.assertLogs("audio.voice_detection", level="INFO") as captured,
        ):
            preload_voice_detection_package(settings)

        import_module.assert_called_once_with("onnxruntime")
        messages = "\n".join(captured.output)
        self.assertIn(
            "event=application_startup_waiting "
            "phase=voice_detection_package_load backend=silero "
            "estimated_max_wait_seconds=30",
            messages,
        )
        self.assertIn("event=voice_detection_package_loaded backend=silero", messages)

    def test_speech_pad_is_forwarded_when_backend_starts_speech(self) -> None:
        backend = SequenceBackend([
            VoiceActivityEvent(False),
            VoiceActivityEvent(True, started=True),
            VoiceActivityEvent(True),
            VoiceActivityEvent(False, ended=True),
        ])
        detector = StreamingVoiceDetector(
            backend,
            frame_duration_ms=20,
            speech_pad_ms=40,
        )
        silence, speech = _block(0), _block(1)
        self.assertEqual(detector.process(silence).forwarded_blocks, ())
        started = detector.process(speech)
        self.assertEqual(started.forwarded_blocks, (silence, speech))
        self.assertTrue(started.speech_started)
        self.assertEqual(detector.process(speech).forwarded_blocks, (speech,))
        ended = detector.process(silence)
        self.assertTrue(ended.speech_ended)
        self.assertFalse(ended.active)

    def test_continuous_speech_does_not_reset_the_backend(self) -> None:
        backend = SequenceBackend([
            VoiceActivityEvent(True, started=True),
            VoiceActivityEvent(True),
            VoiceActivityEvent(True),
        ])
        detector = StreamingVoiceDetector(
            backend,
            frame_duration_ms=20,
            speech_pad_ms=0,
        )
        detector.process(_block(1))
        self.assertFalse(detector.process(_block(1)).speech_ended)
        self.assertFalse(detector.process(_block(1)).speech_ended)
        self.assertEqual(backend.resets, 0)

    def test_short_activation_is_discarded_before_reaching_stt(self) -> None:
        backend = SequenceBackend([
            VoiceActivityEvent(True, started=True),
            VoiceActivityEvent(True),
            VoiceActivityEvent(True, silence_duration_ms=20),
            VoiceActivityEvent(False, ended=True, silence_duration_ms=40),
        ])
        detector = StreamingVoiceDetector(
            backend,
            frame_duration_ms=20,
            speech_pad_ms=40,
            min_speech_duration_ms=60,
        )

        results = [detector.process(_block(1)) for _ in range(4)]

        self.assertTrue(all(not result.forwarded_blocks for result in results))
        self.assertTrue(all(not result.speech_started for result in results))
        self.assertEqual(detector.statistics.activations, 0)

    def test_confirmed_activation_releases_buffered_audio_once(self) -> None:
        backend = SequenceBackend([
            VoiceActivityEvent(False),
            VoiceActivityEvent(True, started=True),
            VoiceActivityEvent(True),
            VoiceActivityEvent(True),
        ])
        detector = StreamingVoiceDetector(
            backend,
            frame_duration_ms=20,
            speech_pad_ms=40,
            min_speech_duration_ms=60,
        )
        silence, speech = _block(0), _block(1)

        self.assertFalse(detector.process(silence).forwarded_blocks)
        self.assertFalse(detector.process(speech).forwarded_blocks)
        self.assertFalse(detector.process(speech).forwarded_blocks)
        confirmed = detector.process(speech)

        self.assertEqual(confirmed.forwarded_blocks, (silence, speech, speech, speech))
        self.assertTrue(confirmed.speech_started)
        self.assertTrue(confirmed.active)
        self.assertEqual(detector.statistics.activations, 1)

    def test_each_direction_has_independent_state_and_reset(self) -> None:
        left_backend = SequenceBackend([VoiceActivityEvent(True, started=True)])
        right_backend = SequenceBackend([VoiceActivityEvent(False)])
        left = StreamingVoiceDetector(
            left_backend, frame_duration_ms=20, speech_pad_ms=0,
        )
        right = StreamingVoiceDetector(
            right_backend, frame_duration_ms=20, speech_pad_ms=0,
        )
        left.process(_block(1))
        right.process(_block(0))
        self.assertTrue(left.active)
        self.assertFalse(right.active)
        left.reset()
        self.assertEqual(left_backend.resets, 1)
        self.assertEqual(right_backend.resets, 0)

    def test_configuration_accepts_only_registered_external_backend(self) -> None:
        pipeline = {
            "threshold": 0.5,
            "negative_threshold": 0.35,
            "min_speech_duration_ms": 100,
            "min_silence_duration_ms": 350,
            "speech_pad_ms": 200,
        }
        validate_voice_detection_settings(
            {"backend": "silero", "silero": {"runtime": "onnx", "opset_version": 16}},
            pipeline,
        )
        with self.assertRaisesRegex(ValueError, "backend"):
            validate_voice_detection_settings({"backend": "energy"}, pipeline)

    def test_configuration_rejects_invalid_negative_threshold(self) -> None:
        pipeline = {
            "threshold": 0.5,
            "negative_threshold": 0.5,
            "min_speech_duration_ms": 100,
            "min_silence_duration_ms": 350,
            "speech_pad_ms": 200,
        }
        with self.assertRaisesRegex(ValueError, "negative_threshold"):
            validate_voice_detection_settings(
                {
                    "backend": "silero",
                    "silero": {"runtime": "onnx", "opset_version": 16},
                },
                pipeline,
            )


class VoiceDetectorLoaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.loader = VoiceDetectorLoader()

    async def asyncTearDown(self) -> None:
        await self.loader.close()

    async def test_serializes_loads_on_one_dedicated_thread(self) -> None:
        active_loads = 0
        maximum_active_loads = 0
        thread_names: list[str] = []
        state_lock = threading.Lock()

        def fake_create(*_args: object, **_kwargs: object) -> object:
            nonlocal active_loads, maximum_active_loads
            with state_lock:
                active_loads += 1
                maximum_active_loads = max(maximum_active_loads, active_loads)
                thread_names.append(threading.current_thread().name)
            time.sleep(0.05)
            with state_lock:
                active_loads -= 1
            return object()

        ticks = 0

        async def tick_event_loop() -> None:
            nonlocal ticks
            for _ in range(5):
                await asyncio.sleep(0.01)
                ticks += 1

        with patch(
            "audio.voice_detection.create_voice_detector",
            side_effect=fake_create,
        ):
            await asyncio.gather(
                self.loader.load({}, {}, 16_000, frame_duration_ms=20),
                self.loader.load({}, {}, 16_000, frame_duration_ms=20),
                tick_event_loop(),
            )

        self.assertEqual(1, maximum_active_loads)
        self.assertEqual(5, ticks)
        self.assertEqual(2, len(thread_names))
        self.assertTrue(
            all(name.startswith("twin-tongue-voice-detector-loader") for name in thread_names)
        )


if __name__ == "__main__":
    unittest.main()
