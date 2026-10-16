from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import yaml

from scripts import ci_diagnostics
from scripts.ci_diagnostics import (
    REPORT_PAGE_BYTES,
    _add_structured_outcomes,
    _all_jobs_terminal,
    _canonical_json,
    _canonical_invocation,
    _decode_report_pages,
    _decode_job_log_payload,
    _matches_pr_run,
    _external_id,
    _page_external_id,
    _token,
    DiagnosticError,
    WORKFLOW_INVENTORY,
    _pack_pages,
    _redact_message,
    parse_log_lines,
)

ROOT = Path(__file__).resolve().parents[1]


def test_job_log_zip_archive_is_decoded_without_extracting_files() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("1_Setup.txt", "setup completed\n")
        archive.writestr("2_Tests.txt", "FAILED tests/test_orders.py::test_order - AssertionError\n")
    decoded = _decode_job_log_payload(buffer.getvalue(), 42)
    assert "setup completed" in decoded
    assert "FAILED tests/test_orders.py::test_order - AssertionError" in decoded


def test_malformed_job_log_archive_fails_closed() -> None:
    try:
        _decode_job_log_payload(bytes([255, 254, 253, 0, 1]), 42)
    except DiagnosticError as exc:
        assert "unrecognized job log archive" in str(exc)
    else:
        raise AssertionError("an invalid job-log payload must not be treated as a complete log")


def test_successful_log_volume_has_no_diagnostic_lines() -> None:
    raw = "".join(f"2026-10-16T10:00:{i % 60:02d}.000Z step printed normal line {i}\n" for i in range(5000))
    assert parse_log_lines(raw) == []


def test_parser_extracts_pytest_error_without_success_noise() -> None:
    raw = (
        "2026-10-16T10:00:00.000Z step reported success\n"
        "2026-10-16T10:00:01.000Z FAILED tests/test_orders.py::test_rejects_bad_order - AssertionError\n"
        "2026-10-16T10:00:02.000Z ordinary teardown completed\n"
    )
    result = parse_log_lines(raw)
    assert len(result) == 1
    assert result[0]["path"] == "tests/test_orders.py"
    assert result[0]["start_line"] is None
    assert "FAILED" in result[0]["message"]


def test_parser_preserves_compiler_location_without_inventing_column() -> None:
    result = parse_log_lines("src/engine.py:42: error: incompatible value type\n")
    assert len(result) == 1
    assert result[0]["path"] == "src/engine.py"
    assert result[0]["start_line"] == 42
    assert result[0]["start_column"] is None
    assert result[0]["message"] == "src/engine.py:42: error: incompatible value type"


def test_parser_extracts_generic_error_line_without_success_noise() -> None:
    result = parse_log_lines("ordinary successful output\nError: unable to load configuration\n")
    assert len(result) == 1
    assert result[0]["category"] == "log_error_error"
    assert result[0]["path"] is None
    assert result[0]["start_line"] is None
    assert result[0]["message"] == "Error: unable to load configuration"


def test_parser_preserves_annotation_columns_when_present() -> None:
    result = parse_log_lines("::error file=src/order.py,line=7,col=3,endLine=7,endColumn=9::invalid order\n")
    assert len(result) == 1
    assert result[0]["path"] == "src/order.py"
    assert result[0]["start_line"] == 7
    assert result[0]["start_column"] == 3
    assert result[0]["end_line"] == 7
    assert result[0]["end_column"] == 9


def test_message_fragments_reassemble_without_truncation() -> None:
    record = {
        "repository": "owner/repo", "pr": 9, "head_sha": "a" * 40,
        "workflow_name": "CI", "workflow_run_id": 10, "run_attempt": 2,
        "job_id": 11, "job_name": "unit", "step_name": "tests",
        "outcome_level": "log_diagnostic", "conclusion": "failure",
        "category": "pytest_error", "path": "tests/test_a.py",
        "start_line": 1, "start_column": None, "end_line": None, "end_column": None,
        "message": "x" * (REPORT_PAGE_BYTES * 3), "source": "GitHub Actions job log",
    }
    pages = _pack_pages([record])
    fragments = [entry for page in pages for entry in page]
    assert len(fragments) > 1
    assert len(pages) > 1
    assert "".join(entry["message_fragment"] or "" for entry in sorted(fragments, key=lambda f: f["message_part_index"])) == record["message"]
    assert all(len(_canonical_json(page).encode("utf-8")) <= REPORT_PAGE_BYTES for page in pages)


def test_report_reader_rejects_stale_page() -> None:
    record = {
        "repository": "owner/repo", "pr": 9, "head_sha": "a" * 40,
        "workflow_name": "CI", "workflow_run_id": 10, "run_attempt": 1,
        "job_id": None, "job_name": None, "step_name": None,
        "outcome_level": "workflow", "conclusion": "failure",
        "category": "workflow_outcome", "path": None, "start_line": None,
        "start_column": None, "end_line": None, "end_column": None,
        "message": None, "source": "GitHub Actions workflow-run API",
    }
    digest = hashlib.sha256(_canonical_json([record]).encode()).hexdigest()
    packed = _pack_pages([record])
    page = {
        "status": "completed", "conclusion": "success", "head_sha": "b" * 40,
        "output": {
            "summary": _canonical_json({"schema_version": 1, "generation_id": digest[:24], "page": 1, "page_count": 1}),
            "text": _canonical_json(packed[0]),
        },
    }
    manifest = {"page_count": 1, "generation_id": digest[:24], "diagnostic_count": 1, "digest": digest}
    try:
        _decode_report_pages(9, "a" * 40, manifest, {1: page})
    except Exception as exc:
        assert "not finalized on the requested SHA" in str(exc)
    else:
        raise AssertionError("stale report page was accepted")


def test_distinct_file_locations_are_retained_and_exact_duplicates_collapse() -> None:
    raw = (
        "src/alpha.py:12: error: incompatible value\n"
        "src/alpha.py:12: error: incompatible value\n"
        "src/beta.py:44:7: error: unknown member\n"
    )
    records = parse_log_lines(raw)
    assert len(records) == 2
    assert {(item["path"], item["start_line"], item["start_column"]) for item in records} == {
        ("src/alpha.py", 12, None),
        ("src/beta.py", 44, 7),
    }


def test_successful_workflow_preserves_skipped_and_cancelled_step_outcomes() -> None:
    records = {}
    run = {"id": 101, "run_attempt": 2, "conclusion": "success"}
    job = {
        "id": 202,
        "name": "optional quality checks",
        "status": "completed",
        "conclusion": "success",
        "steps": [
            {"name": "optional lint", "status": "completed", "conclusion": "skipped"},
            {"name": "cancelled diagnostic", "status": "completed", "conclusion": "cancelled"},
        ],
    }
    _add_structured_outcomes(records, "owner/repo", 313, "a" * 40, "CI", run, [job])
    outcomes = {(item["step_name"], item["conclusion"], item["job_id"]) for item in records.values()}
    assert outcomes == {
        ("optional lint", "skipped", 202),
        ("cancelled diagnostic", "cancelled", 202),
    }


def test_successful_job_log_is_scanned_for_explicit_diagnostics(monkeypatch) -> None:
    records = {}
    run = {"id": 501, "run_attempt": 1, "conclusion": "success"}
    job = {
        "id": 502, "name": "compiler", "status": "completed", "conclusion": "success",
        "steps": [{"name": "build", "status": "completed", "conclusion": "success"}],
    }
    monkeypatch.setattr(
        ci_diagnostics, "_job_log",
        lambda repository, job_id: "src/engine.py:42: warning: deprecated API\nordinary successful output\n",
    )
    ci_diagnostics._add_log_diagnostics(records, "owner/repo", 313, "a" * 40, "CI", run, job)
    diagnostics = list(records.values())
    assert len(diagnostics) == 1
    assert diagnostics[0]["path"] == "src/engine.py"
    assert diagnostics[0]["start_line"] == 42
    assert diagnostics[0]["category"] == "compiler_warning"
    assert diagnostics[0]["conclusion"] == "diagnostic"


def test_failed_timeout_and_skipped_outcomes_survive_without_log_text() -> None:
    records = {}
    run = {"id": 401, "run_attempt": 1, "conclusion": "timed_out"}
    job = {
        "id": 402, "name": "integration", "status": "completed", "conclusion": "failure",
        "steps": [{"name": "timeout step", "status": "completed", "conclusion": "timed_out"}],
    }
    _add_structured_outcomes(records, "owner/repo", 313, "a" * 40, "CI", run, [job])
    skipped = {"id": 403, "run_attempt": 1, "conclusion": "skipped"}
    _add_structured_outcomes(records, "owner/repo", 313, "a" * 40, "Security", skipped, [])
    outcomes = {
        (item["workflow_run_id"], item["outcome_level"], item["conclusion"], item["job_id"], item["step_name"])
        for item in records.values()
    }
    assert (401, "workflow", "timed_out", None, None) in outcomes
    assert (401, "job", "failure", 402, None) in outcomes
    assert (401, "step", "timed_out", 402, "timeout step") in outcomes
    assert (403, "workflow", "skipped", None, None) in outcomes


def test_collector_waits_for_terminal_steps_and_jobs() -> None:
    success = {"conclusion": "success"}
    assert not _all_jobs_terminal([{"status": "in_progress", "steps": []}], success)
    assert not _all_jobs_terminal(
        [{"status": "completed", "steps": [{"status": "in_progress", "conclusion": None}]}],
        success,
    )
    assert _all_jobs_terminal(
        [{"status": "completed", "steps": [{"status": "completed", "conclusion": "skipped"}]}],
        success,
    )
    assert not _all_jobs_terminal([], success)


def test_report_identity_is_pr_and_sha_specific() -> None:
    sha_a, sha_b = "a" * 40, "b" * 40
    assert _external_id(313, sha_a) != _external_id(313, sha_b)
    assert _external_id(313, sha_a) != _external_id(314, sha_a)
    assert _page_external_id(313, sha_a, 1) != _page_external_id(313, sha_a, 2)
    assert _page_external_id(313, sha_a, 1) != _page_external_id(314, sha_a, 1)


def test_run_correlation_never_falls_back_across_another_pr_association() -> None:
    pull = {"number": 313, "head": {"ref": "feature", "sha": "a" * 40}}
    run = {
        "event": "pull_request",
        "head_branch": "feature",
        "head_sha": "synthetic-merge-sha",
        "pull_requests": [{"number": 314, "head": {"sha": "a" * 40}}],
    }
    assert not _matches_pr_run(run, pull, "a" * 40)
    run["pull_requests"] = [{"number": 313, "head": {"sha": "a" * 40}}]
    assert _matches_pr_run(run, pull, "a" * 40)
    run["pull_requests"][0]["head"]["sha"] = "b" * 40
    assert not _matches_pr_run(run, pull, "a" * 40)


def test_report_cli_grammar_is_exact_and_rejects_extra_selectors() -> None:
    assert _canonical_invocation(["--pr", "313"]) == 313
    for args in (
        [],
        ["--pr", "0"],
        ["--pr", "-1"],
        ["--pr", "0313"],
        ["--pr", "313", "--run-id", "9"],
        ["--pr", "313", "--job-id", "7"],
        ["--pr=313"],
        ["313"],
    ):
        assert _canonical_invocation(args) is None


def test_report_cli_uses_existing_gh_auth_without_extra_token(monkeypatch) -> None:
    import subprocess

    monkeypatch.setattr(
        "ci_diagnostics.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="existing_cli_token\n", stderr=""
        ),
    )
    monkeypatch.delenv("PRFAIL_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "collector_token_must_not_be_used")
    monkeypatch.setenv("GH_TOKEN", "environment_token_must_not_be_used")
    assert _token() == "existing_cli_token"


def test_diagnostic_message_redaction_masks_tokens_and_secret_assignments(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "gho_test_secret_value_123456789")
    raw = (
        "Bearer abcdefghijklmnop token=inline_secret_value "
        "GITHUB_TOKEN=gho_test_secret_value_123456789"
    )
    masked = _redact_message(raw)
    assert "gho_test_secret_value_123456789" not in masked
    assert "abcdefghijklmnop" not in masked
    assert "inline_secret_value" not in masked
    assert "[REDACTED" in masked


def test_empty_finalized_report_returns_no_stdout(monkeypatch, capsys) -> None:
    sha = "a" * 40
    digest = hashlib.sha256(_canonical_json([]).encode()).hexdigest()
    manifest = {
        "schema_version": 1, "status": "finalized", "pr": 313, "head_sha": sha,
        "generation_id": digest[:24], "diagnostic_count": 0, "page_count": 0,
        "digest": digest, "collector_run_id": 99, "collector_run_attempt": 1,
    }
    master = {
        "status": "completed", "conclusion": "success", "head_sha": sha,
        "output": {"summary": _canonical_json(manifest)},
    }
    monkeypatch.setattr(ci_diagnostics, "_repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(
        ci_diagnostics, "_load_current_pr",
        lambda slug, pr: {"number": pr, "state": "open", "head": {"sha": sha}},
    )
    monkeypatch.setattr(ci_diagnostics, "_report_runs", lambda slug, head, pr: (master, {}))
    assert ci_diagnostics._retrieve(313) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_missing_report_fails_closed_without_attempting_raw_log_fallback(monkeypatch, capsys) -> None:
    monkeypatch.setattr(ci_diagnostics, "_repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(
        ci_diagnostics,
        "_load_current_pr",
        lambda slug, pr: {"number": pr, "state": "open", "head": {"sha": "a" * 40}},
    )
    monkeypatch.setattr(ci_diagnostics, "_report_runs", lambda slug, sha, pr: (None, {}))

    def forbidden_raw_log(*args, **kwargs):
        raise AssertionError("report retrieval attempted raw Actions log access")

    monkeypatch.setattr(ci_diagnostics, "_job_log", forbidden_raw_log)
    assert ci_diagnostics._retrieve(313) != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "No finalized diagnostics report" in captured.err


def test_collector_workflow_has_explicit_inventory_and_nonblocking_permissions() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci-diagnostics.yml").read_text())
    triggers = workflow.get("on") or workflow.get(True)
    assert triggers["workflow_run"]["types"] == ["completed"]
    assert set(triggers["workflow_run"]["workflows"]) == {"CI", "CodeQL", "Security", "Workflow lint"}
    assert workflow["permissions"]["actions"] == "read"
    assert workflow["permissions"]["checks"] == "write"
    assert workflow["concurrency"]["cancel-in-progress"] is False
    source_files = {
        "CI": "ci.yml",
        "CodeQL": "codeql.yml",
        "Security": "security.yml",
        "Workflow lint": "workflow-lint.yml",
    }
    for item in WORKFLOW_INVENTORY:
        source = yaml.safe_load((ROOT / ".github/workflows" / source_files[item["name"]]).read_text())
        source_on = source.get("on") or source.get(True)
        assert "pull_request" in source_on
        pull_request = source_on["pull_request"]
        if item["base_branches"] is None:
            assert "branches" not in pull_request
        else:
            assert pull_request["branches"] == list(item["base_branches"])



def test_local_patch_applier_refuses_a_mismatched_checkout_head(monkeypatch) -> None:
    from scripts import apply_ci_patch
    from scripts.apply_ci_patch import PatchError

    diff = (
        "diff --git a/tests/test_ci_diagnostics.py b/tests/test_ci_diagnostics.py\\n"
        "index 1111111..2222222 100644\\n"
        "--- a/tests/test_ci_diagnostics.py\\n"
        "+++ b/tests/test_ci_diagnostics.py\\n"
        "@@ -1,1 +1,1 @@\\n"
        "-old_value = 1\\n"
        "+new_value = 1\\n"
    )
    monkeypatch.setattr(apply_ci_patch, "_current_head", lambda: "a" * 40)
    try:
        apply_ci_patch.apply_patch(457, "b" * 40, diff)
    except PatchError as exc:
        assert "does not match diagnostic PR head" in str(exc)
    else:
        raise AssertionError("a patch based on a different PR head was accepted")


def test_local_patch_applier_validates_existing_paths_and_rejects_unsafe_paths() -> None:
    from scripts.apply_ci_patch import PatchError, _validate_patch

    diff = (
        "diff --git a/src/example.py b/src/example.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/src/example.py\n"
        "+++ b/src/example.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-old_value = 1\n"
        "+new_value = 1\n"
    )
    assert _validate_patch(diff.replace("src/example.py", "tests/test_ci_diagnostics.py")) == [
        "tests/test_ci_diagnostics.py"
    ]

    # Path validation requires an existing target, so exercise the guards that
    # do not require a repository file independently.
    unsafe = diff.replace("src/example.py", "../outside.py")
    try:
        _validate_patch(unsafe)
    except PatchError as exc:
        assert "not repository-relative" in str(exc)
    else:
        raise AssertionError("path traversal patch was accepted")

    try:
        _validate_patch(diff.replace("src/example.py", ".git/config"))
    except PatchError as exc:
        assert "Git metadata" in str(exc)
    else:
        raise AssertionError("a Git-metadata patch was accepted")

    try:
        _validate_patch(diff.replace("src/example.py", "src/new_file.py").replace(
            "@@ -1,1 +1,1 @@", "@@ -1,1 +1,1 @@"
        ))
    except PatchError as exc:
        assert "existing regular file" in str(exc)
    else:
        raise AssertionError("a patch targeting a nonexistent path was accepted")
