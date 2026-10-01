"""In-process event bus (foundation layer) — GOV-032."""

from __future__ import annotations

from .bus import (
    DEFAULT_MAXSIZE,
    EVENT_BUS,
    Event,
    EventBus,
    Subscription,
    get_event_bus,
)
from .topics import ALL, APPROVALS, KNOWN_TOPICS, ORDERS, PORTFOLIO, REGIME, RISK

__all__ = [
    "ALL",
    "APPROVALS",
    "DEFAULT_MAXSIZE",
    "KNOWN_TOPICS",
    "ORDERS",
    "PORTFOLIO",
    "REGIME",
    "RISK",
    "EVENT_BUS",
    "Event",
    "EventBus",
    "Subscription",
    "get_event_bus",
]
