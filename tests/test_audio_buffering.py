"""Tests for the preallocated single-producer/single-consumer PCM buffer."""

from threading import Event, Thread
import unittest

from audio.buffering import PcmBlockBuffer


class PcmBlockBufferTests(unittest.TestCase):
    def test_concurrent_handoff_finishes_without_deadlock_or_corruption(self) -> None:
        buffer = PcmBlockBuffer(capacity=32, block_bytes=4)
        producer_done = Event()
        corrupted_blocks: list[bytes] = []
        expected = b"\x01\x02\x03\x04"

        def produce() -> None:
            for _ in range(20_000):
                buffer.write(expected)
            producer_done.set()

        def consume() -> None:
            while not producer_done.is_set() or len(buffer):
                block = buffer.pop_bytes()
                if block is not None and block != expected:
                    corrupted_blocks.append(block)

        producer = Thread(target=produce)
        consumer = Thread(target=consume)
        producer.start()
        consumer.start()
        producer.join(timeout=2)
        consumer.join(timeout=2)

        self.assertFalse(producer.is_alive())
        self.assertFalse(consumer.is_alive())
        self.assertEqual(corrupted_blocks, [])


if __name__ == "__main__":
    unittest.main()
