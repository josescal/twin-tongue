"""Configuration loading for Twin Tongue."""

import atexit
from contextvars import ContextVar, Token
from dataclasses import dataclass
import logging
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
from queue import Full, Queue
import sys
from threading import Lock, RLock
import time
import tomllib
from typing import Any, TextIO

from audio.resampling import validate_resampling_settings
from audio.speech_segmentation import validate_speech_segmentation_settings
from audio.voice_detection import validate_voice_detection_settings

LOG_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)s [%(pipeline)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_PIPELINE: ContextVar[str] = ContextVar("log_pipeline", default="app")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG_PATH = PROJECT_ROOT / "logs" / "twin-tongue.log"
DEFAULT_LOG_SIZE_MB = 5.0
DEFAULT_LOG_MAX_FILES = 5
DEFAULT_LOG_QUEUE_CAPACITY = 1_000
DEFAULT_LOG_FILE_FLUSH_INTERVAL_SECONDS = 2.0
LOGGING_SHUTDOWN_TIMEOUT_SECONDS = 5.0


class PipelineContextFilter(logging.Filter):
    """Attach the current asynchronous pipeline name to every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.pipeline = LOG_PIPELINE.get()
        return True


class ConfigurationError(Exception):
    """Raised when the application configuration cannot be used."""


class NonBlockingQueueHandler(QueueHandler):
    """Enqueue log records without ever waiting for a slow output handler."""

    def __init__(self, log_queue: Queue[logging.LogRecord]) -> None:
        super().__init__(log_queue)
        self._dropped_records = 0
        self._dropped_records_lock = Lock()

    @property
    def dropped_records(self) -> int:
        with self._dropped_records_lock:
            return self._dropped_records

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except Full:
            with self._dropped_records_lock:
                self._dropped_records += 1


class BufferedRotatingFileHandler(RotatingFileHandler):
    """Rotate logs normally while batching non-critical filesystem flushes."""

    def __init__(
        self,
        *args: object,
        flush_interval_seconds: float = DEFAULT_LOG_FILE_FLUSH_INTERVAL_SECONDS,
        **kwargs: object,
    ) -> None:
        self._flush_interval_seconds = flush_interval_seconds
        self._last_flush_monotonic = time.monotonic()
        super().__init__(*args, **kwargs)

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        if record.levelno >= logging.ERROR:
            RotatingFileHandler.flush(self)
            self._last_flush_monotonic = time.monotonic()

    def flush(self) -> None:
        now = time.monotonic()
        if (
            self._flush_interval_seconds == 0
            or now - self._last_flush_monotonic >= self._flush_interval_seconds
        ):
            RotatingFileHandler.flush(self)
            self._last_flush_monotonic = now

    def doRollover(self) -> None:
        RotatingFileHandler.flush(self)
        super().doRollover()
        self._last_flush_monotonic = time.monotonic()

    def close(self) -> None:
        RotatingFileHandler.flush(self)
        super().close()


class BoundedShutdownQueueListener(QueueListener):
    """Drain queued records during shutdown without waiting indefinitely."""

    def stop_with_timeout(self, timeout_seconds: float) -> bool:
        thread = self._thread
        if thread is None:
            return True
        deadline = time.monotonic() + timeout_seconds
        try:
            self.queue.put(self._sentinel, timeout=max(0.0, deadline - time.monotonic()))
        except Full:
            return False
        thread.join(max(0.0, deadline - time.monotonic()))
        if thread.is_alive():
            return False
        self._thread = None
        return True


@dataclass(frozen=True)
class LoggingRuntime:
    """Objects owned by one asynchronous logging configuration."""

    queue_handler: NonBlockingQueueHandler
    listener: BoundedShutdownQueueListener
    output_handlers: tuple[logging.Handler, ...]


_LOGGING_RUNTIME: LoggingRuntime | None = None
_LOGGING_RUNTIME_LOCK = RLock()


def configure_logging(
    level: str = "INFO",
    *,
    log_path: Path | str | None = DEFAULT_LOG_PATH,
    log_size_mb: float = DEFAULT_LOG_SIZE_MB,
    log_max_files: int = DEFAULT_LOG_MAX_FILES,
    log_queue_capacity: int = DEFAULT_LOG_QUEUE_CAPACITY,
    file_flush_interval_seconds: float = DEFAULT_LOG_FILE_FLUSH_INTERVAL_SECONDS,
    console_stream: TextIO | None = None,
) -> None:
    """Queue contextual logs for a dedicated console and file writer thread."""
    global _LOGGING_RUNTIME
    if log_size_mb <= 0:
        raise ValueError("Logging log_size_mb must be greater than zero.")
    if log_max_files < 2:
        raise ValueError("Logging log_max_files must be at least two.")
    if log_queue_capacity < 1:
        raise ValueError("Logging log_queue_capacity must be at least one.")
    if file_flush_interval_seconds < 0:
        raise ValueError("Logging file flush interval cannot be negative.")

    with _LOGGING_RUNTIME_LOCK:
        shutdown_logging()
        root_logger = logging.getLogger()
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
            handler.close()

        root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))
        formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
        output_handlers: list[logging.Handler] = []

        console_handler = logging.StreamHandler(console_stream or sys.stderr)
        console_handler.setFormatter(formatter)
        output_handlers.append(console_handler)

        file_error: OSError | None = None
        if log_path is not None:
            resolved_path = Path(log_path)
            try:
                resolved_path.parent.mkdir(parents=True, exist_ok=True)
                file_handler = BufferedRotatingFileHandler(
                    resolved_path,
                    maxBytes=int(log_size_mb * 1024 * 1024),
                    backupCount=log_max_files - 1,
                    encoding="utf-8",
                    flush_interval_seconds=file_flush_interval_seconds,
                )
            except OSError as error:
                file_error = error
            else:
                file_handler.setFormatter(formatter)
                output_handlers.append(file_handler)

        log_queue: Queue[logging.LogRecord] = Queue(maxsize=log_queue_capacity)
        queue_handler = NonBlockingQueueHandler(log_queue)
        queue_handler.addFilter(PipelineContextFilter())
        listener = BoundedShutdownQueueListener(log_queue, *output_handlers)
        listener.start()
        root_logger.addHandler(queue_handler)
        for logger_name in ("httpx", "httpcore", "websockets", "elevenlabs"):
            logging.getLogger(logger_name).setLevel(logging.WARNING)
        _LOGGING_RUNTIME = LoggingRuntime(
            queue_handler=queue_handler,
            listener=listener,
            output_handlers=tuple(output_handlers),
        )
        if file_error is not None:
            root_logger.warning(
                "event=file_logging_unavailable log_file=%s consequence=console_only error=%s",
                log_path,
                file_error,
            )


def get_dropped_log_count() -> int:
    """Return records discarded because the logging queue was full."""
    with _LOGGING_RUNTIME_LOCK:
        if _LOGGING_RUNTIME is None:
            return 0
        return _LOGGING_RUNTIME.queue_handler.dropped_records


def shutdown_logging(timeout_seconds: float = LOGGING_SHUTDOWN_TIMEOUT_SECONDS) -> int:
    """Stop accepting records, drain outputs within a limit, and close files."""
    if timeout_seconds <= 0:
        raise ValueError("Logging shutdown timeout must be greater than zero.")
    global _LOGGING_RUNTIME
    with _LOGGING_RUNTIME_LOCK:
        runtime = _LOGGING_RUNTIME
        if runtime is None:
            return 0
        _LOGGING_RUNTIME = None
        root_logger = logging.getLogger()
        root_logger.removeHandler(runtime.queue_handler)
        runtime.queue_handler.close()
        dropped_records = runtime.queue_handler.dropped_records
        stopped = runtime.listener.stop_with_timeout(timeout_seconds)
        if stopped:
            if dropped_records:
                record = logging.LogRecord(
                    "logging",
                    logging.WARNING,
                    __file__,
                    0,
                    "Discarded %s log record(s) because the logging queue was full",
                    (dropped_records,),
                    None,
                )
                record.pipeline = "app"
                for handler in runtime.output_handlers:
                    handler.handle(record)
            for handler in runtime.output_handlers:
                handler.close()
        else:
            try:
                sys.__stderr__.write(
                    "Logging shutdown timed out; some queued records may not have been written.\n"
                )
                sys.__stderr__.flush()
            except OSError:
                pass
        return dropped_records


atexit.register(shutdown_logging)


def set_log_pipeline(name: str) -> Token[str]:
    """Set the pipeline label inherited by child tasks and worker threads."""
    return LOG_PIPELINE.set(name)


REQUIRED_SECTIONS = (
    "audio",
    "voice_detection",
    "pipelines",
    "providers",
    "stt",
    "translation",
    "tts",
    "logging",
    "server",
)
REQUIRED_AUDIO_KEYS = (
    "processing_sample_rate",
    "sample_format",
    "frame_duration_ms",
    "device_poll_interval_seconds",
    "resampling",
)
REQUIRED_BARGE_IN_KEYS = ("enabled", "resume_delay_ms")
SUPPORTED_LANGUAGE_CODES = {"en", "es", "fr", "ca"}
REQUIRED_PIPELINE_KEYS = (
    "enabled",
    "mode",
    "voice_gender",
    "input_sample_rate",
    "input_channels",
    "output_channels",
    "output_sample_rate",
    "stt_max_audio_catchup_ms",
    "latency_protection",
    "voice_detection",
    "segmentation",
)
REQUIRED_LATENCY_PROTECTION_KEYS = (
    "enabled",
    "playback_backlog_discard_age_seconds",
    "segment_discard_age_seconds",
    "playback_backlog_segments_to_keep",
)
REQUIRED_SEGMENTATION_KEYS = (
    "minimum_segment_duration_ms",
    "preferred_segment_duration_ms",
    "short_pause_duration_ms",
    "partial_boundary_stability_ms",
    "maximum_segment_duration_ms",
)
REQUIRED_PROVIDER_KEYS = ("stt", "translation", "tts")
REQUIRED_STT_KEYS = (
    "model",
    "audio_format",
    "sample_rate",
    "include_timestamps",
    "no_verbatim",
    "keyterms",
    "send_chunk_duration_ms",
    "queue_capacity_blocks",
    "idle_keepalive_seconds",
    "audio_capture",
)
REQUIRED_STT_AUDIO_CAPTURE_KEYS = (
    "enabled",
    "directory",
    "max_seconds_per_file",
)
REQUIRED_TRANSLATION_KEYS = (
    "endpoint",
    "format",
)
REQUIRED_TTS_KEYS = (
    "model",
    "voice_id",
    "output_format",
    "sample_rate",
    "speed",
    "language_voices",
    "language_models",
)
REQUIRED_LOGGING_KEYS = (
    "level",
    "log_file",
    "log_size_mb",
    "log_max_files",
    "log_queue_capacity",
    "metrics_interval_seconds",
)
REQUIRED_SERVER_KEYS = ("enabled", "host", "port")


def load_config(path: Path) -> dict[str, Any]:
    """Load and minimally validate a TOML configuration file."""
    if not path.is_file():
        raise ConfigurationError(f"Configuration file not found: {path}")
    try:
        with path.open("rb") as config_file:
            config = tomllib.load(config_file)
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(f"Invalid TOML in configuration file: {error}") from error
    except OSError as error:
        raise ConfigurationError(f"Could not read configuration file: {error}") from error
    if not isinstance(config, dict):
        raise ConfigurationError("Configuration must contain a TOML mapping.")
    missing = [section for section in REQUIRED_SECTIONS if section not in config]
    if missing:
        raise ConfigurationError(f"Missing required configuration sections: {', '.join(missing)}")
    _require_keys(config["audio"], REQUIRED_AUDIO_KEYS, "audio")
    resampling = config["audio"]["resampling"]
    _require_keys(resampling, ("backend",), "audio.resampling")
    try:
        validate_resampling_settings(resampling)
    except ValueError as error:
        raise ConfigurationError(f"Invalid audio.resampling configuration: {error}") from error
    voice_detection = config["voice_detection"]
    try:
        validate_voice_detection_settings(voice_detection)
    except ValueError as error:
        raise ConfigurationError(f"Invalid voice_detection configuration: {error}") from error
    processing_rate = config["audio"]["processing_sample_rate"]
    if not isinstance(processing_rate, int) or isinstance(processing_rate, bool) or processing_rate <= 0:
        raise ConfigurationError("audio.processing_sample_rate must be a positive integer.")
    poll_interval = config["audio"]["device_poll_interval_seconds"]
    if (
        not isinstance(poll_interval, (int, float))
        or isinstance(poll_interval, bool)
        or poll_interval <= 0
    ):
        raise ConfigurationError(
            "audio.device_poll_interval_seconds must be greater than zero."
        )
    barge_in = config["pipelines"]["agent_to_remote"].get("barge_in")
    if not isinstance(barge_in, dict):
        raise ConfigurationError(
            "pipelines.agent_to_remote.barge_in must be a mapping."
        )
    _require_keys(
        barge_in,
        REQUIRED_BARGE_IN_KEYS,
        "pipelines.agent_to_remote.barge_in",
    )
    if not isinstance(barge_in["enabled"], bool):
        raise ConfigurationError(
            "pipelines.agent_to_remote.barge_in.enabled must be a boolean."
        )
    resume_delay_ms = barge_in["resume_delay_ms"]
    if (
        not isinstance(resume_delay_ms, (int, float))
        or isinstance(resume_delay_ms, bool)
        or resume_delay_ms < 0
    ):
        raise ConfigurationError(
            "pipelines.agent_to_remote.barge_in.resume_delay_ms must not be negative."
        )
    _require_keys(config["providers"], REQUIRED_PROVIDER_KEYS, "providers")
    _require_keys(config["stt"], REQUIRED_STT_KEYS, "stt")
    stt_rate = config["stt"]["sample_rate"]
    if stt_rate != processing_rate:
        raise ConfigurationError(
            "stt.sample_rate must match audio.processing_sample_rate."
        )
    if config["stt"]["audio_format"] != f"pcm_{stt_rate}":
        raise ConfigurationError(
            "stt.audio_format must be raw PCM matching stt.sample_rate."
        )
    for key in ("include_timestamps", "no_verbatim"):
        if not isinstance(config["stt"][key], bool):
            raise ConfigurationError(f"stt.{key} must be a boolean.")
    keyterms = config["stt"]["keyterms"]
    if not isinstance(keyterms, list):
        raise ConfigurationError("stt.keyterms must be a list of strings.")
    if len(keyterms) > 50:
        raise ConfigurationError("stt.keyterms must contain at most 50 terms.")
    for index, keyterm in enumerate(keyterms):
        if not isinstance(keyterm, str) or not keyterm.strip():
            raise ConfigurationError(f"stt.keyterms[{index}] must be a non-empty string.")
        if len(keyterm) > 20:
            raise ConfigurationError(
                f"stt.keyterms[{index}] must contain at most 20 characters."
            )
    send_chunk_duration_ms = config["stt"]["send_chunk_duration_ms"]
    if (
        not isinstance(send_chunk_duration_ms, int)
        or isinstance(send_chunk_duration_ms, bool)
        or not 20 <= send_chunk_duration_ms <= 1000
    ):
        raise ConfigurationError(
            "stt.send_chunk_duration_ms must be an integer between 20 and 1000."
        )
    stt_queue_capacity = config["stt"]["queue_capacity_blocks"]
    if (
        not isinstance(stt_queue_capacity, int)
        or isinstance(stt_queue_capacity, bool)
        or stt_queue_capacity < 1
    ):
        raise ConfigurationError("stt.queue_capacity_blocks must be at least one.")
    idle_keepalive = config["stt"].get("idle_keepalive_seconds", 5.0)
    if (
        not isinstance(idle_keepalive, (int, float))
        or isinstance(idle_keepalive, bool)
        or idle_keepalive < 0
    ):
        raise ConfigurationError("stt.idle_keepalive_seconds must not be negative.")
    audio_capture = config["stt"]["audio_capture"]
    _require_keys(audio_capture, REQUIRED_STT_AUDIO_CAPTURE_KEYS, "stt.audio_capture")
    if not isinstance(audio_capture["enabled"], bool):
        raise ConfigurationError("stt.audio_capture.enabled must be a boolean.")
    if (
        not isinstance(audio_capture["directory"], str)
        or not audio_capture["directory"].strip()
    ):
        raise ConfigurationError("stt.audio_capture.directory must be a non-empty path.")
    if (
        not isinstance(audio_capture["max_seconds_per_file"], (int, float))
        or isinstance(audio_capture["max_seconds_per_file"], bool)
        or audio_capture["max_seconds_per_file"] <= 0
    ):
        raise ConfigurationError(
            "stt.audio_capture.max_seconds_per_file must be greater than zero."
        )
    write_timing_marks = audio_capture.get("write_timing_marks", False)
    if not isinstance(write_timing_marks, bool):
        raise ConfigurationError(
            "stt.audio_capture.write_timing_marks must be a boolean."
        )
    write_buffer_kb = audio_capture.get("write_buffer_kb", 64)
    if (
        not isinstance(write_buffer_kb, int)
        or isinstance(write_buffer_kb, bool)
        or write_buffer_kb < 1
    ):
        raise ConfigurationError(
            "stt.audio_capture.write_buffer_kb must be an integer of at least one."
        )
    capture_flush_interval = audio_capture.get("flush_interval_seconds", 0.5)
    if (
        not isinstance(capture_flush_interval, (int, float))
        or isinstance(capture_flush_interval, bool)
        or capture_flush_interval <= 0
    ):
        raise ConfigurationError(
            "stt.audio_capture.flush_interval_seconds must be greater than zero."
        )
    _require_keys(config["translation"], REQUIRED_TRANSLATION_KEYS, "translation")
    _require_keys(config["tts"], REQUIRED_TTS_KEYS, "tts")
    tts_rate = config["tts"]["sample_rate"]
    if (
        not isinstance(tts_rate, int)
        or isinstance(tts_rate, bool)
        or tts_rate <= 0
    ):
        raise ConfigurationError("tts.sample_rate must be a positive integer.")
    if config["tts"]["output_format"] != f"pcm_{tts_rate}":
        raise ConfigurationError(
            "tts.output_format must be raw PCM matching tts.sample_rate."
        )
    language_voices = config["tts"]["language_voices"]
    if not isinstance(language_voices, dict):
        raise ConfigurationError("tts.language_voices must be a mapping.")
    for language, voices in language_voices.items():
        if language not in SUPPORTED_LANGUAGE_CODES:
            raise ConfigurationError(f"Unsupported TTS language voice override: {language}.")
        if not isinstance(voices, dict):
            raise ConfigurationError(
                f"tts.language_voices.{language} must be a mapping."
            )
        _require_keys(voices, ("male", "female"), f"tts.language_voices.{language}")
        for gender, voice_id in voices.items():
            if gender not in {"male", "female"}:
                raise ConfigurationError(
                    f"Unsupported TTS voice gender override: {language}.{gender}."
                )
            if not isinstance(voice_id, str) or not voice_id.strip():
                raise ConfigurationError(
                    f"tts.language_voices.{language}.{gender} must be a non-empty voice ID."
                )
    language_models = config["tts"]["language_models"]
    if not isinstance(language_models, dict):
        raise ConfigurationError("tts.language_models must be a mapping.")
    for language, model in language_models.items():
        if language not in SUPPORTED_LANGUAGE_CODES:
            raise ConfigurationError(f"Unsupported TTS language model override: {language}.")
        if not isinstance(model, str) or not model.strip():
            raise ConfigurationError(
                f"tts.language_models.{language} must be a non-empty model name."
            )
    _require_keys(config["logging"], REQUIRED_LOGGING_KEYS, "logging")
    logging_config = config["logging"]
    file_logging_enabled = logging_config.get("file_enabled", True)
    if not isinstance(file_logging_enabled, bool):
        raise ConfigurationError("logging.file_enabled must be a boolean.")
    metrics_enabled = logging_config.get("metrics_enabled", True)
    if not isinstance(metrics_enabled, bool):
        raise ConfigurationError("logging.metrics_enabled must be a boolean.")
    if (
        not isinstance(logging_config["log_file"], str)
        or not logging_config["log_file"].strip()
    ):
        raise ConfigurationError("logging.log_file must be a non-empty path.")
    metrics_file = logging_config.get("metrics_file", "logs/metrics.csv")
    if not isinstance(metrics_file, str) or not metrics_file.strip():
        raise ConfigurationError("logging.metrics_file must be a non-empty path.")
    metrics_retention_days = logging_config.get("metrics_retention_days", 14)
    if (
        not isinstance(metrics_retention_days, int)
        or isinstance(metrics_retention_days, bool)
        or metrics_retention_days < 1
    ):
        raise ConfigurationError("logging.metrics_retention_days must be at least one.")
    if (
        not isinstance(logging_config["log_size_mb"], (int, float))
        or isinstance(logging_config["log_size_mb"], bool)
        or logging_config["log_size_mb"] <= 0
    ):
        raise ConfigurationError("logging.log_size_mb must be greater than zero.")
    if (
        not isinstance(logging_config["log_max_files"], int)
        or isinstance(logging_config["log_max_files"], bool)
        or logging_config["log_max_files"] < 2
    ):
        raise ConfigurationError("logging.log_max_files must be at least two.")
    if (
        not isinstance(logging_config["log_queue_capacity"], int)
        or isinstance(logging_config["log_queue_capacity"], bool)
        or logging_config["log_queue_capacity"] < 1
    ):
        raise ConfigurationError("logging.log_queue_capacity must be at least one.")
    file_flush_interval = logging_config.get("file_flush_interval_seconds", 2.0)
    if (
        not isinstance(file_flush_interval, (int, float))
        or isinstance(file_flush_interval, bool)
        or file_flush_interval < 0
    ):
        raise ConfigurationError(
            "logging.file_flush_interval_seconds must not be negative."
        )
    _require_keys(config["server"], REQUIRED_SERVER_KEYS, "server")
    if not isinstance(config["pipelines"], dict):
        raise ConfigurationError("Configuration section 'pipelines' must be a mapping.")
    for name in ("remote_to_agent", "agent_to_remote"):
        if name not in config["pipelines"]:
            raise ConfigurationError(f"Missing required pipeline configuration: {name}")
        _require_keys(config["pipelines"][name], REQUIRED_PIPELINE_KEYS, f"pipelines.{name}")
        if config["pipelines"][name]["mode"] not in {"translate", "passthrough"}:
            raise ConfigurationError(
                f"pipelines.{name}.mode must be 'translate' or 'passthrough'."
            )
        if config["pipelines"][name]["voice_gender"] not in {"male", "female"}:
            raise ConfigurationError(
                f"pipelines.{name}.voice_gender must be 'male' or 'female'."
            )
        output_rate = config["pipelines"][name]["output_sample_rate"]
        if not isinstance(output_rate, int) or isinstance(output_rate, bool) or output_rate <= 0:
            raise ConfigurationError(
                f"pipelines.{name}.output_sample_rate must be a positive integer."
            )
        input_rate = config["pipelines"][name]["input_sample_rate"]
        if not isinstance(input_rate, int) or isinstance(input_rate, bool) or input_rate <= 0:
            raise ConfigurationError(
                f"pipelines.{name}.input_sample_rate must be a positive integer."
            )
        input_channels = config["pipelines"][name]["input_channels"]
        if input_channels not in (1, 2) or isinstance(input_channels, bool):
            raise ConfigurationError(
                f"pipelines.{name}.input_channels must be 1 or 2."
            )
        latency_protection = config["pipelines"][name]["latency_protection"]
        _require_keys(
            latency_protection,
            REQUIRED_LATENCY_PROTECTION_KEYS,
            f"pipelines.{name}.latency_protection",
        )
        if not isinstance(latency_protection["enabled"], bool):
            raise ConfigurationError(
                f"pipelines.{name}.latency_protection.enabled must be a boolean."
            )
        discard_age = latency_protection["playback_backlog_discard_age_seconds"]
        segment_discard_age = latency_protection["segment_discard_age_seconds"]
        for key, value in (
            ("playback_backlog_discard_age_seconds", discard_age),
            ("segment_discard_age_seconds", segment_discard_age),
        ):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or value <= 0
            ):
                raise ConfigurationError(
                    f"pipelines.{name}.latency_protection.{key} must be greater than zero."
                )
        if segment_discard_age < discard_age:
            raise ConfigurationError(
                f"pipelines.{name}.latency_protection.segment_discard_age_seconds "
                "must be greater than or equal to playback_backlog_discard_age_seconds."
            )
        segments_to_keep = latency_protection["playback_backlog_segments_to_keep"]
        if (
            not isinstance(segments_to_keep, int)
            or isinstance(segments_to_keep, bool)
            or segments_to_keep < 1
        ):
            raise ConfigurationError(
                f"pipelines.{name}.latency_protection.playback_backlog_segments_to_keep "
                "must be at least one."
            )
    for name in ("remote_to_agent", "agent_to_remote"):
        pipeline = config["pipelines"][name]
        try:
            validate_voice_detection_settings(
                voice_detection,
                pipeline["voice_detection"],
            )
        except ValueError as error:
            raise ConfigurationError(
                f"Invalid pipelines.{name}.voice_detection configuration: {error}"
            ) from error
        segmentation = pipeline["segmentation"]
        _require_keys(
            segmentation,
            REQUIRED_SEGMENTATION_KEYS,
            f"pipelines.{name}.segmentation",
        )
        try:
            validate_speech_segmentation_settings(
                segmentation,
                voice_end_silence_duration_ms=float(
                    pipeline["voice_detection"]["min_silence_duration_ms"]
                ),
            )
        except ValueError as error:
            raise ConfigurationError(
                f"Invalid pipelines.{name}.segmentation configuration: {error}"
            ) from error
    server = config["server"]
    if not isinstance(server["enabled"], bool):
        raise ConfigurationError("server.enabled must be a boolean.")
    if not isinstance(server["host"], str) or not server["host"].strip():
        raise ConfigurationError("server.host must be a non-empty string.")
    if (
        not isinstance(server["port"], int)
        or isinstance(server["port"], bool)
        or not 0 <= server["port"] <= 65535
    ):
        raise ConfigurationError("server.port must be between 0 and 65535.")
    return config


def _require_keys(value: Any, keys: tuple[str, ...], section: str) -> None:
    if not isinstance(value, dict):
        raise ConfigurationError(f"Configuration section '{section}' must be a mapping.")
    missing = [key for key in keys if key not in value]
    if missing:
        raise ConfigurationError(f"Missing required properties in '{section}': {', '.join(missing)}")
