"""SEC-0006: the runtime manifest can never resolve a vulnerable urllib3.

urllib3 2.7.0 carries CVE-2026-97687/97688/97689 (fixed in 2.8.0). It arrived
transitively: every ccxt from 4.5.65 to 4.5.84 pins ``urllib3==2.7.0`` exactly,
so pip took the newest ccxt and with it the vulnerable urllib3, and the
pip-audit job failed. The manifest must both demand the fixed urllib3 and keep
ccxt below the releases that pin the vulnerable one -- either alone is not
enough, because pip cannot satisfy ``urllib3>=2.8.0`` alongside a ccxt that
pins 2.7.0 and would fail to resolve instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from packaging.requirements import Requirement

REQUIREMENTS = Path(__file__).resolve().parents[2] / "requirements.txt"


def _parse() -> dict[str, Requirement]:
    reqs = {}
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            req = Requirement(line)
            reqs[req.name.lower()] = req
    return reqs


REQS = _parse()


def test_urllib3_is_declared_with_the_fixed_floor() -> None:
    spec = REQS["urllib3"].specifier
    assert not spec.contains("2.7.0"), "urllib3 2.7.0 (CVE-2026-97687..97689) is still allowed"
    assert spec.contains("2.8.0"), "the fixed urllib3 2.8.0 must stay installable"


@pytest.mark.parametrize("pinning_release", ["4.5.65", "4.5.84"])
def test_ccxt_excludes_the_releases_that_pin_urllib3_2_7_0(pinning_release: str) -> None:
    assert not REQS["ccxt"].specifier.contains(pinning_release)


def test_ccxt_still_allows_the_last_release_without_the_pin() -> None:
    assert REQS["ccxt"].specifier.contains("4.5.64")
