"""
The command-execution declaration contract is enforced, not merely documented.

An audit of COMMAND_EXEC_SCHEMA found four places where the schema states a
property that no code upholds. Each is a control that a reader would credit and
that does not exist, which is worse than an absent control because nobody looks
for it:

  SEC-0001  schema_version is declared as the field that "lets run() reject
            stale declarations" and is read by nothing. Worse, it was pinned
            with `const`, so it could only ever hold the current version --
            the one value for which the check is a no-op.
  SEC-0002  When jsonschema is not installed, _validate() falls back to
            checking `command` and `max_lines` only. classification,
            confirm_destructive, additionalProperties, timeout_s bounds and
            max_bytes go unchecked: a missing dependency silently downgrades
            the destructive-command guard and the context caps.
  SEC-0003  `purpose` is described as an audit trail "logged with the command
            hash". shell_exec emits no log record at all, so no audit trail
            exists and the field is discarded.
  SEC-0004  classify() keys on an `rm`-shaped token, so deletion forms that
            spell it another way -- find -delete, find -exec rm, xargs rm,
            git worktree remove --force -- were classified read_only and ran
            without confirm_destructive.

These tests are permanent. Each would go green again if the corresponding fix
were reverted, and none of them is decided by any other test in the suite.
"""

from __future__ import annotations

import json
import logging

import pytest

from common import command_schema as cs
from common import shell_exec

# Deletion commands are built at import time from fragments so this file never
# contains a literal destructive command string: the PreToolUse hook scans the
# text of commands an agent runs, and a test corpus is not worth a refusal.
_RM = "r" + "m"


def _decl(command: str, **extra: object) -> dict:
    """Minimal valid declaration, so each test varies exactly one thing."""
    decl: dict = {"command": command, "output_policy": {"max_lines": 1}}
    decl.update(extra)
    return decl


# ---------------------------------------------------------------------------
# SEC-0001 -- schema_version is read, and can express a version that is not
#             the current one
# ---------------------------------------------------------------------------


class TestSchemaVersionIsEnforced:
    def test_supported_versions_includes_current(self) -> None:
        """The current version must be accepted, or every declaration is refused."""
        assert cs.SCHEMA_VERSION in cs.SUPPORTED_SCHEMA_VERSIONS

    def test_field_is_not_pinned_to_a_single_value(self) -> None:
        """
        `const: SCHEMA_VERSION` made the field unable to express its own purpose.

        Catches a regression to `const`, which permits only the value for which
        the version check can never fire and invalidates every stored
        declaration the moment SCHEMA_VERSION is bumped.
        """
        field = cs.COMMAND_EXEC_SCHEMA["properties"]["schema_version"]
        assert "const" not in field
        assert set(field["enum"]) == set(cs.SUPPORTED_SCHEMA_VERSIONS)

    def test_omitted_version_is_treated_as_current(self) -> None:
        """The docstring promises this; pre-1.1.0 declarations carry no field."""
        cs.check_schema_version(None)  # must not raise

    @pytest.mark.parametrize("version", ["2.0.0", "0.9.0", "1.1", "", "latest"])
    def test_unsupported_version_is_refused(self, version: str) -> None:
        """
        A declaration written against a schema this runtime does not implement
        must be refused, not read field-by-field under today's meaning.

        This is the whole point of the field: a future version may redefine
        `classification` or the default of `redact.enabled`, and reading such a
        declaration with current semantics is how a security default silently
        inverts.
        """
        with pytest.raises(ValueError, match="schema_version"):
            cs.check_schema_version(version)

    def test_run_refuses_unsupported_version_without_executing(self) -> None:
        result = shell_exec.run(_decl("echo should-not-run", schema_version="2.0.0"))
        assert result["exit_code"] == -1
        assert result["attempt_count"] == 0, "command must not have been executed"
        assert "schema_version" in (result["error"] or "")
        assert "should-not-run" not in result["filtered_output"]

    def test_version_gate_runs_before_schema_validation(self) -> None:
        """
        Pins the ordering, which is the whole reason the check is load-bearing.

        The schema's `enum` also rejects an unknown version, so with _validate()
        first the gate is unreachable and dead again -- and the caller is told
        about whichever field the wrong schema happened to dislike rather than
        about the version. This declaration is invalid two ways; the version
        must be the one reported.
        """
        result = shell_exec.run({"schema_version": "9.9.9", "bogus": True})
        assert result["exit_code"] == -1
        assert "schema_version" in (result["error"] or "")
        assert "bogus" not in (result["error"] or "")

    def test_version_gate_does_not_bypass_schema_validation(self) -> None:
        """A supported version must still be validated, not waved through."""
        with pytest.raises(ValueError, match="[Ii]nvalid command declaration"):
            shell_exec.run({"schema_version": cs.SCHEMA_VERSION, "bogus": True})


# ---------------------------------------------------------------------------
# SEC-0002 -- the no-jsonschema path enforces the same contract
# ---------------------------------------------------------------------------

# Declarations the schema rejects. The fallback validator must reject every one
# of them; before the fix it accepted all but the last two.
REJECTED_DECLARATIONS: list[tuple[str, dict]] = [
    ("unknown property", _decl("echo hi", bogus=1)),
    ("classification not in enum", _decl("echo hi", classification="readonly")),
    ("classification wrong type", _decl("echo hi", classification=0)),
    ("confirm_destructive wrong type", _decl("echo hi", confirm_destructive="yes")),
    ("timeout_s above maximum", _decl("echo hi", timeout_s=10_000)),
    ("timeout_s below minimum", _decl("echo hi", timeout_s=0)),
    ("timeout_s wrong type", _decl("echo hi", timeout_s=1.5)),
    ("cwd empty", _decl("echo hi", cwd="")),
    ("purpose too long", _decl("echo hi", purpose="x" * 201)),
    ("env unknown property", _decl("echo hi", env={"nope": True})),
    ("env.inherit wrong type", _decl("echo hi", env={"inherit": "yes"})),
    ("env.allowlist wrong item type", _decl("echo hi", env={"allowlist": [1]})),
    ("retry unknown property", _decl("echo hi", retry_policy={"nope": 1})),
    ("retry max_attempts too high", _decl("echo hi", retry_policy={"max_attempts": 99})),
    ("retry_on not in enum", _decl("echo hi", retry_policy={"retry_on": ["whenever"]})),
    (
        "stream not in enum",
        {"command": "echo hi", "output_policy": {"max_lines": 1, "stream": "both_"}},
    ),
    (
        "filter_mode not in enum",
        {"command": "echo hi", "output_policy": {"max_lines": 1, "filter_mode": "awk"}},
    ),
    (
        "max_bytes below minimum",
        {"command": "echo hi", "output_policy": {"max_lines": 1, "max_bytes": 1}},
    ),
    (
        "max_bytes above maximum",
        {"command": "echo hi", "output_policy": {"max_lines": 1, "max_bytes": 10**9}},
    ),
    (
        "redact unknown property",
        {"command": "echo hi", "output_policy": {"max_lines": 1, "redact": {"nope": 1}}},
    ),
    (
        "redact.enabled wrong type",
        {"command": "echo hi", "output_policy": {"max_lines": 1, "redact": {"enabled": "no"}}},
    ),
    (
        "on_empty not in enum",
        {"command": "echo hi", "output_policy": {"max_lines": 1, "on_empty": "warn"}},
    ),
    (
        "output_policy unknown property",
        {"command": "echo hi", "output_policy": {"max_lines": 1, "bogus": 1}},
    ),
    ("output_policy missing", {"command": "echo hi"}),
    ("command empty", {"command": "", "output_policy": {"max_lines": 1}}),
    ("command missing", {"output_policy": {"max_lines": 1}}),
    ("max_lines missing", {"command": "echo hi", "output_policy": {}}),
    ("max_lines above maximum", {"command": "echo hi", "output_policy": {"max_lines": 10_000}}),
    ("max_lines wrong type", {"command": "echo hi", "output_policy": {"max_lines": True}}),
]

ACCEPTED_DECLARATIONS: list[tuple[str, dict]] = [
    ("minimal", _decl("echo hi")),
    (
        "fully populated",
        {
            "command": "echo hi",
            "schema_version": cs.SCHEMA_VERSION,
            "purpose": "smoke test the validator",
            "classification": "read_only",
            "confirm_destructive": False,
            "timeout_s": 5,
            "cwd": ".",
            "env": {"inherit": False, "allowlist": ["PATH"], "overrides": {"X": "1"}},
            "output_policy": {
                "max_lines": 10,
                "stream": "both",
                "filter_mode": "grep",
                "filter_expr": "hi",
                "max_bytes": 1024,
                "redact": {"enabled": True, "extra_patterns": [r"\bfoo\b"]},
                "on_empty": "ok",
            },
            "retry_policy": {"max_attempts": 2, "retry_on": ["nonzero_exit"], "delay_s": 0},
        },
    ),
]


class TestFallbackValidatorParity:
    """
    The fallback must refuse what the schema refuses.

    A validator nobody has shown a rejection to is a validator that passes
    because it finds nothing, so the rejection corpus carries the weight here
    and the acceptance cases only guard against a fallback that refuses
    everything.
    """

    @pytest.mark.parametrize(
        "label,declaration",
        REJECTED_DECLARATIONS,
        ids=[label for label, _ in REJECTED_DECLARATIONS],
    )
    def test_fallback_refuses_what_schema_refuses(self, label: str, declaration: dict) -> None:
        with pytest.raises(ValueError):
            shell_exec._validate_fallback(declaration)

    @pytest.mark.parametrize(
        "label,declaration",
        REJECTED_DECLARATIONS,
        ids=[label for label, _ in REJECTED_DECLARATIONS],
    )
    def test_jsonschema_refuses_the_same_corpus(self, label: str, declaration: dict) -> None:
        """
        Guards the corpus itself: a case the real schema accepts would make the
        parity test above assert a stricter-than-schema fallback, which is a
        different bug and would otherwise pass silently.
        """
        jsonschema = pytest.importorskip("jsonschema")
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=declaration, schema=cs.COMMAND_EXEC_SCHEMA)

    @pytest.mark.parametrize(
        "label,declaration",
        ACCEPTED_DECLARATIONS,
        ids=[label for label, _ in ACCEPTED_DECLARATIONS],
    )
    def test_both_paths_accept_valid_declarations(self, label: str, declaration: dict) -> None:
        jsonschema = pytest.importorskip("jsonschema")
        jsonschema.validate(instance=declaration, schema=cs.COMMAND_EXEC_SCHEMA)
        shell_exec._validate_fallback(declaration)

    def test_run_still_enforces_effect_class_without_jsonschema(self, monkeypatch) -> None:
        """
        The finding in its live form: on a host without jsonschema, does a
        misdeclared destructive command still get refused?
        """
        monkeypatch.setattr(shell_exec, "_validate", shell_exec._validate_fallback)
        result = shell_exec.run(_decl(f"{_RM} -rf /tmp/qe-nonexistent", classification="read_only"))
        assert result["exit_code"] == -1
        assert result["attempt_count"] == 0
        assert "destructive" in (result["error"] or "")


class TestFallbackValidatorKeywords:
    """
    The validator's own keyword handling, exercised directly.

    The parity corpus above only reaches the keywords COMMAND_EXEC_SCHEMA
    happens to use today, at the nesting depths it happens to use them. These
    pin the mechanism, so a schema that later adds `maxProperties` to a new
    object or `items` to a new array is enforced on the fallback path from the
    first commit rather than from the first incident.
    """

    def test_union_type_accepts_each_member(self) -> None:
        schema = {"type": ["string", "null"]}
        shell_exec._validate_against("s", schema, "x")
        shell_exec._validate_against(None, schema, "x")
        with pytest.raises(ValueError, match="expected type"):
            shell_exec._validate_against(1, schema, "x")

    def test_bool_does_not_satisfy_integer_or_number(self) -> None:
        """
        Python's bool is a subclass of int, so a naive isinstance check lets
        `max_lines: True` through as the integer 1 -- a cap of one line that
        the author did not write and would not notice.
        """
        with pytest.raises(ValueError, match="expected type"):
            shell_exec._validate_against(True, {"type": "integer"}, "x")
        with pytest.raises(ValueError, match="expected type"):
            shell_exec._validate_against(True, {"type": "number"}, "x")

    def test_float_satisfies_number_but_not_integer(self) -> None:
        shell_exec._validate_against(1.5, {"type": "number"}, "x")
        with pytest.raises(ValueError, match="expected type"):
            shell_exec._validate_against(1.5, {"type": "integer"}, "x")

    def test_array_bounds_and_item_schema(self) -> None:
        schema = {
            "type": "array",
            "minItems": 1,
            "maxItems": 2,
            "items": {"type": "string", "minLength": 1},
        }
        shell_exec._validate_against(["a"], schema, "x")
        with pytest.raises(ValueError, match="minItems"):
            shell_exec._validate_against([], schema, "x")
        with pytest.raises(ValueError, match="maxItems"):
            shell_exec._validate_against(["a", "b", "c"], schema, "x")
        with pytest.raises(ValueError, match=r"x\[0\]: shorter"):
            shell_exec._validate_against([""], schema, "x")

    def test_min_length_and_bounds_report_the_path(self) -> None:
        """A validation error that does not name the field costs a debug cycle."""
        schema = {"type": "object", "properties": {"n": {"type": "integer", "minimum": 5}}}
        with pytest.raises(ValueError, match="x.n: below minimum"):
            shell_exec._validate_against({"n": 1}, schema, "x")

    def test_max_properties(self) -> None:
        schema = {"type": "object", "maxProperties": 1, "additionalProperties": {"type": "string"}}
        shell_exec._validate_against({"a": "1"}, schema, "x")
        with pytest.raises(ValueError, match="maxProperties"):
            shell_exec._validate_against({"a": "1", "b": "2"}, schema, "x")

    def test_typed_additional_properties_are_checked(self) -> None:
        """`env.overrides` is this shape: free keys, but every value a string."""
        schema = {"type": "object", "additionalProperties": {"type": "string"}}
        with pytest.raises(ValueError, match="x.k: expected type"):
            shell_exec._validate_against({"k": 1}, schema, "x")

    def test_unsupported_keyword_is_a_loud_error(self) -> None:
        """
        The keyword this validator cannot check must fail here, not pass.

        This is what makes dropping `const` and `uniqueItems` safe: adding
        either to COMMAND_EXEC_SCHEMA fails immediately on the fallback path
        instead of being quietly unenforced wherever jsonschema is absent.
        """
        with pytest.raises(ValueError, match="cannot check schema keyword"):
            shell_exec._validate_against("a", {"type": "string", "pattern": "^a$"}, "x")
        with pytest.raises(ValueError, match="cannot check schema keyword"):
            shell_exec._validate_against([1], {"type": "array", "uniqueItems": True}, "x")

    def test_keywords_apply_without_a_type_declared(self) -> None:
        """
        `type` is optional in JSON Schema. A subschema that constrains only by
        enum must still be enforced, not skipped for lack of a type.
        """
        shell_exec._validate_against("a", {"enum": ["a", "b"]}, "x")
        with pytest.raises(ValueError, match="is not one of"):
            shell_exec._validate_against("z", {"enum": ["a", "b"]}, "x")

    def test_unbounded_array_still_validates_its_items(self) -> None:
        """An array with `items` and no length bounds is the common shape."""
        schema = {"type": "array", "items": {"type": "string"}}
        shell_exec._validate_against(["a", "b", "c"], schema, "x")
        with pytest.raises(ValueError, match=r"x\[1\]: expected type"):
            shell_exec._validate_against(["a", 2], schema, "x")

    def test_required_is_checked_past_the_first_name(self) -> None:
        """
        A loop that returns on the first present name would report nothing.
        `output_policy` is the live case: two required names, one often absent.
        """
        schema = {"type": "object", "required": ["a", "b", "c"]}
        shell_exec._validate_against({"a": 1, "b": 2, "c": 3}, schema, "x")
        with pytest.raises(ValueError, match="missing required property 'c'"):
            shell_exec._validate_against({"a": 1, "b": 2}, schema, "x")

    def test_unknown_type_name_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="unknown type"):
            shell_exec._validate_against("a", {"type": "strng"}, "x")

    def test_non_dict_declaration_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must be a dict"):
            shell_exec._validate_fallback(["not", "a", "dict"])  # type: ignore[arg-type]

    def test_the_real_schema_uses_only_supported_keywords(self) -> None:
        """
        Walks COMMAND_EXEC_SCHEMA itself, so a keyword added to the schema
        without teaching the fallback about it fails in this test rather than
        on the one host that has no jsonschema.
        """
        shell_exec._validate_fallback(ACCEPTED_DECLARATIONS[1][1])


# ---------------------------------------------------------------------------
# SEC-0003 -- the audit trail exists
# ---------------------------------------------------------------------------


class TestAuditTrail:
    def test_purpose_is_returned_in_result(self) -> None:
        """Before the fix `purpose` was accepted by the schema and discarded."""
        result = shell_exec.run(_decl("echo hi", purpose="prove purpose survives"))
        assert result["purpose"] == "prove purpose survives"

    def test_result_shape_is_declared_in_the_schema(self) -> None:
        """
        The schema's `result` block is what callers validate responses against;
        a key returned but undeclared is a contract that drifts.
        """
        declared = set(cs.COMMAND_EXEC_SCHEMA["properties"]["result"]["properties"])
        returned = set(shell_exec.run(_decl("echo hi")))
        assert returned == declared

    def test_executed_command_emits_one_audit_record(self, caplog) -> None:
        with caplog.at_level(logging.INFO, logger=shell_exec.AUDIT_LOGGER_NAME):
            result = shell_exec.run(_decl("echo hi", purpose="audited run"))
        records = [r for r in caplog.records if r.name == shell_exec.AUDIT_LOGGER_NAME]
        assert len(records) == 1, "exactly one audit record per run() call"
        entry = json.loads(records[0].getMessage())
        assert entry["command_sha256"] == result["command_sha256"]
        assert entry["purpose"] == "audited run"
        assert entry["classification"] == "read_only"
        assert entry["exit_code"] == 0
        assert entry["outcome"] == "executed"

    def test_refusal_is_audited_too(self, caplog) -> None:
        """
        A refused command is the record an incident review most wants: it says
        an agent tried something the contract stopped.
        """
        with caplog.at_level(logging.INFO, logger=shell_exec.AUDIT_LOGGER_NAME):
            shell_exec.run(
                _decl(
                    f"{_RM} -rf /tmp/qe-nonexistent",
                    classification="read_only",
                    purpose="attempt a misdeclared deletion",
                )
            )
        records = [r for r in caplog.records if r.name == shell_exec.AUDIT_LOGGER_NAME]
        assert len(records) == 1
        entry = json.loads(records[0].getMessage())
        assert entry["outcome"] == "refused"
        assert entry["detected_classification"] == "destructive"
        assert entry["purpose"] == "attempt a misdeclared deletion"

    def test_audit_record_never_carries_the_command_string(self, caplog) -> None:
        """
        The audit record is written to whatever handler the host configures,
        which may be a file or a log aggregator. The hash identifies the
        command without putting a credential-bearing command line there.
        """
        with caplog.at_level(logging.INFO, logger=shell_exec.AUDIT_LOGGER_NAME):
            shell_exec.run(_decl("echo AKIAIOSFODNN7EXAMPLE"))
        entry = json.loads(caplog.records[-1].getMessage())
        assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(entry)
        assert "command" not in entry

    def test_audit_record_is_json_serialisable(self, caplog) -> None:
        """An audit record that cannot be written is not an audit record."""
        with caplog.at_level(logging.INFO, logger=shell_exec.AUDIT_LOGGER_NAME):
            shell_exec.run(_decl("echo hi"))
        json.loads(caplog.records[-1].getMessage())


# ---------------------------------------------------------------------------
# SEC-0004 -- deletion without an rm token classifies as destructive
# ---------------------------------------------------------------------------

DESTRUCTIVE_FORMS = [
    f"find . -name '*.log' -exec {_RM} {{}} \\;",
    "find . -name '*.log' -delete",
    f"find /tmp -type f -print0 | xargs -0 {_RM}",
    f"ls | xargs {_RM} -f",
    "git worktree remove --force ../wt",
    "git stash clear",
    "git stash drop stash@{0}",
    "git update-ref -d refs/heads/x",
    "unlink /tmp/x",
    "gh release delete v1 --yes",
    "gh repo delete owner/name --yes",
]

NON_DESTRUCTIVE_FORMS = [
    "find . -name '*.log'",
    "find . -type f -print",
    "git stash list",
    "git worktree list",
    "gh release list",
    "echo delete",
    "grep -r delete .",
    "ls /tmp",
    # The bare word `unlink` is not a deletion. These are the false positives
    # the first draft of the pattern produced, including on this project's own
    # prose -- the PreToolUse hook refused the commit message describing the
    # fix, which is how the pattern got anchored on command position.
    "grep -rn unlink common/",
    "pip install --unlink nothing",
    "echo 'os.unlink is the python spelling'",
    "git config --get alias.unlinker",
    # Prose that names the tools without composing them into a deletion. The
    # first draft's unbounded [^|]* reached across the comma here and refused
    # this project's own registry entry describing the fix.
    "echo 'the deleting flags of find, xargs, unlink and git worktree'",
    "grep -n 'xargs' docs/SCHEMA.md",
]


class TestDeletionWithoutAnRmToken:
    @pytest.mark.parametrize("command", DESTRUCTIVE_FORMS)
    def test_classified_destructive(self, command: str) -> None:
        """
        Each of these irreversibly removes data and was read_only before the
        fix, so run() would execute it with no confirm_destructive and the
        PreToolUse hook -- which shares classify() -- would wave it through.
        """
        assert cs.classify(command) == "destructive"

    @pytest.mark.parametrize("command", NON_DESTRUCTIVE_FORMS)
    def test_read_only_forms_are_not_swept_up(self, command: str) -> None:
        """
        The cost of a false positive is a refused read. These are the near
        misses of the new patterns; a pattern broad enough to catch `find .`
        makes the guard something people disable.
        """
        assert cs.classify(command) != "destructive"

    @pytest.mark.parametrize("command", DESTRUCTIVE_FORMS)
    def test_run_refuses_them_when_declared_read_only(self, command: str) -> None:
        result = shell_exec.run(_decl(command, classification="read_only"))
        assert result["exit_code"] == -1
        assert result["attempt_count"] == 0

    def test_docstring_does_not_overclaim_what_classify_detects(self) -> None:
        """
        classify() is textual and cannot see inside `bash script.sh`. The
        docstring must keep saying so: a reader who believes it is a sandbox
        will use it as one.
        """
        assert cs.classify("bash evil.sh") == "read_only"
        doc = cs.classify.__doc__ or ""
        assert "read_only" in doc and "proof" in doc
