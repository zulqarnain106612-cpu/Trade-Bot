"""SEC-0007: the frontend dependency tree carries no unfixable advisory.

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
SECURITY_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "security.yml"


@pytest.fixture(scope="module")
def manifest() -> dict:
    """Read the frontend manifest once; these tests only inspect it."""
    return json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))


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
