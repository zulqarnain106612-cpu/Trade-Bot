"""
Incremental scanner for shell-integration markers in PTY output.

The bash integration (shell_integration.py) prints FinalTerm/OSC 133
sequences around every command:

    ESC ] 133 ; E ; <command line> BEL   the command the user entered
    ESC ] 133 ; C BEL                    it is about to run
    ESC ] 133 ; D ; <status> BEL         it finished with <status>

They are what turns "the foreground process changed" into "this command
exited 2", which no amount of /proc inspection can recover once the shell has
reaped the child. The scanner only observes: the bytes still go to the
terminal unchanged, where xterm.js and other emulators ignore or use them.

PTY reads split anywhere, so a marker can straddle two chunks. The scanner
keeps the unfinished tail -- bounded, so a stray ESC ] that never terminates
cannot grow without limit -- and reports each marker with its absolute stream
offsets: where it starts (the end of a command's output) and where it ends
(the start of it).

Registry: TERM-002 (config/quality_registry.json).
"""

from __future__ import annotations

from dataclasses import dataclass

_OSC = b"\x1b]"
_BEL = b"\x07"
_ST = b"\x1b\\"
#: Longest marker kept across a chunk boundary. A command line is truncated
#: to 1024 characters by the integration script, so this is generous.
MAX_PENDING_BYTES = 4096
MAX_COMMAND_CHARS = 1024


@dataclass(frozen=True)
class Marker:
    """One shell-integration event and where it sits in the output stream."""

    kind: str  # "command" | "start" | "end"
    start: int
    end: int
    status: int | None = None
    text: str | None = None


class MarkerScanner:
    """Feed it every chunk of output, in order; it returns the markers seen."""

    def __init__(self) -> None:
        self._pending = b""
        self._pending_offset = 0

    def feed(self, chunk: bytes, offset: int) -> list[Marker]:
        """
        Scan *chunk*, which begins at absolute stream *offset*.

        Returns the markers completed by this chunk, in stream order.
        """
        if self._pending:
            data = self._pending + chunk
            base = self._pending_offset
        else:
            data = chunk
            base = offset
        self._pending = b""

        markers: list[Marker] = []
        position = 0
        while True:
            begin = data.find(_OSC, position)
            if begin < 0:
                # A lone ESC at the very end may be the first half of ESC ].
                if data.endswith(b"\x1b"):
                    self._keep(data[-1:], base + len(data) - 1)
                break
            payload_start = begin + len(_OSC)
            ends: list[tuple[int, int]] = []
            for terminator_bytes in (_BEL, _ST):
                found = data.find(terminator_bytes, payload_start)
                if found >= 0:
                    ends.append((found, len(terminator_bytes)))
            if not ends:
                tail = data[begin:]
                if len(tail) <= MAX_PENDING_BYTES:
                    self._keep(tail, base + begin)
                # An unterminated sequence longer than any marker is not a
                # marker; dropping it is what bounds memory.
                break
            terminator, width = min(ends)
            marker = _parse(data[payload_start:terminator], base + begin, base + terminator + width)
            if marker is not None:
                markers.append(marker)
            position = terminator + width
        return markers

    def _keep(self, tail: bytes, at: int) -> None:
        self._pending = tail
        self._pending_offset = at


def _parse(payload: bytes, start: int, end: int) -> Marker | None:
    if not payload.startswith(b"133;"):
        return None
    body = payload[4:].decode("utf-8", errors="replace")
    if body == "C":
        return Marker("start", start, end)
    if body.startswith("D"):
        status: int | None = None
        rest = body[2:] if body.startswith("D;") else ""
        if rest.lstrip("-").isdigit():
            status = int(rest)
        return Marker("end", start, end, status=status)
    if body.startswith("E;"):
        return Marker("command", start, end, text=body[2:][:MAX_COMMAND_CHARS])
    return None
