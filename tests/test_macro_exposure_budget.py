"""Tests for src/risk/macro_exposure_budget.py"""

from __future__ import annotations

import pytest

from src.risk.macro_exposure_budget import (
    MacroExposureBudget,
    get_budget,
)


CAPITAL = 100_000.0


def _budget(**kwargs) -> MacroExposureBudget:
    return MacroExposureBudget(capital_usd=CAPITAL, **kwargs)


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------


def test_init_zero_capital_raises():
    with pytest.raises(ValueError, match="capital_usd"):
        MacroExposureBudget(capital_usd=0.0)


def test_init_negative_capital_raises():
    with pytest.raises(ValueError, match="capital_usd"):
        MacroExposureBudget(capital_usd=-1.0)


def test_init_bad_global_cap_raises():
    with pytest.raises(ValueError, match="global_cap_pct"):
        MacroExposureBudget(capital_usd=CAPITAL, global_cap_pct=0.0)


def test_init_group_cap_exceeds_global_raises():
    with pytest.raises(ValueError, match="default_group_cap_pct"):
        MacroExposureBudget(capital_usd=CAPITAL, global_cap_pct=1.0, default_group_cap_pct=2.0)


def test_set_capital_updates():
    b = _budget()
    b.set_capital(200_000.0)
    assert b.capital == 200_000.0


def test_set_capital_zero_raises():
    b = _budget()
    with pytest.raises(ValueError):
        b.set_capital(0.0)


def test_set_group_cap_valid():
    b = _budget()
    b.set_group_cap("BTC", 0.30)
    # should not raise; reflected in check
    result = b.check("BTC/USDT", "BTC", 29_000.0)
    assert result.allowed is True
    assert result.group_cap == pytest.approx(30_000.0)


def test_set_group_cap_invalid_raises():
    b = _budget(global_cap_pct=2.0)
    with pytest.raises(ValueError):
        b.set_group_cap("BTC", 3.0)  # exceeds global cap


# ---------------------------------------------------------------------------
# update / remove / clear
# ---------------------------------------------------------------------------


def test_update_adds_position():
    b = _budget()
    b.update("BTC/USDT", "BTC", 10_000.0)
    assert b.n_positions == 1


def test_update_zero_removes_position():
    b = _budget()
    b.update("BTC/USDT", "BTC", 10_000.0)
    b.update("BTC/USDT", "BTC", 0.0)
    assert b.n_positions == 0


def test_remove_existing_position():
    b = _budget()
    b.update("ETH/USDT", "alts", 5_000.0)
    b.remove("ETH/USDT")
    assert b.n_positions == 0


def test_remove_nonexistent_no_error():
    b = _budget()
    b.remove("GHOST/USDT")  # should not raise


def test_clear_empties_all():
    b = _budget()
    b.update("BTC/USDT", "BTC", 10_000.0)
    b.update("ETH/USDT", "alts", 5_000.0)
    b.clear()
    assert b.n_positions == 0


# ---------------------------------------------------------------------------
# check — no breach
# ---------------------------------------------------------------------------


def test_check_first_position_allowed():
    b = _budget()
    result = b.check("BTC/USDT", "BTC", 10_000.0)
    assert result.allowed is True
    assert result.reason == ""


def test_check_short_notional_uses_abs():
    b = _budget()
    result = b.check("BTC/USDT", "BTC", -10_000.0)
    assert result.allowed is True


def test_check_updates_current_group_notional():
    b = _budget()
    b.update("BTC/USDT", "BTC", 10_000.0)
    result = b.check("BTC-2/USDT", "BTC", 5_000.0)
    assert result.current_group_notional == pytest.approx(10_000.0)


def test_check_excludes_same_symbol_from_tally():
    b = _budget()
    b.update("BTC/USDT", "BTC", 20_000.0)
    # asking to update same symbol — existing 20k should be excluded
    result = b.check("BTC/USDT", "BTC", 25_000.0)
    assert result.current_group_notional == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# check — group cap breach
# ---------------------------------------------------------------------------


def test_check_group_cap_breach():
    b = _budget(default_group_cap_pct=0.30)  # 30% = 30k
    b.update("BTC/USDT", "BTC", 25_000.0)
    result = b.check("BTC-2/USDT", "BTC", 10_000.0)  # 25k + 10k = 35k > 30k
    assert result.allowed is False
    assert "group_cap_breach" in result.reason


def test_check_group_cap_at_exact_limit_allowed():
    b = _budget(default_group_cap_pct=0.30)  # 30k
    b.update("BTC/USDT", "BTC", 20_000.0)
    result = b.check("BTC-2/USDT", "BTC", 10_000.0)  # exactly 30k
    assert result.allowed is True


def test_check_custom_group_cap_respected():
    b = _budget()
    b.set_group_cap("BTC", 0.20)  # 20k
    b.update("BTC/USDT", "BTC", 18_000.0)
    result = b.check("BTC-2/USDT", "BTC", 5_000.0)  # 18k + 5k = 23k > 20k
    assert result.allowed is False


# ---------------------------------------------------------------------------
# check — global cap breach
# ---------------------------------------------------------------------------


def test_check_global_cap_breach():
    b = _budget(global_cap_pct=1.0, default_group_cap_pct=0.60)
    # global cap = 100k; each group cap = 60k
    b.update("BTC/USDT", "BTC", 60_000.0)
    b.update("ETH/USDT", "alts", 30_000.0)
    # 60k + 30k = 90k current; adding 15k → 105k > 100k
    result = b.check("SOL/USDT", "alts2", 15_000.0)
    assert result.allowed is False
    assert "global_cap_breach" in result.reason


def test_check_global_cap_allows_within_limit():
    b = _budget(global_cap_pct=2.0, default_group_cap_pct=1.0)
    b.update("BTC/USDT", "BTC", 80_000.0)
    result = b.check("ETH/USDT", "alts", 50_000.0)  # 80k + 50k = 130k < 200k
    assert result.allowed is True


# ---------------------------------------------------------------------------
# BudgetCheckResult contract
# ---------------------------------------------------------------------------


def test_result_to_dict_keys():
    b = _budget()
    result = b.check("BTC/USDT", "BTC", 5_000.0)
    d = result.to_dict()
    for key in (
        "allowed",
        "symbol",
        "group",
        "requested_notional",
        "current_group_notional",
        "current_global_notional",
        "group_cap",
        "global_cap",
        "reason",
    ):
        assert key in d


def test_result_frozen():
    b = _budget()
    result = b.check("BTC/USDT", "BTC", 5_000.0)
    with pytest.raises((AttributeError, TypeError)):
        result.allowed = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


def test_summary_structure():
    b = _budget()
    b.update("BTC/USDT", "BTC", 10_000.0)
    b.update("ETH/USDT", "alts", 5_000.0)
    s = b.summary()
    assert "capital_usd" in s
    assert "global_notional" in s
    assert "groups" in s
    assert "BTC" in s["groups"]
    assert "alts" in s["groups"]


def test_summary_utilisation():
    b = _budget(global_cap_pct=2.0)  # 200k global cap
    b.update("BTC/USDT", "BTC", 50_000.0)
    s = b.summary()
    assert s["global_notional"] == pytest.approx(50_000.0)
    assert s["global_utilisation_pct"] == pytest.approx(25.0)


def test_summary_empty():
    b = _budget()
    s = b.summary()
    assert s["global_notional"] == 0.0
    assert s["groups"] == {}


# ---------------------------------------------------------------------------
# singleton
# ---------------------------------------------------------------------------


def test_get_budget_first_call_needs_capital():
    import src.risk.macro_exposure_budget as mod

    mod._REGISTRY = None
    with pytest.raises(RuntimeError, match="capital_usd"):
        get_budget()


def test_get_budget_singleton():
    import src.risk.macro_exposure_budget as mod

    mod._REGISTRY = None
    b1 = get_budget(capital_usd=50_000.0)
    b2 = get_budget()
    assert b1 is b2


def test_get_budget_update_capital():
    import src.risk.macro_exposure_budget as mod

    mod._REGISTRY = None
    get_budget(capital_usd=50_000.0)
    b = get_budget(capital_usd=75_000.0)
    assert b.capital == 75_000.0
