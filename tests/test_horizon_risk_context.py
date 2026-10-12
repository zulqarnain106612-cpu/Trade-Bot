from __future__ import annotations

import math

from src.intel import _validated_risk_context


def _context(as_of_ts: float = 1000.0) -> dict[str, float]:
    return {
        "atr": 1.0,
        "cvar": 0.02,
        "drawdown": 0.01,
        "daily_loss": 0.01,
        "odds": 1.2,
        "as_of_ts": as_of_ts,
    }


def test_risk_context_requires_every_field_and_finite_numeric_values() -> None:
    missing = _context()
    missing.pop("cvar")
    assert _validated_risk_context(missing, now=1000, max_age_seconds=30)[0] is None

    malformed = _context()
    malformed["cvar"] = float("nan")
    values, reason = _validated_risk_context(malformed, now=1000, max_age_seconds=30)
    assert values is None
    assert reason == "portfolio risk context contains non-finite values"

    malformed["cvar"] = math.inf
    assert _validated_risk_context(malformed, now=1000, max_age_seconds=30)[0] is None


def test_risk_context_rejects_stale_future_and_malformed_values() -> None:
    assert _validated_risk_context(_context(900), now=1000, max_age_seconds=30)[0] is None
    assert _validated_risk_context(_context(1001), now=1000, max_age_seconds=30)[0] is None

    malformed = _context()
    malformed["atr"] = "invalid"  # type: ignore[assignment]
    values, reason = _validated_risk_context(malformed, now=1000, max_age_seconds=30)
    assert values is None
    assert reason == "portfolio risk context contains malformed values"


def test_risk_context_accepts_fresh_bounded_values() -> None:
    values, reason = _validated_risk_context(_context(990), now=1000, max_age_seconds=30)
    assert reason is None
    assert values is not None
    assert values["atr"] == 1.0
