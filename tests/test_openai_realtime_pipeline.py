"""Tests for UI transcript publication by the OpenAI Realtime pipeline."""

import asyncio
from collections.abc import Callable
import unittest
from unittest.mock import AsyncMock, MagicMock, call, patch

from app_state import ApplicationState
from audio.speech_latency import StreamingSpeechLatencyTracker
from engines.openai_realtime import (
    RealtimeRateLimitCoordinator,
    RealtimeTranslationError,
)
from pipelines.openai_realtime import (
    OpenAIRealtimeTranslatePipeline,
    RealtimeSessionGate,
    TranslatedAudioBlock,
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
        self.assertFalse(decision.call_active)

    def test_active_application_allows_connection_and_audio(self) -> None:
        decision = self.gate.evaluate(
            requested=True, observation=True, now=10.0
        )

        self.assertTrue(decision.session_required)
        self.assertTrue(decision.audio_allowed)
        self.assertEqual(1, self.gate.activations)
        self.assertTrue(decision.call_active)

    def test_prewarm_requires_session_without_allowing_audio(self) -> None:
        gate = RealtimeSessionGate(
            enabled=True,
            prewarm_session=True,
            disconnect_grace_seconds=3.0,
            monitor_failure_grace_seconds=5.0,
        )

        waiting = gate.evaluate(
            requested=True,
            observation=False,
            now=10.0,
        )
        active = gate.evaluate(
            requested=True,
            observation=True,
            now=11.0,
        )
        gate.evaluate(
            requested=True,
            observation=False,
            now=20.0,
        )
        after_call = gate.evaluate(
            requested=True,
            observation=False,
            now=24.0,
        )

        self.assertTrue(waiting.session_required)
        self.assertFalse(waiting.audio_allowed)
        self.assertTrue(waiting.waiting_for_application)
        self.assertTrue(active.session_required)
        self.assertTrue(active.audio_allowed)
        self.assertTrue(after_call.session_required)
        self.assertFalse(after_call.audio_allowed)
        self.assertTrue(after_call.waiting_for_application)

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
        self.assertTrue(grace.call_active)
        self.assertFalse(expired.session_required)
        self.assertFalse(expired.audio_allowed)
        self.assertTrue(expired.waiting_for_application)
        self.assertFalse(expired.call_active)
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

    def test_disconnect_grace_starts_with_first_inactive_observation(self) -> None:
        self.gate.evaluate(requested=True, observation=True, now=10.0)

        first_miss = self.gate.evaluate(
            requested=True, observation=False, now=100.0
        )
        within_grace = self.gate.evaluate(
            requested=True, observation=False, now=102.9
        )
        expired = self.gate.evaluate(
            requested=True, observation=False, now=103.1
        )

        self.assertTrue(first_miss.audio_allowed)
        self.assertTrue(within_grace.audio_allowed)
        self.assertFalse(expired.audio_allowed)

    def test_passthrough_never_requires_an_api_session(self) -> None:
        decision = self.gate.evaluate(
            requested=False, observation=True, now=10.0
        )

        self.assertFalse(decision.session_required)
        self.assertFalse(decision.audio_allowed)
        self.assertTrue(decision.call_active)


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
        self.pipeline.aec3 = MagicMock()
        self.pipeline.aec3.process_capture.side_effect = (
            lambda pcm16, *, sample_rate: pcm16
        )
        self.pipeline.aec3.statistics_snapshot.return_value = {}
        self.pipeline._transcript_sequence = 0
        self.pipeline._transcript_queue = asyncio.Queue()
        self.pipeline._log_transcript_deltas = False
        self.pipeline._provider_delay_active = False
        self.pipeline._provider_delay_warning_ms = 3000.0
        self.pipeline._provider_delay_recovery_ms = 1500.0
        self.pipeline._output_gain_db = 0.0
        self.pipeline._output_gain_clipped_samples = 0
        self.pipeline._audio_send_queue = None
        self.pipeline._audio_send_dropped_blocks = 0
        self.pipeline._accept_translated_audio = False
        self.pipeline._translated_audio_queue = None
        self.pipeline._translated_audio_pending_bytes = bytearray()
        self.pipeline._translated_audio_block_bytes = 4
        self.pipeline._translated_audio_generation = 0
        self.pipeline._translated_audio_dropped_blocks = 0
        self.pipeline._translated_audio_discontinuities = 0
        self.pipeline._translated_audio_maximum_buffered_blocks = 0
        self.pipeline._translated_audio_silence_boundary_dropped_blocks = 0
        self.pipeline._translated_playback_dropped_blocks = 0
        self.pipeline._passthrough_playback_dropped_blocks = 0
        self.pipeline._adaptive_playout = {
            "enabled": True,
            "target_backlog_ms": 1000.0,
            "accelerated_backlog_ms": 2000.0,
            "emergency_backlog_ms": 2800.0,
            "recovery_backlog_ms": 1500.0,
            "moderate_speed": 1.10,
            "maximum_speed": 1.15,
            "silence_search_ms": 250.0,
            "silence_threshold_dbfs": -42.0,
            "crossfade_ms": 15.0,
        }
        self.pipeline._adaptive_speed = 1.0
        self.pipeline._adaptive_maximum_speed = 1.0
        self.pipeline._adaptive_compressed_input_samples = 0
        self.pipeline._adaptive_compressed_output_samples = 0
        self.pipeline._adaptive_time_above_target_seconds = 0.0
        self.pipeline._adaptive_last_observed_at = None
        self.pipeline._adaptive_last_backlog_ms = 0.0
        from collections import deque

        self.pipeline._adaptive_backlog_samples_ms = deque(maxlen=18_000)
        self.pipeline._time_stretcher = MagicMock()
        self.pipeline._time_stretcher.process.side_effect = (
            lambda pcm16, **_: [pcm16] if pcm16 else []
        )
        self.pipeline._last_status = None
        self.pipeline._input_audio_capture = None
        self.pipeline._accepted_audio_capture = None
        self.pipeline._sent_audio_capture = None
        self.pipeline._received_audio_capture = None
        self.pipeline._output_audio_capture = None
        self.pipeline._diagnostic_tracks = ("captured", "played")
        self.pipeline._diagnostic_manifest = None
        self.pipeline._diagnostic_recording_configured = False
        self.pipeline._diagnostic_recording_active = True
        self.pipeline._diagnostic_capture_session_active = False
        self.pipeline._call_cable_active = True
        self.pipeline._speech_latency = StreamingSpeechLatencyTracker()

    def test_reconnect_delay_uses_jitter_and_shares_rate_limit_cooldown(
        self,
    ) -> None:
        self.pipeline.config = {
            "realtime_translation": {
                "openai": {"reconnect_base_delay_seconds": 2.0}
            }
        }
        coordinator = RealtimeRateLimitCoordinator()
        self.pipeline._rate_limit_coordinator = coordinator

        with patch(
            "pipelines.openai_realtime.random.uniform",
            side_effect=[1.5, 3.0, 1.25],
        ) as uniform:
            first = self.pipeline._reconnect_delay(
                1,
                RealtimeTranslationError("rate limited"),
                now=10.0,
            )
            second = self.pipeline._reconnect_delay(
                2,
                RealtimeTranslationError("HTTP 429"),
                now=10.5,
            )
            ordinary = self.pipeline._reconnect_delay(
                1,
                RealtimeTranslationError("connection reset"),
                now=11.0,
            )

        self.assertEqual(1.5, first)
        self.assertEqual(3.0, second)
        self.assertEqual(1.25, ordinary)
        self.assertEqual(1.5, coordinator.remaining_delay(now=11.0))
        self.assertEqual(
            [call(1.0, 2.0), call(2.0, 4.0), call(1.0, 2.0)],
            uniform.call_args_list,
        )

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
                alignment_wait_seconds=2.0,
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

    async def test_provider_delay_status_uses_hysteresis(self) -> None:
        session = MagicMock()
        session.statistics.output_audio_lag_ms = 3200.0
        session.statistics.output_transcript_lag_ms = None

        self.assertFalse(self.pipeline._refresh_provider_delay(session))

        session.statistics.output_transcript_lag_ms = 3200.0
        self.assertTrue(self.pipeline._refresh_provider_delay(session))

        session.statistics.output_audio_lag_ms = 90000.0
        session.statistics.output_transcript_lag_ms = 2200.0
        self.assertTrue(self.pipeline._refresh_provider_delay(session))

        session.statistics.output_transcript_lag_ms = 1400.0
        self.assertFalse(self.pipeline._refresh_provider_delay(session))

    async def test_delayed_translation_remains_effectively_active(self) -> None:
        await self.state.set_mode("remote_to_agent", "translate")
        await self.state.set_pipeline_status(
            "remote_to_agent",
            "translation_delayed",
        )

        snapshot = self.state.snapshot()["pipelines"]["remote_to_agent"]

        self.assertEqual("translation_delayed", snapshot["status"])
        self.assertEqual("translate", snapshot["effective_mode"])

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
            self.assertEqual("waiting_for_source", entry["pair_status"])
        finally:
            self.pipeline._transcript_queue.put_nowait(None)
            await worker

    async def test_unpaired_output_is_finalized_without_joining_an_older_turn(
        self,
    ) -> None:
        worker = asyncio.create_task(
            self.pipeline._transcript_worker(
                update_interval_seconds=0.005,
                segment_idle_seconds=0.01,
                alignment_wait_seconds=0.03,
            )
        )
        try:
            self.pipeline._enqueue_transcript_delta("input", "First")
            self.pipeline._enqueue_transcript_delta("output", "Primero")
            await self._wait_for(
                lambda: bool(self.state.snapshot()["transcription"]["entries"])
                and self.state.snapshot()["transcription"]["entries"][0][
                    "translation_status"
                ]
                == "complete"
            )

            self.pipeline._enqueue_transcript_delta("output", "Continuation")
            await self._wait_for(
                lambda: len(self.state.snapshot()["transcription"]["entries"])
                == 2
                and self.state.snapshot()["transcription"]["entries"][1][
                    "pair_status"
                ]
                == "translation_without_source"
            )

            entries = self.state.snapshot()["transcription"]["entries"]
            self.assertEqual("aligned", entries[0]["pair_status"])
            self.assertEqual("", entries[1]["source_text"])
            self.assertEqual("Continuation", entries[1]["translated_text"])
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

    async def test_cached_status_overwrites_external_switching_state(self) -> None:
        self.pipeline._last_status = "waiting_for_call"
        await self.state.set_pipeline_status(
            "remote_to_agent",
            "waiting_for_call",
        )
        await self.state.set_mode("remote_to_agent", "translate")
        self.assertEqual(
            "switching",
            self.state.get_pipeline_status("remote_to_agent").value,
        )

        await self.pipeline._set_status("waiting_for_call")

        self.assertEqual(
            "waiting_for_call",
            self.state.get_pipeline_status("remote_to_agent").value,
        )

    async def test_waiting_gate_does_not_accept_call_audio(
        self,
    ) -> None:
        await self.state.set_mode("remote_to_agent", "translate")
        self.pipeline._last_status = "initializing"
        self.pipeline._accept_translated_audio = True

        await self.pipeline._reconcile_gate_status(
            self.pipeline_gate_decision(
                session_required=False,
                audio_allowed=False,
                waiting_for_application=True,
            ),
            session_usable=True,
        )

        pipeline = self.state.snapshot()["pipelines"]["remote_to_agent"]
        self.assertEqual("passthrough", pipeline["status"])
        self.assertTrue(pipeline["waiting_for_call"])
        self.assertEqual("waiting_for_call", self.pipeline._last_status)
        self.assertFalse(self.pipeline._accept_translated_audio)

    async def test_waiting_gate_preserves_audio_during_graceful_drain(
        self,
    ) -> None:
        await self.state.set_mode("remote_to_agent", "translate")
        self.pipeline._accept_translated_audio = True

        await self.pipeline._reconcile_gate_status(
            self.pipeline_gate_decision(
                session_required=False,
                audio_allowed=False,
                waiting_for_application=True,
            ),
            session_usable=False,
            draining=True,
        )

        self.assertTrue(self.pipeline._accept_translated_audio)
        self.assertEqual("waiting_for_call", self.pipeline._last_status)

    async def test_audio_send_queue_drops_oldest_block_to_preserve_latency(self) -> None:
        self.pipeline._audio_send_queue = asyncio.Queue(maxsize=2)

        self.pipeline._enqueue_audio_for_translation(b"oldest")
        self.pipeline._enqueue_audio_for_translation(b"middle")
        self.pipeline._enqueue_audio_for_translation(b"newest")

        self.assertEqual(1, self.pipeline._audio_send_dropped_blocks)
        self.assertEqual(b"middle", self.pipeline._audio_send_queue.get_nowait())
        self.assertEqual(b"newest", self.pipeline._audio_send_queue.get_nowait())

    async def test_audio_send_queue_preserves_digital_silence(self) -> None:
        self.pipeline._audio_send_queue = asyncio.Queue(maxsize=2)
        silence = bytes(200 * 24_000 // 1000 * 2)

        self.pipeline._enqueue_audio_for_translation(silence)

        self.assertEqual(silence, self.pipeline._audio_send_queue.get_nowait())
        self.assertEqual(0, self.pipeline._audio_send_dropped_blocks)

    async def test_prewarmed_call_start_discards_pre_call_media(self) -> None:
        input_resampler = MagicMock()
        passthrough_resampler = MagicMock()
        translated_resampler = MagicMock()
        output = MagicMock()
        self.pipeline._translated_resampler = translated_resampler
        self.pipeline._audio_send_queue = asyncio.Queue()
        self.pipeline._audio_send_queue.put_nowait(b"stale")

        self.pipeline._prepare_call_audio_start(
            input_resampler,
            passthrough_resampler,
            output,
        )

        input_resampler.reset.assert_called_once_with()
        passthrough_resampler.reset.assert_called_once_with()
        translated_resampler.reset.assert_called_once_with()
        output.discard_pending_blocks.assert_called_once_with()
        self.assertTrue(self.pipeline._audio_send_queue.empty())
        self.assertEqual(1, self.pipeline._audio_send_dropped_blocks)

    async def test_discarded_audio_queues_complete_join_accounting(self) -> None:
        self.pipeline._audio_send_queue = asyncio.Queue()
        self.pipeline._audio_send_queue.put_nowait(b"pending-input")
        self.pipeline._translated_audio_queue = asyncio.Queue()
        self.pipeline._translated_audio_queue.put_nowait(
            TranslatedAudioBlock(b"pending-output", generation=0)
        )

        self.pipeline._discard_audio_send_queue("test")
        self.pipeline._discard_translated_audio_queue("test")

        await asyncio.wait_for(self.pipeline._audio_send_queue.join(), timeout=0.1)
        await asyncio.wait_for(
            self.pipeline._translated_audio_queue.join(),
            timeout=0.1,
        )

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

    async def test_source_end_flushes_local_audio_before_graceful_close(
        self,
    ) -> None:
        session = MagicMock()
        session.close_timeout_seconds = 0.5
        session.send_audio = AsyncMock()
        session.close = AsyncMock()
        session.abort = AsyncMock()
        input_resampler = MagicMock()
        input_resampler.process.return_value = (b"final-partial-frame",)
        self.pipeline._audio_send_queue = asyncio.Queue(maxsize=3)
        self.pipeline._translated_audio_queue = None
        sender = asyncio.create_task(
            self.pipeline._audio_sender(session)
        )

        await self.pipeline._close_session_after_source_end(
            session,
            input_resampler,
        )
        self.pipeline._audio_send_queue.put_nowait(None)
        await sender

        input_resampler.process.assert_called_once_with(b"", final=True)
        session.send_audio.assert_awaited_once_with(b"final-partial-frame")
        session.close.assert_awaited_once_with()
        session.abort.assert_not_awaited()

    async def test_source_end_waits_for_physical_playback_to_drain(self) -> None:
        session = MagicMock()
        session.close_timeout_seconds = 0.2
        session.close = AsyncMock()
        session.abort = AsyncMock()
        input_resampler = MagicMock()
        input_resampler.process.return_value = ()
        translated_resampler = MagicMock()
        translated_resampler.process.return_value = ()
        output = MagicMock()
        output.buffered_blocks = 1
        output.frames_per_block = 960
        output.sample_rate = 48_000
        self.pipeline._accept_translated_audio = True
        self.pipeline._translated_audio_queue = asyncio.Queue()
        self.pipeline._translated_resampler = translated_resampler
        self.pipeline._translated_output = output
        worker = asyncio.create_task(self.pipeline._translated_audio_worker())

        async def finish_playback() -> None:
            await asyncio.sleep(0.03)
            output.buffered_blocks = 0

        release = asyncio.create_task(finish_playback())
        started = asyncio.get_running_loop().time()
        await self.pipeline._close_session_after_source_end(
            session,
            input_resampler,
        )
        elapsed = asyncio.get_running_loop().time() - started
        self.pipeline._translated_audio_queue.put_nowait(None)
        await worker
        await release

        self.assertGreaterEqual(elapsed, 0.01)
        session.close.assert_awaited_once_with()

    def test_adaptive_playout_speed_tracks_backlog_watermarks(self) -> None:
        with patch(
            "pipelines.openai_realtime.time.monotonic",
            side_effect=[10.0, 11.0, 12.0, 13.0],
        ):
            normal = self.pipeline._observe_adaptive_backlog(800.0)
            moderate = self.pipeline._observe_adaptive_backlog(1500.0)
            accelerated = self.pipeline._observe_adaptive_backlog(2400.0)
            emergency = self.pipeline._observe_adaptive_backlog(2900.0)

        self.assertEqual(1.0, normal)
        self.assertGreater(moderate, 1.05)
        self.assertGreater(accelerated, moderate)
        self.assertEqual(1.15, emergency)
        self.assertEqual(1.15, self.pipeline._adaptive_maximum_speed)
        self.assertEqual(2.0, self.pipeline._adaptive_time_above_target_seconds)

    async def test_raw_input_is_recorded_during_call_in_both_modes(self) -> None:
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

    async def test_diagnostic_tracks_are_not_written_without_an_active_call(
        self,
    ) -> None:
        input_capture = MagicMock()
        accepted_capture = MagicMock()
        sent_capture = MagicMock()
        received_capture = MagicMock()
        output_capture = MagicMock()
        self.pipeline._input_audio_capture = input_capture
        self.pipeline._accepted_audio_capture = accepted_capture
        self.pipeline._sent_audio_capture = sent_capture
        self.pipeline._received_audio_capture = received_capture
        self.pipeline._output_audio_capture = output_capture
        self.pipeline._call_cable_active = False

        self.pipeline._record_captured_audio(b"captured")
        self.pipeline._record_accepted_audio(b"accepted")
        self.pipeline._record_sent_audio(b"sent")
        self.pipeline._record_received_audio(b"received")
        self.pipeline._record_played_audio(b"\x01\x00", channels=1)

        input_capture.write.assert_not_called()
        accepted_capture.write.assert_not_called()
        sent_capture.write.assert_not_called()
        received_capture.write.assert_not_called()
        output_capture.write.assert_not_called()

    async def test_manual_recording_control_arms_and_closes_realtime_tracks(
        self,
    ) -> None:
        captures = [MagicMock() for _ in range(5)]
        (
            self.pipeline._input_audio_capture,
            self.pipeline._accepted_audio_capture,
            self.pipeline._sent_audio_capture,
            self.pipeline._received_audio_capture,
            self.pipeline._output_audio_capture,
        ) = captures
        self.pipeline._diagnostic_recording_active = False
        self.pipeline.control_state = ApplicationState(
            audio_recording_enabled=True,
            initial_engines={"remote_to_agent": "openai_realtime"},
            initial_languages={"agent": "es", "remote": "en"},
        )

        await self.pipeline.control_state.set_manual_audio_recording(True)
        self.pipeline._sync_diagnostic_recording()
        self.pipeline._record_captured_audio(b"captured")
        await self.pipeline.control_state.set_manual_audio_recording(False)
        self.pipeline._sync_diagnostic_recording()

        self.assertTrue(captures[0].write.called)
        self.assertFalse(self.pipeline._diagnostic_recording_active)
        for capture in captures:
            capture.close_session.assert_called_once_with()

    async def test_ending_call_closes_each_diagnostic_track(self) -> None:
        captures = [MagicMock() for _ in range(5)]
        (
            self.pipeline._input_audio_capture,
            self.pipeline._accepted_audio_capture,
            self.pipeline._sent_audio_capture,
            self.pipeline._received_audio_capture,
            self.pipeline._output_audio_capture,
        ) = captures
        self.pipeline._diagnostic_capture_session_active = True

        self.pipeline._set_call_cable_active(False)

        for capture in captures:
            capture.close_session.assert_called_once_with()

    async def test_selected_tracks_start_with_one_shared_timeline_origin(
        self,
    ) -> None:
        captured = MagicMock()
        played = MagicMock()
        self.pipeline._input_audio_capture = captured
        self.pipeline._output_audio_capture = played
        self.pipeline._diagnostic_capture_session_active = False
        self.pipeline._diagnostic_recording_active = True
        self.pipeline._call_cable_active = True

        with patch(
            "pipelines.openai_realtime.time.monotonic",
            return_value=123.456,
        ):
            self.pipeline._sync_diagnostic_capture_session()

        captured.start_session.assert_called_once_with(123.456)
        played.start_session.assert_called_once_with(123.456)

    async def test_translated_output_is_queued_before_played_callback_records_it(self) -> None:
        translated_output = MagicMock()
        translated_output.buffered_blocks = 0
        translated_output.queue_capacity_blocks = 25
        translated_output.frames_per_block = 960
        translated_output.sample_rate = 48_000
        translated_resampler = MagicMock()
        translated_resampler.process.return_value = [b"\x01\x00\x02\x00"]
        output_capture = MagicMock()
        received_capture = MagicMock()
        self.pipeline._accept_translated_audio = True
        self.pipeline._translated_output = translated_output
        self.pipeline._translated_resampler = translated_resampler
        self.pipeline._translated_output_channels = 1
        self.pipeline._output_audio_capture = output_capture
        self.pipeline._received_audio_capture = received_capture
        self.pipeline._translated_audio_queue = asyncio.Queue(maxsize=2)
        worker = asyncio.create_task(self.pipeline._translated_audio_worker())

        self.pipeline._enqueue_translated_audio(b"\x03\x00\x04\x00")
        await asyncio.sleep(0)
        self.pipeline._translated_audio_queue.put_nowait(None)
        await worker

        output_capture.write.assert_not_called()
        received_capture.write.assert_called_once_with(b"\x03\x00\x04\x00")
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
        self.pipeline._translated_audio_queue = asyncio.Queue(maxsize=2)

        worker = asyncio.create_task(
            self.pipeline._translated_audio_worker()
        )
        self.pipeline._enqueue_translated_audio(b"\x03\x00\x04\x00")
        await asyncio.sleep(0.01)
        self.assertFalse(worker.done())
        translated_output.push_block.assert_not_called()
        translated_output.buffered_blocks = 0
        await asyncio.sleep(0.01)
        self.pipeline._translated_audio_queue.put_nowait(None)
        await worker

        translated_output.push_block.assert_called_once_with(b"\x01\x00")

    async def test_websocket_callback_returns_while_playback_is_full(self) -> None:
        self.pipeline._accept_translated_audio = True
        self.pipeline._translated_audio_queue = asyncio.Queue(maxsize=2)

        self.pipeline._enqueue_translated_audio(b"\x01\x00\x02\x00")

        queued = self.pipeline._translated_audio_queue.get_nowait()
        self.assertEqual(
            TranslatedAudioBlock(b"\x01\x00\x02\x00", generation=0),
            queued,
        )

    async def test_provider_delta_is_split_into_twenty_millisecond_blocks(
        self,
    ) -> None:
        block_bytes = 960
        self.pipeline._translated_audio_block_bytes = block_bytes
        self.pipeline._translated_audio_queue = asyncio.Queue(maxsize=25)
        self.pipeline._accept_translated_audio = True
        delta = b"".join(
            index.to_bytes(2, "little") * (block_bytes // 2)
            for index in range(20)
        )

        self.pipeline._enqueue_translated_audio(delta)

        self.assertEqual(20, self.pipeline._translated_audio_queue.qsize())
        queued = [
            self.pipeline._translated_audio_queue.get_nowait()
            for _ in range(20)
        ]
        self.assertEqual(
            [index.to_bytes(2, "little") for index in range(20)],
            [item.pcm16[:2] for item in queued if item is not None],
        )
        self.assertEqual(
            20,
            self.pipeline._translated_audio_maximum_buffered_blocks,
        )
        self.assertEqual(0, self.pipeline._translated_audio_dropped_blocks)

    async def test_received_queue_overflow_discards_only_oldest_backlog(self) -> None:
        self.pipeline._translated_audio_queue = asyncio.Queue(maxsize=2)
        self.pipeline._accept_translated_audio = True

        with self.assertLogs(
            "pipelines.openai_realtime", level="WARNING"
        ) as captured:
            self.pipeline._enqueue_translated_audio(
                b"\x01\x00\x01\x00"
                b"\x02\x00\x02\x00"
                b"\x03\x00\x03\x00"
            )

        self.assertEqual(0, self.pipeline._translated_audio_generation)
        self.assertEqual(1, self.pipeline._translated_audio_dropped_blocks)
        self.assertEqual(1, self.pipeline._translated_audio_discontinuities)
        self.assertIn(
            "event=realtime_translation_backlog_recovered",
            captured.output[0],
        )
        queued = [
            self.pipeline._translated_audio_queue.get_nowait()
            for _ in range(2)
        ]
        self.assertEqual(
            [b"\x02\x00\x02\x00", b"\x03\x00\x03\x00"],
            [item.pcm16 for item in queued],
        )
        self.assertTrue(all(item.generation == 0 for item in queued))

    async def test_post_call_received_queue_recovery_is_not_a_warning(self) -> None:
        self.pipeline._translated_audio_queue = asyncio.Queue(maxsize=2)
        self.pipeline._accept_translated_audio = True
        self.pipeline._call_cable_active = False

        with self.assertNoLogs("pipelines.openai_realtime", level="WARNING"):
            self.pipeline._enqueue_translated_audio(
                b"\x01\x00\x01\x00"
                b"\x02\x00\x02\x00"
                b"\x03\x00\x03\x00"
            )

        self.assertEqual(1, self.pipeline._translated_audio_dropped_blocks)
        self.assertEqual(1, self.pipeline._translated_audio_discontinuities)

    async def test_disabling_translation_invalidates_in_flight_audio(self) -> None:
        translated_output = MagicMock()
        translated_output.buffered_blocks = 1
        translated_output.queue_capacity_blocks = 1
        translated_output.frames_per_block = 960
        translated_output.sample_rate = 48_000
        translated_resampler = MagicMock()
        translated_resampler.process.return_value = [b"\x01\x00"]
        self.pipeline._accept_translated_audio = True
        self.pipeline._translated_output = translated_output
        self.pipeline._translated_resampler = translated_resampler
        self.pipeline._translated_output_channels = 1
        self.pipeline._translated_audio_queue = asyncio.Queue(maxsize=2)
        worker = asyncio.create_task(self.pipeline._translated_audio_worker())
        self.pipeline._enqueue_translated_audio(b"\x03\x00\x04\x00")
        await asyncio.sleep(0.01)

        self.pipeline._set_translation_audio_acceptance(False)
        translated_output.buffered_blocks = 0
        await asyncio.sleep(0.01)
        self.pipeline._translated_audio_queue.put_nowait(None)
        await worker

        translated_output.push_block.assert_not_called()
        self.assertEqual(1, self.pipeline._translated_audio_generation)

    async def test_actual_remote_playback_is_the_aec3_render_reference(self) -> None:
        output_capture = MagicMock()
        aec3 = MagicMock()
        self.pipeline.pipeline_name = "remote_to_agent"
        self.pipeline.aec3 = aec3
        self.pipeline._output_audio_capture = output_capture
        stereo = b"\x10\x00\x10\x00" * 960

        self.pipeline._record_played_audio(stereo, channels=2)

        output_capture.write.assert_called_once_with(stereo)
        aec3.observe_render.assert_called_once_with(
            stereo,
            sample_rate=48_000,
            channels=2,
        )

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
