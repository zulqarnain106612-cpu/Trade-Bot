"""SEC-0009: the npm gate blocks every finding a repository can actually fix.

`npm audit --audit-level=high` exits non-zero for every high or critical
advisory, and `Security gate (all jobs green)` is a required status check, so one
un-remediable advisory stops every pull request in the repository from merging.
`http-cache-semantics` 4.2.0 (CVE-2026-93748) did exactly that: it has no fixed
release -- 4.2.0 is the only 4.x and is `latest` on the registry, and the GHSA
carries no `first_patched_version` -- and it was reached only through
electron-builder's build-time download chain. SEC-0007 has since removed
electron-builder, so that advisory is out of the tree and the reports below are
fixtures, not claims about the current lockfile. The gate still has to answer
the question, because the next un-remediable advisory arrives the same way.

The gate must therefore distinguish the two cases using the field npm computes
for precisely this question, `fixAvailable`, rather than hardcoding advisory ids:

  * a finding with a fix available blocks, because the repository can remediate
    it, and blocking is the whole point of the gate;
  * a finding with `fixAvailable: false` is reported and does not block, because
    no floor, ceiling, `overrides` entry or `npm audit fix --force` can clear it;
  * a critical finding always blocks, fixable or not, because CLAUDE.md section
    17 does not let a critical requirement be an accepted gap;
  * a report that cannot be read is a failure, never a pass.

And the waiver is not standing: the gate starts failing again by itself as soon
as npm reports a fix, so it cannot quietly outlive the reason it exists.
"""

from __future__ import annotations

import importlib.util
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import ModuleType
from typing import NamedTuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "npm_audit_verdict.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "security.yml"

GHSA = "https://github.com/advisories/GHSA-ch52-4w7c-c8xp"

# The advisory object npm puts in `via` for a directly vulnerable package. Only
# `via` entries that are objects carry an advisory; string entries name a
# package that inherits the finding through a dependency.
ADVISORY = {
    "source": 1122875,
    "name": "http-cache-semantics",
    "dependency": "http-cache-semantics",
    "title": "http-cache-semantics max-stale handling can disclose cross-user cached responses",
    "url": GHSA,
    "severity": "high",
    "cwe": ["CWE-525"],
    "cvss": {"score": 7.5, "vector": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N"},
    "range": "<=4.2.0",
}


def _report(vulnerabilities: dict) -> dict:
    """An `npm audit --json` report carrying exactly these vulnerabilities."""
    counts: dict[str, int] = {"info": 0, "low": 0, "moderate": 0, "high": 0, "critical": 0}
    for entry in vulnerabilities.values():
        counts[entry["severity"]] += 1
    return {
        "auditReportVersion": 2,
        "vulnerabilities": vulnerabilities,
        "metadata": {
            "vulnerabilities": {**counts, "total": len(vulnerabilities)},
            "dependencies": {"prod": 3, "dev": 466, "optional": 0, "peer": 0, "total": 466},
        },
    }


# The real tree: one advisory, six packages carrying it. `http-cache-semantics`
# holds the advisory object; the five above it inherit it and name it as a
# string in `via`. All six resolve to `fixAvailable: false`, because
# electron-builder 26.x -- the only line that exists -- pins `@electron/get@^3`,
# which pins `got@^11`, which pins `cacheable-request@^7`.
UNFIXABLE_TREE = _report(
    {
        "http-cache-semantics": {
            "name": "http-cache-semantics",
            "severity": "high",
            "isDirect": False,
            "via": [ADVISORY],
            "effects": ["cacheable-request"],
            "range": "<=4.2.0",
            "nodes": ["node_modules/http-cache-semantics"],
            "fixAvailable": False,
        },
        "cacheable-request": {
            "name": "cacheable-request",
            "severity": "high",
            "isDirect": False,
            "via": ["http-cache-semantics"],
            "effects": ["got"],
            "range": "<=7.0.4",
            "nodes": ["node_modules/cacheable-request"],
            "fixAvailable": False,
        },
        "got": {
            "name": "got",
            "severity": "high",
            "isDirect": False,
            "via": ["cacheable-request"],
            "effects": ["@electron/get"],
            "range": "<=11.8.6",
            "nodes": ["node_modules/got"],
            "fixAvailable": False,
        },
        "@electron/get": {
            "name": "@electron/get",
            "severity": "high",
            "isDirect": False,
            "via": ["got"],
            "effects": ["app-builder-lib"],
            "range": "<=3.1.0",
            "nodes": ["node_modules/app-builder-lib/node_modules/@electron/get"],
            "fixAvailable": False,
        },
        "app-builder-lib": {
            "name": "app-builder-lib",
            "severity": "high",
            "isDirect": False,
            "via": ["@electron/get"],
            "effects": ["electron-builder"],
            "range": "<=26.17.0",
            "nodes": ["node_modules/app-builder-lib"],
            "fixAvailable": False,
        },
        "electron-builder": {
            "name": "electron-builder",
            "severity": "high",
            "isDirect": True,
            "via": ["app-builder-lib"],
            "effects": [],
            "range": "<=26.17.0",
            "nodes": ["node_modules/electron-builder"],
            "fixAvailable": False,
        },
    }
)


class _Result(NamedTuple):
    """The three fields of a CompletedProcess the assertions below read."""

    returncode: int
    stdout: str
    stderr: str


def _load_script() -> ModuleType:
    """Import the verdict script once, as a module, without spawning python.

    GOV-016 forbids a subprocess where a direct call proves the same property,
    and the script's main() already returns the exit code the workflow reads.
    """
    spec = importlib.util.spec_from_file_location("npm_audit_verdict", SCRIPT)
    assert spec is not None and spec.loader is not None, SCRIPT
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_VERDICT = _load_script()


def _invoke(*argv: str) -> _Result:
    """Run the script's entry point in process and capture what it printed."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = _VERDICT.main(list(argv))
    return _Result(code, out.getvalue(), err.getvalue())


def _run(report: dict | str, tmp_path: Path) -> _Result:
    """Invoke the script the way the workflow does: on a report file."""
    if isinstance(report, str):
        path = tmp_path / "audit.json"
        path.write_text(report, encoding="utf-8")
    else:
        path = tmp_path / "audit.json"
        path.write_text(json.dumps(report), encoding="utf-8")
    return _invoke(str(path))


def test_the_unfixable_advisory_does_not_block(tmp_path: Path) -> None:
    """The finding that held every pull request red reports and passes."""
    result = _run(UNFIXABLE_TREE, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "http-cache-semantics" in result.stdout
    assert "no fixed release exists" in result.stdout
    assert "::warning::" in result.stdout
    assert "::error::" not in result.stdout


def test_one_advisory_is_reported_once_with_its_dependents(tmp_path: Path) -> None:
    """
    Six packages carry CVE-2026-93748; naming all six as findings would bury the
    one line a reader needs. The inherited packages are listed as affected.
    """
    result = _run(UNFIXABLE_TREE, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    # Once as a finding. (The name also appears in the `::warning::` annotation
    # below it, which is the same finding, not a second one.)
    assert result.stdout.count("[warns] http-cache-semantics (high)") == 1
    assert "1 high/critical finding(s)" in result.stdout
    assert "also affects: cacheable-request" in result.stdout
    # The whole inheritance chain is named, not just the nearest dependant.
    for package in (
        "cacheable-request",
        "got",
        "@electron/get",
        "app-builder-lib",
        "electron-builder",
    ):
        assert package in result.stdout
    assert result.stdout.count("::warning::") == 1


@pytest.mark.parametrize(
    ("fix_available", "why"),
    [
        (True, "an in-range fix exists"),
        (
            {"name": "http-cache-semantics", "version": "4.3.0", "isSemVerMajor": False},
            "needs --force (out-of-range)",
        ),
        (
            {"name": "got", "version": "14.4.9", "isSemVerMajor": True},
            "needs --force (semver-major)",
        ),
    ],
)
def test_a_remediable_finding_blocks(fix_available: object, why: str, tmp_path: Path) -> None:
    """Anything the repository can act on keeps failing the gate."""
    report = _report(
        {
            "some-package": {
                "name": "some-package",
                "severity": "high",
                "isDirect": True,
                "via": [ADVISORY],
                "effects": [],
                "range": "<=1.2.3",
                "nodes": ["node_modules/some-package"],
                "fixAvailable": fix_available,
            }
        }
    )
    result = _run(report, tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "::error::" in result.stdout
    assert why in result.stdout


def test_a_missing_fix_available_key_blocks(tmp_path: Path) -> None:
    """
    npm defaults `fixAvailable` to true and only ever sets it false when it has
    proved there is no fix. A report missing the key has proved nothing, so it
    must not be read as permission to skip.
    """
    report = _report(
        {
            "some-package": {
                "name": "some-package",
                "severity": "high",
                "isDirect": True,
                "via": [ADVISORY],
                "range": "<=1.2.3",
                "nodes": ["node_modules/some-package"],
            }
        }
    )
    result = _run(report, tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "npm did not report fixAvailable" in result.stdout


def test_critical_blocks_even_with_no_available_fix(tmp_path: Path) -> None:
    """CLAUDE.md section 17: a critical requirement cannot be an accepted gap."""
    report = _report(
        {
            "http-cache-semantics": {
                "name": "http-cache-semantics",
                "severity": "critical",
                "isDirect": True,
                "via": [{**ADVISORY, "severity": "critical"}],
                "range": "<=4.2.0",
                "nodes": ["node_modules/http-cache-semantics"],
                "fixAvailable": False,
            }
        }
    )
    result = _run(report, tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "::error::" in result.stdout
    assert "::warning::" not in result.stdout


def test_lower_severities_never_reach_the_verdict(tmp_path: Path) -> None:
    """--audit-level=high ignores moderate and below, and so does the script."""
    report = _report(
        {
            "some-package": {
                "name": "some-package",
                "severity": "moderate",
                "isDirect": True,
                "via": [{**ADVISORY, "severity": "moderate"}],
                "range": "<=1.2.3",
                "nodes": ["node_modules/some-package"],
                "fixAvailable": False,
            }
        }
    )
    result = _run(report, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no high or critical advisories" in result.stdout


def test_a_clean_report_passes(tmp_path: Path) -> None:
    result = _run(_report({}), tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ("", "empty, so no scan ran"),
        ("not json at all", "not valid JSON"),
        ("[1, 2, 3]", "must be a JSON object"),
        ('{"auditReportVersion": 2}', "no 'vulnerabilities' object"),
    ],
)
def test_an_unreadable_report_fails_rather_than_passing(
    payload: str, why: str, tmp_path: Path
) -> None:
    """A gate that could not read the report has verified nothing."""
    result = _run(payload, tmp_path)
    assert result.returncode == 2, result.stdout + result.stderr
    assert why in result.stderr


def test_a_missing_report_file_fails(tmp_path: Path) -> None:
    result = _invoke(str(tmp_path / "absent.json"))
    assert result.returncode == 2
    assert "no CVE scan ran" in result.stderr


def test_the_workflow_delegates_its_verdict_to_this_script() -> None:
    """
    The classification is only load-bearing if the gate actually calls it. An
    unused script would leave the workflow failing on exactly the finding this
    exists to stop reporting as blocking.
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "scripts/npm_audit_verdict.py" in workflow, (
        "security.yml no longer calls scripts/npm_audit_verdict.py; the "
        "fixAvailable classification would not be wired into the gate"
    )
    assert "npm audit --json" in workflow, (
        "the workflow must produce the JSON report the script reads"
    )
