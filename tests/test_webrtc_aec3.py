"""Tests for the WebRTC AEC3 streaming adapter."""

import unittest
from unittest.mock import MagicMock

from audio.webrtc_aec3 import WebRtcAec3


class FakeAudioProcessor:
    def __init__(self, frame_size: int = 480) -> None:
        self.frame_size = frame_size
        self.stream_formats: list[tuple[int, int, int, int]] = []
        self.reverse_formats: list[tuple[int, int]] = []
        self.delays: list[int] = []
        self.render_frames: list[bytes] = []
        self.capture_frames: list[bytes] = []

    def set_stream_format(
        self,
        sample_rate_in: int,
        channel_count_in: int,
        sample_rate_out: int,
        channel_count_out: int,
    ) -> None:
        self.stream_formats.append(
            (
                sample_rate_in,
                channel_count_in,
                sample_rate_out,
                channel_count_out,
            )
        )

    def set_reverse_stream_format(
        self,
        sample_rate_in: int,
        channel_count_in: int,
    ) -> None:
        self.reverse_formats.append((sample_rate_in, channel_count_in))

    def set_stream_delay(self, delay_ms: int) -> None:
        self.delays.append(delay_ms)

    def get_frame_size(self) -> int:
        return self.frame_size

    def process_reverse_stream(self, pcm16: bytes) -> bytes:
        self.render_frames.append(pcm16)
        return pcm16

    def process_stream(self, pcm16: bytes) -> bytes:
        self.capture_frames.append(pcm16)
        return pcm16


class WebRtcAec3Tests(unittest.TestCase):
    def test_feeds_render_before_capture_in_ten_millisecond_frames(self) -> None:
        native = FakeAudioProcessor()
        factory = MagicMock(return_value=native)
        aec3 = WebRtcAec3(
            stream_delay_ms=80,
            processor_factory=factory,
        )
        render_stereo_20_ms = b"\x01\x00\x01\x00" * 960
        capture_mono_20_ms = b"\x02\x00" * 960

        aec3.observe_render(
            render_stereo_20_ms,
            sample_rate=48_000,
            channels=2,
        )
        cleaned = aec3.process_capture(
            capture_mono_20_ms,
            sample_rate=48_000,
        )

        self.assertEqual(capture_mono_20_ms, cleaned)
        self.assertEqual([960, 960], [len(frame) for frame in native.render_frames])
        self.assertEqual([960, 960], [len(frame) for frame in native.capture_frames])
        self.assertEqual([(48_000, 1, 48_000, 1)], native.stream_formats)
        self.assertEqual([(48_000, 1)], native.reverse_formats)
        self.assertEqual([80], native.delays)
        factory.assert_called_once_with(
            enable_aec=True,
            enable_ns=False,
            enable_agc=False,
            enable_vad=False,
        )

    def test_disabled_aec3_is_a_bit_exact_bypass(self) -> None:
        factory = MagicMock()
        aec3 = WebRtcAec3(enabled=False, processor_factory=factory)
        captured = b"\x01\x00\x02\x00"

        aec3.observe_render(captured, sample_rate=48_000, channels=1)
        cleaned = aec3.process_capture(captured, sample_rate=48_000)

        self.assertEqual(captured, cleaned)
        factory.assert_not_called()

    def test_render_queue_is_bounded(self) -> None:
        aec3 = WebRtcAec3(
            render_queue_capacity_blocks=1,
            processor_factory=MagicMock(return_value=FakeAudioProcessor()),
        )

        aec3.observe_render(bytes(960), sample_rate=48_000, channels=1)
        aec3.observe_render(bytes(960), sample_rate=48_000, channels=1)

        self.assertEqual(1, aec3.statistics.dropped_render_blocks)

    def test_reset_discards_prior_call_and_recreates_native_state(self) -> None:
        first = FakeAudioProcessor()
        second = FakeAudioProcessor()
        factory = MagicMock(side_effect=[first, second])
        aec3 = WebRtcAec3(processor_factory=factory)

        aec3.process_capture(bytes(1920), sample_rate=48_000)
        aec3.reset()
        aec3.process_capture(bytes(1920), sample_rate=48_000)

        self.assertEqual(2, factory.call_count)
        self.assertEqual(1, aec3.statistics.resets)

    def test_installed_web_rtc_binding_smoke_test(self) -> None:
        aec3 = WebRtcAec3()
        silent_20_ms = bytes(1920)

        aec3.observe_render(silent_20_ms, sample_rate=48_000, channels=1)
        cleaned = aec3.process_capture(silent_20_ms, sample_rate=48_000)

        self.assertEqual(len(silent_20_ms), len(cleaned))
        self.assertEqual(2, aec3.statistics.render_frames)
        self.assertEqual(2, aec3.statistics.capture_frames)


if __name__ == "__main__":
    unittest.main()
