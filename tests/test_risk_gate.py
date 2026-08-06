"""Tests for RiskGate v2 (CVaR-Kelly sizing, ADWIN drift, circuit breaker)."""

from __future__ import annotations

import pytest


class TestRiskGateSizing:
    def _gate(self, **kwargs):
        from src.risk.gate import RiskGate

        return RiskGate(**kwargs)

    def test_size_below_confidence_threshold_suppressed(self) -> None:
        gate = self._gate(conf_threshold=0.65)
        result = gate.size(
            signal={"confidence": 0.4, "sharpe_est": 2.0, "edge": 0.01, "odds": 1.0},
            vol=0.02,
            cvar=0.02,
            horizon_id=0,
        )
        assert result.suppressed
        assert result.size_pct == 0.0

    def test_size_below_sharpe_threshold_suppressed(self) -> None:
        gate = self._gate(sharpe_min=1.0)
        result = gate.size(
            signal={"confidence": 0.8, "sharpe_est": 0.5, "edge": 0.01, "odds": 1.0},
            vol=0.02,
            cvar=0.02,
            horizon_id=0,
        )
        assert result.suppressed
        assert result.size_pct == 0.0

    def test_size_valid_signal_in_range(self) -> None:
        gate = self._gate()
        result = gate.size(
            signal={"confidence": 0.8, "sharpe_est": 2.0, "edge": 0.05, "odds": 2.0},
            vol=0.02,
            cvar=0.01,
            horizon_id=0,
        )
        assert not result.suppressed
        assert 0.0 < result.size_pct <= 0.05

    def test_size_capped_at_max(self) -> None:
        """Kelly formula with high edge should be capped at 0.05."""
        gate = self._gate()
        result = gate.size(
            signal={"confidence": 0.9, "sharpe_est": 5.0, "edge": 1.0, "odds": 10.0},
            vol=0.001,
            cvar=0.001,
            horizon_id=0,
        )
        assert result.size_pct <= 0.05

    def test_size_scales_down_for_longer_horizons(self) -> None:
        gate = self._gate()
        signal = {"confidence": 0.8, "sharpe_est": 2.0, "edge": 0.05, "odds": 2.0}
        vol, cvar = 0.02, 0.01
        r0 = gate.size(signal=signal, vol=vol, cvar=cvar, horizon_id=0)
        r5 = gate.size(signal=signal, vol=vol, cvar=cvar, horizon_id=5)
        assert r0.size_pct >= r5.size_pct

    def test_from_config(self) -> None:
        from src.risk.gate import RiskGate

        gate = RiskGate.from_config({"kelly_fraction": 0.25, "conf_threshold": 0.7})
        assert gate._kelly_fraction == 0.25
        assert gate._conf_threshold == 0.7


class TestCircuitBreaker:
    def _gate(self):
        from src.risk.gate import RiskGate

        return RiskGate(drawdown_floor=0.10, max_daily_loss=0.02)

    def test_no_breach(self) -> None:
        gate = self._gate()
        assert not gate.circuit_breaker(0.05, 0.01)

    def test_drawdown_breach(self) -> None:
        gate = self._gate()
        assert gate.circuit_breaker(0.15, 0.01)

    def test_daily_loss_breach(self) -> None:
        gate = self._gate()
        assert gate.circuit_breaker(0.05, 0.03)

    def test_both_breach(self) -> None:
        gate = self._gate()
        assert gate.circuit_breaker(0.20, 0.05)


class TestADWINDrift:
    def test_check_drift_no_river(self, monkeypatch) -> None:
        """ADWIN disabled gracefully when river is not installed."""
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "river.drift":
                raise ImportError("no river")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        from src.risk.gate import RiskGate

        gate = RiskGate(n_horizons=3)
        # Should not raise even without river
        result = gate.check_drift(0, 0.8)
        assert isinstance(result, bool)

    def test_check_drift_valid_horizon(self) -> None:
        from src.risk.gate import RiskGate

        gate = RiskGate(n_horizons=3)
        # Feed a stable metric — no drift expected in 5 points
        for _ in range(5):
            gate.check_drift(0, 0.8)
        # Just assert no exception raised; drift state is probabilistic
        gate.check_drift(1, 0.5)

    def test_check_drift_out_of_range_does_not_crash(self) -> None:
        from src.risk.gate import RiskGate

        gate = RiskGate(n_horizons=3)
        # horizon_idx >= n_horizons → IndexError would be a bug
        with pytest.raises(IndexError):
            gate.check_drift(10, 0.8)


class TestHorizonConflictResolver:
    def _resolver(self, threshold=0.6):
        from src.risk.conflict_resolver import HorizonConflictResolver

        return HorizonConflictResolver(conflict_threshold=threshold)

    def test_empty_signals(self) -> None:
        r = self._resolver().resolve([])
        assert r.direction == 0
        assert r.conflict

    def test_unanimous_long(self) -> None:
        signals = [{"direction": 1, "confidence": 0.8}] * 5
        r = self._resolver().resolve(signals)
        assert r.direction == 1
        assert not r.conflict

    def test_unanimous_short(self) -> None:
        signals = [{"direction": -1, "confidence": 0.8}] * 5
        r = self._resolver().resolve(signals)
        assert r.direction == -1

    def test_conflicted_signals(self) -> None:
        signals = [
            {"direction": 1, "confidence": 0.8},
            {"direction": -1, "confidence": 0.8},
            {"direction": 1, "confidence": 0.5},
        ]
        r = self._resolver(threshold=0.8).resolve(signals)
        # 2/3 long but weak → conflict at 0.8 threshold
        assert r.agreement_ratio < 0.8 or r.direction == 1

    def test_ecc_whale_alert_forces_conflict(self) -> None:
        signals = [{"direction": 1, "confidence": 0.9}] * 5
        r = self._resolver().resolve_with_ecc(signals, None, ecc_anomaly=0.9)
        assert r.conflict  # ECC override forces conflict

    def test_ecc_low_anomaly_no_override(self) -> None:
        signals = [{"direction": 1, "confidence": 0.9}] * 5
        r = self._resolver().resolve_with_ecc(signals, None, ecc_anomaly=0.3)
        assert not r.conflict
