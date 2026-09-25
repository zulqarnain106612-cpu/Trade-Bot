"""
SEC-0005 -- a file holding real credentials must not be committable.

`.gitignore` carried a bare `.env` pattern. `.env` itself holds
`API_SECRET_KEY` and `OPERATOR_SECRET`, and a bare pattern matches that one
name and nothing else -- so the obvious neighbours of it were all tracked:
`.env.bak`, `.env.bak.20260925100139` (what `cp .env .env.bak.$(date +%s)`
produces before an edit), `.env.local`, `.env.save`.

Those files hold the *same two secrets as the original*. Ignoring `.env` while
leaving its backups committable does not protect the secret, it protects the
filename.

This is a provocation test in the sense SECR-001 uses: it names the concrete
filenames a person or a script actually creates and asserts each one is
covered, rather than asserting the abstract intent "we ignore env files",
which passes on the day the pattern stops matching anything.

Matching uses `fnmatch` rather than a full gitignore engine. Every pattern
under test is a leading-segment glob with no `/`, `!` or `**`, which is the
subset where the two agree; a test that needed more would need a real matcher
and is out of scope here.
"""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Names that carry the same credentials as `.env` and must never be tracked.
#: `.env.bak.<timestamp>` is what a backup-before-edit produces and is the one
#: that was actually left committable.
MUST_BE_IGNORED = [
    ".env",
    ".env.bak",
    ".env.bak.20260925100139",
    ".env.backup",
    ".env.local",
    ".env.save",
    ".env.orig",
]

#: Must stay tracked. `.env.example` is the template and holds no secret; if a
#: pattern ever grows wide enough to swallow it, the onboarding path breaks
#: silently and nobody notices until a fresh clone has no template.
MUST_NOT_BE_IGNORED = [
    ".env.example",
]


@pytest.fixture(scope="module")
def ignore_patterns() -> list[str]:
    """Non-comment, non-blank entries of the repository's .gitignore."""
    text = (_REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _is_ignored(name: str, patterns: list[str]) -> bool:
    """
    Git's last-match-wins rule, including `!` negation.

    `any()` would be wrong here: `.env.example` matches `.env.*` and is then
    rescued by `!.env.example`, and only the *last* matching pattern decides.
    """
    ignored = False
    for pattern in patterns:
        negated = pattern.startswith("!")
        raw = pattern[1:] if negated else pattern
        if fnmatch(name, raw.rstrip("/")):
            ignored = not negated
    return ignored


@pytest.mark.parametrize("name", MUST_BE_IGNORED)
def test_credential_file_is_ignored(name: str, ignore_patterns: list[str]) -> None:
    """Each concrete name that would carry real secrets is covered."""
    assert _is_ignored(name, ignore_patterns), (
        f"{name!r} is not matched by any .gitignore pattern. It carries the same "
        f"API_SECRET_KEY and OPERATOR_SECRET as .env, so it is committable."
    )


@pytest.mark.parametrize("name", MUST_NOT_BE_IGNORED)
def test_template_stays_tracked(name: str, ignore_patterns: list[str]) -> None:
    """The negative case: the secret-free template must not be swallowed."""
    assert not _is_ignored(name, ignore_patterns), (
        f"{name!r} is now ignored. It is the checked-in template and holds no "
        f"secret; ignoring it breaks a fresh clone's onboarding path."
    )


def test_a_bare_env_pattern_alone_would_fail_this_suite() -> None:
    """
    The guard has teeth.

    If someone reduces the patterns back to a bare `.env`, the backup names
    stop matching. Asserting that here means the suite cannot quietly pass on
    a .gitignore that protects only the filename.
    """
    assert not _is_ignored(".env.bak.20260925100139", [".env"])
    assert _is_ignored(".env", [".env"])
