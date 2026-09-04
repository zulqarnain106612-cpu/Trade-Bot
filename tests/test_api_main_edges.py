"""Coverage for the small uncovered branches in src/api/main.py.

Targets the pieces reachable without standing up the full app lifespan:
the shared operator validator behind several request models, the
CryptoBoxSignalAdapter singleton cache, and the /crypto-box/status
response shaping.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import src.api.main as api_main
from src.api.main import (
    RecoveryAcknowledgeRequest,
    StrategyReEnableRequest,
    _get_crypto_box_adapter,
    crypto_box_status,
)


def test_reenable_request_accepts_valid_operator():
    req = StrategyReEnableRequest(operator="alice", operator_secret="s3cret")
    assert req.operator == "alice"
    assert req.force is False


def test_reenable_request_force_defaults_false_and_is_settable():
    req = StrategyReEnableRequest(operator="alice", operator_secret="s", force=True)
    assert req.force is True


def test_reenable_request_rejects_invalid_operator():
    with pytest.raises(ValueError):
        StrategyReEnableRequest(operator="bad operator!!", operator_secret="s")


def test_recovery_ack_request_validates_operator():
    req = RecoveryAcknowledgeRequest(operator="bob", operator_secret="s")
    assert req.operator == "bob"


def test_recovery_ack_request_rejects_invalid_operator():
    with pytest.raises(ValueError):
        RecoveryAcknowledgeRequest(operator="!!!", operator_secret="s")


def test_get_crypto_box_adapter_is_cached():
    _get_crypto_box_adapter.cache_clear()
    fake_adapter = MagicMock()
    try:
        with patch(
            "src.engine.crypto_box_adapter.CryptoBoxSignalAdapter", return_value=fake_adapter
        ) as mock_cls:
            first = _get_crypto_box_adapter()
            second = _get_crypto_box_adapter()
        assert first is second is fake_adapter
        mock_cls.assert_called_once()  # constructed once, reused after
    finally:
        _get_crypto_box_adapter.cache_clear()


async def test_crypto_box_status_shapes_response():
    fake_cache = MagicMock()
    fake_cache.get_sentiment.return_value = {"score": 0.5}
    fake_cache.get_macro.return_value = {"dxy": 103.456789, "label": "risk_on"}
    fake_cache._data = {
        "orderbook_BTCUSDT": {},
        "options_BTC": {},
        "unrelated_key": {},
    }

    fake_adapter = MagicMock(enabled=True)

    with (
        patch("src.data.provider_cache.get_provider_cache", return_value=fake_cache),
        patch.object(api_main, "_get_crypto_box_adapter", return_value=fake_adapter),
    ):
        result = await crypto_box_status()

    assert result["enabled"] is True
    assert result["sentiment"] == {"score": 0.5}
    assert result["macro"]["dxy"] == 103.4568  # floats rounded to 4dp
    assert result["macro"]["label"] == "risk_on"  # non-floats passed through
    assert result["orderbook_symbols"] == ["BTCUSDT"]
    assert result["options_symbols"] == ["BTC"]


async def test_crypto_box_status_handles_empty_macro():
    fake_cache = MagicMock()
    fake_cache.get_sentiment.return_value = None
    fake_cache.get_macro.return_value = None
    fake_cache._data = {}

    fake_adapter = MagicMock(enabled=False)

    with (
        patch("src.data.provider_cache.get_provider_cache", return_value=fake_cache),
        patch.object(api_main, "_get_crypto_box_adapter", return_value=fake_adapter),
    ):
        result = await crypto_box_status()

    assert result["enabled"] is False
    assert result["macro"] == {}
    assert result["orderbook_symbols"] == []
