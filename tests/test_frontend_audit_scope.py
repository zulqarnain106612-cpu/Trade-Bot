"""SEC-0007, SEC-0008: the frontend dependency tree carries no open advisory.

SEC-0008 is the second case and took the opposite shape. Two critical advisories
failed Security gate on every pull request, both ``shell-quote``
GHSA-pqg4-j6r4-53mv -- command injection in ``quote()`` via a line terminator in
a token after a ``{ comment }`` token, vulnerable range ``>=1.8.4 <1.11.0``.
``concurrently`` is a devDependency at ``^10.0.5`` and pins ``shell-quote`` to
exactly ``1.9.0``, not a range, so no amount of updating moves it, and
``concurrently@10.0.5`` is the latest release: 9.2.3 pins 1.8.4, 9.2.4 and
10.0.4 pin 1.9.0, so no release carries the fix. Measured in ``frontend/`` with
the gate's own lockfile transform:

* unchanged manifest ..................................... 2 critical
* ``overrides: {"shell-quote": "^1.11.0"}`` (resolves 1.12.0) 0 vulnerabilities

``npm audit fix`` proposes ``concurrently@9.2.1`` instead -- a semver-major
downgrade off the 10.x line. The override keeps the line and fixes the advisory,
so the tests below hold its floor at 1.11.0 and hold ``concurrently`` on ``^10``:
downgrading to dodge the advisory must not pass as a fix either.

SEC-0007 was the first case and is unchanged below.

SEC-0007: the frontend dependency tree carries no unfixable advisory.

Eight high advisories failed Security gate on every pull request. All eight
arrived through ``electron-builder`` and traced to ``http-cache-semantics``
GHSA-ch52-4w7c-c8xp, whose vulnerable range is ``<= 4.2.0`` with
``first_patched_version`` null -- every release ever published. Measured in
``frontend/``:

* unchanged manifest, full tree ....................... 8 high
* pinned to the 26.5.0 ``npm audit fix --force`` wants . 14 (13 high, 1 critical)
* ``electron-builder`` removed ........................ 0 vulnerabilities

No override escapes it either: ``cacheable-request@13.0.19``, the latest, still
depends on ``http-cache-semantics@^4.2.0``. Since no workflow builds the
desktop app, the dependency was removed rather than the gate being narrowed.

These tests hold that in place: the package must not come back while the
advisory is unfixed, and the gate must keep auditing the whole tree at
``high`` -- the scope must not be quietly reduced instead.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_JSON = PROJECT_ROOT / "frontend" / "package.json"
PACKAGE_LOCK = PROJECT_ROOT / "frontend" / "package-lock.json"
SECURITY_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "security.yml"

# GHSA-pqg4-j6r4-53mv: shell-quote >=1.8.4 <1.11.0. The first version outside
# the range, which is the floor every assertion below measures against.
SHELL_QUOTE_PATCHED = (1, 11, 0)


def _version(spec: str) -> tuple[int, ...]:
    """Numeric tuple for a semver spec, ignoring a leading range operator.

    Only ``^``/``~``/``>=`` floors and exact versions appear here, and a floor
    is what the advisory is judged against -- ``^1.11.0`` admits nothing below
    1.11.0, so comparing its floor is the whole question.
    """
    return tuple(int(part) for part in spec.lstrip("^~>=v").split(".")[:3])


@pytest.fixture(scope="module")
def manifest() -> dict:
    """Read the frontend manifest once; these tests only inspect it."""
    return json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def lockfile() -> dict:
    """Read the frontend lockfile once; these tests only inspect it."""
    return json.loads(PACKAGE_LOCK.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def security_workflow() -> str:
    """Read the security workflow once; these tests only inspect it."""
    return SECURITY_WORKFLOW.read_text(encoding="utf-8")


def test_electron_builder_is_not_a_dependency(manifest: dict) -> None:
    """Re-adding it reintroduces eight advisories with no upstream fix."""
    assert "electron-builder" not in manifest.get("dependencies", {})
    assert "electron-builder" not in manifest.get("devDependencies", {})


def test_no_script_invokes_the_removed_builder(manifest: dict) -> None:
    """A script calling a package that is gone fails at run time, not review."""
    scripts = manifest.get("scripts", {})
    assert not [name for name, cmd in scripts.items() if "electron-builder" in cmd]


def test_gating_audit_still_covers_the_whole_tree(
    security_workflow: str,
) -> None:
    """The fix was to drop the package, not to stop looking at dev dependencies.

    If this ever flips to ``--omit=dev`` the eight advisories are still there,
    just unobserved, so the removal above would silently stop being enforced.
    """
    assert "npm audit --audit-level=high" in security_workflow
    assert "--omit=dev" not in security_workflow


def test_gating_audit_keeps_the_high_threshold(security_workflow: str) -> None:
    """Severity bar stays where it is."""
    assert "--audit-level=high" in security_workflow
    assert "--audit-level=critical" not in security_workflow


def test_shell_quote_override_pins_the_patched_line(manifest: dict) -> None:
    """SEC-0008: without this override the tree resolves a vulnerable 1.9.0.

    ``concurrently`` pins ``shell-quote`` exactly, so the override is the only
    thing holding the resolution above the advisory range. Dropping it, or
    lowering its floor back into ``>=1.8.4 <1.11.0``, reinstates two criticals.
    """
    override = manifest.get("overrides", {}).get("shell-quote")
    assert override is not None, "the shell-quote override is what fixes SEC-0008"
    assert _version(override) >= SHELL_QUOTE_PATCHED


def test_lockfile_resolves_no_vulnerable_shell_quote(lockfile: dict) -> None:
    """The override has to reach the lockfile, which is what npm audit reads.

    Asserting the manifest alone would pass while a stale lockfile still
    resolved 1.9.0 -- the gate audits the lockfile, so that is what is checked.
    """
    resolved = {
        name: meta["version"]
        for name, meta in lockfile["packages"].items()
        if name.split("node_modules/")[-1] == "shell-quote"
    }
    assert resolved, "shell-quote should still be in the tree via concurrently"
    for name, version in resolved.items():
        assert _version(version) >= SHELL_QUOTE_PATCHED, f"{name} is {version}"


def test_concurrently_is_not_downgraded_to_dodge_the_advisory(manifest: dict) -> None:
    """``npm audit fix`` proposes concurrently@9.2.1; that is not the fix taken.

    A downgrade off the 10.x line also silences the advisory, so a future change
    could satisfy the gate by taking it and quietly dropping the override. This
    holds the manifest on ``^10`` so that route fails here instead.
    """
    spec = manifest["devDependencies"]["concurrently"]
    assert _version(spec)[0] >= 10, f"concurrently is pinned to {spec}"
