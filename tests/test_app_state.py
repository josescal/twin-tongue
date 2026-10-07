import unittest
from unittest.mock import Mock, patch

from app_state import ApplicationState, PipelineMode, TranslationEngine, VoiceGender
from twin_tongue_version import __version__


class ApplicationStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_exposes_application_version(self) -> None:
        self.assertEqual(__version__, ApplicationState().snapshot()["application_version"])

    async def test_translation_automatically_activates_enabled_recording(self) -> None:
        state = ApplicationState(audio_recording_enabled=True)

        idle = state.snapshot()["audio_recording"]
        translated = await state.set_mode("agent_to_remote", "translate")

        self.assertFalse(idle["active"])
        self.assertTrue(translated["audio_recording"]["active"])
        self.assertEqual(
            ["agent_to_remote"],
            translated["audio_recording"]["automatic_pipelines"],
        )
        self.assertTrue(state.is_audio_recording_active_for("agent_to_remote"))
        self.assertFalse(state.is_audio_recording_active_for("remote_to_agent"))

    async def test_realtime_recording_is_manual_not_automatic(self) -> None:
        state = ApplicationState(
            audio_recording_enabled=True,
            initial_engines={
                "remote_to_agent": "openai_realtime",
                "agent_to_remote": "openai_realtime",
            },
        )
        await state.set_mode("remote_to_agent", "translate")

        await state.set_pipeline_status("remote_to_agent", "translation_ready")
        self.assertFalse(state.is_audio_recording_active_for("remote_to_agent"))

        recording = await state.set_manual_audio_recording(True)

        self.assertTrue(recording["audio_recording"]["active"])
        self.assertEqual([], recording["audio_recording"]["automatic_pipelines"])
        self.assertTrue(state.is_audio_recording_active_for("remote_to_agent"))

    async def test_snapshot_exposes_requested_and_effective_modes(self) -> None:
        state = ApplicationState(
            active_pipelines=("remote_to_agent",),
            initial_modes={"remote_to_agent": "translate"},
            initial_engines={"remote_to_agent": "openai_realtime"},
        )

        unavailable = await state.set_pipeline_status(
            "remote_to_agent", "translation_unavailable"
        )
        ready = await state.set_pipeline_status(
            "remote_to_agent", "translation_ready"
        )

        self.assertEqual(
            "passthrough",
            unavailable["pipelines"]["remote_to_agent"]["effective_mode"],
        )
        self.assertEqual(
            "translate",
            ready["pipelines"]["remote_to_agent"]["effective_mode"],
        )

    async def test_manual_recording_covers_passthrough_without_persisting_mode(self) -> None:
        state = ApplicationState(audio_recording_enabled=True)

        started = await state.set_manual_audio_recording(True)
        stopped = await state.set_manual_audio_recording(False)

        self.assertTrue(started["audio_recording"]["active"])
        self.assertTrue(started["audio_recording"]["manual"])
        self.assertFalse(stopped["audio_recording"]["active"])
        self.assertFalse(stopped["audio_recording"]["manual"])

    async def test_manual_recording_rejects_disabled_feature(self) -> None:
        state = ApplicationState(audio_recording_enabled=False)

        with self.assertRaisesRegex(ValueError, "disabled in configuration"):
            await state.set_manual_audio_recording(True)

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

    async def test_translation_engine_is_initialized_from_configuration(self) -> None:
        state = ApplicationState(
            initial_engines={"agent_to_remote": "openai_realtime"}
        )

        self.assertEqual(
            TranslationEngine.OPENAI_REALTIME,
            state.get_engine("agent_to_remote"),
        )
        self.assertEqual(
            TranslationEngine.CLASSIC,
            state.get_engine("remote_to_agent"),
        )

    async def test_all_modes_change_in_one_revision(self) -> None:
        state = ApplicationState()

        snapshot = await state.set_all_modes("translate")

        self.assertEqual(1, snapshot["revision"])
        for pipeline in snapshot["pipelines"].values():
            self.assertEqual("translate", pipeline["mode"])
            self.assertEqual("switching", pipeline["status"])

    async def test_all_modes_only_changes_active_pipelines(self) -> None:
        state = ApplicationState(active_pipelines=("remote_to_agent",))

        snapshot = await state.set_all_modes("translate")

        self.assertEqual("translate", snapshot["pipelines"]["remote_to_agent"]["mode"])
        self.assertEqual("passthrough", snapshot["pipelines"]["agent_to_remote"]["mode"])

    async def test_pipeline_can_confirm_operational_status(self) -> None:
        state = ApplicationState()

        snapshot = await state.set_pipeline_status("remote_to_agent", "passthrough")

        self.assertEqual(
            "passthrough",
            snapshot["pipelines"]["remote_to_agent"]["status"],
        )

    async def test_pipeline_can_wait_for_a_call_without_disabling_translation(
        self,
    ) -> None:
        state = ApplicationState(
            initial_modes={"remote_to_agent": "translate"},
            initial_engines={"remote_to_agent": "openai_realtime"},
        )

        snapshot = await state.set_pipeline_status(
            "remote_to_agent", "waiting_for_call"
        )

        pipeline = snapshot["pipelines"]["remote_to_agent"]
        self.assertEqual("translate", pipeline["mode"])
        self.assertEqual("passthrough", pipeline["status"])
        self.assertTrue(pipeline["waiting_for_call"])

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

    async def test_realtime_target_language_options_exclude_catalan(self) -> None:
        state = ApplicationState(
            initial_engines={"agent_to_remote": "openai_realtime"},
            initial_languages={"agent": "ca", "remote": "en"},
        )

        snapshot = state.snapshot()

        self.assertEqual(
            ["en", "es", "fr", "ca"],
            [
                item["code"]
                for item in snapshot["supported_languages_by_role"]["agent"]
            ],
        )
        self.assertEqual(
            ["en", "es", "fr"],
            [
                item["code"]
                for item in snapshot["supported_languages_by_role"]["remote"]
            ],
        )
        with self.assertRaisesRegex(ValueError, "OpenAI Realtime Translate"):
            await state.set_language("remote", "ca")

    async def test_realtime_recovers_unsupported_initial_target_language(self) -> None:
        with self.assertLogs("app_state", level="WARNING") as captured:
            state = ApplicationState(
                initial_engines={"remote_to_agent": "openai_realtime"},
                initial_languages={"agent": "ca", "remote": "en"},
            )

        self.assertEqual("es", state.get_language("agent"))
        self.assertIn("event=unsupported_initial_target_language", captured.output[0])

    async def test_device_preferences_persist_recovered_realtime_language(self) -> None:
        state = ApplicationState(
            initial_engines={"remote_to_agent": "openai_realtime"},
        )
        manager = Mock()
        manager.voice_genders = {
            "remote_to_agent": "male",
            "agent_to_remote": "male",
        }
        manager.ui_language = "en"
        manager.participant_languages = {"agent": "ca", "remote": "en"}

        state.attach_device_manager(manager)

        self.assertEqual("es", state.get_language("agent"))
        manager.set_participant_language.assert_called_once_with("agent", "es")

    async def test_invalid_language_and_role_are_rejected(self) -> None:
        state = ApplicationState()

        with self.assertRaisesRegex(ValueError, "Unsupported language"):
            await state.set_language("remote", "de")
        with self.assertRaisesRegex(ValueError, "Unknown language role"):
            await state.set_language("operator", "es")

    async def test_ui_language_change_is_published(self) -> None:
        state = ApplicationState(initial_ui_language="en")

        async with state.subscribe() as updates:
            snapshot = await state.set_ui_language("es")
            published = await updates.get()

        self.assertEqual("es", state.get_ui_language())
        self.assertEqual("es", snapshot["ui_language"])
        self.assertEqual(["en", "es"], snapshot["supported_ui_languages"])
        self.assertEqual(snapshot, published)

    async def test_invalid_ui_language_is_rejected(self) -> None:
        state = ApplicationState()

        with self.assertRaisesRegex(ValueError, "Unsupported UI language"):
            await state.set_ui_language("fr")

    async def test_voice_gender_change_is_published(self) -> None:
        state = ApplicationState(
            initial_voice_genders={
                "remote_to_agent": "male",
                "agent_to_remote": "female",
            }
        )

        async with state.subscribe() as updates:
            snapshot = await state.set_voice_gender("remote_to_agent", "female")
            published = await updates.get()

        self.assertEqual(
            VoiceGender.FEMALE, state.get_voice_gender("remote_to_agent")
        )
        self.assertEqual(
            VoiceGender.FEMALE, state.get_voice_gender("agent_to_remote")
        )
        self.assertEqual(
            "female", snapshot["pipelines"]["remote_to_agent"]["voice_gender"]
        )
        self.assertEqual(snapshot, published)

    async def test_invalid_voice_gender_is_rejected(self) -> None:
        state = ApplicationState()

        with self.assertRaisesRegex(ValueError, "Invalid voice gender"):
            await state.set_voice_gender("remote_to_agent", "neutral")

    async def test_final_and_translation_are_published_with_their_direction(self) -> None:
        state = ApplicationState()

        state.publish_transcript_final(
            "agent_to_remote", 1, "Buenos días", "es", "en"
        )
        state.publish_transcript_translation("agent_to_remote", 1, "Good morning")

        entry = state.snapshot()["transcription"]["entries"][0]
        self.assertEqual("agent_to_remote", entry["pipeline"])
        self.assertEqual("Buenos días", entry["source_text"])
        self.assertEqual("Good morning", entry["translated_text"])
        self.assertEqual("complete", entry["translation_status"])
        self.assertEqual("es", entry["source_language"])
        self.assertEqual("en", entry["target_language"])
        self.assertRegex(entry["timestamp"], r"^\d{2}:\d{2}:\d{2}$")

    async def test_untranslated_final_can_be_marked_unavailable(self) -> None:
        state = ApplicationState()
        state.publish_transcript_final("remote_to_agent", 1, "Hello", "en", "es")

        state.mark_transcript_translation_unavailable("remote_to_agent", 1)

        entry = state.snapshot()["transcription"]["entries"][0]
        self.assertEqual("unavailable", entry["translation_status"])
        self.assertIsNone(entry["translated_text"])

    async def test_realtime_transcript_is_updated_in_place_and_finalized(self) -> None:
        state = ApplicationState()

        with patch(
            "app_state._transcript_timestamp",
            side_effect=["12:34:56", "12:35:10"],
        ):
            state.publish_realtime_transcript(
                "remote_to_agent", 7, "Hello", "", "en", "es"
            )
            state.publish_realtime_transcript(
                "remote_to_agent", 7, "Hello there", "Hola", "en", "es"
            )

        entries = state.snapshot()["transcription"]["entries"]
        self.assertEqual(1, len(entries))
        self.assertEqual("Hello there", entries[0]["source_text"])
        self.assertEqual("Hola", entries[0]["translated_text"])
        self.assertEqual("pending", entries[0]["translation_status"])
        self.assertEqual("aligned", entries[0]["pair_status"])
        self.assertEqual("12:34:56", entries[0]["timestamp"])

        state.publish_realtime_transcript(
            "remote_to_agent",
            7,
            "Hello there",
            "Hola",
            "en",
            "es",
            final=True,
        )

        entry = state.snapshot()["transcription"]["entries"][0]
        self.assertEqual("complete", entry["translation_status"])

    async def test_realtime_transcript_can_publish_translation_before_source(self) -> None:
        state = ApplicationState()

        state.publish_realtime_transcript(
            "agent_to_remote", 3, "", "Hello", "es", "en"
        )

        entry = state.snapshot()["transcription"]["entries"][0]
        self.assertEqual("", entry["source_text"])
        self.assertEqual("Hello", entry["translated_text"])
        self.assertEqual("pending", entry["translation_status"])
        self.assertEqual("waiting_for_source", entry["pair_status"])

        state.publish_realtime_transcript(
            "agent_to_remote",
            3,
            "",
            "Hello",
            "es",
            "en",
            final=True,
        )
        entry = state.snapshot()["transcription"]["entries"][0]
        self.assertEqual("translation_without_source", entry["pair_status"])

    async def test_realtime_repeated_phrase_is_preserved_as_a_separate_turn(
        self,
    ) -> None:
        state = ApplicationState()

        for transcript_id in range(8, 12):
            state.publish_realtime_transcript(
                "remote_to_agent",
                transcript_id,
                "",
                "¿Qué hora es?",
                "en",
                "es",
                final=True,
            )

        entries = state.snapshot()["transcription"]["entries"]
        self.assertEqual(4, len(entries))
        self.assertEqual([8, 9, 10, 11], [entry["id"] for entry in entries])
        self.assertEqual(
            ["¿Qué hora es?"] * 4,
            [entry["translated_text"] for entry in entries],
        )

    async def test_realtime_equal_captions_are_isolated_by_direction(
        self,
    ) -> None:
        state = ApplicationState()

        state.publish_realtime_transcript(
            "remote_to_agent", 1, "", "Hello!", "es", "en", final=True
        )
        state.publish_realtime_transcript(
            "agent_to_remote", 2, "", "Hello.", "es", "en", final=True
        )

        entries = state.snapshot()["transcription"]["entries"]
        self.assertEqual(2, len(entries))

    async def test_transcription_is_bounded_per_direction_and_can_be_cleared(
        self,
    ) -> None:
        state = ApplicationState()
        for transcript_id in range(5):
            state.publish_transcript_final(
                "remote_to_agent", transcript_id, f"Phrase {transcript_id}", "en", "es"
            )
        for transcript_id in range(60):
            state.publish_transcript_final(
                "agent_to_remote", transcript_id, f"Frase {transcript_id}", "es", "en"
            )

        entries = state.snapshot()["transcription"]["entries"]
        remote_entries = [
            entry for entry in entries if entry["pipeline"] == "remote_to_agent"
        ]
        agent_entries = [
            entry for entry in entries if entry["pipeline"] == "agent_to_remote"
        ]
        self.assertEqual(55, len(entries))
        self.assertEqual([0, 1, 2, 3, 4], [entry["id"] for entry in remote_entries])
        self.assertEqual(50, len(agent_entries))
        self.assertEqual(10, agent_entries[0]["id"])

        snapshot = await state.clear_transcription()
        self.assertEqual([], snapshot["transcription"]["entries"])

    async def test_transcription_retains_fifty_entries_for_each_direction(
        self,
    ) -> None:
        state = ApplicationState()
        for transcript_id in range(60):
            state.publish_transcript_final(
                "remote_to_agent", transcript_id, f"Phrase {transcript_id}", "en", "es"
            )
            state.publish_transcript_final(
                "agent_to_remote", transcript_id, f"Frase {transcript_id}", "es", "en"
            )

        entries = state.snapshot()["transcription"]["entries"]
        self.assertEqual(100, len(entries))
        for pipeline_name in ("remote_to_agent", "agent_to_remote"):
            directional_entries = [
                entry for entry in entries if entry["pipeline"] == pipeline_name
            ]
            self.assertEqual(50, len(directional_entries))
            self.assertEqual(
                list(range(10, 60)),
                [entry["id"] for entry in directional_entries],
            )

    async def test_same_transcript_id_is_isolated_between_directions(self) -> None:
        state = ApplicationState()
        state.publish_transcript_final("remote_to_agent", 1, "Hello", "en", "es")
        state.publish_transcript_final("agent_to_remote", 1, "Hola", "es", "en")

        state.publish_transcript_translation("remote_to_agent", 1, "Hola")

        entries = state.snapshot()["transcription"]["entries"]
        self.assertEqual("Hola", entries[0]["translated_text"])
        self.assertIsNone(entries[1]["translated_text"])
