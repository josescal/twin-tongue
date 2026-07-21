"""Directional audio pipeline for OpenAI's dedicated Realtime translation API."""

import asyncio
import logging
import time

from app_state import ApplicationState, PipelineMode
from audio.capture import QueuedAudioInput
from audio.device_manager import AudioDeviceManager
from audio.pcm import (
    calculate_block_frames,
    convert_int16_channels,
)
from audio.playback import QueuedAudioOutput
from audio.portaudio import AudioDeviceError, format_device
from audio.resampling import StreamingPcmInt16Resampler, create_resampler
from config import set_log_pipeline
from engines.openai_realtime import (
    OpenAIRealtimeTranslationFactory,
    OpenAIRealtimeTranslationSession,
)
from metrics import CsvMetricsWriter


LOGGER = logging.getLogger(__name__)
TranscriptQueueItem = tuple[str, str, str, str] | None
INCOMPLETE_TRANSCRIPT_IDLE_MULTIPLIER = 5


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
        self._accept_translated_audio = False
        self._translated_output: QueuedAudioOutput | None = None
        self._translated_resampler: StreamingPcmInt16Resampler | None = None
        self._translated_output_channels = 1
        self._last_status: str | None = None
        self._transcript_sequence = 0
        self._transcript_queue: asyncio.Queue[TranscriptQueueItem] | None = None
        self._log_transcript_deltas = False

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
        )
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
        graceful_close_task: asyncio.Task[None] | None = None
        input_revision = self.device_manager.revision(input_device_key)
        output_revision = self.device_manager.revision(output_device_key)
        metrics_interval_seconds = float(realtime["metrics_interval_seconds"])
        metrics_at = time.monotonic() + metrics_interval_seconds
        deadline = None if self.duration is None else time.monotonic() + self.duration

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
            capture.start()
            output.start()
            while deadline is None or time.monotonic() < deadline:
                now = time.monotonic()
                requested_translation = self._current_mode() is PipelineMode.TRANSLATE
                target_language = self._current_language(self.target_language_name)

                if configured_target is not None and target_language != configured_target:
                    self._accept_translated_audio = False
                    await session.abort()
                    self._enqueue_transcript_boundary()
                    input_resampler.reset()
                    self._translated_resampler.reset()
                    configured_target = None
                    reconnect_attempt = 0
                    reconnect_at = now

                if not requested_translation:
                    self._accept_translated_audio = False
                    if connect_task is not None and not connect_task.done():
                        connect_task.cancel()
                        await asyncio.gather(connect_task, return_exceptions=True)
                    connect_task = None
                    if session.connected and graceful_close_task is None:
                        graceful_close_task = asyncio.create_task(session.close())
                    if graceful_close_task is not None and graceful_close_task.done():
                        await asyncio.gather(graceful_close_task, return_exceptions=True)
                        self._enqueue_transcript_boundary()
                        graceful_close_task = None
                        configured_target = None
                    await self._set_status("passthrough")
                else:
                    if graceful_close_task is not None:
                        if graceful_close_task.done():
                            await asyncio.gather(graceful_close_task, return_exceptions=True)
                            graceful_close_task = None
                        else:
                            await self._set_status("switching")
                    if session.error_event.is_set():
                        session_error = session.last_error
                        self._accept_translated_audio = False
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
                        except BaseException as error:
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
                        and graceful_close_task is None
                        and now >= reconnect_at
                    ):
                        max_attempts = int(realtime["reconnect_max_attempts"])
                        if reconnect_attempt >= max_attempts:
                            await self._set_status("translation_unavailable")
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

                block = capture.pop_block()
                if block is None:
                    await asyncio.sleep(0.001)
                else:
                    mono = convert_int16_channels(block, capture_channels, 1)
                    if requested_translation and session.connected:
                        for api_block in input_resampler.process(mono):
                            try:
                                await session.send_audio(api_block)
                            except Exception as error:
                                LOGGER.warning(
                                    "event=realtime_translation_send_failed "
                                    "action=reconnect error=%s",
                                    error,
                                )
                                session.error_event.set()
                                break
                    else:
                        for original in passthrough_resampler.process(mono):
                            output.push_block(
                                convert_int16_channels(original, 1, output_channels)
                            )

                current_input_revision = self.device_manager.revision(input_device_key)
                if current_input_revision != input_revision:
                    input_revision = current_input_revision
                    capture.switch_device(
                        self.device_manager.active_index(input_device_key)
                    )
                    input_resampler.reset()
                    passthrough_resampler.reset()
                current_output_revision = self.device_manager.revision(output_device_key)
                if current_output_revision != output_revision:
                    output_revision = current_output_revision
                    output.switch_device(
                        self.device_manager.active_index(output_device_key)
                    )
                    self._translated_resampler.reset()
                    passthrough_resampler.reset()

                if now >= metrics_at:
                    await self._write_metrics(capture, output, session)
                    metrics_at = now + metrics_interval_seconds
        except asyncio.CancelledError:
            raise
        except BaseException:
            LOGGER.exception("event=realtime_translation_pipeline_failed")
            raise
        finally:
            self._accept_translated_audio = False
            capture.stop()
            if connect_task is not None:
                connect_task.cancel()
                await asyncio.gather(connect_task, return_exceptions=True)
            if graceful_close_task is not None:
                await asyncio.gather(graceful_close_task, return_exceptions=True)
            elif session.connected and not session.error_event.is_set():
                await session.close()
            else:
                await session.abort()
            transcript_queue = self._transcript_queue
            if transcript_queue is not None:
                transcript_queue.put_nowait(None)
            await asyncio.gather(transcript_task, return_exceptions=True)
            self._transcript_queue = None
            output.stop()
            LOGGER.info(
                "event=pipeline_stopped engine=openai_realtime "
                "input_duration_seconds=%.3f output_duration_seconds=%.3f "
                "first_audio_latency_ms=%s total_latency_ms=%s errors=%s reconnections=%s",
                session.statistics.input_duration_seconds(session.sample_rate),
                session.statistics.output_duration_seconds(session.sample_rate),
                session.statistics.first_audio_latency_ms,
                session.statistics.total_latency_ms,
                session.statistics.errors,
                session.statistics.reconnections,
            )

    async def _handle_translated_audio(self, pcm16: bytes) -> None:
        output = self._translated_output
        resampler = self._translated_resampler
        if not self._accept_translated_audio or output is None or resampler is None:
            return
        for block in resampler.process(pcm16):
            converted = convert_int16_channels(
                block, 1, self._translated_output_channels
            )
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
    ) -> None:
        values: dict[str, object] = {
            "pipeline": self.pipeline_name,
            "mode": self._current_mode().value,
            "engine": "openai_realtime",
            "capture_blocks": capture.statistics.captured_blocks,
            "capture_dropped_blocks": capture.statistics.dropped_blocks,
            "capture_overflows": capture.statistics.input_overflows,
            "capture_maximum_buffered_blocks": capture.statistics.maximum_buffered_blocks,
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
        }
        LOGGER.info(
            "event=realtime_translation_metrics first_audio_latency_ms=%s "
            "total_latency_ms=%s input_duration_seconds=%.3f "
            "output_duration_seconds=%.3f playback_buffered_ms=%.1f "
            "playback_maximum_buffered_ms=%.1f playback_dropped_blocks=%s "
            "playback_underflows=%s capture_dropped_blocks=%s "
            "input_transcript_characters=%s output_transcript_characters=%s "
            "errors=%s reconnections=%s",
            session.statistics.first_audio_latency_ms,
            session.statistics.total_latency_ms,
            session.statistics.input_duration_seconds(session.sample_rate),
            session.statistics.output_duration_seconds(session.sample_rate),
            values["realtime_playback_buffered_ms"],
            values["realtime_playback_maximum_buffered_ms"],
            output.dropped_blocks,
            output.output_underflows,
            capture.statistics.dropped_blocks,
            session.statistics.input_transcript_characters,
            session.statistics.output_transcript_characters,
            session.statistics.errors,
            session.statistics.reconnections,
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
        if self.control_state is not None:
            await self.control_state.set_pipeline_status(self.pipeline_name, status)

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
