"""
The exchange-response contract.

EXEC-003, EXEC-004, INV-003. An exchange is a boundary this project does not
own: the venue can add a status, rename a field, or return a terminal status
with no fill price, and it will do so without warning. The rule for every such
boundary is the same — **parse against a declared contract; unknown, missing
or malformed fields produce an explicit failure, never a default.**

The specific failure this exists to prevent is the one the source document
names as a trading invariant:

> Unknown exchange order state cannot become FILLED without reconciliation.

Two things make that easy to get wrong:

- `status` is a free-form string. A venue that starts reporting
  `"partially_filled"` where it used to report `"open"` will fall through an
  `if/elif` chain into whatever the `else` happens to do.
- `order.get("filled") or order.get("amount")` treats an explicit `0.0`
  exactly like a missing field, because `0.0` is falsy. A real fill is never
  zero, so "missing" and "explicitly zero" are both rejected here rather than
  one being quietly substituted for the other.

The parse is **total**: every input produces an `OrderUpdate`, and an input
the contract does not recognise produces one whose status is `UNKNOWN` and
whose `problems` say why. Returning a value rather than raising is deliberate
— the caller polling an order needs to distinguish "keep waiting" from "stop
and reconcile", and an exception collapses both into one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from common.command_schema import redact
from src.execution.order_fsm import OrderStatus

#: Longest venue-supplied value echoed into a rejection reason. A venue field
#: is attacker-influenceable in the general case, and a rejection message is
#: the handler most likely to echo whatever it was given straight into a log.
_ECHO_LIMIT: int = 12


def _safe_echo(value: object) -> str:
    """
    Render a venue-supplied value for a log line, redacted and truncated.

    SECR-001: a malformed input is exactly when a handler echoes what it was
    given, so the one place this project prints raw venue strings runs them
    through the same secret masking `common.shell_exec` uses on command
    output, then truncates hard.

    The truncation is the load-bearing half, not the masking. `redact` is
    defence in depth by its own docstring -- a secret in a format no pattern
    describes passes through it -- whereas twelve characters cannot be a
    credential whatever format it is in. Twelve is enough to recognise a new
    venue status in a log and correlate it with the venue's documentation,
    which is the only reason to echo the value at all.
    """
    text = str(value)
    masked, _ = redact(text)
    if len(masked) > _ECHO_LIMIT:
        masked = masked[:_ECHO_LIMIT] + "…"
    return masked


class ExchangeOrderStatus(StrEnum):
    """
    What the venue says, normalised — with an explicit place for "no idea".

    `UNKNOWN` is a value, not an error condition, because it is a real and
    expected outcome: venues add statuses. Giving it a name is what stops it
    being handled by an `else` branch that someone wrote for a different
    reason.
    """

    OPEN = "open"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


#: Venue strings we are willing to interpret, lowercased and stripped.
#: Deliberately explicit rather than a prefix match: "cancel_pending" is not
#: "cancelled", and a prefix rule would say it was.
STATUS_ALIASES: dict[str, ExchangeOrderStatus] = {
    "open": ExchangeOrderStatus.OPEN,
    "pending": ExchangeOrderStatus.OPEN,
    "new": ExchangeOrderStatus.OPEN,
    "partially_filled": ExchangeOrderStatus.OPEN,
    "partial": ExchangeOrderStatus.OPEN,
    "closed": ExchangeOrderStatus.FILLED,
    "filled": ExchangeOrderStatus.FILLED,
    "cancelled": ExchangeOrderStatus.CANCELLED,
    "canceled": ExchangeOrderStatus.CANCELLED,
    "rejected": ExchangeOrderStatus.REJECTED,
    "expired": ExchangeOrderStatus.EXPIRED,
}

#: Statuses that assert the order is done and fully filled. These are the ones
#: that must carry a usable fill price and quantity, because booking a fill is
#: the irreversible part.
TERMINAL_FILLED = frozenset({ExchangeOrderStatus.FILLED})

#: Statuses that end the order without a fill.
TERMINAL_UNFILLED = frozenset(
    {
        ExchangeOrderStatus.CANCELLED,
        ExchangeOrderStatus.REJECTED,
        ExchangeOrderStatus.EXPIRED,
    }
)

#: How each recognised venue status maps into this project's own FSM.
#: `UNKNOWN` is absent on purpose: there is no FSM state it may become, which
#: is the invariant expressed as a missing dictionary entry rather than as a
#: comment.
FSM_STATUS: dict[ExchangeOrderStatus, OrderStatus] = {
    ExchangeOrderStatus.OPEN: OrderStatus.FILLING,
    ExchangeOrderStatus.FILLED: OrderStatus.FILLED,
    ExchangeOrderStatus.CANCELLED: OrderStatus.CANCELLED,
    ExchangeOrderStatus.REJECTED: OrderStatus.FAILED,
    ExchangeOrderStatus.EXPIRED: OrderStatus.FAILED,
}


@dataclass(frozen=True)
class OrderUpdate:
    """
    One exchange response, parsed.

    Frozen, because a parsed response is a record of what the venue said and
    editing it after the fact turns an audit trail into a guess.
    """

    status: ExchangeOrderStatus
    raw_status: str
    order_id: str
    symbol: str
    filled_qty: float | None
    average_price: float | None
    remaining_qty: float | None
    raw: dict[str, Any] = field(default_factory=dict)
    problems: tuple[str, ...] = field(default=())

    @property
    def is_usable(self) -> bool:
        """True when this response can be acted on without reconciliation."""
        return self.status is not ExchangeOrderStatus.UNKNOWN and not self.problems

    @property
    def needs_reconciliation(self) -> bool:
        """
        True when the response must not be acted on automatically.

        Both halves matter. An unrecognised status is obviously not
        actionable; a *recognised* terminal status carrying an unusable fill
        is worse, because it looks actionable.
        """
        return not self.is_usable

    @property
    def fsm_status(self) -> OrderStatus | None:
        """
        The FSM state this response justifies, or None.

        None means "do not transition" -- which is the whole point. There is
        no mapping from UNKNOWN to any FSM state, so an unrecognised status
        cannot become FILLED however the caller is written.
        """
        if self.needs_reconciliation:
            return None
        return FSM_STATUS.get(self.status)

    @property
    def reason(self) -> str:
        if not self.problems:
            return ""
        return (
            f"exchange response for {_safe_echo(self.order_id) or '<no id>'} needs "
            "reconciliation: " + "; ".join(self.problems)
        )


def _coerce_positive(raw: Any) -> float | None:
    """
    A strictly positive finite number, or None.

    None means "not usable", and it covers a missing field, an explicit zero,
    a negative, a NaN and a string the venue sent where a number belongs. All
    five are the same answer to the caller's question, and distinguishing them
    in the return type would only invite a caller to treat one as acceptable.
    """
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0.0:
        return None
    return value


def _coerce_non_negative(raw: Any) -> float | None:
    """As above, but zero is a legitimate value (a fully-filled remainder)."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0.0:
        return None
    return value


def normalise_status(raw: Any) -> tuple[ExchangeOrderStatus, str]:
    """Map a venue status string onto the contract. Returns (status, as-sent)."""
    if not isinstance(raw, str):
        return ExchangeOrderStatus.UNKNOWN, "" if raw is None else str(raw)
    cleaned = raw.strip().lower()
    return STATUS_ALIASES.get(cleaned, ExchangeOrderStatus.UNKNOWN), raw


def parse_order(
    response: Any,
    expected_symbol: str = "",
    expected_order_id: str = "",
) -> OrderUpdate:
    """
    Parse a ccxt-shaped order dict against the contract.

    Total by construction: every input yields an `OrderUpdate`. An input the
    contract cannot vouch for yields one that reports
    `needs_reconciliation`, which is the only outcome a caller is allowed to
    act on differently.
    """
    problems: list[str] = []

    if not isinstance(response, dict):
        return OrderUpdate(
            status=ExchangeOrderStatus.UNKNOWN,
            raw_status="",
            order_id=expected_order_id,
            symbol=expected_symbol,
            filled_qty=None,
            average_price=None,
            remaining_qty=None,
            raw={},
            problems=(f"expected an order mapping, got {type(response).__name__}",),
        )

    status, raw_status = normalise_status(response.get("status"))
    if status is ExchangeOrderStatus.UNKNOWN:
        problems.append(f"unrecognised status {_safe_echo(raw_status)!r}")

    # An *absent* id is not a problem: the caller fetched this order by id
    # and already knows which one it asked about. A *mismatched* id is --
    # that is a response about a different order, and acting on it books the
    # wrong fill. The same reasoning applies to the symbol below.
    order_id = str(response.get("id") or "")
    if expected_order_id and order_id and order_id != expected_order_id:
        problems.append(
            f"order id mismatch: expected {_safe_echo(expected_order_id)!r}, "
            f"got {_safe_echo(order_id)!r}"
        )

    symbol = str(response.get("symbol") or "")
    if expected_symbol and symbol and symbol != expected_symbol:
        # A response for a different instrument is not a malformed field, it
        # is the wrong order -- and booking its fill against this one would be
        # a position in the wrong asset.
        problems.append(
            f"symbol mismatch: expected {_safe_echo(expected_symbol)!r}, got {_safe_echo(symbol)!r}"
        )

    filled_qty = _coerce_positive(response.get("filled"))
    average_price = _coerce_positive(
        response.get("average") if response.get("average") is not None else response.get("price")
    )
    remaining_qty = _coerce_non_negative(response.get("remaining"))

    if status in TERMINAL_FILLED:
        # The irreversible branch. A venue reporting a completed fill with no
        # usable price would have the position recorded as acquired for free.
        if filled_qty is None:
            problems.append("terminal fill carries no usable filled quantity")
        if average_price is None:
            problems.append("terminal fill carries no usable fill price")

    return OrderUpdate(
        status=status,
        raw_status=raw_status,
        order_id=order_id or expected_order_id,
        symbol=symbol or expected_symbol,
        filled_qty=filled_qty,
        average_price=average_price,
        remaining_qty=remaining_qty,
        raw=response,
        problems=tuple(problems),
    )
