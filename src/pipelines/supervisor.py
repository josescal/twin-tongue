"""Runtime selection of one directional translation pipeline implementation."""

import asyncio
import logging
from pathlib import Path

from app_state import ApplicationState, TranslationEngine
from audio.device_manager import AudioDeviceManager
from audio.voice_detection import VoiceDetectorLoader
from engines.openai_realtime import OpenAIRealtimeTranslationFactory
from metrics import CsvMetricsWriter
from pipelines.agent_to_remote import AgentToRemotePipeline
from pipelines.openai_realtime import OpenAIRealtimeTranslatePipeline
from pipelines.remote_to_agent import RemoteToAgentPipeline
from providers.errors import ProviderError
from providers.factory import ProviderFactory


LOGGER = logging.getLogger(__name__)


class DirectionalPipelineSupervisor:
    """Run exactly one engine for a direction and restart only on engine changes."""

    def __init__(
        self,
        *,
        config: dict[str, object],
        env_path: Path,
        pipeline_name: str,
        source_language_name: str,
        target_language_name: str,
        voice_detector_loader: VoiceDetectorLoader,
        control_state: ApplicationState,
        device_manager: AudioDeviceManager,
        duration: float | None = None,
        startup_barrier: asyncio.Barrier | None = None,
        metrics_writer: CsvMetricsWriter | None = None,
        classic_factory: ProviderFactory | None = None,
        realtime_factory: OpenAIRealtimeTranslationFactory | None = None,
    ) -> None:
        self.config = config
        self.env_path = env_path
        self.pipeline_name = pipeline_name
        self.source_language_name = source_language_name
        self.target_language_name = target_language_name
        self.voice_detector_loader = voice_detector_loader
        self.control_state = control_state
        self.device_manager = device_manager
        self.duration = duration
        self.startup_barrier = startup_barrier
        self.metrics_writer = metrics_writer
        self._classic_factory = classic_factory
        self._realtime_factory = realtime_factory

    async def run(self) -> None:
        engine = self.control_state.get_engine(self.pipeline_name)
        try:
            pipeline = self._build_pipeline(
                engine,
                startup_barrier=self.startup_barrier,
            )
        except ProviderError as error:
            LOGGER.error(
                "event=translation_engine_unavailable engine=%s error=%s",
                engine.value,
                error,
            )
            await self.control_state.set_pipeline_status(
                self.pipeline_name, "translation_unavailable"
            )
            await asyncio.Event().wait()
            return

        LOGGER.info("event=translation_engine_started engine=%s", engine.value)
        await pipeline.run()

    def _build_pipeline(
        self,
        engine: TranslationEngine,
        *,
        startup_barrier: asyncio.Barrier | None,
    ) -> RemoteToAgentPipeline | OpenAIRealtimeTranslatePipeline:
        if engine is TranslationEngine.CLASSIC:
            if self._classic_factory is None:
                self._classic_factory = ProviderFactory.from_environment(
                    self.config, self.env_path
                )
            if self.pipeline_name == "agent_to_remote":
                return AgentToRemotePipeline(
                    config=self.config,
                    provider_factory=self._classic_factory,
                    voice_detector_loader=self.voice_detector_loader,
                    duration=self.duration,
                    startup_barrier=startup_barrier,
                    control_state=self.control_state,
                    device_manager=self.device_manager,
                    metrics_writer=self.metrics_writer,
                )
            return RemoteToAgentPipeline(
                config=self.config,
                provider_factory=self._classic_factory,
                voice_detector_loader=self.voice_detector_loader,
                duration=self.duration,
                startup_barrier=startup_barrier,
                control_state=self.control_state,
                device_manager=self.device_manager,
                metrics_writer=self.metrics_writer,
            )

        if self._realtime_factory is None:
            self._realtime_factory = OpenAIRealtimeTranslationFactory.from_environment(
                self.config, self.env_path
            )
        return OpenAIRealtimeTranslatePipeline(
            config=self.config,
            session_factory=self._realtime_factory,
            pipeline_name=self.pipeline_name,
            source_language_name=self.source_language_name,
            target_language_name=self.target_language_name,
            duration=self.duration,
            startup_barrier=startup_barrier,
            control_state=self.control_state,
            device_manager=self.device_manager,
            metrics_writer=self.metrics_writer,
        )
