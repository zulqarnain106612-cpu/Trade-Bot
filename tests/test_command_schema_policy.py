"""
Tests for COMMAND_EXEC_SCHEMA 1.1.0 policy fields and their enforcement.

Covers the five controls added in 1.1.0 -- effect classification, declared
timeout, cwd, environment containment, byte capping and secret redaction --
at the level that matters: not "is the key present" but "does the control
actually stop the thing it exists to stop".
"""

from __future__ import annotations

import os

import jsonschema
import pytest

from common.command_schema import (
    COMMAND_EXEC_SCHEMA,
    ENV_MINIMAL_BASE,
    SCHEMA_VERSION,
    build_env,
    classify,
    rank,
    redact,
)
from common.shell_exec import run


class TestSchemaIntegrity:
    def test_schema_is_a_valid_draft7_schema(self):
        jsonschema.Draft7Validator.check_schema(COMMAND_EXEC_SCHEMA)

    def test_schema_version_is_pinned_in_the_schema_itself(self):
        assert COMMAND_EXEC_SCHEMA["properties"]["schema_version"]["const"] == SCHEMA_VERSION
        assert SCHEMA_VERSION in COMMAND_EXEC_SCHEMA["$id"]

    def test_every_documented_top_level_field_is_declared(self):
        # additionalProperties is False, so a field missing here is a field
        # that silently fails validation for every caller that uses it.
        expected = {
            "command",
            "output_policy",
            "retry_policy",
            "result",
            "schema_version",
            "purpose",
            "classification",
            "confirm_destructive",
            "timeout_s",
            "cwd",
            "env",
        }
        assert set(COMMAND_EXEC_SCHEMA["properties"]) == expected

    def test_top_level_is_closed(self):
        assert COMMAND_EXEC_SCHEMA["additionalProperties"] is False

    def test_unknown_field_is_rejected(self):
        # Schema violations raise, they do not return a result: a malformed
        # declaration is a programming error, not a runtime outcome.
        with pytest.raises(ValueError):
            run(
                {
                    "command": "echo hi",
                    "output_policy": {"max_lines": 2},
                    "not_a_real_field": 1,
                }
            )

    def test_output_policy_declares_the_byte_cap_and_redaction(self):
        props = COMMAND_EXEC_SCHEMA["properties"]["output_policy"]["properties"]
        assert "max_bytes" in props
        assert "redact" in props


class TestClassify:
    @pytest.mark.parametrize(
        "command",
        [
            "rm -rf /tmp/x",
            "rm -f build/out",
            "git push --force origin main",
            "git reset --hard HEAD~3",
            "git clean -fdx",
            "DROP TABLE trades",
            "DELETE FROM orders WHERE 1=1",
            "db.trades.deleteMany({})",
            "terraform destroy -auto-approve",
            "aws s3 rm s3://bucket/key",
            "mkfs.ext4 /dev/sdb1",
            "dd if=/dev/zero of=/dev/sdb",
        ],
    )
    def test_destructive_commands_are_detected(self, command):
        assert classify(command) == "destructive"

    @pytest.mark.parametrize(
        "command",
        [
            "git commit -m 'x'",
            "pip install requests",
            "npm install",
            "mkdir -p build",
            "cp a b",
            "sed -i 's/a/b/' file",
            "echo hi > out.txt",
            "INSERT INTO trades VALUES (1)",
            "kubectl apply -f deploy.yaml",
        ],
    )
    def test_mutating_commands_are_detected(self, command):
        assert classify(command) == "mutating"

    @pytest.mark.parametrize(
        "command",
        [
            "ls -la",
            "grep -m 5 needle file.txt",
            "sed -n '1,5p' file.txt",
            "git log --oneline -n 5",
            "python3 -m pytest -q",
            "cat file | head -5",
        ],
    )
    def test_read_only_commands_are_detected(self, command):
        assert classify(command) == "read_only"

    def test_force_with_lease_is_not_flagged_as_destructive(self):
        # --force-with-lease refuses to clobber work it has not seen, which is
        # exactly the property that makes plain --force destructive.
        assert classify("git push --force-with-lease origin br") != "destructive"

    def test_rank_orders_effect_classes(self):
        assert rank("read_only") < rank("mutating") < rank("destructive")

    def test_rank_rejects_an_unknown_class(self):
        with pytest.raises(ValueError):
            rank("whatever")


class TestClassificationEnforcement:
    def test_under_declared_command_is_refused_without_running(self):
        result = run({"command": "rm -rf /tmp/should-not-run", "output_policy": {"max_lines": 5}})
        assert result["attempt_count"] == 0
        assert "refused" in result["error"]

    def test_destructive_without_confirmation_is_refused(self):
        result = run(
            {
                "command": "rm -rf /tmp/should-not-run",
                "output_policy": {"max_lines": 5},
                "classification": "destructive",
            }
        )
        assert result["attempt_count"] == 0
        assert "confirm_destructive" in result["error"]

    def test_destructive_with_confirmation_runs_and_never_retries(self, tmp_path):
        victim = tmp_path / "victim"
        victim.write_text("x")
        result = run(
            {
                "command": f"rm -rf {victim}",
                "output_policy": {"max_lines": 5},
                "classification": "destructive",
                "confirm_destructive": True,
                "retry_policy": {"max_attempts": 5, "retry_on": ["nonzero_exit"], "delay_s": 0},
            }
        )
        assert result["exit_code"] == 0
        assert result["attempt_count"] == 1, "destructive commands must never retry"
        assert not victim.exists()

    def test_over_declaring_is_allowed(self):
        # Declaring a stricter class than detected is always safe.
        result = run(
            {
                "command": "echo hi",
                "output_policy": {"max_lines": 2},
                "classification": "mutating",
            }
        )
        assert result["exit_code"] == 0

    def test_refusal_result_has_the_same_shape_as_a_normal_result(self):
        ok = run({"command": "echo hi", "output_policy": {"max_lines": 2}})
        refused = run({"command": "rm -rf /tmp/x", "output_policy": {"max_lines": 2}})
        assert set(ok) == set(refused)


class TestTimeout:
    def test_declared_timeout_overrides_the_run_kwarg(self):
        result = run(
            {"command": "sleep 5", "output_policy": {"max_lines": 2}, "timeout_s": 1},
            timeout=120,
        )
        assert result["timed_out"] is True
        assert result["duration_s"] < 5

    def test_a_command_within_its_timeout_is_not_flagged(self):
        result = run({"command": "echo hi", "output_policy": {"max_lines": 2}, "timeout_s": 30})
        assert result["timed_out"] is False


class TestCwd:
    def test_command_runs_in_the_declared_directory(self, tmp_path):
        result = run({"command": "pwd", "output_policy": {"max_lines": 2}, "cwd": str(tmp_path)})
        assert result["filtered_output"].strip() == str(tmp_path)

    def test_missing_cwd_is_refused_before_execution(self):
        result = run({"command": "pwd", "output_policy": {"max_lines": 2}, "cwd": "/no/such/dir"})
        assert result["attempt_count"] == 0
        assert "cwd" in result["error"]


class TestEnvContainment:
    def test_no_env_spec_inherits_the_parent_environment(self, monkeypatch):
        monkeypatch.setenv("TB_TEST_MARKER", "present")
        result = run({"command": "echo $TB_TEST_MARKER", "output_policy": {"max_lines": 2}})
        assert result["filtered_output"].strip() == "present"

    def test_allowlist_drops_everything_it_does_not_name(self, monkeypatch):
        monkeypatch.setenv("TB_SECRET_MARKER", "leaked")
        result = run(
            {
                "command": "env | grep -c TB_SECRET_MARKER",
                "output_policy": {"max_lines": 2},
                "env": {"inherit": True, "allowlist": ["PATH"]},
            }
        )
        assert result["filtered_output"].strip() == "0"

    def test_allowlist_keeps_the_names_it_does_name(self, monkeypatch):
        monkeypatch.setenv("TB_KEPT_MARKER", "kept")
        result = run(
            {
                "command": "echo $TB_KEPT_MARKER",
                "output_policy": {"max_lines": 2},
                "env": {"inherit": True, "allowlist": ["TB_KEPT_MARKER"]},
            }
        )
        assert result["filtered_output"].strip() == "kept"

    def test_overrides_are_applied_last(self):
        result = run(
            {
                "command": "echo $TB_OVERRIDDEN",
                "output_policy": {"max_lines": 2},
                "env": {"overrides": {"TB_OVERRIDDEN": "final"}},
            }
        )
        assert result["filtered_output"].strip() == "final"

    def test_build_env_returns_none_without_a_spec(self):
        assert build_env(None) is None
        assert build_env({}) is None

    def test_non_inheriting_env_still_carries_the_minimal_base(self, monkeypatch):
        monkeypatch.setenv("TB_SECRET_MARKER", "leaked")
        env = build_env({"inherit": False})
        assert "TB_SECRET_MARKER" not in env
        assert "PATH" in env or "PATH" not in os.environ

    def test_minimal_base_never_shrinks_silently(self):
        # PATH is what lets a scrubbed command find its binary at all; losing
        # it turns every scrubbed command into a confusing 127.
        assert "PATH" in ENV_MINIMAL_BASE


class TestByteCap:
    def test_one_enormous_line_is_capped_even_under_the_line_limit(self):
        result = run(
            {
                "command": 'head -c 20000 /dev/zero | tr "\\0" x',
                "output_policy": {"max_lines": 5, "max_bytes": 512},
            }
        )
        assert result["bytes_truncated"] is True
        assert len(result["filtered_output"].encode("utf-8")) <= 512

    def test_small_output_is_not_flagged(self):
        result = run({"command": "echo hi", "output_policy": {"max_lines": 5, "max_bytes": 512}})
        assert result["bytes_truncated"] is False

    def test_byte_cap_applies_after_the_line_cap(self):
        result = run(
            {
                "command": "seq 1 1000",
                "output_policy": {"max_lines": 3, "max_bytes": 65536},
            }
        )
        assert result["truncated"] is True
        assert result["bytes_truncated"] is False


class TestRedaction:
    @pytest.mark.parametrize(
        "secret",
        [
            "ghp_0123456789abcdefghij",
            "AKIAIOSFODNN7EXAMPLE",
            "sk-ant-0123456789abcdefghij",
            "xoxb-0123456789-abcdefghij",
        ],
    )
    def test_known_secret_shapes_are_masked(self, secret):
        masked, count = redact(f"value={secret}")
        assert secret not in masked
        assert count >= 1

    def test_url_credentials_keep_their_structure(self):
        masked, _ = redact("mongodb+srv://user:hunter2@cluster.example.net/db")
        assert "hunter2" not in masked
        assert "mongodb+srv://user:" in masked

    def test_env_style_assignments_keep_the_key_name(self):
        masked, _ = redact("DB_PASSWORD=hunter2")
        assert "hunter2" not in masked
        assert "DB_PASSWORD" in masked

    def test_pem_private_key_block_is_masked(self):
        pem = "-----BEGIN RSA PRIVATE KEY-----\nAAAA\nBBBB\n-----END RSA PRIVATE KEY-----"
        masked, count = redact(pem)
        assert "AAAA" not in masked
        assert count == 1

    def test_clean_text_is_untouched(self):
        masked, count = redact("all clear, nothing here")
        assert masked == "all clear, nothing here"
        assert count == 0

    def test_redaction_runs_on_real_command_output(self):
        result = run(
            {
                "command": "echo ghp_0123456789abcdefghij",
                "output_policy": {"max_lines": 2},
            }
        )
        assert "ghp_0123456789abcdefghij" not in result["filtered_output"]
        assert result["redactions_applied"] >= 1

    def test_redaction_can_be_disabled_explicitly(self):
        result = run(
            {
                "command": "echo ghp_0123456789abcdefghij",
                "output_policy": {"max_lines": 2, "redact": {"enabled": False}},
            }
        )
        assert "ghp_0123456789abcdefghij" in result["filtered_output"]

    def test_extra_patterns_are_honoured(self):
        masked, count = redact("internal-marker-42", extra_patterns=[r"internal-marker-\d+"])
        assert "internal-marker-42" not in masked
        assert count == 1


class TestResultProvenance:
    def test_command_hash_is_stable_and_short(self):
        a = run({"command": "echo hi", "output_policy": {"max_lines": 2}})
        b = run({"command": "echo hi", "output_policy": {"max_lines": 2}})
        c = run({"command": "echo bye", "output_policy": {"max_lines": 2}})
        assert a["command_sha256"] == b["command_sha256"]
        assert a["command_sha256"] != c["command_sha256"]
        assert len(a["command_sha256"]) == 16

    def test_duration_and_start_time_are_populated(self):
        result = run({"command": "echo hi", "output_policy": {"max_lines": 2}})
        assert result["duration_s"] >= 0
        assert result["started_at"].endswith("+00:00")

    def test_classification_is_echoed_back(self):
        result = run(
            {
                "command": "mkdir -p /tmp/tb-classification-echo",
                "output_policy": {"max_lines": 2},
                "classification": "mutating",
            }
        )
        assert result["classification"] == "mutating"
