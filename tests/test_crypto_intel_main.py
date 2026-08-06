"""Tests for src/intel.py — CryptoIntelligence top-level class."""

from __future__ import annotations

import asyncio


class TestIntelSignal:
    def test_signal_fields(self) -> None:
        from src.intel import IntelSignal

        sig = IntelSignal(
            symbol="BTC/USDT",
            direction=1,
            size_pct=0.02,
            confidence=0.75,
            horizon_idx=2,
            algo="IOC",
            ecc_anomaly=0.3,
            conflict=False,
            regime_id=1,
        )
        assert sig.symbol == "BTC/USDT"
        assert sig.direction == 1
        assert sig.algo == "IOC"
        assert sig.ts > 0


class TestCryptoIntelligenceInit:
    def test_init_with_missing_config_uses_defaults(self, tmp_path) -> None:
        from src.intel import CryptoIntelligence

        cfg = tmp_path / "intelligence.yaml"
        # missing file — should not raise
        intel = CryptoIntelligence(config_path=cfg)
        assert intel is not None

    def test_load_config_returns_empty_for_missing_file(self, tmp_path) -> None:
        from src.intel import CryptoIntelligence

        cfg = tmp_path / "nope.yaml"
        intel = CryptoIntelligence.__new__(CryptoIntelligence)
        result = intel._load_config(cfg)
        assert result == {}

    def test_load_config_parses_yaml(self, tmp_path) -> None:
        from src.intel import CryptoIntelligence

        cfg = tmp_path / "cfg.yaml"
        cfg.write_text("n_workers: 4\necc_enabled: true\n")
        intel = CryptoIntelligence.__new__(CryptoIntelligence)
        result = intel._load_config(cfg)
        assert result["n_workers"] == 4
        assert result["ecc_enabled"] is True


class TestCryptoIntelligenceLifecycle:
    def test_start_and_close(self, tmp_path) -> None:
        from src.intel import CryptoIntelligence

        cfg = tmp_path / "i.yaml"
        intel = CryptoIntelligence(config_path=cfg)
        intel.start()
        assert intel._started
        intel.close()
        assert not intel._started

    def test_double_start_idempotent(self, tmp_path) -> None:
        from src.intel import CryptoIntelligence

        cfg = tmp_path / "i.yaml"
        intel = CryptoIntelligence(config_path=cfg)
        intel.start()
        intel.start()
        assert intel._started
        intel.close()


class TestOnBarAsync:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_on_bar_returns_signal_or_none(self, tmp_path) -> None:
        from src.intel import CryptoIntelligence

        cfg = tmp_path / "i.yaml"
        intel = CryptoIntelligence(config_path=cfg)
        intel.start()
        ohlcv = {
            "open": 50000.0,
            "high": 50100.0,
            "low": 49900.0,
            "close": 50050.0,
            "volume": 100.0,
        }
        result = self._run(intel.on_bar("BTC/USDT", ohlcv))
        # RiskGate may suppress → None is valid
        assert result is None or hasattr(result, "direction")
        intel.close()

    def test_on_bar_auto_starts(self, tmp_path) -> None:
        from src.intel import CryptoIntelligence

        cfg = tmp_path / "i.yaml"
        intel = CryptoIntelligence(config_path=cfg)
        assert not intel._started
        ohlcv = {"close": 50000.0, "volume": 10.0}
        self._run(intel.on_bar("BTC/USDT", ohlcv))
        assert intel._started
        intel.close()

    def test_on_bar_with_bids_asks(self, tmp_path) -> None:
        from src.intel import CryptoIntelligence

        cfg = tmp_path / "i.yaml"
        intel = CryptoIntelligence(config_path=cfg)
        ohlcv = {"close": 50000.0, "volume": 10.0}
        bids = [[49990.0, 1.0], [49980.0, 2.0]]
        asks = [[50010.0, 1.0], [50020.0, 2.0]]
        result = self._run(intel.on_bar("BTC/USDT", ohlcv, bids=bids, asks=asks))
        assert result is None or hasattr(result, "symbol")
        intel.close()

    def test_on_bar_with_derivatives_data(self, tmp_path) -> None:
        from src.intel import CryptoIntelligence

        cfg = tmp_path / "i.yaml"
        intel = CryptoIntelligence(config_path=cfg)
        ohlcv = {"close": 50000.0, "volume": 10.0}
        deriv = {"oi_usd": 1e9, "funding_rate": 0.0001, "liquidations_usd": 5e6}
        result = self._run(intel.on_bar("BTC/USDT", ohlcv, derivatives_data=deriv))
        assert result is None or hasattr(result, "symbol")
        intel.close()


class TestCollectECC:
    def test_collect_ecc_returns_dict(self, tmp_path) -> None:
        from src.intel import CryptoIntelligence

        cfg = tmp_path / "i.yaml"
        intel = CryptoIntelligence(config_path=cfg)
        intel.start()
        result = intel._collect_ecc()
        assert isinstance(result, dict)
        intel.close()
