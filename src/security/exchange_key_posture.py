"""
SECR-010 — the exchange key this bot holds cannot withdraw funds.

Everything else in this package limits what an attacker can do *to* the bot.
This limits what they can do *with* it. An API key that can trade is a key
that can lose money slowly, through bad orders somebody has to place and a
market that has to move. An API key that can withdraw is a key that empties
the account in one request. The gap between those two outcomes is a checkbox
on the exchange's key-creation page, and nothing in this process can enforce
it -- the enforcement is on the exchange's side, by construction.

What this module can do is refuse to run against a key whose posture has not
been declared, and make the declaration checkable rather than remembered. The
posture is stated in configuration, the bot asserts it at startup, and the
assertion fails closed: an undeclared posture is treated as unsafe, because
the case where somebody forgot to declare is exactly the case where somebody
also forgot to untick withdrawals.

The residual risk is honest and worth stating plainly: a declaration is not a
capability check. Nothing here proves the key cannot withdraw; it proves
somebody asserted it, in a file, that a reviewer can see. The operational
verification -- attempting a withdrawal against the key and confirming the
exchange refuses -- is a step in `docs/security/KEY_MANAGEMENT.md`, because it
must be done by a human against a live venue and cannot be a unit test.

post_quantum posture (LAW12):
  No migration applies. This module performs no cryptography at all: it reads
  a JSON declaration and compares capability names against a policy. There is
  no key, no signature, no key agreement, and therefore nothing for ML-KEM or
  ML-DSA to replace.

  What *would* change it: signing the declaration file so that a reviewer can
  verify who wrote it. That introduces a signature primitive, and the choice
  would then have to be stated here -- ML-DSA if the declaration is expected
  to be verifiable years after it was made, which it is.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Final

DECLARATION_PATH: Final[Path] = Path("config/exchange_key_posture.json")


class PostureViolation(RuntimeError):
    """The declared key posture is unsafe, or was never declared."""


class Capability(Enum):
    """What an exchange key is permitted to do."""

    READ = "read"
    TRADE = "trade"
    WITHDRAW = "withdraw"
    TRANSFER = "transfer"  # internal/sub-account moves; a withdrawal with extra steps
    MARGIN = "margin"
    FUTURES = "futures"


# The capabilities a key may hold. Anything outside this set fails the check.
PERMITTED: Final[frozenset[Capability]] = frozenset({Capability.READ, Capability.TRADE})

# Named separately from "everything not permitted" so the refusal message can
# say which forbidden capability was found, and so adding a new capability to
# the enum does not silently become permitted.
FORBIDDEN: Final[frozenset[Capability]] = frozenset({Capability.WITHDRAW, Capability.TRANSFER})


@dataclass(frozen=True)
class KeyPosture:
    """
    The declared security posture of one exchange key.

    Every field is a claim somebody made and a reviewer can check against the
    exchange's key page. None of them is verified from this process.
    """

    exchange: str
    capabilities: frozenset[Capability]
    ip_allowlisted: bool
    withdrawal_whitelist_only: bool
    declared_by: str
    declared_on: str  # ISO date

    def forbidden_capabilities(self) -> frozenset[Capability]:
        return frozenset(self.capabilities & FORBIDDEN)


def posture_from_mapping(exchange: str, raw: Any) -> KeyPosture:
    """
    Build a posture from configuration.

    Raises rather than defaulting. A posture assembled from missing fields
    would be a posture nobody declared, dressed as one somebody did.
    """
    if not isinstance(raw, dict):
        raise PostureViolation(f"no key posture declared for {exchange}")
    try:
        caps = frozenset(Capability(c) for c in raw["capabilities"])
    except KeyError as exc:
        raise PostureViolation(f"{exchange}: posture names no capabilities") from exc
    except ValueError as exc:
        raise PostureViolation(f"{exchange}: posture names an unknown capability") from exc

    for field in ("ip_allowlisted", "withdrawal_whitelist_only", "declared_by", "declared_on"):
        if field not in raw:
            raise PostureViolation(f"{exchange}: posture is missing {field!r}")

    return KeyPosture(
        exchange=exchange,
        capabilities=caps,
        ip_allowlisted=bool(raw["ip_allowlisted"]),
        withdrawal_whitelist_only=bool(raw["withdrawal_whitelist_only"]),
        declared_by=str(raw["declared_by"]),
        declared_on=str(raw["declared_on"]),
    )


def assert_safe(posture: KeyPosture) -> None:
    """
    Raise unless the declared posture is one this bot may run with.

    Two independent conditions, both required:

      * no forbidden capability is claimed; and
      * if a withdrawal capability were ever added, it is whitelist-bound.

    The second looks redundant next to the first and is not. It is the
    condition that still holds if somebody relaxes the first in a hurry, and
    defence that survives one bad afternoon is the only kind worth writing.
    """
    found = posture.forbidden_capabilities()
    if found:
        raise PostureViolation(
            f"{posture.exchange}: key claims forbidden capabilities: "
            + ", ".join(sorted(c.value for c in found))
        )
    if Capability.WITHDRAW in posture.capabilities and not posture.withdrawal_whitelist_only:
        raise PostureViolation(
            f"{posture.exchange}: a withdrawal-capable key must be whitelist-bound"
        )
    if not posture.declared_by or not posture.declared_on:
        raise PostureViolation(f"{posture.exchange}: posture has no attribution")


def assert_all_safe(declarations: Any) -> None:
    """
    Check every declared exchange, refusing an empty declaration set.

    Empty is refused on purpose: a deployment with keys and no declarations
    would otherwise pass this check trivially, which is the one outcome that
    must not be available.
    """
    if not isinstance(declarations, dict) or not declarations:
        raise PostureViolation("no exchange key postures are declared")
    checked = 0
    for exchange, raw in declarations.items():
        if str(exchange).startswith("_"):
            continue  # commentary keys, not exchanges
        assert_safe(posture_from_mapping(str(exchange), raw))
        checked += 1
    if checked == 0:
        raise PostureViolation("no exchange key postures are declared")


def load_declarations(path: Path | None = None) -> dict[str, Any]:
    """Read the declaration file, raising if it is absent or unreadable."""
    target = path or DECLARATION_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PostureViolation(f"key posture declaration is unreadable: {target}") from exc
    except ValueError as exc:
        raise PostureViolation(f"key posture declaration is malformed: {target}") from exc
    if not isinstance(raw, dict):
        raise PostureViolation(f"key posture declaration is not an object: {target}")
    return raw


def assert_declared_posture_is_safe(path: Path | None = None) -> None:
    """Startup check: load the declaration file and refuse an unsafe posture."""
    assert_all_safe(load_declarations(path))
