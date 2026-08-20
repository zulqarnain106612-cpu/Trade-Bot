"""
Tests for wiring: CryptoBoxSignalAdapter + RegimeDetector.predict_current_v2.

These are unit-level tests that run without CRYPTO_BOX=true, verifying
that the adapter returns None gracefully and the v2 method returns None
when not enabled.
"""

from __future__ import annotations

import os


def test_adapter_disabled_returns_none():
    """Adapter with CRYPTO_BOX unset must return None synchronously."""
    import asyncio

    # Ensure env flag is unset
    os.environ.pop("CRYPTO_BOX", None)
    from src.engine.crypto_box_adapter import CryptoBoxSignalAdapter

    adapter = CryptoBoxSignalAdapter()
    assert not adapter.enabled
    result = asyncio.get_event_loop().run_until_complete(
        adapter.get_signal("BTC/USDT", {"spot": 50000.0})
    )
    assert result is None


def test_predict_current_v2_disabled_returns_none():
    """predict_current_v2 must return None when CRYPTO_BOX is not set."""
    os.environ.pop("CRYPTO_BOX", None)

    # Import after env cleared (module uses os.environ.get at call time, not import time)
    from src.regime.detector import RegimeDetector

    rd = RegimeDetector.__new__(RegimeDetector)
    rd._log = __import__("structlog").get_logger()
    result = rd.predict_current_v2({})
    assert result is None


def test_predict_current_v2_enabled_no_model_returns_none():
    """When CRYPTO_BOX=true but no model fitted, must return None (not raise)."""
    os.environ["CRYPTO_BOX"] = "true"
    try:
        from src.regime.detector import RegimeDetector

        rd = RegimeDetector.__new__(RegimeDetector)
        rd._log = __import__("structlog").get_logger()
        rd._depth_v2 = None  # type: ignore[attr-defined]
        result = rd.predict_current_v2({})
        assert result is None
    finally:
        os.environ.pop("CRYPTO_BOX", None)


def test_provider_cache_snapshot_snapshot_keys():
    from src.data.provider_cache import _ProviderCache

    c = _ProviderCache()
    snap = c.snapshot("BTC/USDT")
    expected = {
        "sentiment",
        "macro",
        "options",
        "orderbook",
        "onchain",
        "exchange_flows",
        "block_height",
    }
    assert set(snap.keys()) == expected
