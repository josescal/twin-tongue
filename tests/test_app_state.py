import unittest

from app_state import ApplicationState, PipelineMode, VoiceGender
from twin_tongue_version import __version__


class ApplicationStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_exposes_application_version(self) -> None:
        self.assertEqual(__version__, ApplicationState().snapshot()["application_version"])

    async def test_mode_change_updates_revision_and_notifies_subscriber(self) -> None:
        state = ApplicationState(active_pipelines=("remote_to_agent",))

        async with state.subscribe() as updates:
            snapshot = await state.set_mode("remote_to_agent", "translate")
            published = await updates.get()

        self.assertEqual(PipelineMode.TRANSLATE, state.get_mode("remote_to_agent"))
        self.assertEqual(1, snapshot["revision"])
        self.assertEqual("switching", snapshot["pipelines"]["remote_to_agent"]["status"])
        self.assertEqual(snapshot, published)
        pipelines = snapshot["pipelines"]
        self.assertTrue(pipelines["remote_to_agent"]["running"])
        self.assertFalse(pipelines["agent_to_remote"]["running"])

    async def test_repeating_same_mode_does_not_increment_revision(self) -> None:
        state = ApplicationState()

        snapshot = await state.set_mode("agent_to_remote", "passthrough")

        self.assertEqual(0, snapshot["revision"])

    async def test_pipeline_can_confirm_operational_status(self) -> None:
        state = ApplicationState()

        snapshot = await state.set_pipeline_status("remote_to_agent", "passthrough")

        self.assertEqual(
            "passthrough",
            snapshot["pipelines"]["remote_to_agent"]["status"],
        )

    async def test_invalid_pipeline_status_is_rejected(self) -> None:
        state = ApplicationState()

        with self.assertRaisesRegex(ValueError, "Invalid pipeline status"):
            await state.set_pipeline_status("remote_to_agent", "translation_reddy")

    async def test_invalid_pipeline_and_mode_are_rejected(self) -> None:
        state = ApplicationState()

        with self.assertRaisesRegex(ValueError, "Unknown pipeline"):
            await state.set_mode("unknown", "translate")
        with self.assertRaisesRegex(ValueError, "Invalid mode"):
            await state.set_mode("remote_to_agent", "off")

    async def test_language_change_is_published_with_supported_options(self) -> None:
        state = ApplicationState(
            initial_languages={"agent": "es", "remote": "en"}
        )

        async with state.subscribe() as updates:
            snapshot = await state.set_language("remote", "fr")
            published = await updates.get()

        self.assertEqual("fr", state.get_language("remote"))
        self.assertEqual(snapshot, published)
        self.assertEqual("fr", snapshot["languages"]["remote"])
        self.assertEqual(
            ["en", "es", "fr", "ca"],
            [item["code"] for item in snapshot["supported_languages"]],
        )

    async def test_invalid_language_and_role_are_rejected(self) -> None:
        state = ApplicationState()

        with self.assertRaisesRegex(ValueError, "Unsupported language"):
            await state.set_language("remote", "de")
        with self.assertRaisesRegex(ValueError, "Unknown language role"):
            await state.set_language("operator", "es")

    async def test_voice_gender_change_is_published(self) -> None:
        state = ApplicationState(initial_voice_gender="male")

        async with state.subscribe() as updates:
            snapshot = await state.set_voice_gender("female")
            published = await updates.get()

        self.assertEqual(VoiceGender.FEMALE, state.get_voice_gender())
        self.assertEqual("female", snapshot["voice_gender"])
        self.assertEqual(snapshot, published)

    async def test_invalid_voice_gender_is_rejected(self) -> None:
        state = ApplicationState()

        with self.assertRaisesRegex(ValueError, "Invalid voice gender"):
            await state.set_voice_gender("neutral")

    async def test_agent_final_and_translation_are_published_together(self) -> None:
        state = ApplicationState()

        state.publish_agent_final(1, "Buenos días", "es", "en")
        state.publish_agent_translation(1, "Good morning")

        entry = state.snapshot()["transcription"]["entries"][0]
        self.assertEqual("Buenos días", entry["source_text"])
        self.assertEqual("Good morning", entry["translated_text"])
        self.assertEqual("complete", entry["translation_status"])
        self.assertEqual("es", entry["source_language"])
        self.assertEqual("en", entry["target_language"])

    async def test_untranslated_final_can_be_marked_unavailable(self) -> None:
        state = ApplicationState()
        state.publish_agent_final(1, "Hola", "es", "en")

        state.mark_agent_translation_unavailable(1)

        entry = state.snapshot()["transcription"]["entries"][0]
        self.assertEqual("unavailable", entry["translation_status"])
        self.assertIsNone(entry["translated_text"])

    async def test_agent_transcription_is_bounded_and_can_be_cleared(self) -> None:
        state = ApplicationState()
        for transcript_id in range(60):
            state.publish_agent_final(
                transcript_id, f"Frase {transcript_id}", "es", "en"
            )

        entries = state.snapshot()["transcription"]["entries"]
        self.assertEqual(50, len(entries))
        self.assertEqual(10, entries[0]["id"])

        snapshot = await state.clear_agent_transcription()
        self.assertEqual([], snapshot["transcription"]["entries"])
