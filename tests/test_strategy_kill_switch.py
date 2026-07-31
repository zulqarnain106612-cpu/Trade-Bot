"""Tests for src/risk/strategy_kill_switch.py"""

from __future__ import annotations

import pytest

from src.risk.strategy_kill_switch import (
    StrategyKillSwitch,
    StrategyState,
    get_kill_switch,
)


SYM = "BTC/USDT"
TF = "15m"


# ---------------------------------------------------------------------------
# StrategyState helpers
# ---------------------------------------------------------------------------


def test_win_rate_empty():
    s = StrategyState(symbol=SYM, timeframe=TF)
    assert s.win_rate == 0.0


def test_win_rate_computed():
    s = StrategyState(symbol=SYM, timeframe=TF, n_trades=10, n_wins=7)
    assert s.win_rate == pytest.approx(0.7)


def test_drawdown_pct_no_peak():
    s = StrategyState(symbol=SYM, timeframe=TF)
    assert s.drawdown_pct == 0.0


def test_drawdown_pct_at_peak():
    s = StrategyState(symbol=SYM, timeframe=TF, peak_equity=1000.0, current_equity=1000.0)
    assert s.drawdown_pct == 0.0


def test_drawdown_pct_below_peak():
    s = StrategyState(symbol=SYM, timeframe=TF, peak_equity=1000.0, current_equity=800.0)
    assert s.drawdown_pct == pytest.approx(0.2)


def test_to_dict_keys():
    s = StrategyState(symbol=SYM, timeframe=TF)
    d = s.to_dict()
    assert "symbol" in d
    assert "is_paused" in d
    assert "win_rate" in d
    assert "drawdown_pct" in d


# ---------------------------------------------------------------------------
# is_active — unknown strategy
# ---------------------------------------------------------------------------


def test_unknown_strategy_is_active():
    ks = StrategyKillSwitch()
    assert ks.is_active("UNKNOWN/USDT", "1m") is True


# ---------------------------------------------------------------------------
# record_trade — consecutive loss gate
# ---------------------------------------------------------------------------


def test_consecutive_loss_pause():
    ks = StrategyKillSwitch(max_consecutive_losses=3, cooldown_bars=10)
    for _ in range(3):
        ks.record_trade(SYM, TF, pnl_usd=-10.0, equity_usd=970.0)
    assert ks.is_active(SYM, TF) is False


def test_win_resets_consecutive_losses():
    ks = StrategyKillSwitch(max_consecutive_losses=3)
    ks.record_trade(SYM, TF, pnl_usd=-10.0, equity_usd=990.0)
    ks.record_trade(SYM, TF, pnl_usd=-10.0, equity_usd=980.0)
    ks.record_trade(SYM, TF, pnl_usd=20.0, equity_usd=1000.0)  # win resets
    ks.record_trade(SYM, TF, pnl_usd=-10.0, equity_usd=990.0)
    assert ks.is_active(SYM, TF) is True  # only 1 consecutive loss


def test_two_losses_below_threshold():
    ks = StrategyKillSwitch(max_consecutive_losses=5)
    for _ in range(4):
        ks.record_trade(SYM, TF, pnl_usd=-10.0, equity_usd=900.0)
    assert ks.is_active(SYM, TF) is True


# ---------------------------------------------------------------------------
# record_trade — win rate gate
# ---------------------------------------------------------------------------


def test_win_rate_gate_not_fired_before_min_sample():
    ks = StrategyKillSwitch(min_win_rate=0.5, win_rate_min_sample=20)
    for _ in range(10):
        ks.record_trade(SYM, TF, pnl_usd=-1.0, equity_usd=900.0)
    assert ks.is_active(SYM, TF) is True  # < 20 trades, no gate yet


def test_win_rate_gate_fires_at_min_sample():
    ks = StrategyKillSwitch(
        min_win_rate=0.5,
        win_rate_min_sample=5,
        max_consecutive_losses=999,
        strategy_drawdown_ceiling=1.0,
    )
    for _ in range(5):
        ks.record_trade(SYM, TF, pnl_usd=-1.0, equity_usd=900.0)
    # 5 trades, 0 wins → win_rate=0 < 0.5
    assert ks.is_active(SYM, TF) is False
    state = ks.status(SYM, TF)
    assert "win_rate" in state["pause_reason"]


# ---------------------------------------------------------------------------
# record_trade — drawdown gate
# ---------------------------------------------------------------------------


def test_drawdown_gate_fires():
    ks = StrategyKillSwitch(
        strategy_drawdown_ceiling=0.10,
        max_consecutive_losses=999,
        win_rate_min_sample=999,
    )
    ks.record_trade(SYM, TF, pnl_usd=100.0, equity_usd=1100.0)  # peak = 1100
    ks.record_trade(SYM, TF, pnl_usd=-200.0, equity_usd=900.0)  # dd = 18% > 10%
    assert ks.is_active(SYM, TF) is False


def test_drawdown_gate_no_fire_when_below_ceiling():
    ks = StrategyKillSwitch(
        strategy_drawdown_ceiling=0.20,
        max_consecutive_losses=999,
        win_rate_min_sample=999,
    )
    ks.record_trade(SYM, TF, pnl_usd=100.0, equity_usd=1100.0)
    ks.record_trade(SYM, TF, pnl_usd=-50.0, equity_usd=1050.0)  # dd = 4.5%
    assert ks.is_active(SYM, TF) is True


# ---------------------------------------------------------------------------
# cooldown
# ---------------------------------------------------------------------------


def test_cooldown_decrements():
    ks = StrategyKillSwitch(max_consecutive_losses=1, cooldown_bars=3)
    ks.record_trade(SYM, TF, pnl_usd=-10.0, equity_usd=900.0)
    assert ks.is_active(SYM, TF) is False  # paused, cooldown=3
    assert ks.is_active(SYM, TF) is False  # cooldown=2
    assert ks.is_active(SYM, TF) is False  # cooldown=1
    assert ks.is_active(SYM, TF) is True  # cooldown=0, auto-resume


def test_cooldown_zero_auto_resumes_immediately():
    ks = StrategyKillSwitch(max_consecutive_losses=1, cooldown_bars=0)
    ks.record_trade(SYM, TF, pnl_usd=-10.0, equity_usd=900.0)
    assert ks.is_active(SYM, TF) is True  # immediately resumed


# ---------------------------------------------------------------------------
# Manual override — pause / resume
# ---------------------------------------------------------------------------


def test_manual_pause():
    ks = StrategyKillSwitch()
    ks.pause(SYM, TF, reason="operator_test")
    assert ks.is_active(SYM, TF) is False


def test_manual_pause_does_not_auto_resume():
    ks = StrategyKillSwitch(cooldown_bars=0)
    ks.pause(SYM, TF, reason="manual")
    # operator_forced=True → never auto-resumes regardless of cooldown
    for _ in range(5):
        assert ks.is_active(SYM, TF) is False


def test_resume_clears_pause():
    ks = StrategyKillSwitch()
    ks.pause(SYM, TF, reason="manual")
    ks.resume(SYM, TF)
    assert ks.is_active(SYM, TF) is True


def test_resume_nonexistent_is_noop():
    ks = StrategyKillSwitch()
    ks.resume("NEVER/SEEN", "99m")  # should not raise


def test_resume_clears_consecutive_losses():
    ks = StrategyKillSwitch(max_consecutive_losses=1, cooldown_bars=10)
    ks.record_trade(SYM, TF, pnl_usd=-10.0, equity_usd=900.0)
    assert ks.is_active(SYM, TF) is False
    ks.resume(SYM, TF)
    state = ks.status(SYM, TF)
    assert state["consecutive_losses"] == 0


# ---------------------------------------------------------------------------
# status / all_statuses
# ---------------------------------------------------------------------------


def test_status_empty_for_unknown():
    ks = StrategyKillSwitch()
    assert ks.status("X/Y", "1m") == {}


def test_status_after_trade():
    ks = StrategyKillSwitch()
    ks.record_trade(SYM, TF, pnl_usd=10.0, equity_usd=1010.0)
    st = ks.status(SYM, TF)
    assert st["symbol"] == SYM
    assert st["n_trades"] == 1


def test_all_statuses():
    ks = StrategyKillSwitch()
    ks.record_trade(SYM, "1m", pnl_usd=5.0, equity_usd=1005.0)
    ks.record_trade(SYM, "4h", pnl_usd=10.0, equity_usd=1010.0)
    statuses = ks.all_statuses()
    assert len(statuses) == 2


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


def test_get_kill_switch_singleton():
    import src.risk.strategy_kill_switch as mod

    mod._kill_switch = None  # reset
    ks1 = get_kill_switch()
    ks2 = get_kill_switch()
    assert ks1 is ks2
    mod._kill_switch = None  # cleanup
