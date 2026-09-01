"""Tests for src/features/mempool.py -- bitcoind mempool feature fetch."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.features.mempool import fetch_mempool_features


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload


class _FakeSession:
    def __init__(self, info: dict, fees: dict) -> None:
        self._info = info
        self._fees = fees

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json, headers, **_kwargs):
        if json["method"] == "getmempoolinfo":
            return _FakeResponse({"result": self._info})
        return _FakeResponse({"result": self._fees})


async def test_fetch_mempool_features_happy_path():
    fake_session = _FakeSession(
        info={"size": 12000, "bytes": 5_000_000},
        fees={"feerate": 0.00002},  # BTC/kB
    )
    with patch("aiohttp.ClientSession", return_value=fake_session), patch("aiohttp.BasicAuth"):
        feats = await fetch_mempool_features()

    assert feats.tx_count == 12000
    assert feats.mempool_bytes == 5_000_000
    expected_sat_vb = 0.00002 * 1e8 / 1000.0
    assert feats.fee_rate_p50_sat == pytest.approx(expected_sat_vb)
    assert feats.fee_rate_p90_sat == pytest.approx(expected_sat_vb * 1.5)
    assert feats.fee_pressure == pytest.approx(min(expected_sat_vb / 1000.0, 1.0))


async def test_fetch_mempool_features_high_fee_pressure_is_capped_at_one():
    fake_session = _FakeSession(info={"size": 1, "bytes": 1}, fees={"feerate": 0.5})
    with patch("aiohttp.ClientSession", return_value=fake_session), patch("aiohttp.BasicAuth"):
        feats = await fetch_mempool_features()
    assert feats.fee_pressure == 1.0


async def test_fetch_mempool_features_falls_back_to_zero_on_error():
    with patch("aiohttp.ClientSession", side_effect=OSError("no route to host")):
        feats = await fetch_mempool_features()

    assert feats.tx_count == 0
    assert feats.fee_rate_p50_sat == 0.0
    assert feats.fee_rate_p90_sat == 0.0
    assert feats.mempool_bytes == 0
    assert feats.fee_pressure == 0.0


async def test_fetch_mempool_features_accepts_explicit_credentials():
    fake_session = _FakeSession(info={"size": 5, "bytes": 10}, fees={"feerate": 0.0001})
    with patch("aiohttp.ClientSession", return_value=fake_session), patch("aiohttp.BasicAuth"):
        feats = await fetch_mempool_features(rpc_url="http://node:8332", rpc_user="u", rpc_pass="p")
    assert feats.tx_count == 5
