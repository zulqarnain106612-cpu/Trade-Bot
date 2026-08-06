"""Tests for CryptoBoxSignalAdapter's enabled paths.

`_ENABLED` is resolved at import time from the environment, so setting the
env var inside a test does nothing — the enabled branches were unreachable
and the file sat below its 70% coverage floor. These patch the module flag
directly.

The contract that matters: every failure mode returns None, because callers
treat None as "no augmentation" and fall back to the existing pipeline. An
exception escaping here would take down the tick.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.engine import crypto_box_adapter as cba
from src.engine.crypto_box_adapter import CryptoBoxSignalAdapter


class TestDisabled:
    def test_disabled_adapter_is_not_enabled(self):
        with patch.object(cba, "_ENABLED", False):
            assert not CryptoBoxSignalAdapter().enabled

    @pytest.mark.asyncio
    async def test_disabled_get_signal_returns_none(self):
        with patch.object(cba, "_ENABLED", False):
            assert await CryptoBoxSignalAdapter().get_signal("BTC/USDT", {}) is None


class TestEnabled:
    def test_init_builds_orchestrator(self):
        with (
            patch.object(cba, "_ENABLED", True),
            patch("src.engines.orchestrator.EngineOrchestrator") as orch,
        ):
            a = CryptoBoxSignalAdapter()
        assert a.enabled
        orch.assert_called_once()

    def test_init_failure_leaves_adapter_disabled(self):
        """A broken orchestrator must not raise out of the constructor."""
        with (
            patch.object(cba, "_ENABLED", True),
            patch(
                "src.engines.orchestrator.EngineOrchestrator",
                side_effect=RuntimeError("engine import blew up"),
            ),
        ):
            a = CryptoBoxSignalAdapter()
        assert not a.enabled

    @pytest.mark.asyncio
    async def test_get_signal_returns_trade_signal(self):
        sentinel = object()
        result = MagicMock()
        result.trade_signal = sentinel
        orch = MagicMock()
        orch.run = AsyncMock(return_value=result)

        with patch.object(cba, "_ENABLED", True):
            a = CryptoBoxSignalAdapter()
            a._orchestrator = orch
            got = await a.get_signal("BTC/USDT", {"spot": 50_000.0})

        assert got is sentinel
        orch.run.assert_awaited_once_with("BTC/USDT", {"spot": 50_000.0})

    @pytest.mark.asyncio
    async def test_orchestrator_error_returns_none_not_raise(self):
        orch = MagicMock()
        orch.run = AsyncMock(side_effect=RuntimeError("engine exploded"))

        with patch.object(cba, "_ENABLED", True):
            a = CryptoBoxSignalAdapter()
            a._orchestrator = orch
            assert await a.get_signal("BTC/USDT", {}) is None

    @pytest.mark.asyncio
    async def test_enabled_flag_without_orchestrator_returns_none(self):
        """Guards the `self._orchestrator is None` half of the enabled check."""
        with patch.object(cba, "_ENABLED", True):
            a = CryptoBoxSignalAdapter()
            a._orchestrator = None
            assert not a.enabled
            assert await a.get_signal("BTC/USDT", {}) is None
