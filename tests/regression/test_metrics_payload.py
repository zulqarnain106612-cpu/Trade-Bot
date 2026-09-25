"""
REG-0016 -- one bad member cost every Prometheus metric, on every tick.

`_tick_traced` built its metrics snapshot inline and called
`len(_executor.open_positions)`. `open_positions` is a **method** on
AbstractExecutor -- `equity_usd` beside it is a `@property` -- so that raised
`TypeError: object of type 'method' has no len()`.

The dict is evaluated in full before `update_metrics()` is reached, so the
failure did not cost one gauge. It cost `signal_score`, `regime_state`,
`prob_ranging`, `prob_trending`, `prob_volatile`, `kelly_fraction`,
`equity_usd` and `open_positions` -- the entire snapshot -- on every tick of
every timeframe, for as long as the run lasted.

Nothing failed loudly. The call site catches `Exception` and logs
`orchestrator.metrics_update_failed` at warning, by deliberate design so a
metrics fault can never reach the trade path. The comment there even predicts
this exact case ("a typo'd attribute"). Prometheus offers no feedback loop
back into the process, so an empty gauge is indistinguishable from a quiet
market, and the warning scrolled past on every tick of a live paper run.

These tests pin the payload rather than the call site: the seam exists so the
snapshot can be built and asserted without driving a whole tick.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.engine.orchestrator import _metrics_payload
from src.execution.base import AbstractExecutor

_EXPECTED_KEYS = {
    "signal_score",
    "regime_state",
    "prob_ranging",
    "prob_trending",
    "prob_volatile",
    "kelly_fraction",
    "equity_usd",
    "open_positions",
}


class _Executor:
    """Matches AbstractExecutor's shape: property for one, method for the other."""

    def __init__(self, positions: int) -> None:
        self._positions = [{"trade_id": f"t{i}"} for i in range(positions)]

    @property
    def equity_usd(self) -> float:
        return 1234.5

    def open_positions(self) -> list[dict[str, object]]:
        return self._positions


@pytest.fixture(scope="module")
def result() -> SimpleNamespace:
    return SimpleNamespace(
        p_long=0.75,
        regime=SimpleNamespace(state=1, prob_ranging=0.2, prob_trending=0.7, prob_volatile=0.1),
        kelly_result=SimpleNamespace(adjusted_fraction=0.125),
    )


def test_open_positions_is_a_method_not_a_property() -> None:
    """
    The fact the bug turned on.

    If someone later makes `open_positions` a property, `_metrics_payload`
    must change with it -- this test fails and says so, rather than the
    payload silently breaking again behind a warning nobody reads.
    """
    assert not isinstance(AbstractExecutor.__dict__.get("open_positions"), property)
    assert isinstance(AbstractExecutor.__dict__.get("equity_usd"), property)


def test_payload_is_complete_with_an_executor(result: SimpleNamespace) -> None:
    """The whole snapshot survives -- this is what regressed, not one gauge."""
    payload = _metrics_payload(result, _Executor(positions=3))
    assert set(payload) == _EXPECTED_KEYS
    assert payload["open_positions"] == 3
    assert payload["equity_usd"] == 1234.5
    assert payload["signal_score"] == pytest.approx(0.5)
    assert payload["regime_state"] == 1
    assert payload["kelly_fraction"] == 0.125


def test_payload_is_complete_without_an_executor(result: SimpleNamespace) -> None:
    """Before the executor exists the snapshot is still whole, just zeroed."""
    payload = _metrics_payload(result, None)
    assert set(payload) == _EXPECTED_KEYS
    assert payload["open_positions"] == 0
    assert payload["equity_usd"] == 0.0


def test_payload_survives_a_missing_regime_and_kelly() -> None:
    """A tick that produced no regime or sizing still reports every key."""
    bare = SimpleNamespace(p_long=0.5, regime=None, kelly_result=None)
    payload = _metrics_payload(bare, _Executor(positions=0))
    assert set(payload) == _EXPECTED_KEYS
    assert payload["regime_state"] == 0
    assert payload["prob_ranging"] == 0.0
    assert payload["kelly_fraction"] == 0.0


def test_treating_open_positions_as_an_attribute_is_the_original_failure() -> None:
    """
    The negative case, so the suite cannot pass on a reverted fix.

    This is verbatim what the old call site did, and it is why every metric
    vanished rather than just one.
    """
    executor = _Executor(positions=2)
    with pytest.raises(TypeError, match="has no len"):
        len(executor.open_positions)  # type: ignore[arg-type]
