"""Anything the app refuses to start without must appear in .env.example.

main.py's lifespan raises for two environment variables before it accepts a
connection, and both messages say to set them "in .env":

    API_SECRET_KEY is not set. Set a strong random value in .env.
    OPERATOR_SECRET is not set. Set a strong random value in .env.
    Generate with: openssl rand -hex 32

Neither appeared in .env.example, which is the file that sentence points at.
An operator following the README -- copy .env.example to .env, fill it in --
got a server that refused to start and no indication of which key was
missing. The file documented components 1 through 5 and none of the trading
system's own settings.

The check reads the required names out of main.py rather than hardcoding
them, so a third startup guard added later has to be documented too.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = REPO / ".env.example"
API_MAIN = REPO / "src" / "api" / "main.py"

# os.environ.get("NAME", "").strip() followed, within a few lines, by a
# raise that names it. Matching the raise is what makes it "required"
# rather than merely "read".
_REQUIRED_IN_LIFESPAN = re.compile(r'raise RuntimeError\(\s*\n?\s*"([A-Z][A-Z0-9_]*) is not set')


def _required_env_names() -> set[str]:
    return set(_REQUIRED_IN_LIFESPAN.findall(API_MAIN.read_text(encoding="utf-8")))


def _documented_env_names() -> set[str]:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text, re.MULTILINE))


def test_startup_required_variables_are_documented() -> None:
    required = _required_env_names()
    missing = sorted(required - _documented_env_names())

    assert not missing, (
        "src/api/main.py refuses to start without "
        + ", ".join(missing)
        + " and tells the operator to set them in .env, but .env.example does "
        "not list them. Add them with an empty value and a comment saying how "
        "to generate one."
    )


def test_the_required_set_was_actually_found() -> None:
    """Guards the test above against passing because the regex stopped matching.

    If main.py's guards are reworded, the check silently finds nothing and
    passes for the wrong reason. These two are the ones that exist today.
    """
    required = _required_env_names()

    assert {"API_SECRET_KEY", "OPERATOR_SECRET"} <= required, (
        f"expected the two known startup guards, found {sorted(required)} -- "
        "if a guard was reworded, update the pattern in this test"
    )
