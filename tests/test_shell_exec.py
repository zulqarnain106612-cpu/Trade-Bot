"""
Tests for common/shell_exec.py and common/command_schema.py

The module is the runtime enforcer behind COMMAND_EXEC_SCHEMA: it caps command
output before it reaches a caller, applies a pre-cap filter, and honours a
bounded retry policy. The properties worth pinning are the ones a regression
would silently break -- the cap actually dropping lines, the filter running
*before* the cap, and retry firing on exactly the declared conditions.

Every test drives real subprocesses (echo/seq/printf/sleep) rather than mocking
Popen, because the timeout path depends on real process-group behaviour. They
are all sub-second except the two timeout cases, which use a 1s ceiling.
"""

from __future__ import annotations

import builtins
import importlib
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

import common.shell_exec
from common.command_schema import COMMAND_EXEC_SCHEMA
from common.shell_exec import _cap, _capture, _filter, _should_retry, run

# ---------------------------------------------------------------------------
# Schema shape
# ---------------------------------------------------------------------------


class TestCommandExecSchema:
    def test_requires_command_and_output_policy(self):
        assert COMMAND_EXEC_SCHEMA["required"] == ["command", "output_policy"]

    def test_max_lines_is_bounded_to_200(self):
        max_lines = COMMAND_EXEC_SCHEMA["properties"]["output_policy"]["properties"]["max_lines"]
        assert max_lines["minimum"] == 1
        assert max_lines["maximum"] == 200

    def test_filter_modes_are_closed_set(self):
        modes = COMMAND_EXEC_SCHEMA["properties"]["output_policy"]["properties"]["filter_mode"][
            "enum"
        ]
        assert modes == ["none", "head", "tail", "grep", "regex", "jq", "fields"]

    def test_result_is_read_only(self):
        assert COMMAND_EXEC_SCHEMA["properties"]["result"]["readOnly"] is True

    def test_additional_properties_are_rejected(self):
        assert COMMAND_EXEC_SCHEMA["additionalProperties"] is False


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_rejects_max_lines_above_ceiling(self):
        with pytest.raises(ValueError):
            run({"command": "echo hi", "output_policy": {"max_lines": 9999}})

    def test_rejects_max_lines_of_zero(self):
        with pytest.raises(ValueError):
            run({"command": "echo hi", "output_policy": {"max_lines": 0}})

    def test_rejects_missing_output_policy(self):
        with pytest.raises(ValueError):
            run({"command": "echo hi"})

    def test_rejects_empty_command(self):
        with pytest.raises(ValueError):
            run({"command": "", "output_policy": {"max_lines": 5}})

    def test_rejects_unknown_filter_mode(self):
        with pytest.raises(ValueError):
            run(
                {
                    "command": "echo hi",
                    "output_policy": {"max_lines": 5, "filter_mode": "sed"},
                }
            )


class TestValidationFallbackWithoutJsonschema:
    """The module degrades to structural checks when jsonschema is absent."""

    @staticmethod
    def _reimport_without_jsonschema():
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "jsonschema":
                raise ImportError("simulated missing jsonschema")
            return real_import(name, *args, **kwargs)

        saved = sys.modules.pop("common.shell_exec", None)
        try:
            with patch.object(builtins, "__import__", fake_import):
                return importlib.import_module("common.shell_exec")
        finally:
            sys.modules.pop("common.shell_exec", None)
            if saved is not None:
                sys.modules["common.shell_exec"] = saved

    def test_fallback_still_runs_a_valid_declaration(self):
        mod = self._reimport_without_jsonschema()
        result = mod.run({"command": "echo ok", "output_policy": {"max_lines": 5}})
        assert result["exit_code"] == 0
        assert "ok" in result["filtered_output"]

    def test_fallback_rejects_non_dict(self):
        mod = self._reimport_without_jsonschema()
        with pytest.raises(ValueError, match="must be a dict"):
            mod.run(["not", "a", "dict"])

    def test_fallback_rejects_missing_command(self):
        mod = self._reimport_without_jsonschema()
        with pytest.raises(ValueError, match="command"):
            mod.run({"output_policy": {"max_lines": 5}})

    def test_fallback_rejects_missing_output_policy(self):
        mod = self._reimport_without_jsonschema()
        with pytest.raises(ValueError, match="output_policy"):
            mod.run({"command": "echo hi"})

    def test_fallback_rejects_missing_max_lines(self):
        mod = self._reimport_without_jsonschema()
        with pytest.raises(ValueError, match="max_lines"):
            mod.run({"command": "echo hi", "output_policy": {}})

    def test_fallback_rejects_out_of_range_max_lines(self):
        mod = self._reimport_without_jsonschema()
        with pytest.raises(ValueError, match="1"):
            mod.run({"command": "echo hi", "output_policy": {"max_lines": 500}})


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


class TestCapture:
    def test_captures_stdout_only(self):
        code, text = _capture("echo out; echo err >&2", "stdout", 10)
        assert code == 0
        assert "out" in text
        assert "err" not in text

    def test_captures_stderr_only(self):
        _, text = _capture("echo out; echo err >&2", "stderr", 10)
        assert "err" in text
        assert "out" not in text

    def test_captures_both_streams(self):
        _, text = _capture("echo out; echo err >&2", "both", 10)
        assert "out" in text
        assert "err" in text

    def test_propagates_exit_code(self):
        code, _ = _capture("exit 3", "stdout", 10)
        assert code == 3

    def test_timeout_raises_and_kills_the_process_group(self):
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            _capture("sleep 30", "stdout", 1)
        assert time.monotonic() - started < 10

    def test_timeout_tolerates_already_dead_process_group(self):
        """The ProcessLookupError suppression path: group gone before killpg."""
        with (
            patch("common.shell_exec.os.killpg", side_effect=ProcessLookupError),
            pytest.raises(subprocess.TimeoutExpired),
        ):
            _capture("sleep 30", "stdout", 1)

    def test_new_session_is_requested_without_preexec_fn(self):
        """The process group must come from start_new_session, not preexec_fn.

        preexec_fn runs Python between fork and exec. That is not
        async-signal-safe: if any other thread holds a lock at fork time the
        child can deadlock before exec, and this module is called from
        FastAPI handlers and asyncio executors, both threaded. The two
        timeout tests above prove a killable group still exists; this one
        pins how it is created, because both spellings pass those tests and
        only one is safe.
        """
        import ast
        from pathlib import Path as _Path

        module = _Path(common.shell_exec.__file__)
        tree = ast.parse(module.read_text(), filename=str(module))

        popens = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "Popen"
        ]
        assert popens, "no subprocess.Popen call found"

        for call in popens:
            kwargs = {kw.arg for kw in call.keywords}
            assert "preexec_fn" not in kwargs, (
                "preexec_fn is unsafe in a threaded process; use start_new_session=True"
            )
            assert "start_new_session" in kwargs, (
                "the timeout path kills a process group, which needs start_new_session=True"
            )


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


class TestFilter:
    def test_none_passes_text_through(self):
        assert _filter("a\nb", "none", "") == "a\nb"

    def test_empty_mode_passes_text_through(self):
        assert _filter("a\nb", "", "") == "a\nb"

    def test_head_takes_first_n(self):
        assert _filter("1\n2\n3\n4", "head", "2") == "1\n2"

    def test_head_defaults_when_expr_is_not_numeric(self):
        assert _filter("1\n2", "head", "abc") == "1\n2"

    def test_tail_takes_last_n(self):
        assert _filter("1\n2\n3\n4", "tail", "2") == "3\n4"

    def test_tail_defaults_when_expr_is_empty(self):
        assert _filter("1\n2", "tail", "") == "1\n2"

    def test_grep_keeps_only_matching_lines(self):
        assert _filter("INFO ok\nERROR bad", "grep", "ERROR") == "ERROR bad"

    def test_regex_keeps_only_matching_lines(self):
        assert _filter("aaa\nbbb", "regex", r"^b+$") == "bbb"

    def test_regex_rejects_an_invalid_pattern(self):
        with pytest.raises(ValueError, match="not a valid regex"):
            _filter("x", "regex", "(unclosed")

    def test_fields_keeps_only_listed_keys(self):
        out = _filter('{"a": 1, "b": 2, "c": 3}', "fields", "a,c")
        assert out == '{"a": 1, "c": 3}'

    def test_fields_passes_non_json_lines_through(self):
        assert _filter("not json", "fields", "a") == "not json"

    def test_fields_without_expr_returns_text(self):
        assert _filter('{"a": 1}', "fields", "") == '{"a": 1}'

    def test_fields_skips_blank_lines(self):
        assert _filter('\n{"a": 1}\n\n', "fields", "a") == '{"a": 1}'

    def test_fields_tolerates_a_bare_json_scalar(self):
        # json.loads succeeds but the value is not a dict -> TypeError branch.
        assert _filter("12345", "fields", "a") == "12345"

    def test_unknown_mode_passes_text_through(self):
        assert _filter("a\nb", "totally-unknown", "") == "a\nb"


class TestJqFilter:
    def test_raises_a_clear_error_when_jq_is_absent(self):
        with (
            patch("common.shell_exec.shutil.which", return_value=None),
            pytest.raises(RuntimeError, match="requires 'jq' on PATH"),
        ):
            _filter("{}", "jq", ".")

    def test_returns_jq_stdout_when_jq_succeeds(self):
        completed = subprocess.CompletedProcess([], 0, stdout="value\n", stderr="")
        with (
            patch("common.shell_exec.shutil.which", return_value="/usr/bin/jq"),
            patch("common.shell_exec.subprocess.run", return_value=completed),
        ):
            assert _filter('{"k": "value"}', "jq", ".k") == "value"

    def test_raises_when_the_jq_query_fails(self):
        completed = subprocess.CompletedProcess([], 5, stdout="", stderr="boom")
        with (
            patch("common.shell_exec.shutil.which", return_value="/usr/bin/jq"),
            patch("common.shell_exec.subprocess.run", return_value=completed),
            pytest.raises(ValueError, match="jq query failed: boom"),
        ):
            _filter("{}", "jq", ".bad")


# ---------------------------------------------------------------------------
# Cap
# ---------------------------------------------------------------------------


class TestCap:
    def test_returns_text_unchanged_when_within_cap(self):
        text, truncated = _cap("a\nb", 5)
        assert text == "a\nb"
        assert truncated is False

    def test_drops_excess_and_flags_truncation(self):
        text, truncated = _cap("\n".join(str(i) for i in range(10)), 3)
        assert truncated is True
        assert text.splitlines()[:3] == ["0", "1", "2"]

    def test_truncation_notice_reports_the_dropped_count(self):
        text, _ = _cap("\n".join(str(i) for i in range(10)), 4)
        assert "6 lines dropped" in text

    def test_exactly_at_the_cap_is_not_truncated(self):
        _, truncated = _cap("a\nb\nc", 3)
        assert truncated is False


# ---------------------------------------------------------------------------
# Retry predicate
# ---------------------------------------------------------------------------


class TestShouldRetry:
    def test_nonzero_exit_triggers_retry(self):
        assert _should_retry(1, "out", ["nonzero_exit"], "") is True

    def test_zero_exit_does_not_trigger_nonzero_retry(self):
        assert _should_retry(0, "out", ["nonzero_exit"], "") is False

    def test_empty_output_triggers_retry(self):
        assert _should_retry(0, "   ", ["empty_output"], "") is True

    def test_non_empty_output_does_not_trigger_retry(self):
        assert _should_retry(0, "data", ["empty_output"], "") is False

    def test_absent_pattern_triggers_retry(self):
        assert _should_retry(0, "hello", ["pattern_absent"], "NOPE") is True

    def test_present_pattern_does_not_trigger_retry(self):
        assert _should_retry(0, "hello", ["pattern_absent"], "hello") is False

    def test_pattern_absent_without_a_pattern_is_inert(self):
        assert _should_retry(0, "hello", ["pattern_absent"], "") is False

    def test_no_conditions_means_no_retry(self):
        assert _should_retry(1, "", [], "") is False


# ---------------------------------------------------------------------------
# run() end to end
# ---------------------------------------------------------------------------


class TestRun:
    def test_returns_the_documented_result_shape(self):
        result = run({"command": "echo hi", "output_policy": {"max_lines": 5}})
        assert set(result) == {
            "exit_code",
            "filtered_output",
            "truncated",
            "attempt_count",
            "error",
        }

    def test_successful_command_reports_no_error(self):
        result = run({"command": "echo hi", "output_policy": {"max_lines": 5}})
        assert result["exit_code"] == 0
        assert result["error"] is None
        assert result["attempt_count"] == 1

    def test_caps_output_and_flags_truncation(self):
        result = run({"command": "seq 1 100", "output_policy": {"max_lines": 5}})
        assert result["truncated"] is True
        assert len(result["filtered_output"].splitlines()) == 6  # 5 + notice

    def test_filter_runs_before_the_cap(self):
        # 100 lines, only one matches; a cap of 2 must not hide the match.
        result = run(
            {
                "command": "seq 1 100",
                "output_policy": {
                    "max_lines": 2,
                    "filter_mode": "grep",
                    "filter_expr": "42",
                },
            }
        )
        assert result["filtered_output"] == "42"
        assert result["truncated"] is False

    def test_grep_filter_excludes_non_matching_lines(self):
        result = run(
            {
                "command": "printf 'INFO ok\\nERROR bad\\n'",
                "output_policy": {
                    "max_lines": 50,
                    "filter_mode": "grep",
                    "filter_expr": "ERROR",
                },
            }
        )
        assert "INFO" not in result["filtered_output"]
        assert "ERROR bad" in result["filtered_output"]

    def test_captures_stderr_when_requested(self):
        result = run(
            {
                "command": "echo boom >&2",
                "output_policy": {"max_lines": 5, "stream": "stderr"},
            }
        )
        assert "boom" in result["filtered_output"]

    def test_nonzero_exit_is_reported(self):
        result = run({"command": "exit 7", "output_policy": {"max_lines": 5}})
        assert result["exit_code"] == 7


class TestRunRetry:
    def test_retries_on_nonzero_exit_then_succeeds(self, tmp_path):
        flag = tmp_path / "flag"
        result = run(
            {
                "command": f"test -f {flag} && echo ok || (touch {flag} && exit 1)",
                "output_policy": {"max_lines": 5},
                "retry_policy": {
                    "max_attempts": 2,
                    "retry_on": ["nonzero_exit"],
                    "delay_s": 0,
                },
            }
        )
        assert result["attempt_count"] == 2
        assert result["exit_code"] == 0

    def test_stops_at_max_attempts_when_always_failing(self):
        result = run(
            {
                "command": "exit 1",
                "output_policy": {"max_lines": 5},
                "retry_policy": {
                    "max_attempts": 3,
                    "retry_on": ["nonzero_exit"],
                    "delay_s": 0,
                },
            }
        )
        assert result["attempt_count"] == 3
        assert result["exit_code"] == 1

    def test_does_not_retry_a_passing_command(self):
        result = run(
            {
                "command": "echo ok",
                "output_policy": {"max_lines": 5},
                "retry_policy": {
                    "max_attempts": 3,
                    "retry_on": ["nonzero_exit"],
                    "delay_s": 0,
                },
            }
        )
        assert result["attempt_count"] == 1

    def test_retries_while_the_required_pattern_is_absent(self):
        result = run(
            {
                "command": "echo hello",
                "output_policy": {"max_lines": 5},
                "retry_policy": {
                    "max_attempts": 2,
                    "retry_on": ["pattern_absent"],
                    "pattern_absent": "NOPE",
                    "delay_s": 0,
                },
            }
        )
        assert result["attempt_count"] == 2

    def test_does_not_retry_when_the_required_pattern_is_present(self):
        result = run(
            {
                "command": "echo hello",
                "output_policy": {"max_lines": 5},
                "retry_policy": {
                    "max_attempts": 2,
                    "retry_on": ["pattern_absent"],
                    "pattern_absent": "hello",
                    "delay_s": 0,
                },
            }
        )
        assert result["attempt_count"] == 1


class TestRunOnEmpty:
    def test_on_empty_error_sets_an_error(self):
        result = run(
            {
                "command": "true",
                "output_policy": {"max_lines": 5, "on_empty": "error"},
            }
        )
        assert result["error"] == "empty output after filter"

    def test_on_empty_ok_leaves_no_error(self):
        result = run(
            {
                "command": "true",
                "output_policy": {"max_lines": 5, "on_empty": "ok"},
            }
        )
        assert result["error"] is None

    def test_on_empty_error_retries_before_giving_up(self, tmp_path):
        flag = tmp_path / "flag"
        result = run(
            {
                "command": f"test -f {flag} && echo ok || touch {flag}",
                "output_policy": {"max_lines": 5, "on_empty": "error"},
                "retry_policy": {
                    "max_attempts": 2,
                    "retry_on": ["empty_output"],
                    "delay_s": 0,
                },
            }
        )
        assert result["attempt_count"] == 2
        assert result["error"] is None
        assert "ok" in result["filtered_output"]


class TestRunTimeout:
    def test_timeout_is_reported_as_an_error(self):
        result = run({"command": "sleep 30", "output_policy": {"max_lines": 5}}, timeout=1)
        assert result["error"] is not None
        assert "timed out" in result["error"]

    def test_timeout_does_not_exceed_the_budget_by_much(self):
        started = time.monotonic()
        run({"command": "sleep 30", "output_policy": {"max_lines": 5}}, timeout=1)
        assert time.monotonic() - started < 15

    def test_timeout_retries_up_to_max_attempts(self):
        result = run(
            {
                "command": "sleep 30",
                "output_policy": {"max_lines": 5},
                "retry_policy": {
                    "max_attempts": 2,
                    "retry_on": ["nonzero_exit"],
                    "delay_s": 0,
                },
            },
            timeout=1,
        )
        assert result["attempt_count"] == 2
        assert "timed out" in result["error"]
