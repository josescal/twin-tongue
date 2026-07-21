import asyncio
import unittest

from app_state import ApplicationState, TranslationEngine
from pipelines.supervisor import DirectionalPipelineSupervisor


class CompletedPipeline:
    async def run(self) -> None:
        return


class RecordingSupervisor(DirectionalPipelineSupervisor):
    def __init__(self, state: ApplicationState) -> None:
        super().__init__(
            config={},
            env_path=None,  # type: ignore[arg-type]
            pipeline_name="remote_to_agent",
            source_language_name="remote",
            target_language_name="agent",
            voice_detector_loader=None,  # type: ignore[arg-type]
            control_state=state,
            device_manager=None,  # type: ignore[arg-type]
        )
        self.built: list[TranslationEngine] = []

    def _build_pipeline(  # type: ignore[override]
        self, engine: TranslationEngine, *, startup_barrier: asyncio.Barrier | None
    ) -> CompletedPipeline:
        self.built.append(engine)
        return CompletedPipeline()


class DirectionalPipelineSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_builds_only_the_engine_selected_by_configuration(self) -> None:
        state = ApplicationState(
            active_pipelines=("remote_to_agent",),
            initial_engines={"remote_to_agent": "openai_realtime"},
        )
        supervisor = RecordingSupervisor(state)
        await supervisor.run()

        self.assertEqual([TranslationEngine.OPENAI_REALTIME], supervisor.built)


if __name__ == "__main__":
    unittest.main()
