"""Tests for UI transcript publication by the OpenAI Realtime pipeline."""

import asyncio
from collections.abc import Callable
import unittest

from app_state import ApplicationState
from pipelines.openai_realtime import OpenAIRealtimeTranslatePipeline


class OpenAIRealtimePipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.state = ApplicationState(
            initial_languages={"agent": "es", "remote": "en"}
        )
        self.pipeline = OpenAIRealtimeTranslatePipeline.__new__(
            OpenAIRealtimeTranslatePipeline
        )
        self.pipeline.pipeline_name = "remote_to_agent"
        self.pipeline.source_language_name = "remote"
        self.pipeline.target_language_name = "agent"
        self.pipeline.control_state = self.state
        self.pipeline._transcript_sequence = 0
        self.pipeline._transcript_queue = asyncio.Queue()
        self.pipeline._log_transcript_deltas = False

    async def test_deltas_are_published_incrementally_and_finalized_after_idle(
        self,
    ) -> None:
        worker = asyncio.create_task(
            self.pipeline._transcript_worker(
                update_interval_seconds=0.01,
                segment_idle_seconds=0.05,
            )
        )
        try:
            self.pipeline._enqueue_transcript_delta("input", "Hello")
            self.pipeline._enqueue_transcript_delta("input", " there")
            self.pipeline._enqueue_transcript_delta("output", "Hola")

            await self._wait_for(
                lambda: bool(self.state.snapshot()["transcription"]["entries"])
            )
            await self._wait_for(
                lambda: self.state.snapshot()["transcription"]["entries"][0][
                    "translated_text"
                ]
                == "Hola"
            )
            live = self.state.snapshot()["transcription"]["entries"][0]
            self.assertEqual("Hello there", live["source_text"])
            self.assertEqual("pending", live["translation_status"])

            await self._wait_for(
                lambda: self.state.snapshot()["transcription"]["entries"][0][
                    "translation_status"
                ]
                == "complete"
            )
        finally:
            self.pipeline._transcript_queue.put_nowait(None)
            await worker

    async def test_idle_boundary_starts_a_new_transcript_entry(self) -> None:
        worker = asyncio.create_task(
            self.pipeline._transcript_worker(
                update_interval_seconds=0.005,
                segment_idle_seconds=0.02,
            )
        )
        try:
            self.pipeline._enqueue_transcript_delta("input", "First")
            self.pipeline._enqueue_transcript_delta("output", "Primero")
            await self._wait_for(
                lambda: len(self.state.snapshot()["transcription"]["entries"]) == 1
                and self.state.snapshot()["transcription"]["entries"][0][
                    "translation_status"
                ]
                == "complete"
            )

            self.pipeline._enqueue_transcript_delta("input", "Second")
            self.pipeline._enqueue_transcript_delta("output", "Segundo")
            await self._wait_for(
                lambda: len(self.state.snapshot()["transcription"]["entries"]) == 2
            )

            entries = self.state.snapshot()["transcription"]["entries"]
            self.assertEqual([1, 2], [entry["id"] for entry in entries])
            self.assertEqual("Second", entries[1]["source_text"])
        finally:
            self.pipeline._transcript_queue.put_nowait(None)
            await worker

    async def test_delayed_translation_remains_paired_with_source(self) -> None:
        worker = asyncio.create_task(
            self.pipeline._transcript_worker(
                update_interval_seconds=0.005,
                segment_idle_seconds=0.02,
            )
        )
        try:
            self.pipeline._enqueue_transcript_delta("input", "Hello")
            await asyncio.sleep(0.05)
            self.pipeline._enqueue_transcript_delta("output", "Hola")

            await self._wait_for(
                lambda: bool(self.state.snapshot()["transcription"]["entries"])
                and self.state.snapshot()["transcription"]["entries"][0][
                    "translated_text"
                ]
                == "Hola"
            )

            entries = self.state.snapshot()["transcription"]["entries"]
            self.assertEqual(1, len(entries))
            self.assertEqual("Hello", entries[0]["source_text"])
        finally:
            self.pipeline._transcript_queue.put_nowait(None)
            await worker

    async def test_output_transcript_is_visible_before_source_arrives(self) -> None:
        worker = asyncio.create_task(
            self.pipeline._transcript_worker(
                update_interval_seconds=0.005,
                segment_idle_seconds=0.05,
            )
        )
        try:
            self.pipeline._enqueue_transcript_delta("output", "Translated text")

            await self._wait_for(
                lambda: bool(self.state.snapshot()["transcription"]["entries"])
            )
            entry = self.state.snapshot()["transcription"]["entries"][0]
            self.assertEqual("", entry["source_text"])
            self.assertEqual("Translated text", entry["translated_text"])
            self.assertEqual("pending", entry["translation_status"])
        finally:
            self.pipeline._transcript_queue.put_nowait(None)
            await worker

    async def test_session_boundary_finalizes_current_transcript(self) -> None:
        worker = asyncio.create_task(
            self.pipeline._transcript_worker(
                update_interval_seconds=0.01,
                segment_idle_seconds=10.0,
            )
        )
        try:
            self.pipeline._enqueue_transcript_delta("input", "Hello")
            self.pipeline._enqueue_transcript_delta("output", "Hola")
            self.pipeline._enqueue_transcript_boundary()

            await self._wait_for(
                lambda: bool(self.state.snapshot()["transcription"]["entries"])
                and self.state.snapshot()["transcription"]["entries"][0][
                    "translation_status"
                ]
                == "complete"
            )
        finally:
            self.pipeline._transcript_queue.put_nowait(None)
            await worker

    async def _wait_for(
        self, predicate: Callable[[], bool], timeout_seconds: float = 0.5
    ) -> None:
        async with asyncio.timeout(timeout_seconds):
            while not predicate():
                await asyncio.sleep(0.002)


if __name__ == "__main__":
    unittest.main()
