"""Tests for realtime playback, readiness, and latency protection."""

import asyncio
import unittest
from unittest.mock import Mock, patch

from pipelines.remote_to_agent import (
    RemoteToAgentPipeline,
    TimedSynthesis,
    TimedTranscript,
)
from app_state import ApplicationState, PipelineMode
from audio.voice_detection import VoiceDetectorLoader
from providers.factory import ProviderFactory
from providers.stt import RealtimeTranscript, RealtimeTranscriptWord
from providers.translate import TranslationResult
from providers.tts import TTSChunk


def _pipeline() -> RemoteToAgentPipeline:
    return RemoteToAgentPipeline(
        config={},
        provider_factory=Mock(spec=ProviderFactory),
        voice_detector_loader=Mock(spec=VoiceDetectorLoader),
    )


def _synthesis(created_at: float) -> TimedSynthesis:
    chunks: asyncio.Queue[TTSChunk | None] = asyncio.Queue()
    chunks.put_nowait(None)
    return TimedSynthesis(
        chunks=chunks,
        created_at=created_at,
        enqueued_at=created_at,
    )


class RealtimePipelineTests(unittest.TestCase):
    def test_each_pipeline_uses_its_own_voice_gender(self) -> None:
        pipeline = _pipeline()
        pipeline.control_state = ApplicationState(
            initial_voice_genders={
                "remote_to_agent": "female",
                "agent_to_remote": "male",
            }
        )

        self.assertEqual("female", pipeline._current_voice_gender())
        pipeline.pipeline_name = "agent_to_remote"
        self.assertEqual("male", pipeline._current_voice_gender())

    def test_recording_is_automatic_for_translation_and_manual_for_passthrough(self) -> None:
        pipeline = _pipeline()
        pipeline._audio_recording_enabled = True
        pipeline.control_state = ApplicationState(audio_recording_enabled=True)

        self.assertFalse(pipeline._should_record_audio())
        asyncio.run(
            pipeline.control_state.set_mode("remote_to_agent", "translate")
        )
        self.assertTrue(pipeline._should_record_audio())
        asyncio.run(
            pipeline.control_state.set_mode("remote_to_agent", "passthrough")
        )
        asyncio.run(pipeline.control_state.set_manual_audio_recording(True))
        self.assertTrue(pipeline._should_record_audio())

    def test_transcribed_text_is_logged_only_at_debug(self) -> None:
        pipeline = _pipeline()
        pipeline.control_state = ApplicationState(
            initial_modes={"remote_to_agent": "translate"},
            initial_languages={"remote": "en", "agent": "es"},
        )

        with patch("pipelines.remote_to_agent.logger") as pipeline_logger:
            pipeline._handle_final(
                RealtimeTranscript(
                    text="private transcript",
                    words=(
                        RealtimeTranscriptWord("private", logprob=-0.1),
                        RealtimeTranscriptWord("transcript", logprob=-0.3),
                    ),
                )
            )

        pipeline_logger.debug.assert_called_once_with(
            "event=stt_final pipeline=%s source_language=%s target_language=%s "
            "commit_to_final_ms=%s boundary=%s word_count=%s "
            "average_logprob=%s minimum_logprob=%s text=%s",
            "remote_to_agent",
            "en",
            "es",
            "",
            "unknown",
            2,
            "-0.2000",
            "-0.3000",
            "private transcript",
        )
        pipeline_logger.info.assert_not_called()

    def test_agent_final_is_published_to_live_transcription(self) -> None:
        pipeline = _pipeline()
        pipeline.pipeline_name = "agent_to_remote"
        pipeline.source_language_name = "agent"
        pipeline.target_language_name = "remote"
        pipeline.control_state = ApplicationState(
            initial_modes={"agent_to_remote": "translate"},
            initial_languages={"agent": "es", "remote": "en"},
        )

        pipeline._handle_final(RealtimeTranscript(text="Necesito explicarlo mejor"))

        entries = pipeline.control_state.snapshot()["transcription"]["entries"]
        self.assertEqual(1, len(entries))
        self.assertEqual("agent_to_remote", entries[0]["pipeline"])
        self.assertEqual("Necesito explicarlo mejor", entries[0]["source_text"])
        self.assertIsNone(entries[0]["translated_text"])

    def test_remote_final_is_published_to_live_transcription(self) -> None:
        pipeline = _pipeline()
        pipeline.control_state = ApplicationState(
            initial_modes={"remote_to_agent": "translate"},
            initial_languages={"remote": "en", "agent": "es"},
        )

        pipeline._handle_final(RealtimeTranscript(text="I need help"))

        entries = pipeline.control_state.snapshot()["transcription"]["entries"]
        self.assertEqual(1, len(entries))
        self.assertEqual("remote_to_agent", entries[0]["pipeline"])
        self.assertEqual("I need help", entries[0]["source_text"])
        self.assertEqual("en", entries[0]["source_language"])
        self.assertEqual("es", entries[0]["target_language"])

    def test_agent_translation_is_attached_to_its_final_transcript(self) -> None:
        pipeline = _pipeline()
        pipeline.pipeline_name = "agent_to_remote"
        pipeline.control_state = ApplicationState(
            initial_modes={"agent_to_remote": "translate"}
        )
        pipeline.control_state.publish_transcript_final(
            "agent_to_remote", 7, "Hola", "es", "en"
        )
        pipeline.latency_protection_enabled = False
        pipeline.transcript_queue.put_nowait(
            TimedTranscript(
                value=RealtimeTranscript(text="Hola"),
                created_at=0,
                enqueued_at=0,
                source_language="es",
                target_language="en",
                transcript_id=7,
            )
        )

        class FakeTranslator:
            async def translate(self, text: str, source_language: str, target_language: str):
                return TranslationResult(
                    source_text=text,
                    translated_text="Hello",
                    source_language=source_language,
                    target_language=target_language,
                    latency_seconds=0.01,
                )

        stop = asyncio.Event()
        stop.set()
        asyncio.run(pipeline._translation_worker(FakeTranslator(), stop))  # type: ignore[arg-type]

        entry = pipeline.control_state.snapshot()["transcription"]["entries"][0]
        self.assertEqual("Hello", entry["translated_text"])

    def test_streaming_playback_writes_audio_before_final_marker(self) -> None:
        pipeline = _pipeline()
        pipeline.control_state = ApplicationState(
            initial_modes={"remote_to_agent": "translate"},
        )
        chunks: asyncio.Queue[TTSChunk | None] = asyncio.Queue()
        chunks.put_nowait(
            TTSChunk(
                audio=bytes(320),
                sample_rate=16_000,
                channels=1,
                sample_width_bytes=2,
                first_byte_latency_seconds=0.05,
            )
        )
        chunks.put_nowait(
            TTSChunk(
                audio=bytes(320),
                sample_rate=16_000,
                channels=1,
                sample_width_bytes=2,
            )
        )
        chunks.put_nowait(
            TTSChunk(
                audio=b"",
                sample_rate=16_000,
                channels=1,
                sample_width_bytes=2,
                total_latency_seconds=0.1,
                final=True,
            )
        )
        chunks.put_nowait(None)
        item = TimedSynthesis(chunks=chunks, created_at=0, enqueued_at=0)

        class FakeQueuedOutput:
            started = True
            queue_capacity_blocks = 10
            buffered_blocks = 0
            output_underflows = 0

            def __init__(self) -> None:
                self.writes: list[bytes] = []

            def push_block(self, block: bytes) -> None:
                self.writes.append(block)

        output = FakeQueuedOutput()
        pipeline._live_output = output  # type: ignore[assignment]
        asyncio.run(
            pipeline._play_synthesis_stream(
                item,
                output_device=0,
                output_channels=1,
                output_sample_rate=16_000,
                resampling_config={"backend": "soxr", "quality": "HQ"},
                frame_duration_ms=20,
            )
        )

        self.assertEqual(640, sum(map(len, output.writes)))
        self.assertEqual(1, pipeline.statistics.played_segments)
        self.assertEqual(640, pipeline.statistics.played_pcm_bytes)

    def test_translation_remains_passthrough_until_voice_detector_is_ready(self) -> None:
        pipeline = _pipeline()

        self.assertIs(
            PipelineMode.PASSTHROUGH,
            pipeline._effective_audio_mode(PipelineMode.TRANSLATE),
        )

        pipeline._voice_detector = object()  # type: ignore[assignment]

        self.assertIs(
            PipelineMode.TRANSLATE,
            pipeline._effective_audio_mode(PipelineMode.TRANSLATE),
        )

    def test_upstream_stale_policy_uses_only_the_hard_limit(self) -> None:
        pipeline = _pipeline()
        pipeline.playback_backlog_discard_age_seconds = 4.0
        pipeline.segment_discard_age_seconds = 8.0

        with patch("pipelines.remote_to_agent.time.monotonic", return_value=10.0):
            self.assertFalse(pipeline._drop_if_stale(5.0, "TTS"))
            self.assertTrue(pipeline._drop_if_stale(1.9, "TTS"))
            pipeline.latency_protection_enabled = False
            self.assertFalse(pipeline._drop_if_stale(0.0, "TTS"))

    def test_stale_backlog_keeps_the_newest_two_segments(self) -> None:
        pipeline = _pipeline()
        pipeline.playback_backlog_discard_age_seconds = 4.0
        pipeline.segment_discard_age_seconds = 8.0
        pipeline.playback_backlog_segments_to_keep = 2
        middle = _synthesis(7.0)
        newest = _synthesis(9.0)
        pipeline.playback_queue.put_nowait(middle)
        pipeline.playback_queue.put_nowait(newest)

        with patch("pipelines.remote_to_agent.time.monotonic", return_value=10.0):
            selected = pipeline._select_realtime_playback_item(_synthesis(5.0))

        self.assertIs(selected, middle)
        self.assertIs(pipeline.playback_queue.get_nowait(), newest)
        self.assertEqual(pipeline.statistics.superseded_before_playback, 1)
        self.assertEqual(pipeline.statistics.recovered_playback, 1)

    def test_late_segment_inside_hard_limit_is_recovered(self) -> None:
        pipeline = _pipeline()
        pipeline.playback_backlog_discard_age_seconds = 4.0
        pipeline.segment_discard_age_seconds = 8.0
        item = _synthesis(5.0)

        with patch("pipelines.remote_to_agent.time.monotonic", return_value=10.0):
            selected = pipeline._select_realtime_playback_item(item)

        self.assertIs(selected, item)
        self.assertEqual(pipeline.statistics.recovered_playback, 1)

    def test_segment_over_hard_limit_is_dropped(self) -> None:
        pipeline = _pipeline()
        pipeline.playback_backlog_discard_age_seconds = 4.0
        pipeline.segment_discard_age_seconds = 8.0

        with patch("pipelines.remote_to_agent.time.monotonic", return_value=10.0):
            selected = pipeline._select_realtime_playback_item(_synthesis(1.9))

        self.assertIsNone(selected)
        self.assertEqual(pipeline.statistics.stale_before_playback, 1)


if __name__ == "__main__":
    unittest.main()
