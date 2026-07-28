"""Manual realtime Speech-to-Text diagnostic using ElevenLabs."""

import argparse
import asyncio
from collections.abc import Callable
from datetime import datetime
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
import sounddevice as sd

from audio.capture import QueuedAudioInput
from audio.portaudio import AudioDeviceError, format_device, resolve_device, validate_input_settings
from audio.pcm import calculate_block_frames, convert_int16_channels
from audio.resampling import StreamingPcmInt16Resampler, create_resampler
from audio.speech_segmentation import (
    AdaptiveSpeechSegmenter,
    create_speech_segmenter,
    validate_speech_segmentation_settings,
)
from audio.voice_detection import (
    StreamingVoiceDetector,
    create_voice_detector,
    preload_voice_detection_package,
)
from config import ConfigurationError, configure_logging, load_config
from preferences import UserPreferences
from providers.elevenlabs_stt import ElevenLabsRealtimeSTT
from providers.stt import (
    RealtimeTranscript,
    STTConfigurationError,
    STTError,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE_URL = "https://api.elevenlabs.io"
MICROPHONE_WARMUP_SECONDS = 0.5


class TranscriptConsole:
    """Present partial transcripts in place and final transcripts permanently."""

    def __init__(self) -> None:
        self.partial_width = 0

    def show_partial(self, text: str) -> None:
        """Replace the current partial transcript line."""
        line = f"{_console_timestamp()} [PARTIAL] {text}"
        self.partial_width = max(self.partial_width, len(line))
        print(f"\r{line.ljust(self.partial_width)}", end="", flush=True)

    def show_final(self, transcript: RealtimeTranscript) -> None:
        """Clear the partial line and retain one committed transcript line."""
        self.clear_partial()
        if transcript.start_seconds is not None and transcript.end_seconds is not None:
            label = f"[FINAL {_format_audio_time(transcript.start_seconds)}-{_format_audio_time(transcript.end_seconds)}]"
        else:
            label = "[FINAL]"
        logging.info("%s %s", label, transcript.text)

    def clear_partial(self) -> None:
        """Clear the active partial line if present."""
        if self.partial_width:
            print(f"\r{' ' * self.partial_width}\r", end="", flush=True)
            self.partial_width = 0


def parse_args(
    stt_config: dict[str, object],
    audio_config: dict[str, object],
    languages_config: dict[str, object],
    agent_pipeline_config: dict[str, object],
) -> argparse.Namespace:
    """Parse realtime STT diagnostic arguments."""
    parser = argparse.ArgumentParser(description="Stream captured PCM audio to ElevenLabs Realtime STT.")
    parser.add_argument("--input-device", required=True, help="Input device identifier or unique name fragment.")
    parser.add_argument("--duration", type=float, default=60.0, help="Capture duration in seconds (default: 60).")
    parser.add_argument("--sample-rate", type=int, default=agent_pipeline_config["input_sample_rate"])
    parser.add_argument(
        "--channels",
        type=int,
        default=agent_pipeline_config["input_channels"],
        help="Capture channels: 1 or 2.",
    )
    parser.add_argument("--dtype", default=audio_config["sample_format"])
    parser.add_argument("--frame-duration-ms", type=float, default=audio_config["frame_duration_ms"])
    parser.add_argument("--language", default=languages_config["remote"])
    classic_config = agent_pipeline_config["classic"]
    assert isinstance(classic_config, dict)
    voice_detection_config = classic_config["voice_detection"]
    segmentation_config = classic_config["segmentation"]
    assert isinstance(voice_detection_config, dict)
    assert isinstance(segmentation_config, dict)
    parser.add_argument(
        "--voice-threshold",
        type=float,
        default=voice_detection_config["threshold"],
    )
    parser.add_argument(
        "--voice-min-silence-ms",
        type=float,
        default=voice_detection_config["min_silence_duration_ms"],
    )
    parser.add_argument(
        "--voice-preroll-ms",
        type=float,
        default=voice_detection_config["speech_pad_ms"],
    )
    parser.add_argument(
        "--segment-minimum-duration-ms",
        type=float,
        default=segmentation_config["minimum_segment_duration_ms"],
    )
    parser.add_argument(
        "--segment-preferred-duration-ms",
        type=float,
        default=segmentation_config["preferred_segment_duration_ms"],
    )
    parser.add_argument(
        "--segment-short-pause-ms",
        type=float,
        default=segmentation_config["short_pause_duration_ms"],
    )
    parser.add_argument(
        "--segment-partial-stability-ms",
        type=float,
        default=segmentation_config["partial_boundary_stability_ms"],
    )
    parser.add_argument(
        "--segment-maximum-duration-ms",
        type=float,
        default=segmentation_config["maximum_segment_duration_ms"],
    )
    parser.add_argument(
        "--queue-capacity-blocks",
        type=int,
        default=stt_config["queue_capacity_blocks"],
    )
    parser.add_argument(
        "--include-timestamps",
        action=argparse.BooleanOptionalAction,
        default=stt_config["include_timestamps"],
    )
    parser.add_argument("--yes", action="store_true", help="Start without interactive confirmation.")
    return parser.parse_args()


async def run_realtime_stt(
    args: argparse.Namespace,
    config: dict[str, object],
    api_key: str,
    base_url: str,
    on_final: Callable[[RealtimeTranscript], None] | None = None,
) -> None:
    """Coordinate capture, PCM conversion, SDK streaming, and shutdown."""
    stt_config = config["stt"]
    audio_config = config["audio"]
    classic_pipeline_config = config["classic_pipeline"]
    assert isinstance(stt_config, dict)
    assert isinstance(audio_config, dict)
    assert isinstance(classic_pipeline_config, dict)
    _validate_arguments(args, stt_config)
    input_identifier = resolve_device(args.input_device, "input")
    validate_input_settings(input_identifier, args.sample_rate, args.channels, args.dtype)
    _show_configuration(args, stt_config, input_identifier)
    if not args.yes:
        try:
            input("Press Enter to start or Ctrl+C to cancel. ")
        except EOFError as error:
            raise STTConfigurationError(
                "Interactive confirmation is unavailable; run again with --yes."
            ) from error

    transcript_console = TranscriptConsole()
    voice_detection_settings = {
        "threshold": args.voice_threshold,
        "min_silence_duration_ms": args.voice_min_silence_ms,
        "preroll_ms": args.voice_preroll_ms,
    }
    voice_detector = await asyncio.to_thread(
        create_voice_detector,
        classic_pipeline_config["voice_detection"],
        voice_detection_settings,
        int(stt_config["sample_rate"]),
        frame_duration_ms=args.frame_duration_ms,
    )
    speech_segmenter = create_speech_segmenter(
        {
            "minimum_segment_duration_ms": args.segment_minimum_duration_ms,
            "preferred_segment_duration_ms": args.segment_preferred_duration_ms,
            "short_pause_duration_ms": args.segment_short_pause_ms,
            "partial_boundary_stability_ms": args.segment_partial_stability_ms,
            "maximum_segment_duration_ms": args.segment_maximum_duration_ms,
        },
        frame_duration_ms=args.frame_duration_ms,
    )
    resampler = create_resampler(
        dict(audio_config["resampling"]),
        args.sample_rate,
        int(stt_config["sample_rate"]),
        output_block_frames=calculate_block_frames(
            int(stt_config["sample_rate"]),
            args.frame_duration_ms,
        ),
    )

    def handle_partial(text: str) -> None:
        speech_segmenter.observe_partial(text)
        transcript_console.show_partial(text)

    def handle_final(transcript: RealtimeTranscript) -> None:
        transcript_console.show_final(transcript)
        if on_final is not None:
            on_final(transcript)

    provider = ElevenLabsRealtimeSTT(
        api_key=api_key,
        base_url=base_url,
        model=str(stt_config["model"]),
        audio_format=str(stt_config["audio_format"]),
        sample_rate=int(stt_config["sample_rate"]),
        language=args.language,
        include_timestamps=args.include_timestamps,
        reconnect_on_close=True,
        on_partial=handle_partial,
        on_final=handle_final,
    )
    capture = QueuedAudioInput(
        input_device=input_identifier,
        sample_rate=args.sample_rate,
        channels=args.channels,
        dtype=args.dtype,
        frame_duration_ms=args.frame_duration_ms,
        queue_capacity_blocks=args.queue_capacity_blocks,
    )
    sender_stop = asyncio.Event()
    sender_task: asyncio.Task[None] | None = None
    duration_task: asyncio.Task[None] | None = None
    error_task: asyncio.Task[bool] | None = None
    caught_error: BaseException | None = None

    try:
        await provider.connect()
        capture.start()
        logging.info(
            "Microphone warm-up for %.0f ms; wait for the READY message before speaking",
            MICROPHONE_WARMUP_SECONDS * 1000,
        )
        sender_task = asyncio.create_task(
            _send_audio_blocks(
                capture,
                provider,
                args.channels,
                sender_stop,
                voice_detector,
                speech_segmenter,
                resampler,
            )
        )
        error_task = asyncio.create_task(provider.error_event.wait())
        await asyncio.sleep(MICROPHONE_WARMUP_SECONDS)
        if sender_task.done():
            sender_task.result()
            raise STTError("Realtime audio sender stopped during microphone warm-up.")
        if provider.last_error is not None:
            raise provider.last_error
        resampler.reset()
        voice_detector.reset()
        speech_segmenter.reset()
        logging.info("[READY] Puedes empezar a hablar ahora")
        logging.info(
            "Capturing and transcribing audio for %g seconds. Press Ctrl+C to stop.",
            args.duration,
        )
        duration_task = asyncio.create_task(asyncio.sleep(args.duration))
        wait_tasks = {sender_task, duration_task, error_task}
        completed, _ = await asyncio.wait(
            wait_tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if sender_task in completed:
            sender_task.result()
            raise STTError("Realtime audio sender stopped unexpectedly.")
        if error_task in completed and provider.last_error is not None:
            raise provider.last_error
    except BaseException as error:
        caught_error = error
    finally:
        try:
            capture.stop()
        except BaseException as error:
            if caught_error is None:
                caught_error = error
        sender_stop.set()
        if sender_task is not None:
            if provider.last_error is not None:
                sender_task.cancel()
                await asyncio.gather(sender_task, return_exceptions=True)
            else:
                try:
                    await asyncio.wait_for(sender_task, timeout=2)
                except TimeoutError:
                    sender_task.cancel()
                    await asyncio.gather(sender_task, return_exceptions=True)
                    logging.warning("Timed out while draining captured audio blocks")
                except BaseException as error:
                    if caught_error is None:
                        caught_error = error
        if provider.connection is not None:
            try:
                if provider.last_error is None and provider.statistics.sent_blocks:
                    await provider.commit_final()
                    await asyncio.sleep(1.5)
                await provider.close()
            except BaseException as error:
                if caught_error is None:
                    caught_error = error
        tasks_to_cancel = [
            task
            for task in (duration_task, error_task)
            if task is not None and not task.done()
        ]
        for task in tasks_to_cancel:
            task.cancel()
        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
        if caught_error is None and provider.last_error is not None:
            caught_error = provider.last_error
        transcript_console.clear_partial()
        _show_summary(capture, provider, voice_detector)

    if caught_error is not None:
        raise caught_error


async def _send_audio_blocks(
    capture: QueuedAudioInput,
    provider: ElevenLabsRealtimeSTT,
    channels: int,
    stop_event: asyncio.Event,
    voice_detector: StreamingVoiceDetector,
    speech_segmenter: AdaptiveSpeechSegmenter,
    resampler: StreamingPcmInt16Resampler,
) -> None:
    while not stop_event.is_set() or capture.has_pending_blocks():
        block = capture.pop_block()
        if block is None:
            await asyncio.sleep(0.005)
            continue
        mono_pcm = convert_int16_channels(block, channels, 1)
        for processed_mono in resampler.process(mono_pcm):
            detection = voice_detector.process(processed_mono)
            blocks = detection.forwarded_blocks
            for forwarded_block in blocks:
                if provider.connection is None and provider.reconnect_on_close:
                    logging.info("Local speech detected; reconnecting ElevenLabs Realtime STT")
                    await provider.connect()
                await provider.send_audio(forwarded_block)
            segmentation = speech_segmenter.process(detection)
            if segmentation.should_commit:
                await provider.commit_final()


def _validate_arguments(args: argparse.Namespace, stt_config: dict[str, object]) -> None:
    if args.duration <= 0:
        raise STTConfigurationError("Duration must be greater than zero.")
    stt_rate = stt_config["sample_rate"]
    if stt_config["audio_format"] != f"pcm_{stt_rate}":
        raise STTConfigurationError(
            "STT audio_format must be raw PCM matching stt.sample_rate."
        )
    if args.sample_rate <= 0:
        raise STTConfigurationError("Capture sample rate must be greater than zero.")
    if args.channels not in (1, 2):
        raise STTConfigurationError("Realtime STT capture supports only one or two channels.")
    if args.dtype != "int16":
        raise STTConfigurationError("Realtime STT capture supports only PCM int16.")
    if args.frame_duration_ms <= 0:
        raise STTConfigurationError("Frame duration must be greater than zero.")
    if args.queue_capacity_blocks <= 0:
        raise STTConfigurationError("Queue capacity must be greater than zero.")
    if not 0 <= args.voice_threshold <= 1:
        raise STTConfigurationError("Voice detection threshold must be between 0 and 1.")
    if args.voice_min_silence_ms < 0 or args.voice_preroll_ms < 0:
        raise STTConfigurationError(
            "Voice detection timings are invalid."
        )
    try:
        validate_speech_segmentation_settings(
            {
                "minimum_segment_duration_ms": args.segment_minimum_duration_ms,
                "preferred_segment_duration_ms": args.segment_preferred_duration_ms,
                "short_pause_duration_ms": args.segment_short_pause_ms,
                "partial_boundary_stability_ms": args.segment_partial_stability_ms,
                "maximum_segment_duration_ms": args.segment_maximum_duration_ms,
            },
            voice_end_silence_duration_ms=args.voice_min_silence_ms,
        )
    except ValueError as error:
        raise STTConfigurationError(str(error)) from error


def _show_configuration(
    args: argparse.Namespace,
    stt_config: dict[str, object],
    input_identifier: int,
) -> None:
    logging.info("ElevenLabs Realtime STT test")
    logging.info("Input device:        %s", format_device(input_identifier))
    logging.info("Capture format:      PCM %s", args.dtype)
    logging.info("Capture sample rate: %s Hz", args.sample_rate)
    logging.info("Capture channels:    %s", args.channels)
    logging.info("STT channels:        1")
    logging.info("STT sample rate:     %s Hz", stt_config["sample_rate"])
    logging.info("STT audio format:    %s", stt_config["audio_format"])
    logging.info("Language:            %s", args.language)
    logging.info("Model:               %s", stt_config["model"])
    logging.info("Voice detection:     enabled")
    logging.info("VAD threshold:       %.2f", args.voice_threshold)
    logging.info("VAD minimum silence: %g ms", args.voice_min_silence_ms)
    logging.info("VAD pre-roll:        %g ms", args.voice_preroll_ms)
    logging.info("Segment minimum:     %g ms", args.segment_minimum_duration_ms)
    logging.info("Segment preferred:   %g ms", args.segment_preferred_duration_ms)
    logging.info("Segment short pause: %g ms", args.segment_short_pause_ms)
    logging.info("Partial stability:   %g ms", args.segment_partial_stability_ms)
    logging.info("Segment maximum:     %g ms", args.segment_maximum_duration_ms)
    logging.info("Duration:            %g seconds", args.duration)
    logging.info("Audio will be sent to ElevenLabs for transcription.")


def _show_summary(
    capture: QueuedAudioInput,
    provider: ElevenLabsRealtimeSTT,
    voice_detector: StreamingVoiceDetector | None,
) -> None:
    capture_stats = capture.statistics
    provider_stats = provider.statistics
    logging.info("Realtime STT summary:")
    logging.info("  Captured blocks:       %s", capture_stats.captured_blocks)
    logging.info("  Sent blocks:           %s", provider_stats.sent_blocks)
    logging.info("  Dropped blocks:        %s", capture_stats.dropped_blocks)
    logging.info("  Input overflows:       %s", capture_stats.input_overflows)
    logging.info("  Captured PCM bytes:    %s", capture_stats.captured_pcm_bytes)
    logging.info("  Sent mono PCM bytes:   %s", provider_stats.sent_mono_pcm_bytes)
    logging.info("  Partial transcripts:   %s", provider_stats.partial_transcripts)
    logging.info("  Final transcripts:     %s", provider_stats.final_transcripts)
    logging.info("  Connection errors:     %s", provider_stats.connection_errors)
    logging.info("  Idle disconnects:      %s", provider_stats.idle_disconnects)
    logging.info("  Reconnections:         %s", provider_stats.reconnections)
    if voice_detector is not None:
        gate_stats = voice_detector.statistics
        logging.info("  VAD activations:       %s", gate_stats.activations)


def _load_environment() -> tuple[str, str]:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    if not api_key:
        raise STTConfigurationError(
            "ELEVENLABS_API_KEY is missing. Add it to .env or the process environment."
        )
    base_url = os.getenv("ELEVENLABS_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
    parsed_url = urlparse(base_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise STTConfigurationError(
            "ELEVENLABS_BASE_URL must be a valid HTTP or HTTPS URL."
        )
    return api_key, base_url


def _console_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _format_audio_time(seconds: float) -> str:
    minutes = int(seconds // 60)
    remaining_seconds = seconds - minutes * 60
    return f"{minutes:02d}:{remaining_seconds:05.2f}"


def main() -> int:
    """Load configuration and run the isolated realtime STT diagnostic."""
    configure_logging()
    try:
        config = load_config(PROJECT_ROOT / "config" / "default.toml")
        pipelines = config["pipelines"]
        assert isinstance(pipelines, dict)
        agent_pipeline = pipelines["agent_to_remote"]
        assert isinstance(agent_pipeline, dict)
        languages = UserPreferences(
            PROJECT_ROOT / "config" / "preferences.json"
        ).participant_languages
        args = parse_args(
            config["stt"],
            config["audio"],
            languages,
            agent_pipeline,
        )
        classic_pipeline = config["classic_pipeline"]
        assert isinstance(classic_pipeline, dict)
        voice_detection = classic_pipeline["voice_detection"]
        assert isinstance(voice_detection, dict)
        preload_voice_detection_package(voice_detection)
        api_key, base_url = _load_environment()
        asyncio.run(run_realtime_stt(args, config, api_key, base_url))
    except KeyboardInterrupt:
        logging.warning("Realtime STT test interrupted by the user.")
        return 130
    except (ConfigurationError, STTError, AudioDeviceError, sd.PortAudioError, OSError, ValueError) as error:
        logging.error("Realtime STT test failed: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
