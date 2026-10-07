"""Directional audio pipeline for OpenAI's dedicated Realtime translation API."""

import asyncio
from collections import deque
from dataclasses import dataclass
import logging
import math
from pathlib import Path
import random
import time

import numpy as np

from app_state import ApplicationState, PipelineMode
from audio.capture import QueuedAudioInput
from audio.device_manager import AudioDeviceManager
from audio.diagnostic_manifest import (
    DiagnosticCallManifest,
    diagnostic_call_id,
)
from audio.pcm import (
    apply_int16_gain,
    calculate_block_frames,
    convert_int16_channels,
)
from audio.playback import QueuedAudioOutput
from audio.portaudio import AudioDeviceError, format_device
from audio.resampling import PcmInt16Resampler, create_resampler
from audio.speech_latency import StreamingSpeechLatencyTracker
from audio.time_stretch import StreamingWsola
from audio.wav_capture import QueuedDiagnosticWavCapture
from audio.webrtc_aec3 import WebRtcAec3
from config import set_log_pipeline
from engines.openai_realtime import (
    OpenAIRealtimeTranslationFactory,
    OpenAIRealtimeTranslationSession,
    is_realtime_rate_limit_error,
)
from engines.realtime_translation import RealtimeTranscriptDelta
from metrics import CsvMetricsWriter


LOGGER = logging.getLogger(__name__)
TranscriptQueueItem = (
    tuple[str, str, str, str, int | None, float | None] | None
)


@dataclass(frozen=True)
class TranslatedAudioBlock:
    """One provider PCM block tagged with its current playback generation."""

    pcm16: bytes
    generation: int
    final: bool = False


TranslatedAudioQueueItem = TranslatedAudioBlock | None


@dataclass(frozen=True)
class RealtimeSessionGateDecision:
    """Decide whether an API session and audio sending are currently allowed."""

    session_required: bool
    audio_allowed: bool
    waiting_for_application: bool
    observation: bool | None
    call_active: bool = False


class RealtimeSessionGate:
    """Stabilize application presence before allowing routed call audio."""

    def __init__(
        self,
        *,
        enabled: bool,
        prewarm_session: bool = False,
        disconnect_grace_seconds: float,
        monitor_failure_grace_seconds: float,
    ) -> None:
        self.enabled = enabled
        self.prewarm_session = prewarm_session
        self.disconnect_grace_seconds = disconnect_grace_seconds
        self.monitor_failure_grace_seconds = monitor_failure_grace_seconds
        self.activations = 0
        self.deactivations = 0
        self.blocked_seconds = 0.0
        self._last_present_at: float | None = None
        self._inactive_since: float | None = None
        self._unknown_since: float | None = None
        self._last_evaluated_at: float | None = None
        self._last_requested = False
        self._last_audio_allowed = False
        self._last_session_required = False
        self._last_call_active = False

    def evaluate(
        self,
        *,
        requested: bool,
        observation: bool | None,
        now: float,
    ) -> RealtimeSessionGateDecision:
        if (
            self._last_evaluated_at is not None
            and self._last_requested
            and not self._last_audio_allowed
        ):
            self.blocked_seconds += max(0.0, now - self._last_evaluated_at)
        self._last_evaluated_at = now

        if observation is True:
            self._last_present_at = now
            self._inactive_since = None
            self._unknown_since = None
            call_active = True
        elif observation is False:
            self._unknown_since = None
            if self._inactive_since is None:
                self._inactive_since = now
            call_active = (
                self._last_call_active
                and now - self._inactive_since < self.disconnect_grace_seconds
            )
        else:
            self._inactive_since = None
            if self._unknown_since is None:
                self._unknown_since = now
            call_active = (
                self._last_call_active
                and now - self._unknown_since < self.monitor_failure_grace_seconds
            )

        if not requested:
            decision = RealtimeSessionGateDecision(
                False, False, False, observation, call_active
            )
        elif not self.enabled:
            decision = RealtimeSessionGateDecision(
                True, True, False, observation, call_active
            )
        else:
            decision = RealtimeSessionGateDecision(
                call_active or self.prewarm_session,
                call_active,
                not call_active,
                observation,
                call_active,
            )

        if decision.audio_allowed and not self._last_audio_allowed:
            self.activations += 1
        elif self._last_audio_allowed and not decision.audio_allowed:
            self.deactivations += 1
        self._last_requested = requested
        self._last_audio_allowed = decision.audio_allowed
        self._last_session_required = decision.session_required
        self._last_call_active = decision.call_active
        return decision


class OpenAIRealtimeTranslatePipeline:
    """Stream one participant track to one target language and output translated audio."""

    def __init__(
        self,
        config: dict[str, object],
        session_factory: OpenAIRealtimeTranslationFactory,
        *,
        pipeline_name: str,
        source_language_name: str,
        target_language_name: str,
        duration: float | None = None,
        startup_barrier: asyncio.Barrier | None = None,
        control_state: ApplicationState | None = None,
        device_manager: AudioDeviceManager | None = None,
        metrics_writer: CsvMetricsWriter | None = None,
        aec3: WebRtcAec3 | None = None,
    ) -> None:
        self.config = config
        self.session_factory = session_factory
        self._rate_limit_coordinator = session_factory.rate_limit_coordinator
        self.pipeline_name = pipeline_name
        self.source_language_name = source_language_name
        self.target_language_name = target_language_name
        self.duration = duration
        self.startup_barrier = startup_barrier
        self.control_state = control_state
        self.device_manager = device_manager
        self.metrics_writer = metrics_writer
        self.aec3 = aec3 or WebRtcAec3()
        self._diagnostic_manifest: DiagnosticCallManifest | None = None
        self._accept_translated_audio = False
        self._translated_output: QueuedAudioOutput | None = None
        self._translated_resampler: PcmInt16Resampler | None = None
        self._translated_output_channels = 1
        self._translated_audio_queue: (
            asyncio.Queue[TranslatedAudioQueueItem] | None
        ) = None
        self._translated_audio_pending_bytes = bytearray()
        self._translated_audio_block_bytes = 0
        self._translated_audio_block_duration_ms = 20.0
        self._translated_audio_generation = 0
        self._translated_audio_dropped_blocks = 0
        self._translated_audio_discontinuities = 0
        self._translated_audio_maximum_buffered_blocks = 0
        self._translated_audio_silence_boundary_dropped_blocks = 0
        self._translated_playback_dropped_blocks = 0
        self._passthrough_playback_dropped_blocks = 0
        self._adaptive_playout: dict[str, object] = {}
        self._time_stretcher: StreamingWsola | None = None
        self._adaptive_speed = 1.0
        self._adaptive_maximum_speed = 1.0
        self._adaptive_compressed_input_samples = 0
        self._adaptive_compressed_output_samples = 0
        self._adaptive_time_above_target_seconds = 0.0
        self._adaptive_last_observed_at: float | None = None
        self._adaptive_last_backlog_ms = 0.0
        self._adaptive_backlog_samples_ms: deque[float] = deque(maxlen=18_000)
        self._aec3_render_sample_rate = 48_000
        self._last_status: str | None = None
        self._transcript_sequence = 0
        self._transcript_queue: asyncio.Queue[TranscriptQueueItem] | None = None
        self._log_transcript_deltas = False
        self._provider_delay_active = False
        self._provider_delay_warning_ms = 3_000.0
        self._provider_delay_recovery_ms = 1_500.0
        self._output_gain_db = 0.0
        self._output_gain_clipped_samples = 0
        self._audio_send_queue: asyncio.Queue[bytes | None] | None = None
        self._audio_send_dropped_blocks = 0
        self._input_audio_capture: QueuedDiagnosticWavCapture | None = None
        self._accepted_audio_capture: QueuedDiagnosticWavCapture | None = None
        self._sent_audio_capture: QueuedDiagnosticWavCapture | None = None
        self._received_audio_capture: QueuedDiagnosticWavCapture | None = None
        self._output_audio_capture: QueuedDiagnosticWavCapture | None = None
        self._diagnostic_tracks: tuple[str, ...] = ()
        self._diagnostic_recording_configured = False
        self._diagnostic_recording_active = False
        self._diagnostic_capture_session_active = False
        self._call_cable_active = False
        self._speech_latency = StreamingSpeechLatencyTracker()

    async def run(self) -> None:
        set_log_pipeline(self.pipeline_name)
        audio = self._section("audio")
        pipeline = self._pipeline_section()
        realtime = self._realtime_section()
        if self.device_manager is None:
            raise AudioDeviceError("Realtime pipelines require audio device management.")

        capture_rate = int(pipeline["input_sample_rate"])
        capture_channels = int(pipeline["input_channels"])
        output_rate = int(pipeline["output_sample_rate"])
        output_channels = int(pipeline["output_channels"])
        api_rate = int(realtime["sample_rate"])
        frame_duration_ms = float(audio["frame_duration_ms"])
        resampling = dict(audio["resampling"])
        session_gate_settings = dict(realtime["session_gate"])
        virtual_cable_settings = dict(audio["virtual_cables"])
        session_monitor_settings = dict(
            virtual_cable_settings["session_monitor"]
        )
        session_gate = RealtimeSessionGate(
            enabled=bool(session_gate_settings["enabled"]),
            prewarm_session=bool(session_gate_settings["prewarm_session"]),
            disconnect_grace_seconds=float(
                session_gate_settings["disconnect_grace_seconds"]
            ),
            monitor_failure_grace_seconds=float(
                session_monitor_settings["failure_grace_seconds"]
            ),
        )
        session_gate_route = (
            "translated_output"
            if self.pipeline_name == "agent_to_remote"
            else "remote_input"
        )
        input_device_key = (
            "input" if self.pipeline_name == "agent_to_remote" else "remote_input"
        )
        output_device_key = (
            "output" if self.pipeline_name == "remote_to_agent" else "translated_output"
        )
        input_device = self.device_manager.active_index(input_device_key)
        output_device = self.device_manager.active_index(output_device_key)
        capture = QueuedAudioInput(
            input_device=input_device,
            sample_rate=capture_rate,
            channels=capture_channels,
            dtype=str(audio["sample_format"]),
            frame_duration_ms=frame_duration_ms,
            queue_capacity_blocks=int(realtime["input_queue_capacity_blocks"]),
            negotiate_format=input_device_key == "input",
            exclusive=(
                input_device_key == "input"
                and bool(
                    audio.get("physical_capture_exclusive_mode", True)
                )
            ),
        )
        if input_device_key == "input":
            while True:
                try:
                    await asyncio.to_thread(capture.prepare)
                    await self.device_manager.report_physical_device_healthy(
                        "input",
                        input_device,
                        sample_rate=capture.sample_rate,
                        channels=capture.channels,
                    )
                    break
                except Exception as error:
                    changed = await self.device_manager.report_physical_device_failed(
                        "input", input_device, error
                    )
                    if not changed:
                        raise
                    input_device = self.device_manager.active_index("input")
                    capture.input_device_reference = input_device
            capture_rate = capture.sample_rate
            capture_channels = capture.channels
        output_frames = calculate_block_frames(output_rate, frame_duration_ms)
        output = QueuedAudioOutput(
            output_device=output_device,
            sample_rate=output_rate,
            channels=output_channels,
            dtype="int16",
            frames_per_block=output_frames,
            queue_capacity_blocks=int(realtime["playback_queue_capacity_blocks"]),
            discontinuity_fade_ms=float(
                dict(realtime["adaptive_playout"])["crossfade_ms"]
            ),
        )
        input_resampler = create_resampler(
            resampling,
            capture_rate,
            api_rate,
            output_block_frames=calculate_block_frames(
                api_rate, float(realtime["send_chunk_duration_ms"])
            ),
        )
        passthrough_resampler = create_resampler(
            resampling,
            capture_rate,
            output_rate,
            output_block_frames=output_frames,
        )
        self._translated_resampler = create_resampler(
            resampling,
            api_rate,
            output_rate,
            output_block_frames=output_frames,
        )
        self._translated_output = output
        self._translated_output_channels = output_channels
        self._translated_audio_block_bytes = (
            calculate_block_frames(
                api_rate,
                frame_duration_ms,
            )
            * 2
        )
        self._translated_audio_block_duration_ms = frame_duration_ms
        self._translated_audio_queue = asyncio.Queue(
            maxsize=int(realtime["received_queue_capacity_blocks"])
        )
        self._adaptive_playout = dict(realtime["adaptive_playout"])
        self._provider_delay_warning_ms = float(
            realtime["provider_delay_warning_ms"]
        )
        self._provider_delay_recovery_ms = float(
            realtime["provider_delay_recovery_ms"]
        )
        self._provider_delay_active = False
        self._output_gain_db = float(realtime["output_gain_db"])
        self._output_gain_clipped_samples = 0
        self._time_stretcher = StreamingWsola(api_rate)
        self._aec3_render_sample_rate = output_rate
        audio_capture_settings = dict(realtime["audio_capture"])
        capture_directory = Path(str(audio_capture_settings["directory"]))
        capture_enabled = bool(audio_capture_settings["enabled"])
        self._diagnostic_tracks = tuple(
            str(track) for track in audio_capture_settings["tracks"]
        )
        self._diagnostic_recording_configured = capture_enabled
        call_id = diagnostic_call_id()
        capture_options = {
            # Captures are armed but write nothing until the runtime recording
            # control is active. This makes the support button work without a
            # process restart while keeping normal operation disk-free.
            "enabled": True,
            "directory": capture_directory,
            "pipeline_name": self.pipeline_name,
            "max_seconds_per_file": float(
                audio_capture_settings["max_seconds_per_file"]
            ),
            "queue_capacity_blocks": int(
                audio_capture_settings.get("queue_capacity_blocks", 500)
            ),
            "write_timing_marks": bool(
                audio_capture_settings.get("write_timing_marks", True)
            ),
            "write_buffer_bytes": int(
                audio_capture_settings.get("write_buffer_kb", 64)
            )
            * 1024,
            "flush_interval_seconds": float(
                audio_capture_settings.get("flush_interval_seconds", 1.0)
            ),
            "session_id": call_id,
            "log_file_creation": False,
        }

        def create_diagnostic_track(
            stream_name: str,
            *,
            sample_rate: int,
            channels: int,
        ) -> QueuedDiagnosticWavCapture | None:
            if stream_name not in self._diagnostic_tracks:
                return None
            return QueuedDiagnosticWavCapture(
                **capture_options,
                stream_name=stream_name,
                sample_rate=sample_rate,
                channels=channels,
            )

        self._input_audio_capture = create_diagnostic_track(
            "captured",
            sample_rate=capture_rate,
            channels=capture_channels,
        )
        self._accepted_audio_capture = create_diagnostic_track(
            "accepted",
            sample_rate=capture_rate,
            channels=1,
        )
        self._sent_audio_capture = create_diagnostic_track(
            "sent",
            sample_rate=api_rate,
            channels=1,
        )
        self._received_audio_capture = create_diagnostic_track(
            "received",
            sample_rate=api_rate,
            channels=1,
        )
        self._output_audio_capture = create_diagnostic_track(
            "played",
            sample_rate=output_rate,
            channels=output_channels,
        )
        self._diagnostic_capture_session_active = False
        self._call_cable_active = False
        output.played_observer = lambda block: self._record_played_audio(
            block,
            output_channels,
        )
        self._diagnostic_manifest = DiagnosticCallManifest(
            enabled=False,
            directory=capture_directory,
            pipeline_name=self.pipeline_name,
            call_id=call_id,
            devices={
                "input": format_device(input_device),
                "output": format_device(output_device),
            },
            formats={
                name: audio_format
                for name, audio_format in {
                    "captured": {
                        "sample_rate": capture_rate,
                        "channels": capture_channels,
                        "format": str(audio["sample_format"]),
                        "exclusive": capture.exclusive,
                    },
                    "accepted": {"sample_rate": capture_rate, "channels": 1},
                    "sent": {"sample_rate": api_rate, "channels": 1},
                    "received": {"sample_rate": api_rate, "channels": 1},
                    "played": {
                        "sample_rate": output_rate,
                        "channels": output_channels,
                        "exclusive": output.exclusive,
                    },
                }.items()
                if name in self._diagnostic_tracks
            },
        )
        self._sync_diagnostic_recording()
        self._log_transcript_deltas = bool(realtime["log_transcript_deltas"])
        self._transcript_queue = asyncio.Queue()
        session = self.session_factory.create(
            pipeline_name=self.pipeline_name,
            on_audio=self._enqueue_translated_audio,
            on_input_transcript=lambda event: self._enqueue_transcript_delta(
                "input", event
            ),
            on_output_transcript=lambda event: self._enqueue_transcript_delta(
                "output", event
            ),
        )
        self._audio_send_queue = asyncio.Queue(
            maxsize=int(realtime["send_queue_capacity_frames"])
        )
        audio_sender_task = asyncio.create_task(
            self._audio_sender(session),
            name=f"{self.pipeline_name} realtime audio sender",
        )
        translated_audio_task = asyncio.create_task(
            self._translated_audio_worker(),
            name=f"{self.pipeline_name} realtime playback worker",
        )
        transcript_task = asyncio.create_task(
            self._transcript_worker(
                update_interval_seconds=(
                    float(realtime["transcript_ui_update_interval_ms"]) / 1000
                ),
                segment_idle_seconds=(
                    float(realtime["transcript_segment_idle_ms"]) / 1000
                ),
                alignment_wait_seconds=(
                    float(realtime["transcript_alignment_wait_ms"]) / 1000
                ),
            ),
            name=f"{self.pipeline_name} realtime transcript publisher",
        )
        transcript_task.add_done_callback(self._log_transcript_task_failure)
        reconnect_attempt = 0
        reconnect_at = 0.0
        configured_target: str | None = None
        connect_task: asyncio.Task[None] | None = None
        close_task: asyncio.Task[None] | None = None
        input_revision = self.device_manager.revision(input_device_key)
        output_revision = self.device_manager.revision(output_device_key)
        metrics_interval_seconds = float(realtime["metrics_interval_seconds"])
        metrics_at = time.monotonic() + metrics_interval_seconds
        deadline = None if self.duration is None else time.monotonic() + self.duration
        last_gate_audio_allowed: bool | None = None

        LOGGER.info(
            "event=pipeline_started engine=openai_realtime input_device=%s "
            "output_device=%s source_language=%s target_language=%s",
            format_device(input_device),
            format_device(output_device),
            self._current_language(self.source_language_name),
            self._current_language(self.target_language_name),
        )
        try:
            if self.startup_barrier is not None:
                await self.startup_barrier.wait()
            await asyncio.to_thread(capture.start)
            output.start()
            while deadline is None or time.monotonic() < deadline:
                now = time.monotonic()
                requested_translation = self._current_mode() is PipelineMode.TRANSLATE
                session_observation = self.device_manager.application_session_active(
                    session_gate_route
                )
                gate_decision = session_gate.evaluate(
                    requested=requested_translation,
                    observation=session_observation,
                    now=now,
                )
                self._sync_diagnostic_recording()
                self._set_call_cable_active(gate_decision.call_active)
                if close_task is not None and close_task.done():
                    try:
                        close_task.result()
                    except asyncio.CancelledError:
                        pass
                    except Exception as error:
                        LOGGER.warning(
                            "event=realtime_translation_source_close_failed "
                            "action=abort error=%r",
                            error,
                        )
                        await session.abort()
                    close_task = None
                    configured_target = None
                    session.error_event.clear()
                    reconnect_attempt = 0
                    reconnect_at = now
                    self._enqueue_transcript_boundary()
                    self._set_translation_audio_acceptance(False)
                    input_resampler.reset()
                    self._translated_resampler.reset()
                previous_gate_audio_allowed = last_gate_audio_allowed
                if gate_decision.audio_allowed != previous_gate_audio_allowed:
                    LOGGER.info(
                        "event=realtime_call_state_changed route=%s "
                        "active=%s translation_requested=%s "
                        "application_observation=%s",
                        session_gate_route,
                        gate_decision.audio_allowed,
                        requested_translation,
                        session_observation,
                    )
                    last_gate_audio_allowed = gate_decision.audio_allowed
                    if self._diagnostic_manifest is not None:
                        self._diagnostic_manifest.event(
                            "call_audio_gate_changed",
                            active=gate_decision.audio_allowed,
                            application_session=session_observation,
                            call_active=gate_decision.call_active,
                        )
                    if (
                        gate_decision.audio_allowed
                        and previous_gate_audio_allowed is not True
                    ):
                        self._prepare_call_audio_start(
                            input_resampler,
                            passthrough_resampler,
                            output,
                        )
                    if (
                        previous_gate_audio_allowed is True
                        and not gate_decision.audio_allowed
                    ):
                        passthrough_resampler.reset()
                        if connect_task is not None:
                            if not connect_task.done():
                                connect_task.cancel()
                            await asyncio.gather(
                                connect_task,
                                return_exceptions=True,
                            )
                            connect_task = None
                        if session.connected and close_task is None:
                            close_task = asyncio.create_task(
                                self._close_session_after_source_end(
                                    session,
                                    input_resampler,
                                ),
                                name=(
                                    f"{self.pipeline_name} realtime source "
                                    "stream close"
                                ),
                            )
                            LOGGER.info(
                                "event=realtime_translation_source_ended "
                                "action=graceful_close"
                            )
                        else:
                            self._set_translation_audio_acceptance(False)
                            self._discard_audio_send_queue(
                                "call_audio_gate_closed_without_session"
                            )
                            input_resampler.reset()
                            self._translated_resampler.reset()
                            self._enqueue_transcript_boundary()
                target_language = self._current_language(self.target_language_name)

                if (
                    close_task is None
                    and configured_target is not None
                    and target_language != configured_target
                ):
                    self._set_translation_audio_acceptance(False)
                    self._discard_audio_send_queue("target_language_changed")
                    await session.abort()
                    self._enqueue_transcript_boundary()
                    input_resampler.reset()
                    self._translated_resampler.reset()
                    configured_target = None
                    reconnect_attempt = 0
                    reconnect_at = now

                if not requested_translation:
                    if connect_task is not None and not connect_task.done():
                        connect_task.cancel()
                        await asyncio.gather(connect_task, return_exceptions=True)
                    connect_task = None
                    if session.connected and close_task is None:
                        close_task = asyncio.create_task(
                            self._close_session_after_source_end(
                                session,
                                input_resampler,
                            ),
                            name=(
                                f"{self.pipeline_name} realtime mode-change drain"
                            ),
                        )
                        LOGGER.info(
                            "event=realtime_translation_mode_changed "
                            "action=drain_before_passthrough"
                        )
                    if close_task is None:
                        self._set_translation_audio_acceptance(False)
                        self._discard_audio_send_queue(
                            "translation_not_requested"
                        )
                        input_resampler.reset()
                        self._translated_resampler.reset()
                        passthrough_resampler.reset()
                    await self._set_status(
                        "draining"
                        if close_task is not None
                        else (
                            "waiting_for_call"
                            if gate_decision.waiting_for_application
                            else "passthrough"
                        )
                    )
                else:
                    if close_task is None and session.error_event.is_set():
                        session_error = session.last_error
                        self._set_translation_audio_acceptance(False)
                        self._discard_audio_send_queue("connection_lost")
                        await session.abort()
                        self._enqueue_transcript_boundary()
                        session.error_event.clear()
                        reconnect_attempt += 1
                        reconnect_at = now + self._reconnect_delay(
                            reconnect_attempt,
                            session_error,
                            now=now,
                        )
                        configured_target = None
                        LOGGER.warning(
                            "event=realtime_translation_connection_lost "
                            "attempt=%s action=reconnect error=%r",
                            reconnect_attempt,
                            session_error,
                        )
                        await self._set_status("reconnecting")
                    if connect_task is not None and connect_task.done():
                        try:
                            connect_task.result()
                        except asyncio.CancelledError:
                            raise
                        except Exception as error:
                            self._discard_audio_send_queue("connection_failed")
                            session.error_event.clear()
                            reconnect_attempt += 1
                            reconnect_at = now + self._reconnect_delay(
                                reconnect_attempt,
                                error,
                                now=now,
                            )
                            LOGGER.warning(
                                "event=realtime_translation_connection_failed "
                                "attempt=%s action=retry error=%s",
                                reconnect_attempt,
                                error,
                            )
                            await self._set_status("reconnecting")
                        else:
                            reconnect_attempt = 0
                            configured_target = target_language
                            self._set_translation_audio_acceptance(
                                gate_decision.audio_allowed
                            )
                            self._discard_audio_send_queue("connection_ready")
                            output.discard_pending_blocks()
                            await self._reconcile_gate_status(
                                gate_decision,
                                session_usable=True,
                                provider_delayed=self._refresh_provider_delay(
                                    session
                                ),
                            )
                            LOGGER.info(
                                "event=realtime_translation_session_prepared "
                                "target_language=%s audio_allowed=%s",
                                target_language,
                                gate_decision.audio_allowed,
                            )
                        connect_task = None
                    if (
                        gate_decision.session_required
                        and close_task is None
                        and not session.connected
                        and connect_task is None
                    ):
                        shared_rate_limit_delay = (
                            self._rate_limit_coordinator.remaining_delay(now=now)
                        )
                        if shared_rate_limit_delay > 0:
                            reconnect_at = max(
                                reconnect_at,
                                now + shared_rate_limit_delay,
                            )
                    if (
                        gate_decision.session_required
                        and close_task is None
                        and not session.connected
                        and connect_task is None
                        and now >= reconnect_at
                    ):
                        max_attempts = int(realtime["reconnect_max_attempts"])
                        if reconnect_attempt >= max_attempts:
                            await self._set_status("translation_unavailable")
                            reconnect_attempt = 0
                            reconnect_at = now + float(
                                realtime["reconnect_cooldown_seconds"]
                            )
                            LOGGER.warning(
                                "event=realtime_translation_retry_cycle_exhausted "
                                "action=retry_after_cooldown cooldown_seconds=%.1f",
                                float(realtime["reconnect_cooldown_seconds"]),
                            )
                        else:
                            await self._set_status(
                                "initializing" if reconnect_attempt == 0 else "reconnecting"
                            )
                            connect_task = asyncio.create_task(
                                session.connect(
                                    target_language,
                                    reconnecting=reconnect_attempt > 0,
                                )
                            )
                    await self._reconcile_gate_status(
                        gate_decision,
                        session_usable=(
                            session.connected
                            and connect_task is None
                            and close_task is None
                        ),
                        draining=close_task is not None,
                        provider_delayed=self._refresh_provider_delay(session),
                    )

                block = await capture.wait_for_block(timeout_seconds=0.05)
                if block is not None:
                    self._record_captured_audio(block)
                    mono = convert_int16_channels(block, capture.channels, 1)
                    accepted = (
                        self.aec3.process_capture(
                            mono,
                            sample_rate=capture.sample_rate,
                        )
                        if (
                            self.pipeline_name == "agent_to_remote"
                            and self._call_cable_active
                        )
                        else mono
                    )
                    self._record_accepted_audio(accepted)
                    if (
                        gate_decision.audio_allowed
                        and session.connected
                        and close_task is None
                    ):
                        # Translation sessions have a continuous media timeline.
                        # Forward every captured block, including digital silence;
                        # VAD-gating would collapse real pauses and break alignment.
                        for api_block in input_resampler.process(accepted):
                            self._enqueue_audio_for_translation(api_block)
                    elif close_task is None:
                        for original in passthrough_resampler.process(accepted):
                            played = convert_int16_channels(
                                original, 1, output_channels
                            )
                            self._push_output_block(
                                output,
                                played,
                                source="passthrough",
                            )

                current_input_revision = self.device_manager.revision(input_device_key)
                if current_input_revision != input_revision:
                    input_revision = current_input_revision
                    target_input = self.device_manager.active_index(input_device_key)
                    try:
                        changed = await asyncio.to_thread(
                            capture.switch_device,
                            target_input,
                        )
                    except Exception as error:
                        if input_device_key != "input":
                            raise
                        await self.device_manager.report_physical_device_failed(
                            "input", target_input, error
                        )
                    else:
                        if input_device_key == "input":
                            await self.device_manager.report_physical_device_healthy(
                                "input",
                                target_input,
                                sample_rate=capture.sample_rate,
                                channels=capture.channels,
                            )
                        if changed:
                            if self._diagnostic_manifest is not None:
                                self._diagnostic_manifest.event(
                                    "audio_device_changed",
                                    direction="input",
                                    device=format_device(target_input),
                                    sample_rate=capture.sample_rate,
                                    channels=capture.channels,
                                )
                            input_resampler = create_resampler(
                                resampling,
                                capture.sample_rate,
                                api_rate,
                                output_block_frames=calculate_block_frames(
                                    api_rate,
                                    float(realtime["send_chunk_duration_ms"]),
                                ),
                            )
                            passthrough_resampler = create_resampler(
                                resampling,
                                capture.sample_rate,
                                output_rate,
                                output_block_frames=output_frames,
                            )
                current_output_revision = self.device_manager.revision(output_device_key)
                if current_output_revision != output_revision:
                    output_revision = current_output_revision
                    await asyncio.to_thread(
                        output.switch_device,
                        self.device_manager.active_index(output_device_key),
                    )
                    if self._diagnostic_manifest is not None:
                        self._diagnostic_manifest.event(
                            "audio_device_changed",
                            direction="output",
                            device=format_device(output.output_device),
                            sample_rate=output.sample_rate,
                            channels=output.channels,
                        )
                    self._translated_resampler.reset()
                    passthrough_resampler.reset()

                if now >= metrics_at:
                    await self._write_metrics(
                        capture,
                        output,
                        session,
                        session_gate=session_gate,
                        gate_decision=gate_decision,
                    )
                    metrics_at = now + metrics_interval_seconds
                if translated_audio_task.done():
                    translated_audio_task.result()
                    raise RuntimeError(
                        "Realtime translated-audio worker stopped unexpectedly."
                    )
        except asyncio.CancelledError:
            raise
        except BaseException:
            LOGGER.exception("event=realtime_translation_pipeline_failed")
            raise
        finally:
            self._set_call_cable_active(False)
            capture.stop()
            if connect_task is not None:
                connect_task.cancel()
                await asyncio.gather(connect_task, return_exceptions=True)
            if close_task is not None:
                close_results = await asyncio.gather(
                    close_task,
                    return_exceptions=True,
                )
                if isinstance(close_results[0], BaseException):
                    LOGGER.warning(
                        "event=realtime_translation_source_close_failed "
                        "action=abort error=%r",
                        close_results[0],
                    )
                    await session.abort()
            elif session.connected and not session.error_event.is_set():
                await self._close_session_after_source_end(
                    session,
                    input_resampler,
                )
            else:
                await session.abort()
            self._set_translation_audio_acceptance(False)
            self._discard_audio_send_queue("pipeline_stopping")
            audio_send_queue = self._audio_send_queue
            if audio_send_queue is not None:
                audio_send_queue.put_nowait(None)
            await asyncio.gather(audio_sender_task, return_exceptions=True)
            self._audio_send_queue = None
            self._discard_translated_audio_queue("pipeline_stopping")
            translated_audio_queue = self._translated_audio_queue
            if translated_audio_queue is not None:
                translated_audio_queue.put_nowait(None)
            await asyncio.gather(translated_audio_task, return_exceptions=True)
            self._translated_audio_queue = None
            transcript_queue = self._transcript_queue
            if transcript_queue is not None:
                transcript_queue.put_nowait(None)
            await asyncio.gather(transcript_task, return_exceptions=True)
            self._transcript_queue = None
            output.stop()
            diagnostic_captures = tuple(
                item
                for item in (
                    self._input_audio_capture,
                    self._accepted_audio_capture,
                    self._sent_audio_capture,
                    self._received_audio_capture,
                    self._output_audio_capture,
                )
                if item is not None
            )
            await asyncio.gather(
                *(
                    asyncio.to_thread(item.shutdown)
                    for item in diagnostic_captures
                )
            )
            diagnostic_dropped_blocks = sum(
                item.dropped_blocks for item in diagnostic_captures
            )
            tracks = {
                name: [str(path) for path in capture_item.files]
                for name, capture_item in (
                    ("captured", self._input_audio_capture),
                    ("accepted", self._accepted_audio_capture),
                    ("sent", self._sent_audio_capture),
                    ("received", self._received_audio_capture),
                    ("played", self._output_audio_capture),
                )
                if capture_item is not None and capture_item.files
            }
            aec3_statistics = self.aec3.statistics_snapshot()
            if self._diagnostic_manifest is not None and tracks:
                self._diagnostic_manifest.enabled = True
                manifest_path = self._diagnostic_manifest.close(
                    tracks=tracks,
                    statistics={
                        "aec3": aec3_statistics,
                        "speech_latency": self._speech_latency.snapshot(),
                        "capture_dropped_blocks": diagnostic_dropped_blocks,
                        "send_queue_dropped_blocks": self._audio_send_dropped_blocks,
                        "received_queue_dropped_blocks": (
                            self._translated_audio_dropped_blocks
                        ),
                        "received_queue_discontinuities": (
                            self._translated_audio_discontinuities
                        ),
                        "received_queue_silence_boundary_dropped_blocks": (
                            self._translated_audio_silence_boundary_dropped_blocks
                        ),
                        "received_queue_maximum_buffered_blocks": (
                            self._translated_audio_maximum_buffered_blocks
                        ),
                        "adaptive_playout": {
                            "backlog_p50_ms": self._percentile(
                                self._adaptive_backlog_samples_ms, 0.50
                            ),
                            "backlog_p95_ms": self._percentile(
                                self._adaptive_backlog_samples_ms, 0.95
                            ),
                            "backlog_p99_ms": self._percentile(
                                self._adaptive_backlog_samples_ms, 0.99
                            ),
                            "maximum_speed": self._adaptive_maximum_speed,
                            "time_above_target_seconds": (
                                self._adaptive_time_above_target_seconds
                            ),
                        },
                    },
                )
                LOGGER.info(
                    "event=diagnostic_call_manifest_closed file=%s",
                    manifest_path,
                )
            self._input_audio_capture = None
            self._accepted_audio_capture = None
            self._sent_audio_capture = None
            self._received_audio_capture = None
            self._output_audio_capture = None
            self._diagnostic_manifest = None
            LOGGER.info("event=pipeline_stopped engine=openai_realtime")

    def _record_captured_audio(self, pcm16: bytes) -> None:
        """Record raw endpoint PCM, including CABLE A during passthrough."""
        capture = self._input_audio_capture
        if (
            self._diagnostic_recording_active
            and self._call_cable_active
            and capture is not None
        ):
            capture.write(pcm16)

    def _record_accepted_audio(self, pcm16_mono: bytes) -> None:
        """Record canonical mono PCM after echo rejection."""
        capture = self._accepted_audio_capture
        if (
            self._diagnostic_recording_active
            and self._call_cable_active
            and capture is not None
        ):
            capture.write(pcm16_mono)
        if self._call_cable_active and self._accept_translated_audio:
            self._observe_speech_latency("accepted", pcm16_mono)

    def _record_played_audio(self, pcm16: bytes, channels: int) -> None:
        """Record actual playback and publish it as the far-end AEC reference."""
        capture = self._output_audio_capture
        if (
            self._diagnostic_recording_active
            and self._call_cable_active
            and capture is not None
        ):
            capture.write(pcm16)
        if self._call_cable_active and self._accept_translated_audio:
            self._observe_speech_latency("played", pcm16)
        if self._call_cable_active and self.pipeline_name == "remote_to_agent":
            self.aec3.observe_render(
                pcm16,
                sample_rate=getattr(
                    self,
                    "_aec3_render_sample_rate",
                    48_000,
                ),
                channels=channels,
            )

    def _record_input_audio(
        self, pcm16: bytes, *, translation_active: bool
    ) -> None:
        """Backward-compatible alias for the now continuous captured track."""
        self._record_captured_audio(pcm16)

    def _record_sent_audio(self, pcm16: bytes) -> None:
        """Record audio accepted by the API only while a call cable is active."""
        capture = self._sent_audio_capture
        if (
            self._diagnostic_recording_active
            and self._call_cable_active
            and capture is not None
        ):
            capture.write(pcm16)
        if self._call_cable_active:
            self._observe_speech_latency("sent", pcm16)

    def _record_received_audio(self, pcm16: bytes) -> None:
        """Record mono PCM received from the provider before local resampling."""
        capture = self._received_audio_capture
        if (
            self._diagnostic_recording_active
            and self._call_cable_active
            and capture is not None
        ):
            capture.write(pcm16)
        if self._call_cable_active and self._accept_translated_audio:
            self._observe_speech_latency("received", pcm16)

    def _set_call_cable_active(self, active: bool) -> None:
        """Update AEC3 and diagnostic recording for the routed call cable."""
        if active == self._call_cable_active:
            return
        self._call_cable_active = active
        speech_latency = getattr(self, "_speech_latency", None)
        if speech_latency is not None:
            speech_latency.reset_stream()
        if self.pipeline_name == "agent_to_remote":
            self.aec3.reset()
        LOGGER.debug(
            "event=diagnostic_call_capture_changed active=%s",
            active,
        )
        if self._diagnostic_manifest is not None:
            self._diagnostic_manifest.event(
                "diagnostic_call_capture_changed",
                active=active,
            )
        self._sync_diagnostic_capture_session()

    def _diagnostic_captures(
        self,
    ) -> tuple[QueuedDiagnosticWavCapture, ...]:
        return tuple(
            capture
            for capture in (
                self._input_audio_capture,
                self._accepted_audio_capture,
                self._sent_audio_capture,
                self._received_audio_capture,
                self._output_audio_capture,
            )
            if capture is not None
        )

    def _sync_diagnostic_capture_session(self) -> None:
        """Give every selected WAV one common sample-zero recording origin."""
        should_be_active = (
            self._diagnostic_recording_active and self._call_cable_active
        )
        if should_be_active == getattr(
            self, "_diagnostic_capture_session_active", False
        ):
            return
        self._diagnostic_capture_session_active = should_be_active
        captures = self._diagnostic_captures()
        if should_be_active:
            origin_monotonic = time.monotonic()
            for capture in captures:
                capture.start_session(origin_monotonic)
            if self._diagnostic_manifest is not None:
                self._diagnostic_manifest.event(
                    "diagnostic_timeline_started",
                    tracks=list(getattr(self, "_diagnostic_tracks", ())),
                )
            return
        for capture in captures:
            capture.close_session()

    def _sync_diagnostic_recording(self) -> None:
        """Apply the live support recording control without restarting audio."""
        manual_active = (
            self.control_state is not None
            and self.control_state.is_audio_recording_active_for(self.pipeline_name)
        )
        active = self._diagnostic_recording_configured or manual_active
        if active == self._diagnostic_recording_active:
            return
        self._diagnostic_recording_active = active
        LOGGER.info(
            "event=realtime_diagnostic_audio_capture_changed active=%s "
            "streams=%s",
            active,
            ",".join(getattr(self, "_diagnostic_tracks", ())) or "none",
        )
        if self._diagnostic_manifest is not None:
            if active:
                self._diagnostic_manifest.enabled = True
            self._diagnostic_manifest.event(
                "diagnostic_audio_capture_changed",
                active=active,
            )
            if not active:
                self._diagnostic_manifest.enabled = False
        self._sync_diagnostic_capture_session()

    def _set_translation_audio_acceptance(self, active: bool) -> None:
        """Enable translated output and reset incomplete latency pairings on edges."""
        previous = self._accept_translated_audio
        self._accept_translated_audio = active
        if active == previous:
            return
        if not active:
            self._provider_delay_active = False
            self._discard_translated_audio_queue("translation_audio_disabled")
        speech_latency = getattr(self, "_speech_latency", None)
        if speech_latency is not None:
            speech_latency.reset_stream()

    def _prepare_call_audio_start(
        self,
        input_resampler: PcmInt16Resampler,
        passthrough_resampler: PcmInt16Resampler,
        output: QueuedAudioOutput,
    ) -> None:
        """Remove pre-call media before a prewarmed session starts translation."""
        passthrough_resampler.reset()
        output.discard_pending_blocks()
        self._discard_audio_send_queue("call_audio_gate_opened")
        input_resampler.reset()
        self._translated_resampler.reset()

    def _observe_speech_latency(self, stage: str, pcm16: bytes) -> None:
        tracker = getattr(self, "_speech_latency", None)
        if tracker is None:
            return
        measurement = tracker.observe(stage, pcm16)
        if measurement is None:
            return
        values = measurement.as_dict()
        if self._diagnostic_manifest is not None:
            self._diagnostic_manifest.event(
                "voice_latency_measured",
                **values,
            )

    def _enqueue_audio_for_translation(self, pcm16: bytes) -> None:
        """Queue current audio without letting network backpressure block routing."""
        queue = self._audio_send_queue
        if queue is None or not pcm16:
            return
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            else:
                queue.task_done()
                self._audio_send_dropped_blocks += 1
        queue.put_nowait(pcm16)

    def _discard_audio_send_queue(self, reason: str) -> None:
        queue = self._audio_send_queue
        if queue is None:
            return
        discarded = 0
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                queue.task_done()
                discarded += 1
        if discarded:
            self._audio_send_dropped_blocks += discarded
            LOGGER.debug(
                "event=realtime_translation_send_queue_discarded "
                "reason=%s blocks=%s",
                reason,
                discarded,
            )

    async def _audio_sender(
        self,
        session: OpenAIRealtimeTranslationSession,
    ) -> None:
        """Serialize API sends independently from device and passthrough handling."""
        queue = self._audio_send_queue
        if queue is None:
            return
        while True:
            block = await queue.get()
            try:
                if block is None:
                    return
                await session.send_audio(block)
                self._record_sent_audio(block)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOGGER.warning(
                    "event=realtime_translation_send_failed "
                    "action=reconnect error=%s",
                    error,
                )
                session.error_event.set()
            finally:
                queue.task_done()

    async def _close_session_after_source_end(
        self,
        session: OpenAIRealtimeTranslationSession,
        input_resampler: PcmInt16Resampler,
    ) -> None:
        """Flush local input, drain provider output, and close one call session."""
        for final_block in input_resampler.process(b"", final=True):
            self._enqueue_audio_for_translation(final_block)

        send_queue = self._audio_send_queue
        if send_queue is not None:
            try:
                await asyncio.wait_for(
                    send_queue.join(),
                    timeout=session.close_timeout_seconds,
                )
            except TimeoutError:
                LOGGER.warning(
                    "event=realtime_translation_send_drain_timeout action=abort"
                )
                self._discard_audio_send_queue("source_end_send_drain_timeout")
                await session.abort()
                return

        await session.close()

        received_queue = self._translated_audio_queue
        if received_queue is not None:
            try:
                await asyncio.wait_for(
                    received_queue.put(
                        TranslatedAudioBlock(
                            pcm16=b"",
                            generation=self._translated_audio_generation,
                            final=True,
                        )
                    ),
                    timeout=session.close_timeout_seconds,
                )
                await asyncio.wait_for(
                    received_queue.join(),
                    timeout=session.close_timeout_seconds,
                )
                output = self._translated_output
                if output is not None:
                    deadline = time.monotonic() + session.close_timeout_seconds
                    while output.buffered_blocks > 0:
                        if time.monotonic() >= deadline:
                            raise TimeoutError
                        await asyncio.sleep(
                            output.frames_per_block / output.sample_rate / 2
                        )
            except TimeoutError:
                LOGGER.warning(
                    "event=realtime_translation_receive_drain_timeout "
                    "action=discard_pending"
                )
                self._discard_translated_audio_queue(
                    "source_end_receive_drain_timeout"
                )

    def _enqueue_translated_audio(self, pcm16: bytes) -> None:
        """Accept provider audio without ever waiting for physical playback."""
        queue = self._translated_audio_queue
        block_bytes = self._translated_audio_block_bytes
        if (
            not self._accept_translated_audio
            or queue is None
            or block_bytes <= 0
            or not pcm16
        ):
            return
        self._record_received_audio(pcm16)
        self._translated_audio_pending_bytes.extend(pcm16)
        while len(self._translated_audio_pending_bytes) >= block_bytes:
            block = bytes(self._translated_audio_pending_bytes[:block_bytes])
            del self._translated_audio_pending_bytes[:block_bytes]
            if queue.full():
                self._recover_received_queue()
            queue.put_nowait(
                TranslatedAudioBlock(
                    pcm16=block,
                    generation=self._translated_audio_generation,
                )
            )
            self._translated_audio_maximum_buffered_blocks = max(
                self._translated_audio_maximum_buffered_blocks,
                queue.qsize(),
            )

    def _recover_received_queue(self) -> None:
        """Remove only stale backlog, preferring the quietest nearby boundary."""
        queue = self._translated_audio_queue
        if queue is None or queue.empty():
            return
        buffered: list[TranslatedAudioBlock] = []
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            queue.task_done()
            if item is not None:
                buffered.append(item)
        if not buffered:
            return

        block_ms = self._received_block_ms()
        recovery_ms = float(self._adaptive_playout["recovery_backlog_ms"])
        target_blocks = max(1, round(recovery_ms / block_ms))
        required_drop = max(1, len(buffered) - target_blocks)
        search_blocks = max(
            1,
            round(
                float(self._adaptive_playout["silence_search_ms"])
                / block_ms
            ),
        )
        first = max(1, required_drop - search_blocks)
        last = min(len(buffered) - 1, required_drop + search_blocks)
        cut = min(required_drop, len(buffered) - 1)
        quietest_dbfs = 0.0
        if first <= last:
            candidates = [
                (self._pcm_rms_dbfs(buffered[index].pcm16), index)
                for index in range(first, last + 1)
            ]
            quietest_dbfs, cut = min(candidates)

        retained = buffered[cut:]
        for item in retained:
            queue.put_nowait(item)
        self._translated_audio_dropped_blocks += cut
        self._translated_audio_discontinuities += 1
        silence_threshold = float(
            self._adaptive_playout["silence_threshold_dbfs"]
        )
        if quietest_dbfs <= silence_threshold:
            self._translated_audio_silence_boundary_dropped_blocks += cut
        output = getattr(self, "_translated_output", None)
        if output is not None:
            output.mark_discontinuity()
        message = (
            "event=realtime_translation_backlog_recovered "
            "reason=received_queue_high_watermark dropped_blocks=%s "
            "retained_blocks=%s boundary_rms_dbfs=%.1f silence_boundary=%s"
        )
        arguments = (
            cut,
            len(retained),
            quietest_dbfs,
            quietest_dbfs <= silence_threshold,
        )
        if self._call_cable_active:
            LOGGER.warning(message, *arguments)
        else:
            # session.close may flush provider output faster than physical
            # playback after the call route has already disappeared. Metrics
            # retain the discarded duration; repeating warnings here would
            # describe post-call cleanup as a live audio incident.
            LOGGER.debug(message, *arguments)

    def _discard_translated_audio_queue(
        self,
        reason: str,
        *,
        preserve_pending: bool = False,
    ) -> None:
        """Invalidate queued and in-flight translated audio at a stream boundary."""
        queue = getattr(self, "_translated_audio_queue", None)
        discarded = 0
        if queue is not None:
            while True:
                try:
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if item is not None:
                    discarded += 1
                queue.task_done()
        pending = getattr(self, "_translated_audio_pending_bytes", None)
        if pending is not None and not preserve_pending:
            pending.clear()
        self._translated_audio_generation = (
            getattr(self, "_translated_audio_generation", 0) + 1
        )
        if discarded:
            self._translated_audio_dropped_blocks += discarded
            self._translated_audio_discontinuities += 1
            LOGGER.debug(
                "event=realtime_translation_received_queue_discarded "
                "reason=%s blocks=%s generation=%s",
                reason,
                discarded,
                self._translated_audio_generation,
            )

    async def _translated_audio_worker(self) -> None:
        """Adapt playout speed and feed playback independently from socket reads."""
        queue = self._translated_audio_queue
        output = self._translated_output
        resampler = self._translated_resampler
        time_stretcher = self._time_stretcher
        if (
            queue is None
            or output is None
            or resampler is None
            or time_stretcher is None
        ):
            return
        worker_generation = self._translated_audio_generation
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                if (
                    not self._accept_translated_audio
                    or item.generation != self._translated_audio_generation
                ):
                    continue
                if item.generation != worker_generation:
                    resampler.reset()
                    time_stretcher.reset()
                    worker_generation = item.generation
                backlog_ms = (
                    (queue.qsize() + 1) * self._received_block_ms()
                    + output.buffered_blocks
                    * output.frames_per_block
                    / output.sample_rate
                    * 1000
                )
                speed = self._observe_adaptive_backlog(backlog_ms)
                stretched_blocks = time_stretcher.process(
                    item.pcm16,
                    speed=speed,
                    final=item.final,
                )
                self._adaptive_compressed_input_samples += len(item.pcm16) // 2
                self._adaptive_compressed_output_samples += sum(
                    len(block) // 2 for block in stretched_blocks
                )
                for stretched in stretched_blocks:
                    await self._play_translated_pcm(
                        stretched,
                        item,
                        output,
                        resampler,
                    )
                if item.final:
                    for final_block in resampler.process(b"", final=True):
                        final_block = self._apply_translated_output_gain(
                            final_block
                        )
                        converted = convert_int16_channels(
                            final_block,
                            1,
                            self._translated_output_channels,
                        )
                        await self._wait_for_playback_capacity(output, item)
                        if (
                            self._accept_translated_audio
                            and item.generation
                            == self._translated_audio_generation
                        ):
                            self._push_output_block(
                                output,
                                converted,
                                source="translated",
                            )
            finally:
                queue.task_done()

    async def _play_translated_pcm(
        self,
        pcm16: bytes,
        item: TranslatedAudioBlock,
        output: QueuedAudioOutput,
        resampler: PcmInt16Resampler,
    ) -> None:
        for block in resampler.process(pcm16):
            block = self._apply_translated_output_gain(block)
            converted = convert_int16_channels(
                block, 1, self._translated_output_channels
            )
            await self._wait_for_playback_capacity(output, item)
            if (
                not self._accept_translated_audio
                or item.generation != self._translated_audio_generation
            ):
                return
            self._push_output_block(output, converted, source="translated")

    def _apply_translated_output_gain(self, pcm16: bytes) -> bytes:
        """Raise translated playout level with saturating PCM16 arithmetic."""
        amplified, clipped_samples = apply_int16_gain(
            pcm16,
            getattr(self, "_output_gain_db", 0.0),
        )
        self._output_gain_clipped_samples = (
            getattr(self, "_output_gain_clipped_samples", 0)
            + clipped_samples
        )
        return amplified

    async def _wait_for_playback_capacity(
        self,
        output: QueuedAudioOutput,
        item: TranslatedAudioBlock,
    ) -> None:
        while (
            self._accept_translated_audio
            and item.generation == self._translated_audio_generation
            and output.buffered_blocks >= output.queue_capacity_blocks
        ):
            await asyncio.sleep(
                output.frames_per_block / output.sample_rate / 4
            )

    def _observe_adaptive_backlog(self, backlog_ms: float) -> float:
        """Choose a pitch-preserving catch-up speed from the current backlog."""
        now = time.monotonic()
        target = float(self._adaptive_playout["target_backlog_ms"])
        accelerated = float(
            self._adaptive_playout["accelerated_backlog_ms"]
        )
        emergency = float(self._adaptive_playout["emergency_backlog_ms"])
        moderate = float(self._adaptive_playout["moderate_speed"])
        maximum = float(self._adaptive_playout["maximum_speed"])
        if self._adaptive_last_observed_at is not None:
            elapsed = max(0.0, now - self._adaptive_last_observed_at)
            if self._adaptive_last_backlog_ms > target:
                self._adaptive_time_above_target_seconds += elapsed
        self._adaptive_last_observed_at = now
        self._adaptive_last_backlog_ms = backlog_ms
        self._adaptive_backlog_samples_ms.append(backlog_ms)

        if not bool(self._adaptive_playout["enabled"]) or backlog_ms <= target:
            speed = 1.0
        elif backlog_ms < accelerated:
            progress = (backlog_ms - target) / (accelerated - target)
            speed = 1.05 + progress * (moderate - 1.05)
        elif backlog_ms < emergency:
            progress = (backlog_ms - accelerated) / (
                emergency - accelerated
            )
            speed = moderate + progress * (maximum - moderate)
        else:
            speed = maximum
        self._adaptive_speed = speed
        self._adaptive_maximum_speed = max(
            self._adaptive_maximum_speed,
            speed,
        )
        return speed

    def _received_block_ms(self) -> float:
        return max(
            1.0,
            float(
                getattr(
                    self,
                    "_translated_audio_block_duration_ms",
                    20.0,
                )
            ),
        )

    @staticmethod
    def _pcm_rms_dbfs(pcm16: bytes) -> float:
        if not pcm16:
            return -120.0
        samples = np.frombuffer(pcm16, dtype="<i2").astype(np.float32)
        rms = math.sqrt(float(np.mean(samples * samples)))
        if rms <= 1e-6:
            return -120.0
        return 20.0 * math.log10(rms / 32768.0)

    def _push_output_block(
        self,
        output: QueuedAudioOutput,
        block: bytes,
        *,
        source: str,
    ) -> None:
        dropped_before = output.dropped_blocks
        output.push_block(block)
        if output.dropped_blocks == dropped_before:
            return
        if source == "translated":
            self._translated_playback_dropped_blocks += 1
        else:
            self._passthrough_playback_dropped_blocks += 1

    @staticmethod
    def _percentile(samples: deque[float], percentile: float) -> float:
        if not samples:
            return 0.0
        ordered = sorted(samples)
        index = (len(ordered) - 1) * percentile
        lower = math.floor(index)
        upper = math.ceil(index)
        if lower == upper:
            return ordered[lower]
        fraction = index - lower
        return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction

    def _enqueue_transcript_delta(
        self,
        direction: str,
        delta: RealtimeTranscriptDelta | str,
    ) -> None:
        """Queue transcript text without delaying WebSocket audio reception."""
        if isinstance(delta, RealtimeTranscriptDelta):
            text = delta.text
            elapsed_ms = delta.elapsed_ms
            provider_lag_ms = delta.provider_lag_ms
        else:
            text = delta
            elapsed_ms = None
            provider_lag_ms = None
        if self._log_transcript_deltas:
            self._log_transcript(direction, text)
        queue = self._transcript_queue
        if queue is None or not text:
            return
        queue.put_nowait(
            (
                direction,
                text,
                self._current_language(self.source_language_name),
                self._current_language(self.target_language_name),
                elapsed_ms,
                provider_lag_ms,
            )
        )

    def _enqueue_transcript_boundary(self) -> None:
        queue = self._transcript_queue
        if queue is not None:
            queue.put_nowait(("boundary", "", "", "", None, None))

    async def _transcript_worker(
        self,
        *,
        update_interval_seconds: float,
        segment_idle_seconds: float,
        alignment_wait_seconds: float | None = None,
    ) -> None:
        """Coalesce transcript deltas and publish bounded UI state updates."""
        queue = self._transcript_queue
        if queue is None:
            return
        source_parts: list[str] = []
        translated_parts: list[str] = []
        source_language = ""
        target_language = ""
        transcript_id: int | None = None
        last_delta_at: float | None = None
        last_publish_at = 0.0
        source_elapsed_ms: int | None = None
        output_elapsed_ms: int | None = None
        dirty = False
        alignment_wait = (
            max(segment_idle_seconds, alignment_wait_seconds)
            if alignment_wait_seconds is not None
            else segment_idle_seconds * 30
        )

        def reset() -> None:
            nonlocal source_parts, translated_parts
            nonlocal source_language, target_language, transcript_id
            nonlocal last_delta_at, last_publish_at, dirty
            nonlocal source_elapsed_ms, output_elapsed_ms
            source_parts = []
            translated_parts = []
            source_language = ""
            target_language = ""
            transcript_id = None
            last_delta_at = None
            last_publish_at = 0.0
            source_elapsed_ms = None
            output_elapsed_ms = None
            dirty = False

        def publish(*, final: bool) -> None:
            nonlocal dirty, last_publish_at
            if (
                transcript_id is None
                or self.control_state is None
                or (not source_parts and not translated_parts)
            ):
                last_publish_at = time.monotonic()
                return
            self.control_state.publish_realtime_transcript(
                self.pipeline_name,
                transcript_id,
                "".join(source_parts),
                "".join(translated_parts),
                source_language,
                target_language,
                final=final,
            )
            dirty = False
            last_publish_at = time.monotonic()

        while True:
            now = time.monotonic()
            timeouts = [update_interval_seconds]
            if dirty:
                timeouts.append(
                    max(0.0, update_interval_seconds - (now - last_publish_at))
                )
            if last_delta_at is not None:
                finalization_idle = (
                    segment_idle_seconds
                    if source_parts and translated_parts
                    else alignment_wait
                )
                timeouts.append(
                    max(0.0, finalization_idle - (now - last_delta_at))
                )
            timeout = max(0.001, min(timeouts))
            try:
                item = await asyncio.wait_for(queue.get(), timeout=timeout)
            except TimeoutError:
                item = ("tick", "", "", "", None, None)

            if item is None:
                publish(final=True)
                return

            (
                direction,
                delta,
                item_source_language,
                item_target_language,
                elapsed_ms,
                _provider_lag_ms,
            ) = item
            now = time.monotonic()
            if direction == "boundary":
                publish(final=True)
                reset()
                continue
            if direction != "tick":
                languages_changed = transcript_id is not None and (
                    source_language != item_source_language
                    or target_language != item_target_language
                )
                if languages_changed:
                    publish(final=True)
                    reset()
                if transcript_id is None:
                    self._transcript_sequence += 1
                    transcript_id = self._transcript_sequence
                    source_language = item_source_language
                    target_language = item_target_language
                opposite_elapsed_ms = (
                    output_elapsed_ms if direction == "input" else source_elapsed_ms
                )
                if (
                    transcript_id is not None
                    and elapsed_ms is not None
                    and opposite_elapsed_ms is not None
                    and abs(elapsed_ms - opposite_elapsed_ms)
                    > alignment_wait * 1000
                ):
                    publish(final=True)
                    reset()
                    self._transcript_sequence += 1
                    transcript_id = self._transcript_sequence
                    source_language = item_source_language
                    target_language = item_target_language
                if direction == "input":
                    source_parts.append(delta)
                    source_elapsed_ms = elapsed_ms
                elif direction == "output":
                    translated_parts.append(delta)
                    output_elapsed_ms = elapsed_ms
                else:
                    continue
                dirty = True
                last_delta_at = now

            if (
                last_delta_at is not None
                and now - last_delta_at
                >= (
                    segment_idle_seconds
                    if source_parts and translated_parts
                    else alignment_wait
                )
            ):
                publish(final=True)
                reset()
            elif dirty and now - last_publish_at >= update_interval_seconds:
                publish(final=False)

    async def _write_metrics(
        self,
        capture: QueuedAudioInput,
        output: QueuedAudioOutput,
        session: OpenAIRealtimeTranslationSession,
        *,
        session_gate: RealtimeSessionGate,
        gate_decision: RealtimeSessionGateDecision,
    ) -> None:
        speech_latency = self._speech_latency.snapshot()
        received_queue = self._translated_audio_queue
        received_queue_blocks = (
            received_queue.qsize() if received_queue is not None else 0
        )
        received_block_ms = (
            self._translated_audio_block_bytes / 2 / session.sample_rate * 1000
            if self._translated_audio_block_bytes > 0
            else 0.0
        )
        values: dict[str, object] = {
            "pipeline": self.pipeline_name,
            "mode": self._current_mode().value,
            "engine": "openai_realtime",
            "capture_blocks": capture.statistics.captured_blocks,
            "capture_dropped_blocks": capture.statistics.dropped_blocks,
            "capture_overflows": capture.statistics.input_overflows,
            "capture_invalid_blocks": capture.statistics.invalid_block_sizes,
            "capture_maximum_buffered_blocks": capture.statistics.maximum_buffered_blocks,
            "capture_maximum_callback_gap_ms": (
                capture.statistics.maximum_callback_gap_ms
            ),
            "realtime_send_queue_buffered_blocks": (
                self._audio_send_queue.qsize()
                if self._audio_send_queue is not None
                else 0
            ),
            "realtime_send_queue_dropped_blocks": self._audio_send_dropped_blocks,
            "realtime_received_queue_buffered_blocks": received_queue_blocks,
            "realtime_received_queue_buffered_ms": (
                received_queue_blocks * received_block_ms
            ),
            "realtime_received_queue_maximum_buffered_blocks": (
                self._translated_audio_maximum_buffered_blocks
            ),
            "realtime_received_queue_maximum_buffered_ms": (
                self._translated_audio_maximum_buffered_blocks
                * received_block_ms
            ),
            "realtime_received_queue_dropped_blocks": (
                self._translated_audio_dropped_blocks
            ),
            "realtime_received_queue_dropped_ms": (
                self._translated_audio_dropped_blocks * received_block_ms
            ),
            "realtime_received_queue_discontinuities": (
                self._translated_audio_discontinuities
            ),
            "realtime_received_queue_silence_boundary_dropped_blocks": (
                self._translated_audio_silence_boundary_dropped_blocks
            ),
            "realtime_backlog_p50_ms": self._percentile(
                self._adaptive_backlog_samples_ms, 0.50
            ),
            "realtime_backlog_p95_ms": self._percentile(
                self._adaptive_backlog_samples_ms, 0.95
            ),
            "realtime_backlog_p99_ms": self._percentile(
                self._adaptive_backlog_samples_ms, 0.99
            ),
            "realtime_backlog_time_above_target_ms": (
                self._adaptive_time_above_target_seconds * 1000
            ),
            "realtime_adaptive_speed": self._adaptive_speed,
            "realtime_adaptive_maximum_speed": self._adaptive_maximum_speed,
            "realtime_time_compression_ratio": (
                self._adaptive_compressed_input_samples
                / self._adaptive_compressed_output_samples
                if self._adaptive_compressed_output_samples
                else 1.0
            ),
            "translated_output_underflows": output.output_underflows,
            "realtime_playback_buffered_blocks": output.buffered_blocks,
            "realtime_playback_buffered_ms": (
                output.buffered_blocks * output.frames_per_block / output.sample_rate * 1000
            ),
            "realtime_playback_maximum_buffered_blocks": output.maximum_buffered_blocks,
            "realtime_playback_maximum_buffered_ms": (
                output.maximum_buffered_blocks
                * output.frames_per_block
                / output.sample_rate
                * 1000
            ),
            "realtime_playback_dropped_blocks": output.dropped_blocks,
            "realtime_translated_playback_dropped_blocks": (
                self._translated_playback_dropped_blocks
            ),
            "realtime_passthrough_playback_dropped_blocks": (
                self._passthrough_playback_dropped_blocks
            ),
            "realtime_playback_empty_buffer_events": output.empty_buffer_events,
            "realtime_playback_invalid_blocks": output.invalid_block_sizes,
            "realtime_playback_maximum_callback_gap_ms": (
                output.maximum_callback_gap_ms
            ),
            "realtime_first_audio_latency_ms": (
                session.statistics.first_audio_latency_ms
                if session.statistics.first_audio_latency_ms is not None
                else ""
            ),
            "realtime_total_latency_ms": (
                session.statistics.total_latency_ms
                if session.statistics.total_latency_ms is not None
                else ""
            ),
            "realtime_input_audio_duration_ms": (
                session.statistics.input_duration_seconds(session.sample_rate) * 1000
            ),
            "realtime_output_audio_duration_ms": (
                session.statistics.output_duration_seconds(session.sample_rate) * 1000
            ),
            "realtime_input_rms_dbfs": session.statistics.input_rms_dbfs(),
            "realtime_input_peak_amplitude": (
                session.statistics.input_peak_amplitude
            ),
            "realtime_output_rms_dbfs": session.statistics.output_rms_dbfs(),
            "realtime_output_peak_amplitude": (
                session.statistics.output_peak_amplitude
            ),
            "realtime_provider_input_transcript_lag_ms": (
                session.statistics.input_transcript_lag_ms
                if session.statistics.input_transcript_lag_ms is not None
                else ""
            ),
            "realtime_provider_output_transcript_lag_ms": (
                session.statistics.output_transcript_lag_ms
                if session.statistics.output_transcript_lag_ms is not None
                else ""
            ),
            "realtime_provider_audio_lag_ms": (
                session.statistics.output_audio_lag_ms
                if session.statistics.output_audio_lag_ms is not None
                else ""
            ),
            "realtime_provider_audio_lag_p95_ms": (
                session.statistics.output_audio_lag_percentile(0.95)
                if session.statistics.output_audio_lag_ms is not None
                else ""
            ),
            "realtime_provider_audio_maximum_lag_ms": (
                session.statistics.output_audio_maximum_lag_ms
            ),
            "realtime_provider_delayed": self._provider_delay_active,
            "realtime_output_gain_db": self._output_gain_db,
            "realtime_output_gain_clipped_samples": (
                self._output_gain_clipped_samples
            ),
            "realtime_errors": session.statistics.errors,
            "realtime_reconnections": session.statistics.reconnections,
            "realtime_input_transcript_characters": (
                session.statistics.input_transcript_characters
            ),
            "realtime_output_transcript_characters": (
                session.statistics.output_transcript_characters
            ),
            "realtime_session_gate_enabled": session_gate.enabled,
            "realtime_session_gate_audio_allowed": gate_decision.audio_allowed,
            "realtime_session_gate_observation": (
                "active"
                if gate_decision.observation is True
                else "inactive"
                if gate_decision.observation is False
                else "unknown"
            ),
            "realtime_session_gate_call_active": gate_decision.call_active,
            "realtime_session_gate_activations": session_gate.activations,
            "realtime_session_gate_deactivations": session_gate.deactivations,
            "realtime_session_gate_blocked_ms": (
                session_gate.blocked_seconds * 1000
            ),
            "realtime_voice_latency_completed_utterances": speech_latency[
                "completed_utterances"
            ],
            "realtime_voice_latency_pending_utterances": speech_latency[
                "pending_utterances"
            ],
            "realtime_voice_latency_last_ms": (
                speech_latency["last_accepted_to_played_ms"]
                if speech_latency["last_accepted_to_played_ms"] is not None
                else ""
            ),
            "realtime_voice_latency_average_ms": (
                speech_latency["average_accepted_to_played_ms"]
                if speech_latency["average_accepted_to_played_ms"] is not None
                else ""
            ),
        }
        if self.metrics_writer is not None:
            await asyncio.to_thread(self.metrics_writer.write, values)

    def _reconnect_delay(
        self,
        attempt: int,
        error: BaseException | None = None,
        *,
        now: float | None = None,
    ) -> float:
        realtime = self._realtime_section()
        base = float(realtime["reconnect_base_delay_seconds"])
        maximum = min(30.0, base * (2 ** max(0, attempt - 1)))
        delay = random.uniform(maximum * 0.5, maximum)
        if not is_realtime_rate_limit_error(error):
            return delay
        current_time = time.monotonic() if now is None else now
        shared_delay = self._rate_limit_coordinator.defer(
            maximum * 0.5,
            now=current_time,
        )
        return max(delay, shared_delay)

    def _current_mode(self) -> PipelineMode:
        if self.control_state is None:
            return PipelineMode.TRANSLATE
        return self.control_state.get_mode(self.pipeline_name)

    def _current_language(self, role: str) -> str:
        if self.control_state is not None:
            return self.control_state.get_language(role)
        if self.device_manager is not None:
            language = self.device_manager.participant_languages.get(role)
            if language is not None:
                return language
        raise RuntimeError("Participant language preferences are not available.")

    async def _set_status(self, status: str) -> None:
        state_status = (
            self.control_state.get_pipeline_status(self.pipeline_name).value
            if self.control_state is not None
            else None
        )
        if status == self._last_status and status == state_status:
            return
        changed = status != self._last_status
        self._last_status = status
        if changed:
            LOGGER.info("event=pipeline_status_changed status=%s", status)
        if self.control_state is not None:
            await self.control_state.set_pipeline_status(self.pipeline_name, status)

    async def _reconcile_gate_status(
        self,
        decision: RealtimeSessionGateDecision,
        *,
        session_usable: bool,
        draining: bool = False,
        provider_delayed: bool = False,
    ) -> None:
        """Publish call presence without discarding a graceful provider drain."""
        if decision.waiting_for_application:
            if not draining:
                self._set_translation_audio_acceptance(False)
            await self._set_status("waiting_for_call")
        elif decision.audio_allowed and session_usable:
            self._set_translation_audio_acceptance(True)
            await self._set_status(
                "translation_delayed"
                if provider_delayed
                else "translation_ready"
            )
        elif not session_usable:
            self._set_translation_audio_acceptance(False)

    def _refresh_provider_delay(
        self,
        session: OpenAIRealtimeTranslationSession,
    ) -> bool:
        """Apply hysteresis to semantic output lag for stable UI status."""
        # Audio ``elapsed_ms`` follows generated output duration rather than the
        # continuous input timeline. Comparing it with wall time therefore turns
        # ordinary silence into an ever-growing delay. Transcript elapsed time
        # remains aligned with the source stream and is safe for this status.
        lag_ms = session.statistics.output_transcript_lag_ms
        if lag_ms is None:
            return self._provider_delay_active
        previous = self._provider_delay_active
        if previous:
            self._provider_delay_active = (
                lag_ms > self._provider_delay_recovery_ms
            )
        else:
            self._provider_delay_active = (
                lag_ms >= self._provider_delay_warning_ms
            )
        if previous != self._provider_delay_active:
            LOGGER.info(
                "event=realtime_provider_delay_changed delayed=%s "
                "lag_ms=%.1f warning_ms=%.1f recovery_ms=%.1f",
                self._provider_delay_active,
                lag_ms,
                self._provider_delay_warning_ms,
                self._provider_delay_recovery_ms,
            )
        return self._provider_delay_active

    def _log_transcript(self, direction: str, delta: str) -> None:
        LOGGER.debug(
            "event=realtime_translation_transcript_delta direction=%s text=%r",
            direction,
            delta,
        )

    def _log_transcript_task_failure(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            LOGGER.error(
                "event=realtime_transcript_publisher_failed error=%r",
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

    def _section(self, name: str) -> dict[str, object]:
        section = self.config.get(name)
        if not isinstance(section, dict):
            raise ValueError(f"Configuration section {name!r} must be a mapping.")
        return section

    def _pipeline_section(self) -> dict[str, object]:
        pipelines = self._section("pipelines")
        pipeline = pipelines.get(self.pipeline_name)
        if not isinstance(pipeline, dict):
            raise ValueError(
                f"Pipeline configuration {self.pipeline_name!r} must be a mapping."
            )
        return pipeline

    def _realtime_section(self) -> dict[str, object]:
        realtime = self._section("realtime_translation")
        directions = realtime.get("directions")
        if isinstance(directions, dict):
            direction = directions.get(self.pipeline_name)
            if isinstance(direction, dict):
                return direction
        openai = realtime.get("openai")
        if not isinstance(openai, dict):
            raise ValueError("realtime_translation.openai must be a mapping.")
        return openai
