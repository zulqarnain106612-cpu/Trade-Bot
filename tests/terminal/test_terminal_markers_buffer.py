"""
TERM-002, TERM-005 -- shell-integration markers and offset-addressed output.

A PTY read splits anywhere, so a marker can arrive in two halves; the
scanner must still report it once, with the right offsets, and must not grow
without bound on an escape sequence that never terminates. The output buffer
is what lets a reconnecting client resume from the byte it reached -- or be
told, explicitly, that the bytes it wanted are gone.

Decides:
  - TERM-002 -- process identity and exit status are observed, not invented
  - TERM-005 -- output survives a client reconnect, gaps are reported
"""

from __future__ import annotations

import pytest

from src.terminal.buffer import OutputBuffer
from src.terminal.markers import MAX_PENDING_BYTES, Marker, MarkerScanner

START = b"\x1b]133;C\x07"
END_2 = b"\x1b]133;D;2\x07"
CMD = b"\x1b]133;E;make test\x07"


class TestMarkerScanner:
    def test_a_whole_command_cycle(self):
        stream = CMD + START + b"output\r\n" + END_2
        markers = MarkerScanner().feed(stream, 100)
        assert [m.kind for m in markers] == ["command", "start", "end"]
        assert markers[0].text == "make test"
        start, end = markers[1], markers[2]
        # The command's own output lies exactly between the two markers.
        assert stream[start.end - 100 : end.start - 100] == b"output\r\n"
        assert end.status == 2

    @pytest.mark.parametrize("split", range(1, len(END_2)))
    def test_a_marker_split_anywhere_is_reported_once(self, split):
        scanner = MarkerScanner()
        first = scanner.feed(b"ab" + END_2[:split], 0)
        second = scanner.feed(END_2[split:] + b"cd", 2 + split)
        assert first == []
        assert second == [Marker("end", 2, 2 + len(END_2), status=2)]

    def test_string_terminator_is_accepted_as_well_as_bel(self):
        (marker,) = MarkerScanner().feed(b"\x1b]133;D;0\x1b\\", 0)
        assert marker.status == 0

    def test_end_without_a_status_is_unknown_not_success(self):
        assert MarkerScanner().feed(b"\x1b]133;D\x07", 0)[0].status is None
        assert MarkerScanner().feed(b"\x1b]133;D;x\x07", 0)[0].status is None

    def test_other_escape_sequences_are_ignored(self):
        stream = b"\x1b]0;window title\x07\x1b]7;file://h/tmp\x07\x1b]133;A\x07\x1b[31mred"
        assert MarkerScanner().feed(stream, 0) == []

    def test_an_unterminated_sequence_is_dropped_once_it_is_too_long(self):
        scanner = MarkerScanner()
        assert scanner.feed(b"\x1b]133;E;" + b"x" * (MAX_PENDING_BYTES + 10), 0) == []
        # Nothing was kept, so a fresh marker afterwards parses normally.
        assert scanner.feed(START, 10_000)[0].kind == "start"

    def test_a_lone_escape_at_the_end_of_a_chunk_is_kept(self):
        scanner = MarkerScanner()
        assert scanner.feed(b"abc\x1b", 0) == []
        (marker,) = scanner.feed(b"]133;C\x07", 4)
        assert (marker.start, marker.end) == (3, 3 + len(START))

    def test_a_long_command_line_is_truncated(self):
        (marker,) = MarkerScanner().feed(b"\x1b]133;E;" + b"y" * 2000 + b"\x07", 0)
        assert len(marker.text) == 1024


class TestOutputBuffer:
    def test_offsets_are_absolute_and_stable(self):
        buffer = OutputBuffer(8)
        assert buffer.append(b"abcd") == 0
        assert buffer.append(b"efgh") == 4
        assert buffer.append(b"ij") == 8
        assert (buffer.start, buffer.end) == (2, 10)

    def test_reading_from_an_offset_returns_exactly_the_missing_bytes(self):
        buffer = OutputBuffer(16)
        buffer.append(b"hello world")
        assert buffer.read(6) == (6, b"world", False)
        assert buffer.read(None) == (0, b"hello world", False)
        assert buffer.read(99) == (11, b"", False)

    def test_asking_for_bytes_no_longer_retained_says_so(self):
        buffer = OutputBuffer(4)
        buffer.append(b"0123456789")
        assert buffer.read(2) == (6, b"6789", True)

    def test_slice(self):
        buffer = OutputBuffer(6)
        buffer.append(b"0123456789")  # retains "456789", from offset 4
        assert buffer.slice(5, 8) == (b"567", False)
        assert buffer.slice(2, 8) == (b"4567", True)
        assert buffer.slice(9, 4) == (b"", False)

    def test_capacity_must_be_positive(self):
        with pytest.raises(ValueError):
            OutputBuffer(0)
