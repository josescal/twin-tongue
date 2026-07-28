"""Tests for far-end-reference echo suppression."""

from array import array
import unittest

from audio.echo_guard import EchoReferenceBus, ReferenceEchoGuard


def pcm(amplitude: int, frames: int = 960) -> bytes:
    return array("h", (amplitude,) * frames).tobytes()


class ReferenceEchoGuardTests(unittest.TestCase):
    def test_weak_microphone_audio_is_suppressed_during_far_end_speech(self) -> None:
        reference = EchoReferenceBus(reference_activity_dbfs=-48.0)
        guard = ReferenceEchoGuard(
            reference,
            near_end_override_dbfs=-26.0,
            hangover_ms=350.0,
            correlation_window_ms=0,
        )
        reference.observe(pcm(4_000), observed_at=10.0)

        accepted = guard.process(pcm(500), now=10.1)

        self.assertEqual(bytes(len(pcm(500))), accepted)
        self.assertEqual(1, guard.statistics.suppressed_blocks)

    def test_strong_near_end_voice_is_preserved_during_far_end_speech(self) -> None:
        reference = EchoReferenceBus()
        guard = ReferenceEchoGuard(
            reference,
            near_end_override_dbfs=-26.0,
            correlation_window_ms=0,
        )
        microphone = pcm(8_000)
        reference.observe(pcm(4_000), observed_at=20.0)

        accepted = guard.process(microphone, now=20.1)

        self.assertEqual(microphone, accepted)
        self.assertEqual(1, guard.statistics.near_end_override_blocks)

    def test_microphone_is_preserved_after_reference_hangover(self) -> None:
        reference = EchoReferenceBus()
        guard = ReferenceEchoGuard(
            reference,
            hangover_ms=350.0,
            correlation_window_ms=0,
        )
        microphone = pcm(500)
        reference.observe(pcm(4_000), observed_at=30.0)

        self.assertEqual(microphone, guard.process(microphone, now=30.5))

    def test_moderate_voice_is_preserved_while_reference_is_releasing(self) -> None:
        reference = EchoReferenceBus()
        guard = ReferenceEchoGuard(
            reference,
            near_end_override_dbfs=-26.0,
            post_reference_override_dbfs=-38.0,
            near_end_hold_ms=800.0,
            correlation_window_ms=0,
        )
        moderate_voice = pcm(1_200)
        quiet_syllable = pcm(300)
        reference.observe(pcm(4_000), observed_at=40.0)
        reference.observe(pcm(0), observed_at=40.05)

        self.assertEqual(moderate_voice, guard.process(moderate_voice, now=40.1))
        self.assertEqual(quiet_syllable, guard.process(quiet_syllable, now=40.3))
        self.assertEqual(2, guard.statistics.near_end_override_blocks)

    def test_moderate_echo_is_suppressed_while_reference_is_playing(self) -> None:
        reference = EchoReferenceBus()
        guard = ReferenceEchoGuard(
            reference,
            near_end_override_dbfs=-26.0,
            post_reference_override_dbfs=-38.0,
            correlation_window_ms=0,
        )
        moderate_echo = pcm(1_200)
        reference.observe(pcm(4_000), observed_at=50.0)

        self.assertEqual(
            bytes(len(moderate_echo)),
            guard.process(moderate_echo, now=50.1),
        )

    def test_delayed_correlated_playback_is_suppressed(self) -> None:
        reference = EchoReferenceBus()
        guard = ReferenceEchoGuard(
            reference,
            correlation_window_ms=200.0,
            reference_delay_max_ms=700.0,
            correlation_threshold=0.72,
            block_duration_ms=20.0,
        )
        amplitudes = [0, 500, 2_000, 4_000, 1_000, 3_000, 200, 2_500, 700, 0]
        for index, amplitude in enumerate(amplitudes):
            reference.observe(
                pcm(amplitude),
                observed_at=60.0 + index * 0.02,
            )

        outputs = [
            guard.process(
                pcm(amplitude // 4),
                now=60.36 + index * 0.02,
            )
            for index, amplitude in enumerate(amplitudes)
        ]

        self.assertEqual(bytes(len(pcm(0))), outputs[-1])
        self.assertGreaterEqual(guard.statistics.correlated_echo_blocks, 1)
        self.assertGreaterEqual(
            guard.statistics.maximum_reference_correlation,
            0.99,
        )

    def test_uncorrelated_near_end_speech_is_preserved(self) -> None:
        reference = EchoReferenceBus()
        guard = ReferenceEchoGuard(
            reference,
            correlation_window_ms=200.0,
            reference_delay_max_ms=700.0,
            correlation_threshold=0.72,
            block_duration_ms=20.0,
        )
        reference_amplitudes = [
            0, 500, 2_000, 4_000, 1_000, 3_000, 200, 2_500, 700, 0,
        ]
        microphone_amplitudes = [
            1_500, 2_500, 1_800, 2_200, 1_600,
            2_800, 1_700, 2_400, 1_900, 2_100,
        ]
        for index, amplitude in enumerate(reference_amplitudes):
            reference.observe(
                pcm(amplitude),
                observed_at=70.0 + index * 0.02,
            )

        outputs = [
            guard.process(
                pcm(amplitude),
                now=70.36 + index * 0.02,
            )
            for index, amplitude in enumerate(microphone_amplitudes)
        ]

        self.assertEqual(pcm(microphone_amplitudes[0]), outputs[-1])
        self.assertEqual(0, guard.statistics.correlated_echo_blocks)

    def test_low_level_uncorrelated_audio_is_preserved_for_downstream_vad(self) -> None:
        guard = ReferenceEchoGuard(
            EchoReferenceBus(),
            correlation_window_ms=200.0,
            block_duration_ms=20.0,
        )

        outputs = [
            guard.process(pcm(100), now=80.0 + index * 0.02)
            for index in range(12)
        ]

        self.assertTrue(
            all(output == bytes(len(output)) for output in outputs[:9])
        )
        self.assertTrue(all(output == pcm(100) for output in outputs[9:]))
        self.assertEqual(0, guard.statistics.suppressed_blocks)

    def test_weak_uncorrelated_voice_is_not_mistaken_for_echo(self) -> None:
        reference = EchoReferenceBus()
        guard = ReferenceEchoGuard(
            reference,
            correlation_window_ms=200.0,
            reference_delay_max_ms=700.0,
            correlation_threshold=0.72,
            block_duration_ms=20.0,
        )
        reference_amplitudes = [
            0, 500, 2_000, 4_000, 1_000, 3_000, 200, 2_500, 700, 0,
        ]
        microphone_amplitudes = [
            100, 180, 120, 210, 90, 160, 130, 200, 110, 170,
        ]
        for index, amplitude in enumerate(reference_amplitudes):
            reference.observe(pcm(amplitude), observed_at=90.0 + index * 0.02)

        outputs = [
            guard.process(
                pcm(amplitude),
                now=90.36 + index * 0.02,
            )
            for index, amplitude in enumerate(microphone_amplitudes)
        ]

        self.assertEqual(pcm(microphone_amplitudes[0]), outputs[-1])
        self.assertEqual(0, guard.statistics.correlated_echo_blocks)
        self.assertEqual(0, guard.statistics.suppressed_blocks)
