"""Reusable remote-to-agent realtime dubbing pipeline."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
from pathlib import Path
import time

import sounddevice as sd

from audio.capture import QueuedAudioInput
from audio.device_manager import AudioDeviceManager
from audio.webrtc_aec3 import WebRtcAec3
from audio.portaudio import (
    AudioDeviceError,
    format_device,
    validate_input_settings,
    validate_output_settings,
)
from audio.pcm import calculate_block_frames, convert_int16_channels
from audio.pacing import RealtimeAudioPacer
from audio.playback import QueuedAudioOutput
from audio.speech_segmentation import (
    AdaptiveSpeechSegmenter,
    create_speech_segmenter,
)
from audio.wav_capture import QueuedDiagnosticWavCapture
from audio.resampling import PcmInt16Resampler, create_resampler
from audio.voice_detection import StreamingVoiceDetector, VoiceDetectorLoader
from config import set_log_pipeline
from app_state import ApplicationState, PipelineMode
from metrics import CsvMetricsWriter
from providers.factory import ProviderFactory
from providers.stt import RealtimeTranscript, SpeechToText
from providers.translate import TranslationResult, Translator
from providers.tts import TTSChunk, TextToSpeech

logger = logging.getLogger(__name__)


@dataclass
class PipelineStatistics:
    """Counters across the asynchronous text and audio stages."""

    final_transcripts: int = 0
    dropped_transcripts: int = 0
    translations: int = 0
    dropped_translations: int = 0
    syntheses: int = 0
    dropped_syntheses: int = 0
    played_segments: int = 0
    played_pcm_bytes: int = 0
    stale_before_translation: int = 0
    stale_before_tts: int = 0
    stale_before_playback: int = 0
    superseded_before_playback: int = 0
    recovered_playback: int = 0
    maximum_playback_start_latency_ms: float = 0
    translated_output_underflows: int = 0


@dataclass(frozen=True)
class TimedTranscript:
    """A final transcript carrying its pipeline creation and queue timestamps."""

    value: RealtimeTranscript
    created_at: float
    enqueued_at: float
    source_language: str
    target_language: str
    transcript_id: int | None = None


@dataclass(frozen=True)
class TimedTranslation:
    """A translation retaining the originating transcript timestamp."""

    value: TranslationResult
    created_at: float
    enqueued_at: float
    target_language: str


@dataclass(frozen=True)
class TimedSynthesis:
    """One streaming synthesis retaining the originating transcript timestamp."""

    chunks: asyncio.Queue[TTSChunk | None]
    created_at: float
    enqueued_at: float


class RemoteToAgentPipeline:
    """Capture remote audio, translate it, synthesize it, and play it locally."""

    TRANSCRIPT_QUEUE_CAPACITY = 20
    TTS_QUEUE_CAPACITY = 20
    PLAYBACK_QUEUE_CAPACITY = 10

    def __init__(
        self,
        config: dict[str, object],
        provider_factory: ProviderFactory,
        voice_detector_loader: VoiceDetectorLoader,
        duration: float | None = None,
        *,
        pipeline_name: str = "remote_to_agent",
        source_language_name: str = "remote",
        target_language_name: str = "agent",
        startup_barrier: asyncio.Barrier | None = None,
        control_state: ApplicationState | None = None,
        device_manager: AudioDeviceManager | None = None,
        metrics_writer: CsvMetricsWriter | None = None,
        aec3: WebRtcAec3 | None = None,
    ) -> None:
        self.config = config
        self.provider_factory = provider_factory
        self.voice_detector_loader = voice_detector_loader
        self.duration = duration
        self.pipeline_name = pipeline_name
        self.source_language_name = source_language_name
        self.target_language_name = target_language_name
        self.startup_barrier = startup_barrier
        self.control_state = control_state
        self.device_manager = device_manager
        self.metrics_writer = metrics_writer
        self.aec3 = aec3 or WebRtcAec3()
        self.statistics = PipelineStatistics()
        self.transcript_queue: asyncio.Queue[TimedTranscript] = asyncio.Queue(
            maxsize=self.TRANSCRIPT_QUEUE_CAPACITY
        )
        self.translation_queue: asyncio.Queue[TimedTranslation] = asyncio.Queue(
            maxsize=self.TTS_QUEUE_CAPACITY
        )
        self.playback_queue: asyncio.Queue[TimedSynthesis] = asyncio.Queue(
            maxsize=self.PLAYBACK_QUEUE_CAPACITY
        )
        self._voice_detector: StreamingVoiceDetector | None = None
        self._voice_detector_load_error: BaseException | None = None
        self._speech_segmenter: AdaptiveSpeechSegmenter | None = None
        self._input_resampler: PcmInt16Resampler | None = None
        self._passthrough_resampler: PcmInt16Resampler | None = None
        self.playback_backlog_discard_age_seconds = 4.0
        self.segment_discard_age_seconds = 8.0
        self.playback_backlog_segments_to_keep = 2
        self.latency_protection_enabled = True
        self._last_segment_boundary_at: float | None = None
        self._last_segment_boundary_reason: str | None = None
        self._tts_playback_active = False
        self._language_switching = False
        self._transcript_sequence = 0
        self._live_output: QueuedAudioOutput | None = None
        self._audio_recording_enabled = False
        self._event_loop_maximum_lag_ms = 0.0

    async def run(self) -> None:
        """Run until duration expires or the task is cancelled."""
        set_log_pipeline(self.pipeline_name)
        audio = self._section("audio")
        stt_config = self._section("stt")
        logging_config = self._section("logging")
        pipeline_config = self._pipeline_section(self.pipeline_name)
        latency_protection_config = dict(pipeline_config["latency_protection"])
        capture_rate = int(pipeline_config["input_sample_rate"])
        processing_rate = int(audio["processing_sample_rate"])
        output_sample_rate = int(pipeline_config["output_sample_rate"])
        resampling_config = dict(audio["resampling"])
        metrics_interval_seconds = float(logging_config["metrics_interval_seconds"])
        if metrics_interval_seconds <= 0:
            raise ValueError("Metrics interval must be greater than zero.")
        self.playback_backlog_discard_age_seconds = float(
            latency_protection_config["playback_backlog_discard_age_seconds"]
        )
        self.segment_discard_age_seconds = float(
            latency_protection_config["segment_discard_age_seconds"]
        )
        self.playback_backlog_segments_to_keep = int(
            latency_protection_config["playback_backlog_segments_to_keep"]
        )
        self.latency_protection_enabled = bool(latency_protection_config["enabled"])
        classic_config = dict(pipeline_config["classic"])
        voice_detection_config = dict(classic_config["voice_detection"])
        segmentation_config = dict(classic_config["segmentation"])
        voice_detection_backend = str(
            self.config["classic_pipeline"]["voice_detection"]["backend"]
        )
        stt_max_audio_catchup_ms = float(
            pipeline_config["stt_max_audio_catchup_ms"]
        )
        stt_keepalive_seconds = float(stt_config.get("idle_keepalive_seconds", 5.0))
        if stt_max_audio_catchup_ms < 0:
            raise ValueError("STT maximum audio catch-up must not be negative.")
        if stt_keepalive_seconds < 0:
            raise ValueError("STT idle keepalive interval must not be negative.")
        output_channels = int(pipeline_config["output_channels"])
        if output_channels not in (1, 2):
            raise ValueError("Pipeline output channels must be 1 or 2.")
        if self.playback_backlog_discard_age_seconds <= 0:
            raise ValueError("Playback backlog discard age must be greater than zero.")
        if self.segment_discard_age_seconds < self.playback_backlog_discard_age_seconds:
            raise ValueError(
                "Segment discard age must be greater than or equal to playback backlog discard age."
            )
        if self.playback_backlog_segments_to_keep < 1:
            raise ValueError("Playback backlog must retain at least one segment.")
        if self.device_manager is None:
            raise AudioDeviceError("Realtime pipelines require audio device management.")
        input_key = "input" if self.pipeline_name == "agent_to_remote" else "remote_input"
        output_key = "output" if self.pipeline_name == "remote_to_agent" else "translated_output"
        input_device = self.device_manager.active_index(input_key)
        output_device = self.device_manager.active_index(output_key)
        capture_channels = int(pipeline_config["input_channels"])
        capture = QueuedAudioInput(
            input_device=input_device,
            sample_rate=capture_rate,
            channels=capture_channels,
            dtype=str(audio["sample_format"]),
            frame_duration_ms=float(audio["frame_duration_ms"]),
            queue_capacity_blocks=int(stt_config["queue_capacity_blocks"]),
            negotiate_format=input_key == "input",
            exclusive=(
                input_key == "input"
                and bool(
                    audio.get("physical_capture_exclusive_mode", True)
                )
            ),
        )
        if input_key == "input":
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
        else:
            validate_input_settings(
                input_device,
                capture_rate,
                capture_channels,
                str(audio["sample_format"]),
            )
        validate_output_settings(
            output_device,
            output_sample_rate,
            output_channels,
            "int16",
        )

        speech_segmenter = create_speech_segmenter(
            segmentation_config,
            frame_duration_ms=float(audio["frame_duration_ms"]),
        )
        self._speech_segmenter = speech_segmenter
        translator: Translator = self.provider_factory.create_translator()
        tts: TextToSpeech = self.provider_factory.create_tts()
        stt: SpeechToText = self.provider_factory.create_stt(
            language=self._current_language(self.source_language_name),
            on_partial=speech_segmenter.observe_partial,
            on_final=self._handle_final,
        )
        providers_initialized = False
        provider_initialization_lock = asyncio.Lock()
        stt_audio_capture_config = dict(stt_config["audio_capture"])
        self._audio_recording_enabled = bool(stt_audio_capture_config["enabled"])
        capture_directory = Path(str(stt_audio_capture_config["directory"]))
        max_capture_seconds = float(stt_audio_capture_config["max_seconds_per_file"])
        capture_write_options = {
            "write_timing_marks": bool(
                stt_audio_capture_config.get("write_timing_marks", False)
            ),
            "write_buffer_bytes": int(
                stt_audio_capture_config.get("write_buffer_kb", 64)
            )
            * 1024,
            "flush_interval_seconds": float(
                stt_audio_capture_config.get("flush_interval_seconds", 0.5)
            ),
        }
        stt_audio_capture = QueuedDiagnosticWavCapture(
            enabled=bool(stt_audio_capture_config["enabled"]),
            directory=capture_directory,
            pipeline_name=self.pipeline_name,
            stream_name="stt",
            sample_rate=int(stt_config["sample_rate"]),
            channels=1,
            sample_width_bytes=2,
            max_seconds_per_file=max_capture_seconds,
            **capture_write_options,
        )
        input_audio_capture = QueuedDiagnosticWavCapture(
            enabled=self._audio_recording_enabled,
            directory=capture_directory,
            pipeline_name=self.pipeline_name,
            stream_name="input",
            sample_rate=capture_rate,
            channels=capture_channels,
            max_seconds_per_file=max_capture_seconds,
            **capture_write_options,
        )
        output_audio_capture = QueuedDiagnosticWavCapture(
            enabled=self._audio_recording_enabled,
            directory=capture_directory,
            pipeline_name=self.pipeline_name,
            stream_name="output",
            sample_rate=output_sample_rate,
            channels=output_channels,
            max_seconds_per_file=max_capture_seconds,
            **capture_write_options,
        )

        async def ensure_providers() -> bool:
            nonlocal providers_initialized
            if providers_initialized:
                return False
            async with provider_initialization_lock:
                if providers_initialized:
                    return False
                await self._set_pipeline_status("initializing")
                logger.info("event=providers_initialization_started")
                provider_start = time.perf_counter()
                initialization_results = await asyncio.gather(
                    asyncio.to_thread(translator.connect),
                    asyncio.to_thread(tts.connect),
                    stt.connect(),
                    return_exceptions=True,
                )
                for result in initialization_results:
                    if isinstance(result, BaseException):
                        await self._set_pipeline_status("error")
                        raise result
                providers_initialized = True
                logger.info(
                    "event=providers_ready duration_ms=%.0f",
                    (time.perf_counter() - provider_start) * 1000,
                )
                return True

        async def close_providers() -> bool:
            nonlocal providers_initialized
            async with provider_initialization_lock:
                was_initialized = providers_initialized or stt.connection is not None
                await asyncio.gather(stt.close(), translator.close(), tts.close())
                providers_initialized = False
                if was_initialized:
                    logger.info("event=providers_stopped reason=passthrough")
                return was_initialized
        self._input_resampler = create_resampler(
            resampling_config,
            capture_rate,
            processing_rate,
            output_block_frames=calculate_block_frames(
                processing_rate,
                float(audio["frame_duration_ms"]),
            ),
        )
        self._passthrough_resampler = create_resampler(
            resampling_config,
            capture_rate,
            output_sample_rate,
            output_block_frames=calculate_block_frames(
                output_sample_rate,
                float(audio["frame_duration_ms"]),
            ),
        )
        stop_sender = asyncio.Event()
        stop_translation = asyncio.Event()
        stop_tts = asyncio.Event()
        stop_playback = asyncio.Event()
        tasks: dict[str, asyncio.Task[object]] = {}
        caught_error: BaseException | None = None

        logger.info(
            "event=pipeline_started input_device=%s output_device=%s output_channels=%s "
            "source_language=%s target_language=%s requested_mode=%s",
            format_device(input_device),
            format_device(output_device),
            output_channels,
            self._current_language(self.source_language_name),
            self._current_language(self.target_language_name),
            self._current_mode().value,
        )
        try:
            if self.startup_barrier is not None:
                await self.startup_barrier.wait()
            await asyncio.to_thread(capture.start)
            voice_detector_load_task = asyncio.create_task(
                self._load_voice_detector(
                    voice_detection_backend,
                    voice_detection_config,
                    processing_rate,
                    float(audio["frame_duration_ms"]),
                ),
                name=f"{self.pipeline_name} voice detector load",
            )
            tasks = {
                "voice detector load": voice_detector_load_task,
                "audio sender": asyncio.create_task(
                    self._audio_sender(
                        capture,
                        stt,
                        stop_sender,
                        output_device,
                        output_channels,
                        output_sample_rate,
                        voice_detector_load_task,
                        speech_segmenter,
                        stt_max_audio_catchup_ms,
                        stt_keepalive_seconds,
                        ensure_providers,
                        close_providers,
                        stt_audio_capture,
                        input_audio_capture,
                        output_audio_capture,
                    )
                ),
                "translation worker": asyncio.create_task(
                    self._translation_worker(
                        translator,
                        stop_translation,
                    )
                ),
                "TTS worker": asyncio.create_task(
                    self._tts_worker(
                        tts,
                        stop_tts,
                    )
                ),
                "playback worker": asyncio.create_task(
                    self._playback_worker(
                        output_device,
                        output_channels,
                        output_sample_rate,
                        resampling_config,
                        float(audio["frame_duration_ms"]),
                        stop_playback,
                        output_audio_capture,
                    )
                ),
                "STT error": asyncio.create_task(stt.error_event.wait()),
                "metrics": asyncio.create_task(
                    self._log_metrics(
                        capture,
                        metrics_interval_seconds,
                        (stt_audio_capture, input_audio_capture, output_audio_capture),
                    )
                ),
                "event loop watchdog": asyncio.create_task(
                    self._monitor_event_loop_lag()
                ),
            }
            if self.pipeline_name == "agent_to_remote":
                tasks["physical input monitor"] = asyncio.create_task(
                    self._monitor_physical_input(
                        capture,
                        self.device_manager,
                        resampling_config,
                        processing_rate,
                        output_sample_rate,
                    )
                )
            if self.pipeline_name == "remote_to_agent":
                tasks["virtual input monitor"] = asyncio.create_task(
                    self._monitor_virtual_input(capture, self.device_manager)
                )
            if self._current_mode() is PipelineMode.TRANSLATE:
                logger.info("event=pipeline_ready effective_mode=passthrough requested_mode=translate")
            else:
                logger.info("event=pipeline_ready effective_mode=passthrough requested_mode=passthrough")
            if self.duration is not None:
                tasks["duration"] = asyncio.create_task(asyncio.sleep(self.duration))
            wait_tasks = {
                task
                for name, task in tasks.items()
                if name not in {"metrics", "voice detector load"}
            }
            completed, _ = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
            if tasks.get("duration") in completed:
                logger.info("event=pipeline_duration_completed")
            elif tasks["STT error"] in completed and stt.last_error is not None:
                raise stt.last_error
            else:
                for name, task in tasks.items():
                    if task in completed:
                        task.result()
                        raise RuntimeError(f"{name} stopped unexpectedly.")
        except BaseException as error:
            caught_error = error
        finally:
            try:
                capture.stop()
            except BaseException as error:
                caught_error = caught_error or error
            cancelled = isinstance(caught_error, asyncio.CancelledError)
            if cancelled:
                for event in (stop_sender, stop_translation, stop_tts, stop_playback):
                    event.set()
                worker_tasks: list[asyncio.Task[object]] = []
                for name, task in tasks.items():
                    if name == "voice detector load":
                        continue
                    worker_tasks.append(task)
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*worker_tasks, return_exceptions=True)
                try:
                    await asyncio.wait_for(
                        asyncio.gather(stt.close(), translator.close(), tts.close()),
                        timeout=2.0,
                    )
                except Exception as error:
                    logger.warning(
                        "event=provider_shutdown_timeout timeout_seconds=2 consequence=forced_shutdown error=%s",
                        error,
                    )
            else:
                stop_sender.set()
                caught_error = await self._drain_task(
                    tasks.get("audio sender"), 2, "audio sender", caught_error
                )
                if stt.connection is not None:
                    try:
                        if stt.last_error is None and stt.statistics.sent_blocks:
                            await stt.commit_final()
                            await asyncio.sleep(1.0)
                        await stt.close()
                    except BaseException as error:
                        caught_error = caught_error or error
                stop_translation.set()
                caught_error = await self._drain_task(
                    tasks.get("translation worker"), 15, "translation worker", caught_error
                )
                stop_tts.set()
                caught_error = await self._drain_task(
                    tasks.get("TTS worker"), 30, "TTS worker", caught_error
                )
                stop_playback.set()
                caught_error = await self._drain_task(
                    tasks.get("playback worker"), 60, "playback worker", caught_error
                )
                try:
                    await translator.close()
                except BaseException as error:
                    caught_error = caught_error or error
                try:
                    await tts.close()
                except BaseException as error:
                    caught_error = caught_error or error
                for name in (
                    "duration",
                    "STT error",
                    "metrics",
                    "event loop watchdog",
                    "physical input monitor",
                    "virtual input monitor",
                ):
                    task = tasks.get(name)
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(
                    *(
                        task
                        for name, task in tasks.items()
                        if name != "voice detector load"
                    ),
                    return_exceptions=True,
                )
            await self._wait_for_voice_detector_shutdown(
                tasks.get("voice detector load")
            )
            await asyncio.gather(
                asyncio.to_thread(stt_audio_capture.shutdown),
                asyncio.to_thread(input_audio_capture.shutdown),
                asyncio.to_thread(output_audio_capture.shutdown),
            )
            logger.info(
                "event=pipeline_stopped reason=%s",
                "cancelled" if cancelled else "completed",
            )
        if caught_error is not None:
            raise caught_error

    def _handle_final(self, transcript: RealtimeTranscript) -> None:
        if self._language_switching or self._current_mode() is PipelineMode.PASSTHROUGH:
            return
        commit_to_final_ms: float | None = None
        boundary_reason = self._last_segment_boundary_reason
        if self._last_segment_boundary_at is not None:
            commit_to_final_ms = (
                time.monotonic() - self._last_segment_boundary_at
            ) * 1000
            self._last_segment_boundary_at = None
            self._last_segment_boundary_reason = None
        self.statistics.final_transcripts += 1
        now = time.monotonic()
        source_language = self._current_language(self.source_language_name)
        target_language = self._current_language(self.target_language_name)
        logger.debug(
            "event=stt_final pipeline=%s source_language=%s target_language=%s "
            "commit_to_final_ms=%s boundary=%s word_count=%s "
            "average_logprob=%s minimum_logprob=%s text=%s",
            self.pipeline_name,
            source_language,
            target_language,
            "" if commit_to_final_ms is None else f"{commit_to_final_ms:.0f}",
            boundary_reason or "unknown",
            len(transcript.words),
            "" if transcript.average_logprob is None else f"{transcript.average_logprob:.4f}",
            "" if transcript.minimum_logprob is None else f"{transcript.minimum_logprob:.4f}",
            transcript.text,
        )
        transcript_id: int | None = None
        if self.control_state is not None:
            self._transcript_sequence += 1
            transcript_id = self._transcript_sequence
            self.control_state.publish_transcript_final(
                self.pipeline_name,
                transcript_id,
                transcript.text,
                source_language,
                target_language,
            )
        item = TimedTranscript(
            value=transcript,
            created_at=now,
            enqueued_at=now,
            source_language=source_language,
            target_language=target_language,
            transcript_id=transcript_id,
        )
        if self.transcript_queue.full():
            discarded = self.transcript_queue.get_nowait()
            self._mark_transcript_translation_unavailable(discarded)
            self.statistics.dropped_transcripts += 1
        self.transcript_queue.put_nowait(item)

    async def _audio_sender(
        self,
        capture: QueuedAudioInput,
        stt: SpeechToText,
        stop: asyncio.Event,
        output_device: int,
        output_channels: int,
        output_sample_rate: int,
        voice_detector_load_task: asyncio.Task[StreamingVoiceDetector],
        speech_segmenter: AdaptiveSpeechSegmenter,
        stt_max_audio_catchup_ms: float,
        stt_keepalive_seconds: float,
        ensure_providers: Callable[[], Awaitable[bool]],
        close_providers: Callable[[], Awaitable[bool]],
        stt_audio_capture: QueuedDiagnosticWavCapture,
        input_audio_capture: QueuedDiagnosticWavCapture,
        output_audio_capture: QueuedDiagnosticWavCapture,
    ) -> None:
        if self._input_resampler is None or self._passthrough_resampler is None:
            raise RuntimeError("Input resamplers have not been initialized.")
        pacer = RealtimeAudioPacer(
            capture.frame_duration_ms,
            stt_max_audio_catchup_ms,
        )
        keepalive_pcm_bytes = calculate_block_frames(
            stt.sample_rate,
            capture.frame_duration_ms,
        ) * 2
        live_output = QueuedAudioOutput(
            output_device=output_device,
            sample_rate=output_sample_rate,
            channels=output_channels,
            dtype=capture.dtype,
            frames_per_block=calculate_block_frames(
                output_sample_rate, capture.frame_duration_ms
            ),
            played_observer=(
                lambda block: self.aec3.observe_render(
                    block,
                    sample_rate=output_sample_rate,
                    channels=output_channels,
                )
                if self.pipeline_name == "remote_to_agent"
                else None
            ),
        )
        self._live_output = live_output
        detector_load_checked = False
        requested_mode = self._current_mode()
        observed_mode = self._effective_audio_mode(requested_mode)
        reported_status: str | None = None
        output_switch_retry_at = 0.0
        output_switch_error: str | None = None
        next_keepalive_at = time.monotonic() + stt_keepalive_seconds
        recording_observed = False
        aec_call_observed: bool | None = None
        logger.info(
            "event=pipeline_mode_observed requested_mode=%s effective_mode=%s",
            requested_mode.value,
            observed_mode.value,
        )
        try:
            while not stop.is_set() or capture.has_pending_blocks():
                if not detector_load_checked and voice_detector_load_task.done():
                    detector_load_checked = True
                    try:
                        self._voice_detector = voice_detector_load_task.result()
                    except asyncio.CancelledError:
                        raise
                    except BaseException as error:
                        self._voice_detector_load_error = error
                        logger.error(
                            "event=voice_detection_failed consequence=translation_unavailable "
                            "effective_mode=passthrough error=%s",
                            error,
                        )
                    else:
                        logger.info("event=voice_detection_ready")
                current_output = self._current_output_device(output_device)
                if (
                    current_output != live_output.output_device
                    and time.monotonic() >= output_switch_retry_at
                ):
                    try:
                        await asyncio.to_thread(live_output.switch_device, current_output)
                        if output_switch_error is not None:
                            logger.info(
                                "event=audio_device_switch_recovered direction=output device=%s",
                                current_output,
                            )
                            output_switch_error = None
                    except (AudioDeviceError, sd.PortAudioError, OSError, RuntimeError, ValueError) as error:
                        output_switch_retry_at = time.monotonic() + 1.0
                        detail = str(error)
                        if detail != output_switch_error:
                            logger.warning(
                                "event=audio_device_switch_failed direction=output "
                                "action=retrying error=%s",
                                detail,
                            )
                            output_switch_error = detail
                block = capture.pop_block()
                if block is None:
                    await asyncio.sleep(0.005)
                    continue
                mono = convert_int16_channels(block, capture.channels, 1)
                if self.pipeline_name == "agent_to_remote":
                    call_active = self._call_session_active()
                    if call_active != aec_call_observed:
                        aec_call_observed = call_active
                        self.aec3.reset()
                        logger.info(
                            "event=webrtc_aec3_call_state_changed active=%s",
                            call_active,
                        )
                    if call_active:
                        mono = self.aec3.process_capture(
                            mono,
                            sample_rate=capture.sample_rate,
                        )
                recording_active = self._should_record_audio()
                if recording_active is not recording_observed:
                    recording_observed = recording_active
                    logger.info(
                        "event=audio_recording_changed active=%s",
                        recording_active,
                    )
                    if not recording_active:
                        await asyncio.gather(
                            asyncio.to_thread(stt_audio_capture.close_session),
                            asyncio.to_thread(input_audio_capture.close_session),
                            asyncio.to_thread(output_audio_capture.close_session),
                        )
                if recording_active:
                    input_audio_capture.write(block)
                requested_mode = self._current_mode()
                mode = self._effective_audio_mode(requested_mode)
                if mode is not observed_mode:
                    observed_mode = mode
                    logger.info(
                        "event=pipeline_mode_changed requested_mode=%s effective_mode=%s",
                        requested_mode.value,
                        mode.value,
                    )
                    if mode is PipelineMode.PASSTHROUGH:
                        self._discard_processing_backlog()
                        self._reset_processing_audio()
                        live_output.discard_pending_blocks()
                        self._language_switching = True
                        try:
                            await close_providers()
                        finally:
                            self._language_switching = False
                    else:
                        if not live_output.started:
                            live_output.start()
                            logger.info("event=translated_output_ready")
                        pacer.reset()
                        self._reset_processing_audio()
                        next_keepalive_at = time.monotonic() + stt_keepalive_seconds
                if mode is PipelineMode.PASSTHROUGH:
                    if self._tts_playback_active:
                        continue
                    if self._voice_detector_load_error is not None:
                        desired_status = "translation_unavailable"
                    elif self._voice_detector is None:
                        desired_status = "system_loading"
                    else:
                        desired_status = "passthrough"
                    if desired_status != reported_status:
                        await self._set_pipeline_status(desired_status)
                        reported_status = desired_status
                    passthrough_blocks = self._passthrough_resampler.process(mono)
                    for passthrough_mono in passthrough_blocks:
                        output_block = convert_int16_channels(
                            passthrough_mono, 1, output_channels
                        )
                        if recording_active:
                            output_audio_capture.write(output_block)
                        if not live_output.started:
                            live_output.start(initial_blocks=(output_block,))
                            logger.info("event=passthrough_ready")
                        else:
                            live_output.push_block(output_block)
                    continue
                initialized_now = await ensure_providers()
                if self._current_mode() is not PipelineMode.TRANSLATE:
                    continue
                if initialized_now:
                    discarded_blocks = 1
                    while capture.pop_block() is not None:
                        discarded_blocks += 1
                    self._reset_processing_audio()
                    pacer.reset()
                    logger.info(
                        "event=translation_ready discarded_capture_blocks=%s",
                        discarded_blocks,
                    )
                    await self._set_pipeline_status("translation_ready")
                    reported_status = "translation_ready"
                    continue
                if reported_status != "translation_ready":
                    await self._set_pipeline_status("translation_ready")
                    reported_status = "translation_ready"
                selected_language = self._current_language(self.source_language_name)
                if selected_language != stt.language:
                    logger.info(
                        "event=language_change_started component=stt previous_language=%s language=%s",
                        stt.language,
                        selected_language,
                    )
                    self._language_switching = True
                    await self._set_pipeline_status("initializing")
                    try:
                        self._reset_processing_audio()
                        if stt.connection is not None:
                            await stt.close()
                        stt.language = selected_language
                        await stt.connect()
                        pacer.reset()
                    finally:
                        self._language_switching = False
                    logger.info("event=language_changed component=stt language=%s", selected_language)
                    await self._set_pipeline_status("translation_ready")
                    reported_status = "translation_ready"
                voice_detector = self._voice_detector
                if voice_detector is None:
                    raise RuntimeError(
                        "Translation became effective before voice detection was ready."
                    )
                processed_blocks = self._input_resampler.process(mono)
                forwarded_any = False
                for processed_mono in processed_blocks:
                    detection = voice_detector.process(processed_mono)
                    blocks = detection.forwarded_blocks
                    for forwarded_block in blocks:
                        if stt.connection is None and stt.reconnect_on_close:
                            logger.info("event=provider_reconnect_started provider=elevenlabs_stt reason=local_speech")
                            await stt.connect()
                            pacer.reset()
                        await pacer.wait()
                        if recording_active:
                            stt_audio_capture.write(forwarded_block)
                        await stt.send_audio(forwarded_block)
                        forwarded_any = True
                        next_keepalive_at = time.monotonic() + stt_keepalive_seconds
                    segmentation = speech_segmenter.process(detection)
                    if segmentation.should_commit:
                        self._last_segment_boundary_at = time.monotonic()
                        self._last_segment_boundary_reason = segmentation.reason
                        await stt.commit_final()
                if (
                    not forwarded_any
                    and stt.connection is not None
                    and stt_keepalive_seconds > 0
                    and time.monotonic() >= next_keepalive_at
                ):
                    await stt.send_keepalive(keepalive_pcm_bytes)
                    next_keepalive_at = time.monotonic() + stt_keepalive_seconds
        finally:
            self._reset_processing_audio()
            await asyncio.gather(
                asyncio.to_thread(stt_audio_capture.close_session),
                asyncio.to_thread(input_audio_capture.close_session),
                asyncio.to_thread(output_audio_capture.close_session),
            )
            live_output.stop()
            self._live_output = None

    async def _translation_worker(
        self,
        translator: Translator,
        stop: asyncio.Event,
    ) -> None:
        while not stop.is_set() or not self.transcript_queue.empty():
            try:
                item = await asyncio.wait_for(self.transcript_queue.get(), timeout=0.1)
            except TimeoutError:
                continue
            if self._current_mode() is PipelineMode.PASSTHROUGH:
                self._mark_transcript_translation_unavailable(item)
                self.statistics.dropped_transcripts += 1
                continue
            if self._drop_if_stale(item.created_at, "translation"):
                self._mark_transcript_translation_unavailable(item)
                self.statistics.stale_before_translation += 1
                continue
            try:
                result = await translator.translate(
                    item.value.text,
                    source_language=item.source_language,
                    target_language=item.target_language,
                )
            except Exception:
                self._mark_transcript_translation_unavailable(item)
                if self._current_mode() is PipelineMode.PASSTHROUGH:
                    self.statistics.dropped_translations += 1
                    continue
                raise
            if self._current_mode() is PipelineMode.PASSTHROUGH:
                self._mark_transcript_translation_unavailable(item)
                self.statistics.dropped_translations += 1
                continue
            self.statistics.translations += 1
            if item.transcript_id is not None and self.control_state is not None:
                self.control_state.publish_transcript_translation(
                    self.pipeline_name, item.transcript_id, result.translated_text
                )
            translated = TimedTranslation(
                value=result,
                created_at=item.created_at,
                enqueued_at=time.monotonic(),
                target_language=item.target_language,
            )
            if self.translation_queue.full():
                self.translation_queue.get_nowait()
                self.statistics.dropped_translations += 1
            self.translation_queue.put_nowait(translated)

    def _mark_transcript_translation_unavailable(self, item: TimedTranscript) -> None:
        if item.transcript_id is not None and self.control_state is not None:
            self.control_state.mark_transcript_translation_unavailable(
                self.pipeline_name, item.transcript_id
            )

    async def _tts_worker(
        self,
        tts: TextToSpeech,
        stop: asyncio.Event,
    ) -> None:
        while not stop.is_set() or not self.translation_queue.empty():
            try:
                item = await asyncio.wait_for(self.translation_queue.get(), timeout=0.1)
            except TimeoutError:
                continue
            if self._current_mode() is PipelineMode.PASSTHROUGH:
                self.statistics.dropped_translations += 1
                continue
            if self._drop_if_stale(item.created_at, "TTS"):
                self.statistics.stale_before_tts += 1
                continue
            try:
                chunks: asyncio.Queue[TTSChunk | None] = asyncio.Queue()
                synthesis = TimedSynthesis(
                    chunks=chunks,
                    created_at=item.created_at,
                    enqueued_at=time.monotonic(),
                )
                if self.playback_queue.full():
                    self.playback_queue.get_nowait()
                    self.statistics.dropped_syntheses += 1
                self.playback_queue.put_nowait(synthesis)

                completed = False
                try:
                    async for chunk in tts.synthesize_stream(
                        item.value.translated_text,
                        item.target_language,
                        self._current_voice_gender(),
                    ):
                        if self._current_mode() is PipelineMode.PASSTHROUGH:
                            break
                        await chunks.put(chunk)
                        if chunk.final:
                            completed = True
                finally:
                    chunks.put_nowait(None)
            except Exception:
                if self._current_mode() is PipelineMode.PASSTHROUGH:
                    self.statistics.dropped_syntheses += 1
                    continue
                raise
            if self._current_mode() is PipelineMode.PASSTHROUGH:
                self.statistics.dropped_syntheses += 1
                continue
            if not completed:
                self.statistics.dropped_syntheses += 1
                continue
            self.statistics.syntheses += 1

    async def _playback_worker(
        self,
        output_device: int,
        output_channels: int,
        output_sample_rate: int,
        resampling_config: dict[str, object],
        frame_duration_ms: float,
        stop: asyncio.Event,
        output_audio_capture: QueuedDiagnosticWavCapture,
    ) -> None:
        while not stop.is_set() or not self.playback_queue.empty():
            try:
                item = await asyncio.wait_for(self.playback_queue.get(), timeout=0.1)
            except TimeoutError:
                continue
            if self._current_mode() is PipelineMode.PASSTHROUGH:
                self.statistics.dropped_syntheses += 1
                continue
            item = self._select_realtime_playback_item(item)
            if item is None:
                continue
            try:
                await self._play_synthesis_stream(
                    item,
                    output_device,
                    output_channels,
                    output_sample_rate,
                    resampling_config,
                    frame_duration_ms,
                    output_audio_capture,
                )
            except (AudioDeviceError, sd.PortAudioError, OSError, RuntimeError, ValueError) as error:
                self._tts_playback_active = False
                logger.warning(
                    "event=playback_interrupted consequence=segment_lost "
                    "action=retry_next_segment error=%s",
                    error,
                )

    async def _play_synthesis_stream(
        self,
        item: TimedSynthesis,
        output_device: int,
        output_channels: int,
        output_sample_rate: int,
        resampling_config: dict[str, object],
        frame_duration_ms: float,
        output_audio_capture: QueuedDiagnosticWavCapture | None = None,
    ) -> None:
        """Resample a TTS response into the persistent callback output."""
        live_output = self._live_output
        if live_output is None:
            raise RuntimeError("Translated output is not initialized.")
        resampler: PcmInt16Resampler | None = None
        pending_input = bytearray()
        output_pcm_bytes = 0
        output_underflows_before = live_output.output_underflows
        playback_started = False
        completed = False
        block_frames = calculate_block_frames(output_sample_rate, frame_duration_ms)
        while True:
            chunk = await item.chunks.get()
            if chunk is None:
                break
            if chunk.sample_width_bytes != 2 or chunk.channels != 1:
                raise ValueError("Streaming TTS audio must be mono PCM int16.")
            if resampler is None:
                resampler = create_resampler(
                    resampling_config,
                    chunk.sample_rate,
                    output_sample_rate,
                    output_block_frames=block_frames,
                )
            elif chunk.sample_rate != resampler.input_rate:
                raise ValueError("Streaming TTS sample rate changed within one segment.")
            pending_input.extend(chunk.audio)
            complete_bytes = len(pending_input) - len(pending_input) % 2
            source_pcm = bytes(pending_input[:complete_bytes])
            del pending_input[:complete_bytes]
            if chunk.final and pending_input:
                raise ValueError("Streaming TTS ended with an incomplete PCM sample.")
            output_blocks = resampler.process(source_pcm, final=chunk.final)
            for mono_block in output_blocks:
                if not mono_block:
                    continue
                if self._current_mode() is PipelineMode.PASSTHROUGH:
                    self._tts_playback_active = False
                    return
                block = convert_int16_channels(mono_block, 1, output_channels)
                if not playback_started:
                    playback_latency_ms = (time.monotonic() - item.created_at) * 1000
                    self.statistics.maximum_playback_start_latency_ms = max(
                        self.statistics.maximum_playback_start_latency_ms,
                        playback_latency_ms,
                    )
                    playback_started = True
                    self._tts_playback_active = True
                while live_output.buffered_blocks >= live_output.queue_capacity_blocks:
                    if self._current_mode() is PipelineMode.PASSTHROUGH:
                        self._tts_playback_active = False
                        return
                    await asyncio.sleep(frame_duration_ms / 4_000)
                live_output.push_block(block)
                if output_audio_capture is not None and self._should_record_audio():
                    output_audio_capture.write(block)
                output_pcm_bytes += len(block)
            if chunk.final:
                completed = True
                break
        self._tts_playback_active = False
        if not completed or not playback_started:
            return
        self.statistics.played_segments += 1
        self.statistics.played_pcm_bytes += output_pcm_bytes
        underflow_count = live_output.output_underflows - output_underflows_before
        if underflow_count:
            self.statistics.translated_output_underflows += underflow_count
            logger.warning(
                "event=playback_underflow stream_underflows=%s consequence=audio_glitch_possible",
                underflow_count,
            )

    async def _log_metrics(
        self,
        capture: QueuedAudioInput,
        interval_seconds: float,
        audio_captures: tuple[QueuedDiagnosticWavCapture, ...],
    ) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            vad = self._voice_detector.statistics if self._voice_detector is not None else None
            segmentation = (
                self._speech_segmenter.statistics
                if self._speech_segmenter is not None
                else None
            )
            values: dict[str, object] = {
                "pipeline": self.pipeline_name,
                "mode": self._current_mode().value,
                "engine": "classic",
                "capture_blocks": capture.statistics.captured_blocks,
                "capture_dropped_blocks": capture.statistics.dropped_blocks,
                "capture_overflows": capture.statistics.input_overflows,
                "capture_invalid_blocks": capture.statistics.invalid_block_sizes,
                "capture_maximum_buffered_blocks": (
                    capture.statistics.maximum_buffered_blocks
                ),
                "capture_maximum_callback_gap_ms": (
                    capture.statistics.maximum_callback_gap_ms
                ),
                "passthrough_output_dropped_blocks": (
                    self._live_output.dropped_blocks if self._live_output else 0
                ),
                "passthrough_output_underflows": (
                    self._live_output.output_underflows if self._live_output else 0
                ),
                "passthrough_output_empty_buffer_events": (
                    self._live_output.empty_buffer_events if self._live_output else 0
                ),
                "passthrough_output_invalid_blocks": (
                    self._live_output.invalid_block_sizes if self._live_output else 0
                ),
                "passthrough_output_maximum_buffered_blocks": (
                    self._live_output.maximum_buffered_blocks if self._live_output else 0
                ),
                "passthrough_output_maximum_callback_gap_ms": (
                    self._live_output.maximum_callback_gap_ms if self._live_output else 0
                ),
                "translated_output_underflows": (
                    self.statistics.translated_output_underflows
                ),
                "recording_dropped_blocks": sum(
                    item.dropped_blocks for item in audio_captures
                ),
                "event_loop_maximum_lag_ms": self._event_loop_maximum_lag_ms,
                "final_transcripts": self.statistics.final_transcripts,
                "translations": self.statistics.translations,
                "syntheses": self.statistics.syntheses,
                "played_segments": self.statistics.played_segments,
                "dropped_transcripts": self.statistics.dropped_transcripts,
                "dropped_translations": self.statistics.dropped_translations,
                "dropped_syntheses": self.statistics.dropped_syntheses,
                "stale_before_translation": self.statistics.stale_before_translation,
                "stale_before_tts": self.statistics.stale_before_tts,
                "stale_before_playback": self.statistics.stale_before_playback,
                "superseded_before_playback": (
                    self.statistics.superseded_before_playback
                ),
                "maximum_playback_start_latency_ms": (
                    self.statistics.maximum_playback_start_latency_ms
                ),
                "vad_peak_probability": vad.interval_max_score if vad and vad.interval_max_score is not None else "",
                "vad_activations": vad.activations if vad else "",
                "vad_endings": vad.endings if vad else "",
                "vad_max_inference_ms": vad.maximum_inference_ms if vad else "",
                "segmentation_silence_boundaries": (
                    segmentation.silence_boundaries if segmentation else ""
                ),
                "segmentation_punctuation_boundaries": (
                    segmentation.punctuation_boundaries if segmentation else ""
                ),
                "segmentation_short_pause_boundaries": (
                    segmentation.short_pause_boundaries if segmentation else ""
                ),
                "segmentation_maximum_duration_boundaries": (
                    segmentation.maximum_duration_boundaries if segmentation else ""
                ),
            }
            if self.metrics_writer is not None:
                try:
                    await asyncio.to_thread(self.metrics_writer.write, values)
                except OSError as error:
                    logger.warning(
                        "event=metrics_write_failed consequence=metrics_gap "
                        "action=retry_next_interval error=%s",
                        error,
                    )
            if vad is not None:
                vad.interval_max_score = None
            self._event_loop_maximum_lag_ms = 0.0

    async def _monitor_event_loop_lag(self) -> None:
        """Measure scheduling stalls that can starve realtime audio buffers."""
        interval_seconds = 0.02
        expected = time.monotonic() + interval_seconds
        while True:
            await asyncio.sleep(interval_seconds)
            now = time.monotonic()
            self._event_loop_maximum_lag_ms = max(
                self._event_loop_maximum_lag_ms,
                max(0.0, now - expected) * 1000,
            )
            expected = now + interval_seconds

    async def _drain_task(
        self,
        task: asyncio.Task[object] | None,
        timeout: float,
        name: str,
        caught_error: BaseException | None,
    ) -> BaseException | None:
        if task is None:
            return caught_error
        if not task.done():
            try:
                await asyncio.wait_for(task, timeout=timeout)
            except TimeoutError as error:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                logger.warning(
                    "event=worker_drain_timeout worker=%s timeout_seconds=%.0f action=cancelled",
                    name,
                    timeout,
                )
                return caught_error or error
            except BaseException as error:
                return caught_error or error
        elif caught_error is None:
            try:
                task.result()
            except BaseException as error:
                return error
        return caught_error

    def _reset_processing_audio(self) -> None:
        """Reset the shared 48-to-16 kHz stream and all consumers of its timeline."""
        if self._input_resampler is not None:
            self._input_resampler.reset()
        if self._voice_detector is not None:
            self._voice_detector.reset()
        if self._speech_segmenter is not None:
            self._speech_segmenter.reset()

    async def _load_voice_detector(
        self,
        backend_name: str,
        pipeline_settings: dict[str, object],
        processing_rate: int,
        frame_duration_ms: float,
    ) -> StreamingVoiceDetector:
        """Load the stateful VAD backend without delaying passthrough startup."""
        detector_load_started = time.perf_counter()
        try:
            voice_detector = await self.voice_detector_loader.load(
                self.config["classic_pipeline"]["voice_detection"],
                pipeline_settings,
                processing_rate,
                frame_duration_ms=frame_duration_ms,
            )
        except BaseException:
            raise
        detector_load_ms = (time.perf_counter() - detector_load_started) * 1000
        load_metrics = voice_detector.backend.load_metrics
        logger.info(
            "event=voice_detection_initialized backend=%s duration_ms=%.0f "
            "package_import_ms=%.0f model_load_ms=%.0f iterator_initialization_ms=%.0f",
            backend_name,
            detector_load_ms,
            load_metrics.package_import_ms,
            load_metrics.model_load_ms,
            load_metrics.iterator_initialization_ms,
        )
        return voice_detector

    def _effective_audio_mode(self, requested_mode: PipelineMode) -> PipelineMode:
        """Keep original audio flowing until translation prerequisites are ready."""
        if requested_mode is PipelineMode.TRANSLATE and self._voice_detector is None:
            return PipelineMode.PASSTHROUGH
        return requested_mode

    async def _wait_for_voice_detector_shutdown(
        self,
        task: asyncio.Task[object] | None,
    ) -> None:
        """Make an unavoidable native model load visible during shutdown."""
        if task is None:
            return
        if not task.done():
            logger.warning(
                "event=shutdown_waiting component=voice_detection "
                "reason=native_initialization_not_interruptible"
            )
        while not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except TimeoutError:
                logger.warning("event=shutdown_still_waiting component=voice_detection")
            except asyncio.CancelledError:
                raise
            except BaseException:
                break
        if task.done() and not task.cancelled():
            try:
                task.result()
            except BaseException:
                pass

    def _drop_if_stale(self, created_at: float, stage: str) -> bool:
        age = time.monotonic() - created_at
        if not self.latency_protection_enabled or age <= self.segment_discard_age_seconds:
            return False
        logger.warning(
            "event=stale_segment_dropped stage=%s age_ms=%.0f hard_limit_ms=%.0f",
            stage,
            age * 1000,
            self.segment_discard_age_seconds * 1000,
        )
        return True

    def _current_mode(self) -> PipelineMode:
        if self.control_state is None:
            return PipelineMode.TRANSLATE
        return self.control_state.get_mode(self.pipeline_name)

    def _should_record_audio(self) -> bool:
        if not self._audio_recording_enabled:
            return False
        if not self._call_session_active():
            return False
        if self.control_state is None:
            return self._current_mode() is PipelineMode.TRANSLATE
        return self.control_state.is_audio_recording_active_for(self.pipeline_name)

    def _call_session_active(self) -> bool:
        if self.device_manager is None:
            return True
        route = (
            "translated_output"
            if self.pipeline_name == "agent_to_remote"
            else "remote_input"
        )
        return self.device_manager.application_session_active(route) is True

    def _current_output_device(self, fallback: int) -> int:
        if self.device_manager is not None and self.pipeline_name == "remote_to_agent":
            return self.device_manager.active_index("output")
        if self.device_manager is not None and self.pipeline_name == "agent_to_remote":
            return self.device_manager.active_index("translated_output", fallback)
        return fallback

    async def _monitor_physical_input(
        self,
        capture: QueuedAudioInput,
        manager: AudioDeviceManager,
        resampling_config: dict[str, object],
        processing_rate: int,
        output_sample_rate: int,
    ) -> None:
        revision = manager.revision("input")
        switch_error: str | None = None
        while True:
            revision = await manager.wait_for_change("input", revision)
            while capture.input_device != manager.active_index("input"):
                target = manager.active_index("input")
                try:
                    await asyncio.to_thread(capture.switch_device, target)
                    self._input_resampler = create_resampler(
                        resampling_config,
                        capture.sample_rate,
                        processing_rate,
                        output_block_frames=calculate_block_frames(
                            processing_rate, capture.frame_duration_ms
                        ),
                    )
                    self._passthrough_resampler = create_resampler(
                        resampling_config,
                        capture.sample_rate,
                        output_sample_rate,
                        output_block_frames=calculate_block_frames(
                            output_sample_rate, capture.frame_duration_ms
                        ),
                    )
                    self._reset_processing_audio()
                    await manager.report_physical_device_healthy(
                        "input",
                        target,
                        sample_rate=capture.sample_rate,
                        channels=capture.channels,
                    )
                    if switch_error is not None:
                        logger.info(
                            "event=audio_device_switch_recovered direction=input device=%s",
                            target,
                        )
                        switch_error = None
                except asyncio.CancelledError:
                    raise
                except (AudioDeviceError, sd.PortAudioError, OSError, RuntimeError, ValueError) as error:
                    detail = str(error)
                    if detail != switch_error:
                        logger.warning(
                            "event=audio_device_switch_failed direction=input "
                            "action=retrying error=%s",
                            detail,
                        )
                        switch_error = detail
                    changed = await manager.report_physical_device_failed(
                        "input", target, error
                    )
                    if not changed:
                        await asyncio.sleep(1.0)
                    if manager.revision("input") != revision:
                        revision = manager.revision("input")

    async def _monitor_virtual_input(
        self,
        capture: QueuedAudioInput,
        manager: AudioDeviceManager,
    ) -> None:
        revision = manager.revision("remote_input")
        switch_error: str | None = None
        while True:
            revision = await manager.wait_for_change("remote_input", revision)
            while True:
                target = manager.active_index("remote_input", capture.input_device)
                if target == capture.input_device:
                    break
                try:
                    await asyncio.to_thread(capture.switch_device, target)
                    self._reset_processing_audio()
                    if switch_error is not None:
                        logger.info(
                            "event=audio_device_switch_recovered direction=remote_input device=%s",
                            target,
                        )
                        switch_error = None
                    break
                except asyncio.CancelledError:
                    raise
                except (AudioDeviceError, sd.PortAudioError, OSError, RuntimeError, ValueError) as error:
                    detail = str(error)
                    if detail != switch_error:
                        logger.warning(
                            "event=audio_device_switch_failed direction=remote_input "
                            "action=retrying error=%s",
                            detail,
                        )
                        switch_error = detail
                    await asyncio.sleep(1.0)
                    if manager.revision("remote_input") != revision:
                        revision = manager.revision("remote_input")

    async def _set_pipeline_status(self, status: str) -> None:
        if self.control_state is not None:
            await self.control_state.set_pipeline_status(self.pipeline_name, status)

    def _current_language(self, role: str) -> str:
        if self.control_state is None:
            if self.device_manager is not None:
                language = self.device_manager.participant_languages.get(role)
                if language is not None:
                    return language
            raise RuntimeError("Participant language preferences are not available.")
        return self.control_state.get_language(role)

    def _current_voice_gender(self) -> str:
        if self.control_state is None:
            return str(
                self._pipeline_section(self.pipeline_name).get("voice_gender", "male")
            )
        return self.control_state.get_voice_gender(self.pipeline_name).value

    def _discard_processing_backlog(self) -> None:
        """Remove queued translated work when switching to original audio."""
        discarded_transcripts = self._clear_queue(self.transcript_queue)
        discarded_translations = self._clear_queue(self.translation_queue)
        discarded_syntheses = self._clear_queue(self.playback_queue)
        self.statistics.dropped_transcripts += discarded_transcripts
        self.statistics.dropped_translations += discarded_translations
        self.statistics.dropped_syntheses += discarded_syntheses
        discarded = discarded_transcripts + discarded_translations + discarded_syntheses
        if discarded:
            logger.info(
                "event=processing_backlog_discarded reason=passthrough segments=%s",
                discarded,
            )

    @staticmethod
    def _clear_queue(queue: asyncio.Queue[object]) -> int:
        count = 0
        while not queue.empty():
            try:
                queue.get_nowait()
                count += 1
            except asyncio.QueueEmpty:
                break
        return count

    def _select_realtime_playback_item(
        self,
        item: TimedSynthesis,
    ) -> TimedSynthesis | None:
        """Keep fresh audio in order, but collapse a stale backlog to its newest item."""
        if not self.latency_protection_enabled:
            return item
        age = time.monotonic() - item.created_at
        if age <= self.playback_backlog_discard_age_seconds:
            return item

        backlog = [item]
        while not self.playback_queue.empty():
            backlog.append(self.playback_queue.get_nowait())
        superseded = max(
            0, len(backlog) - self.playback_backlog_segments_to_keep
        )
        if superseded:
            self.statistics.superseded_before_playback += superseded
            logger.warning(
                "event=playback_backlog_discarded segments=%s retained_segments=%s",
                superseded,
                self.playback_backlog_segments_to_keep,
            )
        retained = backlog[superseded:]
        item = retained[0]
        for queued_item in retained[1:]:
            self.playback_queue.put_nowait(queued_item)

        age = time.monotonic() - item.created_at
        if age > self.segment_discard_age_seconds:
            self.statistics.stale_before_playback += 1
            logger.warning(
                "event=stale_segment_dropped stage=playback age_ms=%.0f hard_limit_ms=%.0f",
                age * 1000,
                self.segment_discard_age_seconds * 1000,
            )
            return None

        self.statistics.recovered_playback += 1
        if age > self.playback_backlog_discard_age_seconds:
            logger.warning(
                "event=late_playback_recovered age_ms=%.0f soft_limit_ms=%.0f",
                age * 1000,
                self.playback_backlog_discard_age_seconds * 1000,
            )
        else:
            logger.info("event=playback_recovered age_ms=%.0f", age * 1000)
        return item

    def _section(self, name: str) -> dict[str, object]:
        section = self.config[name]
        if not isinstance(section, dict):
            raise ValueError(f"Configuration section {name!r} must be a mapping.")
        return section

    def _pipeline_section(self, name: str) -> dict[str, object]:
        pipelines = self._section("pipelines")
        section = pipelines[name]
        if not isinstance(section, dict):
            raise ValueError(f"Pipeline configuration {name!r} must be a mapping.")
        return section
