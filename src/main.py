"""Twin Tongue application entry point."""

import argparse
import asyncio
import logging
from pathlib import Path
import signal
import sys

import sounddevice as sd

from audio.device_manager import AudioDeviceManager
from audio.portaudio import AudioDeviceError
from audio.voice_detection import VoiceDetectorLoader, preload_voice_detection_package
from config import ConfigurationError, configure_logging, load_config, shutdown_logging
from app_state import ApplicationState
from metrics import CsvMetricsWriter
from twin_tongue_version import __version__
from pipelines import AgentToRemotePipeline, RemoteToAgentPipeline
from providers.errors import ProviderError
from providers.factory import ProviderFactory
from ui import LocalControlServer

PROJECT_ROOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent.parent
)


def parse_args() -> argparse.Namespace:
    """Parse production pipeline overrides."""
    parser = argparse.ArgumentParser(
        description="Run one or both Twin Tongue realtime dubbing directions."
    )
    parser.add_argument(
        "--pipeline",
        choices=("both", "remote_to_agent", "agent_to_remote"),
        default="both",
        help="Pipeline direction to run (default: both).",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0,
        help="Run duration in seconds; 0 runs until Ctrl+C (default: 0).",
    )
    parser.add_argument(
        "--web",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable the local control page (configuration default is used if omitted).",
    )
    parser.add_argument("--web-host", help="Override server.host (loopback addresses only).")
    parser.add_argument("--web-port", type=int, help="Override server.port.")
    return parser.parse_args()


async def run_application(args: argparse.Namespace) -> None:
    """Load configuration and run the selected pipeline directions."""
    config = load_config(PROJECT_ROOT / "config" / "default.toml")
    logging_settings = config["logging"]
    assert isinstance(logging_settings, dict)
    configured_log_path = Path(str(logging_settings["log_file"]))
    if not configured_log_path.is_absolute():
        configured_log_path = PROJECT_ROOT / configured_log_path
    configure_logging(
        str(logging_settings["level"]),
        log_path=configured_log_path,
        log_size_mb=float(logging_settings["log_size_mb"]),
        log_max_files=int(logging_settings["log_max_files"]),
        log_queue_capacity=int(logging_settings["log_queue_capacity"]),
    )
    configured_metrics_path = Path(
        str(logging_settings.get("metrics_file", "logs/metrics.csv"))
    )
    if not configured_metrics_path.is_absolute():
        configured_metrics_path = PROJECT_ROOT / configured_metrics_path
    metrics_writer = CsvMetricsWriter(
        configured_metrics_path,
        retention_days=int(logging_settings.get("metrics_retention_days", 14)),
    )
    if args.duration < 0:
        raise ConfigurationError("Duration must not be negative.")
    duration = None if args.duration == 0 else args.duration
    pipelines = config["pipelines"]
    server_settings = config["server"]
    assert isinstance(pipelines, dict)
    assert isinstance(server_settings, dict)
    remote_settings = pipelines["remote_to_agent"]
    agent_settings = pipelines["agent_to_remote"]
    assert isinstance(remote_settings, dict)
    assert isinstance(agent_settings, dict)
    selected_names = (
        ("remote_to_agent", "agent_to_remote")
        if args.pipeline == "both"
        else (args.pipeline,)
    )
    for name in selected_names:
        settings = remote_settings if name == "remote_to_agent" else agent_settings
        if not bool(settings["enabled"]):
            raise ConfigurationError(f"pipelines.{name} must be enabled.")
    audio_settings = config["audio"]
    assert isinstance(audio_settings, dict)
    device_manager = AudioDeviceManager(
        PROJECT_ROOT / "config" / "audio-device-preferences.json",
        poll_interval_seconds=float(
            audio_settings.get("device_poll_interval_seconds", 1.5)
        ),
        default_voice_gender=str(config["tts"]["voice_gender"]),
    )
    try:
        control_state = ApplicationState(
            initial_modes={
                "remote_to_agent": str(remote_settings["mode"]),
                "agent_to_remote": str(agent_settings["mode"]),
            },
            initial_languages={
                "agent": str(config["languages"]["agent"]),
                "remote": str(config["languages"]["remote"]),
            },
            initial_voice_gender=device_manager.voice_gender,
            active_pipelines=selected_names,
        )
    except ValueError as error:
        raise ConfigurationError(str(error)) from error
    control_state.attach_device_manager(device_manager)
    provider_factory = ProviderFactory.from_environment(
        config,
        PROJECT_ROOT / ".env",
    )
    logger = logging.getLogger(__name__)
    logger.info(
        "event=application_started version=%s pipelines=%s config=%s log_file=%s",
        __version__,
        ",".join(selected_names),
        PROJECT_ROOT / "config" / "default.toml",
        configured_log_path,
    )
    voice_detection_settings = config["voice_detection"]
    assert isinstance(voice_detection_settings, dict)
    preload_voice_detection_package(voice_detection_settings)
    startup_barrier = asyncio.Barrier(2) if args.pipeline == "both" else None
    voice_detector_loader = VoiceDetectorLoader()
    selected_pipelines: dict[str, RemoteToAgentPipeline] = {}
    if "remote_to_agent" in selected_names:
        selected_pipelines["remote_to_agent"] = RemoteToAgentPipeline(
            config=config,
            provider_factory=provider_factory,
            voice_detector_loader=voice_detector_loader,
            input_device=remote_settings["input_device"],
            output_device=remote_settings["output_device"],
            duration=duration,
            startup_barrier=startup_barrier,
            control_state=control_state,
            device_manager=device_manager,
            metrics_writer=metrics_writer,
        )
    if "agent_to_remote" in selected_names:
        selected_pipelines["agent_to_remote"] = AgentToRemotePipeline(
            config=config,
            provider_factory=provider_factory,
            voice_detector_loader=voice_detector_loader,
            input_device=agent_settings["input_device"],
            output_device=agent_settings["output_device"],
            duration=duration,
            startup_barrier=startup_barrier,
            control_state=control_state,
            device_manager=device_manager,
            metrics_writer=metrics_writer,
        )
    await device_manager.start()
    web_enabled = bool(server_settings["enabled"]) if args.web is None else args.web
    control_server: LocalControlServer | None = None
    if web_enabled:
        host = args.web_host or str(server_settings["host"])
        port = args.web_port if args.web_port is not None else int(server_settings["port"])
        try:
            control_server = LocalControlServer(control_state, host, port)
            await control_server.start()
        except ValueError as error:
            await device_manager.close()
            raise ConfigurationError(str(error)) from error
    tasks = {
        name: asyncio.create_task(pipeline.run(), name=name)
        for name, pipeline in selected_pipelines.items()
    }
    try:
        await asyncio.gather(*tasks.values())
    except asyncio.CancelledError:
        logger.info("event=application_shutdown_started reason=cancelled")
        for name, task in tasks.items():
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        raise
    except BaseException:
        logger.error("event=application_degraded reason=pipeline_failed action=stopping_all")
        for name, task in tasks.items():
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        raise
    finally:
        await voice_detector_loader.close()
        if control_server is not None:
            await control_server.close()
        await device_manager.close()
    logger.info("event=application_stopped reason=completed")


async def run_application_with_interrupt_trace(args: argparse.Namespace) -> bool:
    """Trace Ctrl+C immediately and keep repeated presses from corrupting shutdown."""
    loop = asyncio.get_running_loop()
    application_task = asyncio.current_task()
    if application_task is None:
        raise RuntimeError("Application task is not available.")
    shutdown_requested = False
    logger = logging.getLogger(__name__)

    def request_shutdown() -> None:
        nonlocal shutdown_requested
        if shutdown_requested:
            return
        shutdown_requested = True
        logger.info("event=application_shutdown_requested reason=user_interrupt")
        application_task.cancel()

    def handle_sigint(_signum: int, _frame: object) -> None:
        loop.call_soon_threadsafe(request_shutdown)

    previous_sigint_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, handle_sigint)
    try:
        try:
            await run_application(args)
        except asyncio.CancelledError:
            if not shutdown_requested:
                raise
        return shutdown_requested
    finally:
        signal.signal(signal.SIGINT, previous_sigint_handler)


def main() -> int:
    """Run the application and return a process exit code."""
    try:
        interrupted = asyncio.run(
            run_application_with_interrupt_trace(parse_args())
        )
        if interrupted:
            logging.info("event=application_stopped reason=user_interrupt")
            return 130
    except KeyboardInterrupt:
        logging.info("event=application_stopped reason=user_interrupt")
        return 130
    except (
        ConfigurationError,
        ProviderError,
        AudioDeviceError,
        sd.PortAudioError,
        OSError,
        RuntimeError,
        ValueError,
    ) as error:
        logging.error("event=application_failed error=%s", error)
        return 1
    finally:
        shutdown_logging()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
