"""
In-process event bus (INV-032).

Lives in the ``foundation`` layer so every producer -- io, analytics,
decision, orchestration -- and the ``api`` consumer can import it without
inverting the GOV-018 layer contract. See ``bus.py`` for why publishing is
synchronous, non-blocking and total.
"""

from src.eventbus.bus import (
    CORRELATION_KEYS,
    DEFAULT_MAXLEN,
    TOPICS,
    Event,
    EventBus,
    Subscription,
    get_event_bus,
)
from src.eventbus.envelope import EventEnvelope, correlated, envelope_of

__all__ = [
    "CORRELATION_KEYS",
    "DEFAULT_MAXLEN",
    "TOPICS",
    "Event",
    "EventBus",
    "EventEnvelope",
    "Subscription",
    "correlated",
    "envelope_of",
    "get_event_bus",
]
