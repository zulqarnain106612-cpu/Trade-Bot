"""
Macro daily data provider for E-13 (cross-asset contagion engine).

Sources: yfinance SPX / DXY / GLD / VIX — free, no auth.
Polls once per 24h (market-close cadence).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_TICKERS = {
    "spx": "^GSPC",
    "dxy": "DX-Y.NYB",
    "gld": "GLD",
    "vix": "^VIX",
}
_POLL_INTERVAL = 86_400  # 24 hours
_STALE_LIMIT_DAYS = 2  # accept weekend gaps


class MacroProvider:
    def __init__(self, data_root: Path = Path("data")) -> None:
        self._data_root = data_root
        self._latest: dict | None = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def latest(self) -> dict | None:
        if self._latest is not None:
            return self._latest
        return self._load_cached()

    async def fetch_once(self) -> dict | None:
        return await asyncio.get_running_loop().run_in_executor(None, self._fetch_sync)

    async def run_loop(self) -> None:
        while True:
            result = await self.fetch_once()
            if result:
                self._latest = result
                try:
                    from src.data.provider_cache import get_provider_cache

                    get_provider_cache().set_macro(result)
                except Exception as exc:
                    log.warning(
                        "provider_cache_publish_failed", field="macro", exc=str(exc)
                    )
            await asyncio.sleep(_POLL_INTERVAL)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _fetch_sync(self) -> dict | None:
        try:
            import yfinance as yf  # type: ignore[import]

            closes: dict[str, float] = {}
            returns: dict[str, float] = {}
            series: dict[str, list] = {}
            for key, ticker in _TICKERS.items():
                # 90-day window to support Granger causality in E-13
                hist = yf.download(ticker, period="90d", interval="1d", progress=False)
                if hist.empty:
                    continue
                close_col = "Close"
                if close_col not in hist.columns:
                    continue
                close_vals = hist[close_col].dropna()
                closes[f"{key}_close"] = float(close_vals.iloc[-1])
                ret_series = close_vals.pct_change().dropna()
                if len(ret_series) >= 2:
                    returns[f"{key}_ret"] = float(ret_series.iloc[-1])
                    series[f"{key}_series"] = ret_series.values.tolist()

            if not closes:
                return None

            row: dict = {
                "date": datetime.now(UTC).date().isoformat(),
                **closes,
                **returns,
                **series,
            }
            self._persist(row)
            return row
        except Exception as exc:
            log.warning("macro_fetch_error", exc=str(exc))
            return None

    def _persist(self, row: dict) -> None:
        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        path = self._data_root / "macro" / f"{date_str}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([row]).to_parquet(path, index=False)

    def _load_cached(self) -> dict | None:
        macro_dir = self._data_root / "macro"
        if not macro_dir.exists():
            return None
        files = sorted(macro_dir.glob("*.parquet"), reverse=True)
        for f in files[:_STALE_LIMIT_DAYS]:
            try:
                df = pd.read_parquet(f)
                if not df.empty:
                    return df.iloc[-1].to_dict()
            except Exception as exc:
                # A corrupt or half-written parquet must not hide the older
                # snapshots behind it, so fall through to the next file.
                log.warning("macro_snapshot_unreadable", path=str(f), exc=str(exc))
                continue
        return None
