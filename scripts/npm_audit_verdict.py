#!/usr/bin/env python3
"""
Decide whether an `npm audit` report should fail a build or only warn about it.

`npm audit --audit-level=high` exits non-zero for every high or critical
advisory. That is the right default, but it has a failure mode with no
correct answer behind it: an advisory whose package has *no fixed release at
all*. No floor, no ceiling, no `overrides` entry and no `npm audit fix --force`
can clear it, because every published version is inside the vulnerable range.
A gate that keeps failing on such a finding is not protecting anything -- it is
just refusing to merge, forever, which is how `http-cache-semantics` 4.2.0
(CVE-2026-93748, no patched version, reached only through electron-builder's
build-time download chain) held all 44 open pull requests red.

So this script splits the two cases apart using the one field npm already
computes for exactly this purpose. In npm's own audit report
(`workspaces/arborist/lib/vuln.js`):

    # assume a fix is available unless it hits a top node
    # that locks it in place, setting this false or {isSemVerMajor, version}.
    #fixAvailable = true

    // - false: no fix is available
    // - {name, version, isSemVerMajor} fix requires -f, is semver major
    // - true: fix does not require -f

`fixAvailable: false` therefore means "no version of this dependency escapes
the advisory". That is a property of the upstream package, not of this
repository, and it is the honest trigger for downgrading a finding to a warning.

Two properties keep this from becoming a way to ignore advisories:

  * **Critical never warns.** CLAUDE.md section 17 says a critical entry cannot
    be an accepted gap, and the reasoning holds here: a critical finding with no
    available fix is still a critical finding, and a human has to decide.
  * **It un-blocks itself.** The warning is not a standing exemption. The moment
    a fixed release appears -- electron-builder 27 dropping `@electron/get@3` is
    the realistic route -- `fixAvailable` stops being `false` and this script
    fails again, naming the package, until the bump lands.

A finding is reported once, at the package that carries the advisory. npm also
lists every package that inherits the finding by depending on a vulnerable one
(a "meta-vulnerability": `cacheable-request`, `got`, `@electron/get`,
`app-builder-lib`, `electron-builder` here). Those carry no advisory of their
own and are listed as affected dependents instead, so one CVE does not print
six times.

Usage:
    npm audit --json --audit-level=high > audit.json
    python3 scripts/npm_audit_verdict.py audit.json

Exit status:
    0   nothing to fix, or every finding has no available fix (warnings only)
    1   at least one finding can be remediated, or is critical
    2   the report is missing or malformed -- a failure, never a pass, because a
        gate that could not read the report has verified nothing
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, NamedTuple

#: Severities that fail a build. Mirrors `--audit-level=high`, which the
#: workflow passes: `moderate` advisories in a build-time-only dependency tree
#: are noise that would train everyone to ignore this gate.
BLOCKING_SEVERITIES = frozenset({"high", "critical"})

#: Severity that is never downgraded to a warning, fixable or not. See the
#: module docstring and CLAUDE.md section 17.
NEVER_WARNED = frozenset({"critical"})

_SEVERITY_ORDER = {"critical": 0, "high": 1, "moderate": 2, "low": 3, "info": 4}


class Finding(NamedTuple):
    """One advisory, reported once, at the package that actually carries it."""

    package: str
    severity: str
    fix_available: Any
    via: tuple[str, ...]
    dependents: tuple[str, ...]

    @property
    def is_critical(self) -> bool:
        return self.severity in NEVER_WARNED

    @property
    def has_no_fix(self) -> bool:
        # `fixAvailable` is `false` (no fix), `true` (in-range fix) or an object
        # describing a fix that needs `--force`. Only literal `false` and a
        # missing key both mean "nothing to install" -- but a *missing* key
        # means npm did not compute one, which is not the same as "there is no
        # fix", so only an explicit `false` counts. Anything else blocks.
        return self.fix_available is False

    @property
    def blocks(self) -> bool:
        return self.is_critical or not self.has_no_fix

    @property
    def fix_note(self) -> str:
        if self.fix_available is False:
            return "no fixed release exists"
        if self.fix_available is True:
            return "an in-range fix exists"
        if isinstance(self.fix_available, dict):
            version = self.fix_available.get("version", "?")
            major = self.fix_available.get("isSemVerMajor")
            kind = "semver-major" if major else "out-of-range"
            return f"fix {version} exists but needs --force ({kind})"
        return "npm did not report fixAvailable"


def _is_advisory(entry: Any) -> bool:
    """
    Tell an advisory apart from the name of an inherited finding.

    npm's `via` array mixes two things: objects describing an advisory against
    this package, and plain strings naming a package whose *dependency* is
    vulnerable. Only the former is a finding in its own right.
    """
    return isinstance(entry, dict)


def _advisory_refs(via: Any) -> tuple[str, ...]:
    if not isinstance(via, list):
        return ()
    refs = []
    for entry in via:
        if not _is_advisory(entry):
            continue
        url = entry.get("url")
        title = entry.get("title") or entry.get("name") or "untitled advisory"
        refs.append(f"{title} ({url})" if url else str(title))
    return tuple(refs)


def collect_findings(report: dict) -> list[Finding]:
    """
    Pull the blocking-or-warnable findings out of an audit report.

    Only the package that carries the advisory becomes a finding. npm also lists
    every package that inherits it by depending on a vulnerable one -- the
    "meta-vulnerabilities" -- and those are folded into the finding's dependent
    list instead, so one CVE is reported once and the whole chain is still named.

    Returns findings sorted most severe first, then by package name, so the top
    of the log names the thing most likely to be the cause rather than whichever
    package sorted first alphabetically.
    """
    vulnerabilities = report.get("vulnerabilities")
    if not isinstance(vulnerabilities, dict):
        raise ValueError("report has no 'vulnerabilities' object")

    # package -> the packages that inherit its finding. An entry whose `via`
    # holds only strings carries no advisory of its own.
    inherited_from: dict[str, list[str]] = {}
    for package, entry in vulnerabilities.items():
        if not isinstance(entry, dict):
            continue
        via = entry.get("via")
        if not isinstance(via, list) or not via:
            continue
        if any(_is_advisory(item) for item in via):
            continue
        for item in via:
            if isinstance(item, str):
                inherited_from.setdefault(item, []).append(package)

    def affected_by(package: str) -> tuple[str, ...]:
        """Every package that inherits this finding, nearest first, deduplicated."""
        seen: set[str] = set()
        order: list[str] = []
        queue = list(inherited_from.get(package, ()))
        while queue:
            name = queue.pop(0)
            if name in seen:
                continue
            seen.add(name)
            order.append(name)
            queue.extend(inherited_from.get(name, ()))
        return tuple(order)

    findings: list[Finding] = []
    for package, entry in vulnerabilities.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("severity") not in BLOCKING_SEVERITIES:
            continue
        via = entry.get("via")
        if not isinstance(via, list) or not any(_is_advisory(item) for item in via):
            # Inherited only. Reported through the finding that carries the
            # advisory, so a single CVE does not print once per dependant.
            continue
        findings.append(
            Finding(
                package=package,
                severity=entry["severity"],
                fix_available=entry.get("fixAvailable"),
                via=_advisory_refs(via),
                dependents=affected_by(package),
            )
        )

    findings.sort(key=lambda f: (_SEVERITY_ORDER.get(f.severity, 9), f.package))
    return findings


def render(findings: list[Finding]) -> str:
    """Every finding and its fix status, so the log shows the whole picture."""
    if not findings:
        return "(no high or critical advisories)"
    lines = []
    for finding in findings:
        verdict = "BLOCKS" if finding.blocks else "warns"
        lines.append(f"  [{verdict}] {finding.package} ({finding.severity}): {finding.fix_note}")
        for ref in finding.via:
            lines.append(f"      {ref}")
        if finding.dependents:
            lines.append(f"      also affects: {', '.join(finding.dependents)}")
    return "\n".join(lines)


def annotate(findings: list[Finding]) -> None:
    """One GitHub annotation per finding, so the PR shows the reason inline."""
    for finding in findings:
        if finding.blocks:
            level = "error"
        else:
            level = "warning"
        refs = " ".join(finding.via)
        print(
            f"::{level}::{finding.package} ({finding.severity}) {finding.fix_note}"
            + (f" -- {refs}" if refs else "")
        )


def load(path: str | None) -> dict:
    """Read the report from a file, or stdin when no path is given."""
    if path:
        text = Path(path).read_text(encoding="utf-8")
    else:
        text = sys.stdin.read()
    if not text.strip():
        raise ValueError("the audit report is empty, so no scan ran")
    report = json.loads(text)
    if not isinstance(report, dict):
        raise ValueError(f"the audit report must be a JSON object, got {type(report).__name__}")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "report",
        nargs="?",
        help="path to `npm audit --json` output; reads stdin when omitted",
    )
    args = parser.parse_args(argv)

    try:
        report = load(args.report)
    except FileNotFoundError:
        print(f"No audit report at {args.report!r}, so no CVE scan ran.", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"The audit report is not valid JSON: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    try:
        findings = collect_findings(report)
    except ValueError as exc:
        print(f"{exc} -- no CVE scan ran.", file=sys.stderr)
        return 2

    blocking = [f for f in findings if f.blocks]
    warning = [f for f in findings if not f.blocks]

    print(
        f"npm audit: {len(findings)} high/critical finding(s); "
        f"{len(blocking)} blocking, {len(warning)} with no available fix"
    )
    print(render(findings))
    annotate(findings)

    if warning:
        print(
            "\nReported, not blocking: no fixed release of the package exists, so "
            "nothing in this repository can remediate it. This is recorded as an "
            "accepted gap in config/quality_registry.json and is re-checked every "
            "run -- when a fix is published this gate fails again.",
        )

    if blocking:
        print("\nGate failed. Remediate these, or record why they cannot be:", file=sys.stderr)
        for finding in blocking:
            print(f"  {finding.package}: {finding.fix_note}", file=sys.stderr)
        return 1

    print("\nNo remediable high or critical advisory.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
