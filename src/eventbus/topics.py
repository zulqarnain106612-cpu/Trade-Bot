"""
Event topic constants — GOV-032.

Topics are plain strings so the bus stays dependency-free, but every
producer and consumer names one from here rather than spelling a literal.
A typo in a literal is a silent no-op: the publisher reports zero
subscribers and the panel simply never updates, which is the failure mode
the GUI cannot distinguish from "nothing happened".
"""

from __future__ import annotations

from typing import Final

#: Portfolio-level state: equity, cash, open positions.
PORTFOLIO: Final = "portfolio"

#: Manual-approval queue transitions (created, approved, rejected, expired).
APPROVALS: Final = "approvals"

#: Risk decisions that block or resize an order, including kill-switch trips.
RISK: Final = "risk"

#: Order lifecycle transitions (submitted, partially filled, filled, cancelled).
ORDERS: Final = "orders"

#: Regime / signal snapshots published at bar close.
REGIME: Final = "regime"

#: Wildcard accepted by :meth:`EventBus.subscribe` to receive every topic.
ALL: Final = "*"

#: Every routable topic. ``ALL`` is a subscribe-side wildcard, never a
#: publish target, so it is deliberately absent.
KNOWN_TOPICS: Final[frozenset[str]] = frozenset({PORTFOLIO, APPROVALS, RISK, ORDERS, REGIME})
