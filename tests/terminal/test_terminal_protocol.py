"""
TERM-003 -- every request is validated before it reaches a PTY.

A browser page, a confused script and a well-behaved CLI all speak to the same
daemon. These tests hold the boundary: oversized and malformed frames are
refused with a stable code, identifiers must have the daemon's own shape, a
job's argv is a list handed to execve (never a string for a shell), and only
the four signals a terminal user sends can be delivered.

Decides:
  - TERM-003 -- terminal requests are validated at the boundary
"""

from __future__ import annotations

import signal

import pytest

from src.terminal import protocol
from src.terminal.protocol import ProtocolError


def _code(fn, *args, **kwargs) -> str:
    with pytest.raises(ProtocolError) as exc_info:
        fn(*args, **kwargs)
    return exc_info.value.code


class TestFrames:
    def test_a_well_formed_frame_is_returned_as_a_dict(self):
        assert protocol.decode_frame('{"t":"ping","id":3}', 100) == {"t": "ping", "id": 3}

    def test_bytes_are_accepted_too(self):
        assert protocol.decode_frame(b'{"t":"ping"}', 100)["t"] == "ping"

    @pytest.mark.parametrize(
        ("raw", "code"),
        [
            ("x" * 101, "frame_too_large"),
            ("{nope", "bad_json"),
            (b"\xff\xfe", "bad_json"),
            ("[1, 2]", "bad_frame"),
            ('{"id": 1}', "bad_frame"),
            ('{"t": ""}', "bad_frame"),
            ('{"t": "ping", "id": true}', "bad_frame"),
            ('{"t": "ping", "id": [1]}', "bad_frame"),
            ('{"t": "ping", "id": "' + "i" * 65 + '"}', "bad_frame"),
        ],
    )
    def test_a_bad_frame_is_refused_with_a_stable_code(self, raw, code):
        assert _code(protocol.decode_frame, raw, 100) == code

    def test_encode_round_trips_unicode_without_escaping(self):
        assert protocol.encode_frame({"t": "x", "s": "é✓"}) == '{"t":"x","s":"é✓"}'

    def test_output_is_base64(self):
        assert protocol.b64encode(b"\x1b[31m\xff") == "G1szMW3/"


class TestIdentifiers:
    def test_new_ids_have_the_validated_shape(self):
        assert protocol.SESSION_ID_RE.match(protocol.new_session_id())
        assert protocol.PROCESS_ID_RE.match(protocol.new_process_id())

    @pytest.mark.parametrize("value", [None, 7, "s-123", "s-ABCDEFGH", "../etc", "s-0123456789"])
    def test_a_malformed_session_id_is_refused(self, value):
        assert _code(protocol.session_id, {"sid": value}) == "bad_session_id"

    def test_a_well_formed_session_id_passes(self):
        assert protocol.session_id({"sid": "s-0a1b2c3d"}) == "s-0a1b2c3d"

    @pytest.mark.parametrize("value", [None, "p-xyz", "s-0a1b2c3d"])
    def test_a_malformed_process_id_is_refused(self, value):
        assert _code(protocol.process_id, {"process_id": value}) == "bad_process_id"

    def test_a_well_formed_process_id_passes(self):
        assert protocol.process_id({"process_id": "p-0a1b2c3d"}) == "p-0a1b2c3d"


class TestFields:
    def test_bounded_int_takes_the_default_when_absent(self):
        assert protocol.bounded_int({}, "rows", 1, 10, 5) == 5

    @pytest.mark.parametrize(
        "frame", [{}, {"rows": 0}, {"rows": 11}, {"rows": True}, {"rows": "3"}]
    )
    def test_bounded_int_refuses_missing_or_out_of_range(self, frame):
        assert _code(protocol.bounded_int, frame, "rows", 1, 10) == "bad_field"

    def test_offsets(self):
        assert protocol.optional_offset({}) is None
        assert protocol.optional_offset({"since": 9}) == 9
        for bad in (-1, True, "1"):
            assert _code(protocol.optional_offset, {"since": bad}) == "bad_field"

    def test_text_fields_are_bounded(self):
        assert protocol.text_field({"data": "ab"}, "data", 2) == "ab"
        assert _code(protocol.text_field, {"data": "abc"}, "data", 2) == "too_large"
        assert _code(protocol.text_field, {"data": 3}, "data", 2) == "bad_field"

    @pytest.mark.parametrize("value", ["", "   ", "x" * 65, "a\x1bb", "line\nbreak", 5])
    def test_a_name_must_be_short_printable_text(self, value):
        assert _code(protocol.validate_name, value) == "bad_name"

    def test_a_name_is_trimmed(self):
        assert protocol.validate_name("  build  ") == "build"
        assert protocol.optional_name({}) is None
        assert protocol.optional_name({"name": "x"}) == "x"


class TestArgvAndCwd:
    def test_argv_is_passed_through_as_a_list(self):
        assert protocol.validate_argv(["sh", "-c", "echo $HOME; rm -rf /"]) == [
            "sh",
            "-c",
            "echo $HOME; rm -rf /",
        ]

    @pytest.mark.parametrize(
        "value",
        ["sh -c ls", [], [""], ["ok", 3], ["a\x00b"], ["x" * 5000], ["a"] * 300],
    )
    def test_anything_but_a_list_of_clean_strings_is_refused(self, value):
        assert _code(protocol.validate_argv, value) == "bad_argv"

    def test_cwd_defaults_and_must_be_an_existing_absolute_directory(self, tmp_path):
        assert protocol.validate_cwd(None, tmp_path) == tmp_path
        assert protocol.validate_cwd(str(tmp_path), tmp_path) == tmp_path
        for bad in ("", 3, "relative/dir", str(tmp_path / "missing"), "a\x00b"):
            assert _code(protocol.validate_cwd, bad, tmp_path) == "bad_cwd"


class TestSignals:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("INT", signal.SIGINT),
            ("term", signal.SIGTERM),
            ("KILL", signal.SIGKILL),
            ("HUP", signal.SIGHUP),
        ],
    )
    def test_the_four_terminal_signals(self, name, expected):
        assert protocol.signal_from({"signal": name}) == expected

    def test_the_default_applies_when_absent(self):
        assert protocol.signal_from({}, "TERM") == signal.SIGTERM

    @pytest.mark.parametrize("name", ["STOP", "USR1", "9", 9, ""])
    def test_any_other_signal_is_refused(self, name):
        assert _code(protocol.signal_from, {"signal": name}) == "bad_signal"

    def test_the_signal_table_cannot_be_extended_at_runtime(self):
        with pytest.raises(TypeError):
            protocol.SIGNALS["STOP"] = signal.SIGSTOP  # type: ignore[index]


def test_job_registration_is_not_a_client_request():
    """job.* is for local programs; a browser frame of that type is a different list."""
    assert not protocol.JOB_REQUESTS & protocol.CLIENT_REQUESTS
