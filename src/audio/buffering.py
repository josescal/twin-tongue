"""Preallocated non-blocking buffers for PortAudio callback handoff."""

from collections import deque

WRITE_STORED = 0
WRITE_DROPPED_OLDEST = 1
WRITE_REJECTED = 2
WRITE_INVALID_SIZE = 3


class PcmBlockBuffer:
    """Transfer fixed PCM blocks without waits or callback-time allocation."""

    def __init__(self, capacity: int, block_bytes: int) -> None:
        if capacity <= 0:
            raise ValueError("Buffer capacity must be greater than zero.")
        if block_bytes <= 0:
            raise ValueError("PCM block size must be greater than zero.")
        self.capacity = capacity
        self.block_bytes = block_bytes
        self._available = deque(bytearray(block_bytes) for _ in range(capacity))
        self._ready: deque[bytearray] = deque()

    def write(self, block: object) -> int:
        """Copy one block into reserved storage and report any discarded block."""
        if len(block) != self.block_bytes:  # type: ignore[arg-type]
            return WRITE_INVALID_SIZE
        try:
            target = self._available.popleft()
        except IndexError:
            try:
                target = self._ready.popleft()
            except IndexError:
                return WRITE_REJECTED
            result = WRITE_DROPPED_OLDEST
        else:
            result = WRITE_STORED
        try:
            target[:] = block  # type: ignore[index]
        except BaseException:
            self._available.append(target)
            raise
        self._ready.append(target)
        return result

    def pop_bytes(self) -> bytes | None:
        """Copy and return the oldest block outside the realtime callback."""
        try:
            block = self._ready.popleft()
        except IndexError:
            return None
        try:
            return bytes(block)
        finally:
            self._available.append(block)

    def read_into(self, destination: object) -> bool:
        """Copy the oldest block into a callback buffer without allocating."""
        if len(destination) != self.block_bytes:  # type: ignore[arg-type]
            return False
        try:
            block = self._ready.popleft()
        except IndexError:
            return False
        try:
            destination[:] = block  # type: ignore[index]
        finally:
            self._available.append(block)
        return True

    def __len__(self) -> int:
        return len(self._ready)
