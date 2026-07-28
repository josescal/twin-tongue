"""Reusable agent-to-remote realtime dubbing pipeline."""

import asyncio

from audio.device_manager import AudioDeviceManager
from audio.echo_guard import EchoReferenceBus
from audio.voice_detection import VoiceDetectorLoader
from app_state import ApplicationState
from metrics import CsvMetricsWriter
from pipelines.remote_to_agent import RemoteToAgentPipeline
from providers.factory import ProviderFactory


class AgentToRemotePipeline(RemoteToAgentPipeline):
    """Capture the agent microphone and send translated speech to a virtual cable."""

    def __init__(
        self,
        config: dict[str, object],
        provider_factory: ProviderFactory,
        voice_detector_loader: VoiceDetectorLoader,
        duration: float | None = None,
        startup_barrier: asyncio.Barrier | None = None,
        control_state: ApplicationState | None = None,
        device_manager: AudioDeviceManager | None = None,
        metrics_writer: CsvMetricsWriter | None = None,
        echo_reference: EchoReferenceBus | None = None,
    ) -> None:
        super().__init__(
            config=config,
            provider_factory=provider_factory,
            voice_detector_loader=voice_detector_loader,
            duration=duration,
            pipeline_name="agent_to_remote",
            source_language_name="agent",
            target_language_name="remote",
            startup_barrier=startup_barrier,
            control_state=control_state,
            device_manager=device_manager,
            metrics_writer=metrics_writer,
            echo_reference=echo_reference,
        )
