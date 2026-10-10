"""
Bounded, offset-addressed output retention for one PTY session.

Every byte a session produces gets an absolute offset that never changes.
Clients remember the offset they have seen up to, so a reconnecting dashboard
or a re-attaching CLI asks for "everything from N" and gets exactly the bytes
it missed -- or, if the session has since produced more than the buffer
keeps, the oldest bytes still held plus an explicit ``truncated`` flag rather
than a silent gap.

Registry: TERM-005 (config/quality_registry.json).
"""

from __future__ import annotations


class OutputBuffer:
    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._data = bytearray()
        self._start = 0

    @property
    def start(self) -> int:
        """Offset of the oldest byte still retained."""
        return self._start

    @property
    def end(self) -> int:
        """Offset one past the newest byte ever written."""
        return self._start + len(self._data)

    def append(self, chunk: bytes) -> int:
        """Store *chunk*; return the offset of its first byte."""
        offset = self.end
        self._data += chunk
        excess = len(self._data) - self._capacity
        if excess > 0:
            del self._data[:excess]
            self._start += excess
        return offset

    def read(self, since: int | None) -> tuple[int, bytes, bool]:
        """
        Bytes from *since* (or the oldest retained) to the end.

        Returns ``(offset, data, truncated)``; *truncated* is true when the
        caller asked for bytes that are no longer retained.
        """
        begin = self._start if since is None else since
        truncated = begin < self._start
        begin = min(max(begin, self._start), self.end)
        return begin, bytes(self._data[begin - self._start :]), truncated

    def slice(self, begin: int, end: int) -> tuple[bytes, bool]:
        """Bytes in ``[begin, end)``, clipped to what is retained."""
        truncated = begin < self._start
        lo = max(begin, self._start) - self._start
        hi = max(min(end, self.end) - self._start, lo)
        return bytes(self._data[lo:hi]), truncated
