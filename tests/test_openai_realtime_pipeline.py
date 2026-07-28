"""Tests for UI transcript publication by the OpenAI Realtime pipeline."""

import asyncio
from collections.abc import Callable
import unittest
from unittest.mock import MagicMock, call

from app_state import ApplicationState
from audio.echo_guard import EchoReferenceBus
from engines.openai_realtime import RealtimeTranslationError
from pipelines.openai_realtime import (
    OpenAIRealtimeTranslatePipeline,
    RealtimeSessionGate,
)


class RealtimeSessionGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = RealtimeSessionGate(
            enabled=True,
            disconnect_grace_seconds=3.0,
            monitor_failure_grace_seconds=5.0,
        )

    def test_does_not_require_session_without_connected_application(self) -> None:
        decision = self.gate.evaluate(
            requested=True, observation=False, now=10.0
        )

        self.assertFalse(decision.session_required)
        self.assertFalse(decision.audio_allowed)
        self.assertTrue(decision.waiting_for_application)

    def test_active_application_allows_connection_and_audio(self) -> None:
        decision = self.gate.evaluate(
            requested=True, observation=True, now=10.0
        )

        self.assertTrue(decision.session_required)
        self.assertTrue(decision.audio_allowed)
        self.assertEqual(1, self.gate.activations)

    def test_disconnect_keeps_call_continuous_and_closes_after_grace(self) -> None:
        self.gate.evaluate(requested=True, observation=True, now=10.0)

        grace = self.gate.evaluate(
            requested=True, observation=False, now=11.0
        )
        expired = self.gate.evaluate(
            requested=True, observation=False, now=40.1
        )

        self.assertTrue(grace.session_required)
        self.assertTrue(grace.audio_allowed)
        self.assertFalse(grace.waiting_for_application)
        self.assertFalse(expired.session_required)
        self.assertFalse(expired.audio_allowed)
        self.assertTrue(expired.waiting_for_application)
        self.assertEqual(1, self.gate.deactivations)

    def test_monitor_failure_is_fail_closed_after_grace(self) -> None:
        self.gate.evaluate(requested=True, observation=True, now=10.0)

        temporary = self.gate.evaluate(
            requested=True, observation=None, now=11.0
        )
        expired = self.gate.evaluate(
            requested=True, observation=None, now=16.1
        )

        self.assertTrue(temporary.session_required)
        self.assertTrue(temporary.audio_allowed)
        self.assertFalse(expired.session_required)
        self.assertFalse(expired.audio_allowed)

    def test_passthrough_never_requires_an_api_session(self) -> None:
        decision = self.gate.evaluate(
            requested=False, observation=True, now=10.0
        )

        self.assertFalse(decision.session_required)
        self.assertFalse(decision.audio_allowed)


class OpenAIRealtimePipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.state = ApplicationState(
            initial_languages={"agent": "es", "remote": "en"}
        )
        self.pipeline = OpenAIRealtimeTranslatePipeline.__new__(
            OpenAIRealtimeTranslatePipeline
        )
        self.pipeline.pipeline_name = "remote_to_agent"
        self.pipeline.source_language_name = "remote"
        self.pipeline.target_language_name = "agent"
        self.pipeline.control_state = self.state
        self.pipeline._transcript_sequence = 0
        self.pipeline._transcript_queue = asyncio.Queue()
        self.pipeline._log_transcript_deltas = False
        self.pipeline._audio_send_queue = None
        self.pipeline._audio_send_dropped_blocks = 0
        self.pipeline._input_audio_capture = None
        self.pipeline._accepted_audio_capture = None
        self.pipeline._sent_audio_capture = None
        self.pipeline._output_audio_capture = None

    async def test_deltas_are_published_incrementally_and_finalized_after_idle(
        self,
    ) -> None:
        worker = asyncio.create_task(
            self.pipeline._transcript_worker(
                update_interval_seconds=0.01,
                segment_idle_seconds=0.05,
            )
        )
        try:
            self.pipeline._enqueue_transcript_delta("input", "Hello")
            self.pipeline._enqueue_transcript_delta("input", " there")
            self.pipeline._enqueue_transcript_delta("output", "Hola")

            await self._wait_for(
                lambda: bool(self.state.snapshot()["transcription"]["entries"])
            )
            await self._wait_for(
                lambda: self.state.snapshot()["transcription"]["entries"][0][
                    "translated_text"
                ]
                == "Hola"
            )
            live = self.state.snapshot()["transcription"]["entries"][0]
            self.assertEqual("Hello there", live["source_text"])
            self.assertIn(
                live["translation_status"],
                {"pending", "complete"},
            )

            await self._wait_for(
                lambda: self.state.snapshot()["transcription"]["entries"][0][
                    "translation_status"
                ]
                == "complete"
            )
        finally:
            self.pipeline._transcript_queue.put_nowait(None)
            await worker

    async def test_idle_boundary_starts_a_new_transcript_entry(self) -> None:
        worker = asyncio.create_task(
            self.pipeline._transcript_worker(
                update_interval_seconds=0.005,
                segment_idle_seconds=0.02,
            )
        )
        try:
            self.pipeline._enqueue_transcript_delta("input", "First")
            self.pipeline._enqueue_transcript_delta("output", "Primero")
            await self._wait_for(
                lambda: len(self.state.snapshot()["transcription"]["entries"]) == 1
                and self.state.snapshot()["transcription"]["entries"][0][
                    "translation_status"
                ]
                == "complete"
            )

            self.pipeline._enqueue_transcript_delta("input", "Second")
            self.pipeline._enqueue_transcript_delta("output", "Segundo")
            await self._wait_for(
                lambda: len(self.state.snapshot()["transcription"]["entries"]) == 2
            )

            entries = self.state.snapshot()["transcription"]["entries"]
            self.assertEqual([1, 2], [entry["id"] for entry in entries])
            self.assertEqual("Second", entries[1]["source_text"])
        finally:
            self.pipeline._transcript_queue.put_nowait(None)
            await worker

    async def test_delayed_translation_remains_paired_with_source(self) -> None:
        worker = asyncio.create_task(
            self.pipeline._transcript_worker(
                update_interval_seconds=0.005,
                segment_idle_seconds=0.02,
            )
        )
        try:
            self.pipeline._enqueue_transcript_delta("input", "Hello")
            await asyncio.sleep(0.05)
            self.pipeline._enqueue_transcript_delta("output", "Hola")

            await self._wait_for(
                lambda: bool(self.state.snapshot()["transcription"]["entries"])
                and self.state.snapshot()["transcription"]["entries"][0][
                    "translated_text"
                ]
                == "Hola"
            )

            entries = self.state.snapshot()["transcription"]["entries"]
            self.assertEqual(1, len(entries))
            self.assertEqual("Hello", entries[0]["source_text"])
        finally:
            self.pipeline._transcript_queue.put_nowait(None)
            await worker

    async def test_output_transcript_is_visible_before_source_arrives(self) -> None:
        worker = asyncio.create_task(
            self.pipeline._transcript_worker(
                update_interval_seconds=0.005,
                segment_idle_seconds=0.05,
            )
        )
        try:
            self.pipeline._enqueue_transcript_delta("output", "Translated text")

            await self._wait_for(
                lambda: bool(self.state.snapshot()["transcription"]["entries"])
            )
            entry = self.state.snapshot()["transcription"]["entries"][0]
            self.assertEqual("", entry["source_text"])
            self.assertEqual("Translated text", entry["translated_text"])
            self.assertEqual("pending", entry["translation_status"])
        finally:
            self.pipeline._transcript_queue.put_nowait(None)
            await worker

    async def test_session_boundary_finalizes_current_transcript(self) -> None:
        worker = asyncio.create_task(
            self.pipeline._transcript_worker(
                update_interval_seconds=0.01,
                segment_idle_seconds=10.0,
            )
        )
        try:
            self.pipeline._enqueue_transcript_delta("input", "Hello")
            self.pipeline._enqueue_transcript_delta("output", "Hola")
            self.pipeline._enqueue_transcript_boundary()

            await self._wait_for(
                lambda: bool(self.state.snapshot()["transcription"]["entries"])
                and self.state.snapshot()["transcription"]["entries"][0][
                    "translation_status"
                ]
                == "complete"
            )
        finally:
            self.pipeline._transcript_queue.put_nowait(None)
            await worker

    async def test_reused_session_returns_from_waiting_to_ready(self) -> None:
        self.pipeline._last_status = "waiting_for_call"
        self.pipeline._accept_translated_audio = False
        await self.state.set_pipeline_status(
            "remote_to_agent", "waiting_for_call"
        )

        await self.pipeline._reconcile_gate_status(
            self.pipeline_gate_decision(
                session_required=True,
                audio_allowed=True,
                waiting_for_application=False,
            ),
            session_usable=True,
        )

        pipeline = self.state.snapshot()["pipelines"]["remote_to_agent"]
        self.assertEqual("translation_ready", pipeline["status"])
        self.assertFalse(pipeline["waiting_for_call"])
        self.assertTrue(self.pipeline._accept_translated_audio)

    async def test_audio_send_queue_drops_oldest_block_to_preserve_latency(self) -> None:
        self.pipeline._audio_send_queue = asyncio.Queue(maxsize=2)

        self.pipeline._enqueue_audio_for_translation(b"oldest")
        self.pipeline._enqueue_audio_for_translation(b"middle")
        self.pipeline._enqueue_audio_for_translation(b"newest")

        self.assertEqual(1, self.pipeline._audio_send_dropped_blocks)
        self.assertEqual(b"middle", self.pipeline._audio_send_queue.get_nowait())
        self.assertEqual(b"newest", self.pipeline._audio_send_queue.get_nowait())

    async def test_audio_sender_reports_failure_without_raising_to_pipeline(self) -> None:
        class FailedSession:
            def __init__(self) -> None:
                self.error_event = asyncio.Event()

            async def send_audio(self, block: bytes) -> None:
                raise RealtimeTranslationError("network unavailable")

        session = FailedSession()
        sent_capture = MagicMock()
        self.pipeline._sent_audio_capture = sent_capture
        self.pipeline._audio_send_queue = asyncio.Queue()
        sender = asyncio.create_task(self.pipeline._audio_sender(session))  # type: ignore[arg-type]
        self.pipeline._audio_send_queue.put_nowait(b"audio")
        self.pipeline._audio_send_queue.put_nowait(None)

        await sender

        self.assertTrue(session.error_event.is_set())
        sent_capture.write.assert_not_called()

    async def test_audio_sender_records_only_successfully_sent_audio(self) -> None:
        class SuccessfulSession:
            def __init__(self) -> None:
                self.blocks: list[bytes] = []
                self.error_event = asyncio.Event()

            async def send_audio(self, block: bytes) -> None:
                self.blocks.append(block)

        session = SuccessfulSession()
        sent_capture = MagicMock()
        self.pipeline._sent_audio_capture = sent_capture
        self.pipeline._audio_send_queue = asyncio.Queue()
        sender = asyncio.create_task(self.pipeline._audio_sender(session))  # type: ignore[arg-type]
        self.pipeline._audio_send_queue.put_nowait(b"\x01\x00\x02\x00")
        self.pipeline._audio_send_queue.put_nowait(None)

        await sender

        self.assertEqual([b"\x01\x00\x02\x00"], session.blocks)
        sent_capture.write.assert_called_once_with(b"\x01\x00\x02\x00")

    async def test_raw_input_is_recorded_continuously_for_echo_diagnostics(self) -> None:
        input_capture = MagicMock()
        self.pipeline._input_audio_capture = input_capture

        self.pipeline._record_input_audio(
            b"\x01\x00",
            translation_active=False,
        )
        self.pipeline._record_input_audio(
            b"\x02\x00",
            translation_active=True,
        )

        self.assertEqual(
            [call(b"\x01\x00"), call(b"\x02\x00")],
            input_capture.write.call_args_list,
        )

    async def test_translated_output_is_queued_before_played_callback_records_it(self) -> None:
        translated_output = MagicMock()
        translated_output.buffered_blocks = 0
        translated_output.queue_capacity_blocks = 25
        translated_output.frames_per_block = 960
        translated_output.sample_rate = 48_000
        translated_resampler = MagicMock()
        translated_resampler.process.return_value = [b"\x01\x00\x02\x00"]
        output_capture = MagicMock()
        self.pipeline._accept_translated_audio = True
        self.pipeline._translated_output = translated_output
        self.pipeline._translated_resampler = translated_resampler
        self.pipeline._translated_output_channels = 1
        self.pipeline._output_audio_capture = output_capture

        await self.pipeline._handle_translated_audio(b"\x03\x00")

        output_capture.write.assert_not_called()
        translated_output.push_block.assert_called_once_with(
            b"\x01\x00\x02\x00"
        )

    async def test_translated_output_waits_for_playback_capacity(self) -> None:
        translated_output = MagicMock()
        translated_output.buffered_blocks = 25
        translated_output.queue_capacity_blocks = 25
        translated_output.frames_per_block = 960
        translated_output.sample_rate = 48_000
        translated_resampler = MagicMock()
        translated_resampler.process.return_value = [b"\x01\x00"]
        self.pipeline._accept_translated_audio = True
        self.pipeline._translated_output = translated_output
        self.pipeline._translated_resampler = translated_resampler
        self.pipeline._translated_output_channels = 1

        handler = asyncio.create_task(
            self.pipeline._handle_translated_audio(b"\x03\x00")
        )
        await asyncio.sleep(0.01)
        self.assertFalse(handler.done())
        translated_output.buffered_blocks = 0
        await handler

        translated_output.push_block.assert_called_once_with(b"\x01\x00")

    async def test_actual_remote_playback_is_the_echo_reference(self) -> None:
        output_capture = MagicMock()
        reference = EchoReferenceBus()
        self.pipeline.pipeline_name = "remote_to_agent"
        self.pipeline.echo_reference = reference
        self.pipeline._output_audio_capture = output_capture
        stereo = b"\x10\x00\x10\x00" * 960

        self.pipeline._record_played_audio(stereo, channels=2)

        output_capture.write.assert_called_once_with(stereo)
        self.assertEqual(1, reference.reference_blocks)
        self.assertGreater(reference.snapshot()[1], -96.0)

    @staticmethod
    def pipeline_gate_decision(
        *,
        session_required: bool,
        audio_allowed: bool,
        waiting_for_application: bool,
    ):
        from pipelines.openai_realtime import RealtimeSessionGateDecision

        return RealtimeSessionGateDecision(
            session_required=session_required,
            audio_allowed=audio_allowed,
            waiting_for_application=waiting_for_application,
            observation=True,
        )

    async def _wait_for(
        self, predicate: Callable[[], bool], timeout_seconds: float = 5.0
    ) -> None:
        async with asyncio.timeout(timeout_seconds):
            while not predicate():
                await asyncio.sleep(0.002)


if __name__ == "__main__":
    unittest.main()
