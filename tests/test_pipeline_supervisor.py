import asyncio
import unittest
from unittest.mock import Mock

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
            voice_detector_loader=None,
            control_state=state,
            device_manager=None,  # type: ignore[arg-type]
        )
        self.built: list[TranslationEngine] = []

    def _build_pipeline(  # type: ignore[override]
        self, engine: TranslationEngine, *, startup_barrier: asyncio.Barrier | None
    ) -> CompletedPipeline:
        self.built.append(engine)
        return CompletedPipeline()


class FailingOncePipeline:
    def __init__(self, supervisor: "RestartingSupervisor") -> None:
        self.supervisor = supervisor

    async def run(self) -> None:
        if self.supervisor.run_attempts == 1:
            raise RuntimeError("simulated directional failure")


class RestartingSupervisor(RecordingSupervisor):
    RESTART_BASE_DELAY_SECONDS = 0
    RESTART_MAX_DELAY_SECONDS = 0

    def __init__(self, state: ApplicationState) -> None:
        super().__init__(state)
        self.run_attempts = 0

    def _build_pipeline(  # type: ignore[override]
        self, engine: TranslationEngine, *, startup_barrier: asyncio.Barrier | None
    ) -> FailingOncePipeline:
        self.run_attempts += 1
        return FailingOncePipeline(self)


class DirectionalPipelineSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_builds_only_the_engine_selected_by_configuration(self) -> None:
        state = ApplicationState(
            active_pipelines=("remote_to_agent",),
            initial_engines={"remote_to_agent": "openai_realtime"},
        )
        supervisor = RecordingSupervisor(state)
        await supervisor.run()

        self.assertEqual([TranslationEngine.OPENAI_REALTIME], supervisor.built)

    async def test_direction_restarts_after_runtime_failure(self) -> None:
        state = ApplicationState(
            active_pipelines=("remote_to_agent",),
            initial_engines={"remote_to_agent": "openai_realtime"},
        )
        supervisor = RestartingSupervisor(state)

        await supervisor.run()

        self.assertEqual(2, supervisor.run_attempts)

    def test_classic_engine_requires_voice_detector_loader(self) -> None:
        state = ApplicationState(
            active_pipelines=("remote_to_agent",),
            initial_engines={"remote_to_agent": "classic"},
        )
        supervisor = DirectionalPipelineSupervisor(
            config={},
            env_path=None,  # type: ignore[arg-type]
            pipeline_name="remote_to_agent",
            source_language_name="remote",
            target_language_name="agent",
            voice_detector_loader=None,
            control_state=state,
            device_manager=None,  # type: ignore[arg-type]
            classic_factory=Mock(),
        )

        with self.assertRaisesRegex(
            RuntimeError, "Classic pipeline requires the Silero"
        ):
            supervisor._build_pipeline(
                TranslationEngine.CLASSIC,
                startup_barrier=None,
            )


if __name__ == "__main__":
    unittest.main()
