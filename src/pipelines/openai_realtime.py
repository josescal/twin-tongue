"""Directional audio pipeline for OpenAI's dedicated Realtime translation API."""

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import time

from app_state import ApplicationState, PipelineMode
from audio.capture import QueuedAudioInput
from audio.device_manager import AudioDeviceManager
from audio.diagnostic_manifest import (
    DiagnosticCallManifest,
    diagnostic_call_id,
)
from audio.echo_guard import EchoReferenceBus, ReferenceEchoGuard
from audio.pcm import (
    calculate_block_frames,
    convert_int16_channels,
)
from audio.playback import QueuedAudioOutput
from audio.portaudio import AudioDeviceError, format_device
from audio.resampling import StreamingPcmInt16Resampler, create_resampler
from audio.wav_capture import QueuedDiagnosticWavCapture
from config import set_log_pipeline
from engines.openai_realtime import (
    OpenAIRealtimeTranslationFactory,
    OpenAIRealtimeTranslationSession,
)
from metrics import CsvMetricsWriter


LOGGER = logging.getLogger(__name__)
TranscriptQueueItem = tuple[str, str, str, str] | None
INCOMPLETE_TRANSCRIPT_IDLE_MULTIPLIER = 5


@dataclass(frozen=True)
class RealtimeSessionGateDecision:
    """Decide whether an API session and audio sending are currently allowed."""

    session_required: bool
    audio_allowed: bool
    waiting_for_application: bool
    observation: bool | None


class RealtimeSessionGate:
    """Keep Realtime disconnected until the selected cable has an active client."""

    def __init__(
        self,
        *,
        enabled: bool,
        disconnect_grace_seconds: float,
        monitor_failure_grace_seconds: float,
    ) -> None:
        self.enabled = enabled
        self.disconnect_grace_seconds = disconnect_grace_seconds
        self.monitor_failure_grace_seconds = monitor_failure_grace_seconds
        self.activations = 0
        self.deactivations = 0
        self.blocked_seconds = 0.0
        self._last_present_at: float | None = None
        self._unknown_since: float | None = None
        self._last_evaluated_at: float | None = None
        self._last_requested = False
        self._last_audio_allowed = False
        self._last_session_required = False

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

        if not requested:
            self._last_present_at = None
            self._unknown_since = None
            decision = RealtimeSessionGateDecision(False, False, False, observation)
        elif not self.enabled:
            decision = RealtimeSessionGateDecision(True, True, False, observation)
        elif observation is True:
            self._last_present_at = now
            self._unknown_since = None
            decision = RealtimeSessionGateDecision(True, True, False, observation)
        elif observation is False:
            self._unknown_since = None
            keep_session = (
                self._last_present_at is not None
                and now - self._last_present_at < self.disconnect_grace_seconds
            )
            decision = RealtimeSessionGateDecision(
                keep_session,
                keep_session,
                not keep_session,
                observation,
            )
        else:
            if self._unknown_since is None:
                self._unknown_since = now
            keep_previous = (
                self._last_session_required
                and now - self._unknown_since < self.monitor_failure_grace_seconds
            )
            decision = RealtimeSessionGateDecision(
                keep_previous,
                self._last_audio_allowed and keep_previous,
                True,
                observation,
            )

        if decision.audio_allowed and not self._last_audio_allowed:
            self.activations += 1
        elif self._last_audio_allowed and not decision.audio_allowed:
            self.deactivations += 1
        self._last_requested = requested
        self._last_audio_allowed = decision.audio_allowed
        self._last_session_required = decision.session_required
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
        echo_reference: EchoReferenceBus | None = None,
    ) -> None:
        self.config = config
        self.session_factory = session_factory
        self.pipeline_name = pipeline_name
        self.source_language_name = source_language_name
        self.target_language_name = target_language_name
        self.duration = duration
        self.startup_barrier = startup_barrier
        self.control_state = control_state
        self.device_manager = device_manager
        self.metrics_writer = metrics_writer
        self.echo_reference = echo_reference or EchoReferenceBus()
        self._echo_guard: ReferenceEchoGuard | None = None
        self._diagnostic_manifest: DiagnosticCallManifest | None = None
        self._accept_translated_audio = False
        self._translated_output: QueuedAudioOutput | None = None
        self._translated_resampler: StreamingPcmInt16Resampler | None = None
        self._translated_output_channels = 1
        self._last_status: str | None = None
        self._transcript_sequence = 0
        self._transcript_queue: asyncio.Queue[TranscriptQueueItem] | None = None
        self._log_transcript_deltas = False
        self._audio_send_queue: asyncio.Queue[bytes | None] | None = None
        self._audio_send_dropped_blocks = 0
        self._input_audio_capture: QueuedDiagnosticWavCapture | None = None
        self._accepted_audio_capture: QueuedDiagnosticWavCapture | None = None
        self._sent_audio_capture: QueuedDiagnosticWavCapture | None = None
        self._output_audio_capture: QueuedDiagnosticWavCapture | None = None

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
        audio_capture_settings = dict(realtime["audio_capture"])
        capture_directory = Path(str(audio_capture_settings["directory"]))
        capture_enabled = bool(audio_capture_settings["enabled"])
        call_id = diagnostic_call_id()
        capture_options = {
            "enabled": capture_enabled,
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
        }
        self._input_audio_capture = QueuedDiagnosticWavCapture(
            **capture_options,
            stream_name="captured",
            sample_rate=capture_rate,
            channels=capture_channels,
        )
        self._accepted_audio_capture = QueuedDiagnosticWavCapture(
            **capture_options,
            stream_name="accepted",
            sample_rate=capture_rate,
            channels=1,
        )
        self._sent_audio_capture = QueuedDiagnosticWavCapture(
            **capture_options,
            stream_name="sent",
            sample_rate=api_rate,
            channels=1,
        )
        self._output_audio_capture = QueuedDiagnosticWavCapture(
            **capture_options,
            stream_name="played",
            sample_rate=output_rate,
            channels=output_channels,
        )
        output.played_observer = lambda block: self._record_played_audio(
            block,
            output_channels,
        )
        echo_settings = dict(audio_capture_settings.get("echo_guard", {}))
        self.echo_reference.reference_activity_dbfs = float(
            echo_settings.get("reference_activity_dbfs", -48.0)
        )
        self._echo_guard = ReferenceEchoGuard(
            self.echo_reference,
            enabled=bool(echo_settings.get("enabled", True))
            and self.pipeline_name == "agent_to_remote",
            near_end_override_dbfs=float(
                echo_settings.get("near_end_override_dbfs", -26.0)
            ),
            post_reference_override_dbfs=float(
                echo_settings.get("post_reference_override_dbfs", -38.0)
            ),
            near_end_hold_ms=float(
                echo_settings.get("near_end_hold_ms", 800.0)
            ),
            hangover_ms=float(echo_settings.get("hangover_ms", 350.0)),
            correlation_window_ms=float(
                echo_settings.get("correlation_window_ms", 200.0)
            ),
            reference_delay_max_ms=float(
                echo_settings.get("reference_delay_max_ms", 500.0)
            ),
            correlation_threshold=float(
                echo_settings.get("correlation_threshold", 0.72)
            ),
            block_duration_ms=float(audio["frame_duration_ms"]),
        )
        self._diagnostic_manifest = DiagnosticCallManifest(
            enabled=capture_enabled,
            directory=capture_directory,
            pipeline_name=self.pipeline_name,
            call_id=call_id,
            devices={
                "input": format_device(input_device),
                "output": format_device(output_device),
            },
            formats={
                "captured": {
                    "sample_rate": capture_rate,
                    "channels": capture_channels,
                    "format": str(audio["sample_format"]),
                    "exclusive": capture.exclusive,
                },
                "accepted": {"sample_rate": capture_rate, "channels": 1},
                "sent": {"sample_rate": api_rate, "channels": 1},
                "played": {
                    "sample_rate": output_rate,
                    "channels": output_channels,
                    "exclusive": output.exclusive,
                },
            },
        )
        if capture_enabled:
            LOGGER.info(
                "event=realtime_diagnostic_audio_capture_enabled directory=%s "
                "streams=captured,accepted,sent,played",
                capture_directory,
            )
        self._log_transcript_deltas = bool(realtime["log_transcript_deltas"])
        self._transcript_queue = asyncio.Queue()
        session = self.session_factory.create(
            pipeline_name=self.pipeline_name,
            on_audio=self._handle_translated_audio,
            on_input_transcript=lambda delta: self._enqueue_transcript_delta(
                "input", delta
            ),
            on_output_transcript=lambda delta: self._enqueue_transcript_delta(
                "output", delta
            ),
        )
        self._audio_send_queue = asyncio.Queue(
            maxsize=int(realtime["input_queue_capacity_blocks"])
        )
        audio_sender_task = asyncio.create_task(
            self._audio_sender(session),
            name=f"{self.pipeline_name} realtime audio sender",
        )
        transcript_task = asyncio.create_task(
            self._transcript_worker(
                update_interval_seconds=(
                    float(realtime["transcript_ui_update_interval_ms"]) / 1000
                ),
                segment_idle_seconds=(
                    float(realtime["transcript_segment_idle_ms"]) / 1000
                ),
            ),
            name=f"{self.pipeline_name} realtime transcript publisher",
        )
        transcript_task.add_done_callback(self._log_transcript_task_failure)
        reconnect_attempt = 0
        reconnect_at = 0.0
        configured_target: str | None = None
        connect_task: asyncio.Task[None] | None = None
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
                previous_gate_audio_allowed = last_gate_audio_allowed
                if gate_decision.audio_allowed != previous_gate_audio_allowed:
                    LOGGER.info(
                        "event=realtime_session_gate_changed route=%s "
                        "translation_requested=%s application_session=%s "
                        "audio_allowed=%s session_required=%s",
                        session_gate_route,
                        requested_translation,
                        session_observation,
                        gate_decision.audio_allowed,
                        gate_decision.session_required,
                    )
                    last_gate_audio_allowed = gate_decision.audio_allowed
                    if self._diagnostic_manifest is not None:
                        self._diagnostic_manifest.event(
                            "call_audio_gate_changed",
                            active=gate_decision.audio_allowed,
                            application_session=session_observation,
                        )
                target_language = self._current_language(self.target_language_name)

                if configured_target is not None and target_language != configured_target:
                    self._accept_translated_audio = False
                    self._discard_audio_send_queue("target_language_changed")
                    await session.abort()
                    self._enqueue_transcript_boundary()
                    input_resampler.reset()
                    self._translated_resampler.reset()
                    configured_target = None
                    reconnect_attempt = 0
                    reconnect_at = now

                if not gate_decision.session_required:
                    self._accept_translated_audio = False
                    self._discard_audio_send_queue("session_not_required")
                    if connect_task is not None and not connect_task.done():
                        connect_task.cancel()
                        await asyncio.gather(connect_task, return_exceptions=True)
                    connect_task = None
                    if session.connected:
                        # A mode change is not the end of a media stream. Do not
                        # drain delayed translations into passthrough; close the
                        # socket immediately and discard the old audio instead.
                        await session.abort()
                    if configured_target is not None:
                        self._enqueue_transcript_boundary()
                        configured_target = None
                    if previous_gate_audio_allowed is True:
                        discarded = output.discard_pending_blocks()
                        input_resampler.reset()
                        self._translated_resampler.reset()
                        passthrough_resampler.reset()
                        LOGGER.info(
                            "event=realtime_translation_audio_discarded "
                            "reason=translation_disabled blocks=%s",
                            discarded,
                        )
                    await self._set_status(
                        "waiting_for_call"
                        if gate_decision.waiting_for_application
                        else "passthrough"
                    )
                else:
                    if session.error_event.is_set():
                        session_error = session.last_error
                        self._accept_translated_audio = False
                        self._discard_audio_send_queue("connection_lost")
                        await session.abort()
                        self._enqueue_transcript_boundary()
                        session.error_event.clear()
                        reconnect_attempt += 1
                        reconnect_at = now + self._reconnect_delay(reconnect_attempt)
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
                            reconnect_at = now + self._reconnect_delay(reconnect_attempt)
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
                            self._accept_translated_audio = True
                            self._discard_audio_send_queue("connection_ready")
                            output.discard_pending_blocks()
                            await self._set_status("translation_ready")
                            LOGGER.info(
                                "event=realtime_translation_ready target_language=%s",
                                target_language,
                            )
                        connect_task = None
                    if (
                        not session.connected
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
                        ),
                    )

                block = await capture.wait_for_block(timeout_seconds=0.05)
                if block is not None:
                    self._record_captured_audio(block)
                    mono = convert_int16_channels(block, capture.channels, 1)
                    accepted = (
                        self._echo_guard.process(mono)
                        if self._echo_guard is not None
                        else mono
                    )
                    self._record_accepted_audio(accepted)
                    if gate_decision.audio_allowed and session.connected:
                        for api_block in input_resampler.process(accepted):
                            self._enqueue_audio_for_translation(api_block)
                    else:
                        for original in passthrough_resampler.process(accepted):
                            played = convert_int16_channels(
                                original, 1, output_channels
                            )
                            output.push_block(played)

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
        except asyncio.CancelledError:
            raise
        except BaseException:
            LOGGER.exception("event=realtime_translation_pipeline_failed")
            raise
        finally:
            self._accept_translated_audio = False
            capture.stop()
            self._discard_audio_send_queue("pipeline_stopping")
            audio_send_queue = self._audio_send_queue
            if audio_send_queue is not None:
                audio_send_queue.put_nowait(None)
            await asyncio.gather(audio_sender_task, return_exceptions=True)
            self._audio_send_queue = None
            if connect_task is not None:
                connect_task.cancel()
                await asyncio.gather(connect_task, return_exceptions=True)
            if session.connected and not session.error_event.is_set():
                await session.close()
            else:
                await session.abort()
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
                    ("played", self._output_audio_capture),
                )
                if capture_item is not None
            }
            echo_statistics = (
                vars(self._echo_guard.statistics)
                if self._echo_guard is not None
                else {}
            )
            if self._diagnostic_manifest is not None:
                manifest_path = self._diagnostic_manifest.close(
                    tracks=tracks,
                    statistics={
                        "echo_guard": echo_statistics,
                        "capture_dropped_blocks": diagnostic_dropped_blocks,
                        "send_queue_dropped_blocks": self._audio_send_dropped_blocks,
                    },
                )
                LOGGER.info(
                    "event=diagnostic_call_manifest_closed file=%s",
                    manifest_path,
                )
            self._input_audio_capture = None
            self._accepted_audio_capture = None
            self._sent_audio_capture = None
            self._output_audio_capture = None
            self._diagnostic_manifest = None
            LOGGER.info(
                "event=pipeline_stopped engine=openai_realtime "
                "input_duration_seconds=%.3f output_duration_seconds=%.3f "
                "first_audio_latency_ms=%s total_latency_ms=%s errors=%s "
                "reconnections=%s diagnostic_capture_dropped_blocks=%s",
                session.statistics.input_duration_seconds(session.sample_rate),
                session.statistics.output_duration_seconds(session.sample_rate),
                session.statistics.first_audio_latency_ms,
                session.statistics.total_latency_ms,
                session.statistics.errors,
                session.statistics.reconnections,
                diagnostic_dropped_blocks,
            )

    def _record_captured_audio(self, pcm16: bytes) -> None:
        """Record raw endpoint PCM, including CABLE A during passthrough."""
        capture = self._input_audio_capture
        if capture is not None:
            capture.write(pcm16)

    def _record_accepted_audio(self, pcm16_mono: bytes) -> None:
        """Record canonical mono PCM after echo rejection."""
        capture = self._accepted_audio_capture
        if capture is not None:
            capture.write(pcm16_mono)

    def _record_played_audio(self, pcm16: bytes, channels: int) -> None:
        """Record actual playback and publish it as the far-end AEC reference."""
        capture = self._output_audio_capture
        if capture is not None:
            capture.write(pcm16)
        if self.pipeline_name == "remote_to_agent":
            self.echo_reference.observe(
                convert_int16_channels(pcm16, channels, 1)
            )

    def _record_input_audio(
        self, pcm16: bytes, *, translation_active: bool
    ) -> None:
        """Backward-compatible alias for the now continuous captured track."""
        self._record_captured_audio(pcm16)

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
                discarded += 1
        if discarded:
            LOGGER.info(
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
            if block is None:
                return
            try:
                await session.send_audio(block)
                if self._sent_audio_capture is not None:
                    self._sent_audio_capture.write(block)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOGGER.warning(
                    "event=realtime_translation_send_failed "
                    "action=reconnect error=%s",
                    error,
                )
                session.error_event.set()

    async def _handle_translated_audio(self, pcm16: bytes) -> None:
        output = self._translated_output
        resampler = self._translated_resampler
        if not self._accept_translated_audio or output is None or resampler is None:
            return
        for block in resampler.process(pcm16):
            converted = convert_int16_channels(
                block, 1, self._translated_output_channels
            )
            while (
                self._accept_translated_audio
                and output.buffered_blocks >= output.queue_capacity_blocks
            ):
                await asyncio.sleep(
                    output.frames_per_block / output.sample_rate / 4
                )
            if not self._accept_translated_audio:
                return
            output.push_block(converted)

    def _enqueue_transcript_delta(self, direction: str, delta: str) -> None:
        """Queue transcript text without delaying WebSocket audio reception."""
        if self._log_transcript_deltas:
            self._log_transcript(direction, delta)
        queue = self._transcript_queue
        if queue is None or not delta:
            return
        queue.put_nowait(
            (
                direction,
                delta,
                self._current_language(self.source_language_name),
                self._current_language(self.target_language_name),
            )
        )

    def _enqueue_transcript_boundary(self) -> None:
        queue = self._transcript_queue
        if queue is not None:
            queue.put_nowait(("boundary", "", "", ""))

    async def _transcript_worker(
        self,
        *,
        update_interval_seconds: float,
        segment_idle_seconds: float,
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
        dirty = False

        def reset() -> None:
            nonlocal source_parts, translated_parts
            nonlocal source_language, target_language, transcript_id
            nonlocal last_delta_at, last_publish_at, dirty
            source_parts = []
            translated_parts = []
            source_language = ""
            target_language = ""
            transcript_id = None
            last_delta_at = None
            last_publish_at = 0.0
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
                timeouts.append(max(0.0, segment_idle_seconds - (now - last_delta_at)))
            timeout = max(0.001, min(timeouts))
            try:
                item = await asyncio.wait_for(queue.get(), timeout=timeout)
            except TimeoutError:
                item = ("tick", "", "", "")

            if item is None:
                publish(final=True)
                return

            direction, delta, item_source_language, item_target_language = item
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
                if direction == "input":
                    source_parts.append(delta)
                elif direction == "output":
                    translated_parts.append(delta)
                else:
                    continue
                dirty = True
                last_delta_at = now

            if (
                last_delta_at is not None
                and now - last_delta_at
                >= (
                    segment_idle_seconds
                    if translated_parts
                    else segment_idle_seconds
                    * INCOMPLETE_TRANSCRIPT_IDLE_MULTIPLIER
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
        values: dict[str, object] = {
            "pipeline": self.pipeline_name,
            "mode": self._current_mode().value,
            "engine": "openai_realtime",
            "capture_blocks": capture.statistics.captured_blocks,
            "capture_dropped_blocks": capture.statistics.dropped_blocks,
            "capture_overflows": capture.statistics.input_overflows,
            "capture_maximum_buffered_blocks": capture.statistics.maximum_buffered_blocks,
            "realtime_send_queue_buffered_blocks": (
                self._audio_send_queue.qsize()
                if self._audio_send_queue is not None
                else 0
            ),
            "realtime_send_queue_dropped_blocks": self._audio_send_dropped_blocks,
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
            "realtime_playback_empty_buffer_events": output.empty_buffer_events,
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
            "realtime_session_gate_activations": session_gate.activations,
            "realtime_session_gate_deactivations": session_gate.deactivations,
            "realtime_session_gate_blocked_seconds": session_gate.blocked_seconds,
        }
        LOGGER.info(
            "event=realtime_translation_metrics first_audio_latency_ms=%s "
            "total_latency_ms=%s input_duration_seconds=%.3f "
            "output_duration_seconds=%.3f input_rms_dbfs=%s input_peak=%s "
            "output_rms_dbfs=%s output_peak=%s playback_buffered_ms=%.1f "
            "playback_maximum_buffered_ms=%.1f playback_dropped_blocks=%s "
            "playback_underflows=%s capture_dropped_blocks=%s "
            "input_transcript_characters=%s output_transcript_characters=%s "
            "errors=%s reconnections=%s gate_audio_allowed=%s "
            "gate_observation=%s gate_blocked_seconds=%.3f",
            session.statistics.first_audio_latency_ms,
            session.statistics.total_latency_ms,
            session.statistics.input_duration_seconds(session.sample_rate),
            session.statistics.output_duration_seconds(session.sample_rate),
            session.statistics.input_rms_dbfs(),
            session.statistics.input_peak_amplitude,
            session.statistics.output_rms_dbfs(),
            session.statistics.output_peak_amplitude,
            values["realtime_playback_buffered_ms"],
            values["realtime_playback_maximum_buffered_ms"],
            output.dropped_blocks,
            output.output_underflows,
            capture.statistics.dropped_blocks,
            session.statistics.input_transcript_characters,
            session.statistics.output_transcript_characters,
            session.statistics.errors,
            session.statistics.reconnections,
            gate_decision.audio_allowed,
            values["realtime_session_gate_observation"],
            session_gate.blocked_seconds,
        )
        if self.metrics_writer is not None:
            await asyncio.to_thread(self.metrics_writer.write, values)

    def _reconnect_delay(self, attempt: int) -> float:
        realtime = self._realtime_section()
        base = float(realtime["reconnect_base_delay_seconds"])
        return min(30.0, base * (2 ** max(0, attempt - 1)))

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
        if status == self._last_status:
            return
        self._last_status = status
        LOGGER.info("event=pipeline_status_changed status=%s", status)
        if self.control_state is not None:
            await self.control_state.set_pipeline_status(self.pipeline_name, status)

    async def _reconcile_gate_status(
        self,
        decision: RealtimeSessionGateDecision,
        *,
        session_usable: bool,
    ) -> None:
        """Publish call presence changes even when the WebSocket is reused."""
        if decision.waiting_for_application:
            await self._set_status("waiting_for_call")
        elif decision.audio_allowed and session_usable:
            self._accept_translated_audio = True
            await self._set_status("translation_ready")

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
