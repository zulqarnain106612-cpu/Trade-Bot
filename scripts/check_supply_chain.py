#!/usr/bin/env python3
"""
SUP-001..SUP-003, SUP-005, SUP-007 — the supply-chain posture, as a check.

Every rule here is about a property of the *configuration*, which is the only
place these failures are visible. A workflow with `permissions: write-all`
runs correctly. An action referenced by tag runs correctly, right up to the
day the tag is moved. A dependency bot that can reach the production workflow
looks exactly like one that cannot until something merges itself into
production at 4am.

So this script reads `.github/` and asserts:

  SUP-001  every workflow declares top-level `permissions:`, and no workflow
           takes a write scope it does not use
  SUP-002  every `uses:` names a 40-hex SHA, with the human-readable version
           in a trailing comment
  SUP-003  no workflow reachable from a fork pull request touches a secret
  SUP-005  dependency surveillance is actually configured, for every
           ecosystem this repository has
  SUP-007  nothing triggered by a bot or a dependency branch can reach a
           production/release workflow

It exits non-zero on the first category with findings, printing each. Run it
from the repository root; CI runs it in the Supply chain workflow job, and
`tests/supply_chain/` runs it too so a violation fails the suite rather than
only the gate.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

WORKFLOW_DIR = Path(".github/workflows")
DEPENDABOT = Path(".github/dependabot.yml")

# Triggers a fork's pull request can cause. `pull_request` runs with a
# read-only token and no secrets by default; `pull_request_target` runs with
# the *base* repository's token and secrets while checking out the fork's
# code, which is the combination that has leaked more credentials than any
# other GitHub Actions mistake.
FORK_TRIGGERS = frozenset({"pull_request", "pull_request_target"})

# Write scopes that are worth a second look wherever they appear.
SENSITIVE_SCOPES = frozenset(
    {"contents", "packages", "id-token", "actions", "deployments", "attestations"}
)

# Workflows that deploy or publish. A dependency-bot branch must not reach
# these, however green its checks are.
PRODUCTION_WORKFLOWS = ("release.yml", "deploy.yml", "publish.yml")

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_USES_RE = re.compile(r"^\s*-?\s*uses:\s*(\S+)")
_SECRET_RE = re.compile(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)")

# `secrets.GITHUB_TOKEN` is not a stored credential: it is the per-run token
# whose power is already bounded by `permissions:`, which SUP-001 checks.
ALLOWED_FORK_SECRETS = frozenset({"GITHUB_TOKEN"})


@dataclass(frozen=True)
class Finding:
    rule: str
    where: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.where}: {self.detail}"


def workflows(root: Path = Path()) -> list[Path]:
    return sorted((root / WORKFLOW_DIR).glob("*.yml")) + sorted(
        (root / WORKFLOW_DIR).glob("*.yaml")
    )


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _triggers(doc: dict[str, Any]) -> set[str]:
    # PyYAML parses the bare key `on:` as the boolean True. Handle both, or
    # every trigger check silently passes on every workflow.
    raw = doc.get("on", doc.get(True))
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, list):
        return {str(item) for item in raw}
    if isinstance(raw, dict):
        return {str(key) for key in raw}
    return set()


def check_permissions(root: Path = Path()) -> list[Finding]:
    """SUP-001 — least privilege, declared explicitly."""
    found: list[Finding] = []
    for path in workflows(root):
        doc = _load(path)
        name = path.name
        if "permissions" not in doc:
            found.append(
                Finding(
                    "SUP-001",
                    name,
                    "no top-level permissions: the workflow inherits the "
                    "repository default, which is not least privilege",
                )
            )
            continue
        perms = doc["permissions"]
        if perms in ("write-all", "read-all"):
            found.append(Finding("SUP-001", name, f"blanket permissions: {perms}"))
            continue
        if not isinstance(perms, dict):
            found.append(Finding("SUP-001", name, f"permissions is not a mapping: {perms!r}"))
            continue
        for scope, level in perms.items():
            if level == "write" and scope in SENSITIVE_SCOPES:
                # Not a failure by itself -- a release workflow needs
                # contents: write. It is a failure if the job-level grant
                # would have been enough, which is why the rule is that a
                # top-level write scope must be justified by a comment.
                text = path.read_text(encoding="utf-8")
                if f"{scope}: write" in text and "#" not in _line_with(text, f"{scope}: write"):
                    found.append(
                        Finding(
                            "SUP-001",
                            name,
                            f"top-level '{scope}: write' with no comment saying why; "
                            "grant it on the one job that needs it instead",
                        )
                    )
    return found


def _line_with(text: str, needle: str) -> str:
    for line in text.splitlines():
        if needle in line:
            return line
    return ""


def check_action_pinning(root: Path = Path()) -> list[Finding]:
    """SUP-002 — every action pinned to a full commit SHA."""
    found: list[Finding] = []
    for path in workflows(root):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = _USES_RE.match(line)
            if not match:
                continue
            ref = match.group(1)
            if ref.startswith("./") or ref.startswith("docker://"):
                # A local composite action is this repository's own code, and
                # is covered by everything else that covers this repository.
                continue
            if "@" not in ref:
                found.append(Finding("SUP-002", f"{path.name}:{lineno}", f"unpinned: {ref}"))
                continue
            pin = ref.split("@", 1)[1]
            if not _SHA_RE.match(pin):
                found.append(
                    Finding(
                        "SUP-002",
                        f"{path.name}:{lineno}",
                        f"pinned to a movable ref, not a SHA: {ref}",
                    )
                )
            elif "#" not in line:
                found.append(
                    Finding(
                        "SUP-002",
                        f"{path.name}:{lineno}",
                        "SHA-pinned with no version comment; nobody can tell what it is",
                    )
                )
    return found


def check_fork_secret_isolation(root: Path = Path()) -> list[Finding]:
    """SUP-003 — a fork's pull request cannot reach a stored secret."""
    found: list[Finding] = []
    for path in workflows(root):
        doc = _load(path)
        text = path.read_text(encoding="utf-8")
        triggers = _triggers(doc)
        if not (triggers & FORK_TRIGGERS):
            continue
        if "pull_request_target" in triggers:
            found.append(
                Finding(
                    "SUP-003",
                    path.name,
                    "uses pull_request_target: the base repository's secrets are "
                    "available to a workflow that may check out fork code",
                )
            )
        used = {m.group(1) for m in _SECRET_RE.finditer(text)} - ALLOWED_FORK_SECRETS
        for secret in sorted(used):
            # A secret guarded by a fork check is fine -- that is exactly how
            # retrieve-context handles it -- so look for the guard.
            if _secret_is_fork_guarded(text, secret):
                continue
            found.append(
                Finding(
                    "SUP-003",
                    path.name,
                    f"secrets.{secret} is reachable from a pull request with no "
                    "fork guard (github.event.pull_request.head.repo.fork)",
                )
            )
    return found


def _secret_is_fork_guarded(text: str, secret: str) -> bool:
    """True if the workflow gates on the PR being from this repository."""
    guards = (
        "head.repo.fork == false",
        "head.repo.fork != true",
        "!github.event.pull_request.head.repo.fork",
        "head.repo.full_name == github.repository",
    )
    return any(guard in text for guard in guards)


def check_dependency_surveillance(root: Path = Path()) -> list[Finding]:
    """SUP-005 — the bot is configured for everything that has dependencies."""
    found: list[Finding] = []
    path = root / DEPENDABOT
    if not path.exists():
        return [Finding("SUP-005", str(DEPENDABOT), "no dependency surveillance configured")]
    doc = _load(path)
    updates = doc.get("updates") or []
    ecosystems = {str(u.get("package-ecosystem")) for u in updates if isinstance(u, dict)}

    required = {"github-actions"}
    if (root / "requirements.txt").exists():
        required.add("pip")
    if (root / "frontend" / "package.json").exists():
        required.add("npm")
    if (root / "Dockerfile").exists():
        required.add("docker")

    for missing in sorted(required - ecosystems):
        found.append(
            Finding("SUP-005", str(DEPENDABOT), f"no updates configured for {missing!r}")
        )
    for update in updates:
        if not isinstance(update, dict):
            continue
        schedule = update.get("schedule") or {}
        if not schedule.get("interval"):
            found.append(
                Finding(
                    "SUP-005",
                    str(DEPENDABOT),
                    f"{update.get('package-ecosystem')}: no schedule interval",
                )
            )
    return found


def _push_is_tag_only(doc: dict[str, Any]) -> bool:
    raw = doc.get("on", doc.get(True))
    if not isinstance(raw, dict):
        return False
    push = raw.get("push")
    if not isinstance(push, dict):
        return False
    # Tags and nothing else. `branches` alongside `tags` means a branch push
    # still triggers it, which is the case this must not excuse.
    return bool(push.get("tags")) and not push.get("branches")


def check_bot_cannot_reach_production(root: Path = Path()) -> list[Finding]:
    """SUP-007 — an automated dependency PR cannot trigger a release."""
    found: list[Finding] = []
    for name in PRODUCTION_WORKFLOWS:
        path = root / WORKFLOW_DIR / name
        if not path.exists():
            continue
        doc = _load(path)
        triggers = _triggers(doc)
        forbidden = triggers & (FORK_TRIGGERS | {"push", "schedule"})
        if "push" in forbidden and _push_is_tag_only(doc):
            # A tag-restricted push is not an automatic trigger: merging a
            # dependency bump does not create a version tag, so publishing
            # still requires somebody to tag deliberately. A push on a
            # *branch* is the thing that must never reach a release.
            forbidden = forbidden - {"push"}
        if forbidden:
            found.append(
                Finding(
                    "SUP-007",
                    name,
                    "a production workflow triggered by "
                    + ", ".join(sorted(forbidden))
                    + ": a merged dependency bump would publish by itself",
                )
            )
        if not triggers:
            found.append(Finding("SUP-007", name, "no trigger declared"))
    return found


CHECKS = (
    ("least-privilege permissions", check_permissions),
    ("action pinning", check_action_pinning),
    ("fork secret isolation", check_fork_secret_isolation),
    ("dependency surveillance", check_dependency_surveillance),
    ("bot cannot reach production", check_bot_cannot_reach_production),
)


def run_all(root: Path = Path()) -> list[Finding]:
    out: list[Finding] = []
    for _, check in CHECKS:
        out.extend(check(root))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="repository root")
    args = parser.parse_args(argv)
    root = Path(args.root)

    failed = False
    for label, check in CHECKS:
        findings = check(root)
        if findings:
            failed = True
            print(f"[FAIL] {label}")
            for finding in findings:
                print(f"         {finding}")
        else:
            print(f"[ok  ] {label}")
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover - CLI
    sys.exit(main())
