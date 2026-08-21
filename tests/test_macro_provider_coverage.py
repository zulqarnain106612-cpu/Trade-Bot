"""Coverage for MacroProvider's fetch, persistence and cache-fallback paths."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.data.macro_provider import MacroProvider


def _hist(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"Close": closes})


def _yf(frame: pd.DataFrame) -> MagicMock:
    return MagicMock(**{"download.return_value": frame})


class TestFetchSync:
    def test_closes_and_returns_are_extracted(self, tmp_path: Path) -> None:
        provider = MacroProvider(data_root=tmp_path)
        with patch.dict("sys.modules", {"yfinance": _yf(_hist([100.0, 101.0, 102.0]))}):
            row = provider._fetch_sync()
        assert row is not None
        assert row["spx_close"] == 102.0
        assert "spx_ret" in row
        assert "date" in row

    def test_an_empty_history_yields_no_row(self, tmp_path: Path) -> None:
        provider = MacroProvider(data_root=tmp_path)
        with patch.dict("sys.modules", {"yfinance": _yf(pd.DataFrame())}):
            assert provider._fetch_sync() is None

    def test_a_frame_without_a_close_column_is_skipped(self, tmp_path: Path) -> None:
        provider = MacroProvider(data_root=tmp_path)
        with patch.dict("sys.modules", {"yfinance": _yf(pd.DataFrame({"Open": [1.0]}))}):
            assert provider._fetch_sync() is None

    def test_too_few_points_gives_a_close_but_no_return(self, tmp_path: Path) -> None:
        provider = MacroProvider(data_root=tmp_path)
        with patch.dict("sys.modules", {"yfinance": _yf(_hist([100.0]))}):
            row = provider._fetch_sync()
        assert row is not None
        assert row["spx_close"] == 100.0
        assert "spx_ret" not in row

    def test_a_download_fault_is_swallowed(self, tmp_path: Path) -> None:
        provider = MacroProvider(data_root=tmp_path)
        broken = MagicMock(**{"download.side_effect": RuntimeError("network down")})
        with patch.dict("sys.modules", {"yfinance": broken}):
            assert provider._fetch_sync() is None

    def test_a_successful_fetch_is_persisted(self, tmp_path: Path) -> None:
        provider = MacroProvider(data_root=tmp_path)
        with patch.dict("sys.modules", {"yfinance": _yf(_hist([100.0, 101.0, 102.0]))}):
            provider._fetch_sync()
        assert list((tmp_path / "macro").glob("*.parquet"))


class TestPersistAndCache:
    def test_persist_then_load_round_trips(self, tmp_path: Path) -> None:
        provider = MacroProvider(data_root=tmp_path)
        provider._persist({"date": "2026-01-01", "spx_close": 4200.0})
        assert provider._load_cached()["spx_close"] == 4200.0

    def test_a_missing_directory_is_not_an_error(self, tmp_path: Path) -> None:
        assert MacroProvider(data_root=tmp_path)._load_cached() is None

    def test_an_unreadable_file_is_skipped_for_the_next_one(self, tmp_path: Path) -> None:
        provider = MacroProvider(data_root=tmp_path)
        provider._persist({"date": "2026-01-01", "spx_close": 4200.0})
        # Sorts first under reverse=True, so it is tried before the good file.
        (tmp_path / "macro" / "9999-01-01.parquet").write_text("not parquet")
        assert provider._load_cached()["spx_close"] == 4200.0

    def test_an_empty_frame_is_skipped(self, tmp_path: Path) -> None:
        provider = MacroProvider(data_root=tmp_path)
        (tmp_path / "macro").mkdir(parents=True)
        pd.DataFrame({"spx_close": []}).to_parquet(tmp_path / "macro" / "2026-01-01.parquet")
        assert provider._load_cached() is None


class TestLatest:
    def test_the_in_memory_value_wins(self, tmp_path: Path) -> None:
        provider = MacroProvider(data_root=tmp_path)
        provider._latest = {"spx_close": 1.0}
        assert provider.latest() == {"spx_close": 1.0}

    def test_it_falls_back_to_the_cache(self, tmp_path: Path) -> None:
        provider = MacroProvider(data_root=tmp_path)
        provider._persist({"date": "2026-01-01", "spx_close": 4200.0})
        assert provider.latest()["spx_close"] == 4200.0


class TestAsync:
    @pytest.mark.asyncio
    async def test_fetch_once_runs_the_sync_fetch_off_the_loop(self, tmp_path: Path) -> None:
        provider = MacroProvider(data_root=tmp_path)
        with patch.object(provider, "_fetch_sync", return_value={"spx_close": 1.0}):
            assert await provider.fetch_once() == {"spx_close": 1.0}

    @pytest.mark.asyncio
    async def test_run_loop_publishes_a_result_to_the_shared_cache(self, tmp_path: Path) -> None:
        provider = MacroProvider(data_root=tmp_path)
        cache = MagicMock()
        with (
            patch.object(provider, "fetch_once", return_value={"spx_close": 1.0}),
            patch("src.data.provider_cache.get_provider_cache", return_value=cache),
            patch("asyncio.sleep", side_effect=asyncio.CancelledError),
        ):
            with pytest.raises(asyncio.CancelledError):
                await provider.run_loop()
        assert provider._latest == {"spx_close": 1.0}
        cache.set_macro.assert_called_once_with({"spx_close": 1.0})

    @pytest.mark.asyncio
    async def test_a_cache_fault_does_not_stop_the_loop(self, tmp_path: Path) -> None:
        provider = MacroProvider(data_root=tmp_path)
        with (
            patch.object(provider, "fetch_once", return_value={"spx_close": 1.0}),
            patch(
                "src.data.provider_cache.get_provider_cache", side_effect=RuntimeError("no cache")
            ),
            patch("asyncio.sleep", side_effect=asyncio.CancelledError),
        ):
            with pytest.raises(asyncio.CancelledError):
                await provider.run_loop()
        assert provider._latest == {"spx_close": 1.0}

    @pytest.mark.asyncio
    async def test_an_empty_result_is_not_published(self, tmp_path: Path) -> None:
        provider = MacroProvider(data_root=tmp_path)
        with (
            patch.object(provider, "fetch_once", return_value=None),
            patch("asyncio.sleep", side_effect=asyncio.CancelledError),
        ):
            with pytest.raises(asyncio.CancelledError):
                await provider.run_loop()
        assert provider._latest is None
