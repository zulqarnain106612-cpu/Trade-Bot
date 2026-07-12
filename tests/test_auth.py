"""Tests for src/api/auth.py — API key validation for REST and WebSocket."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from src.api.auth import verify_api_key, verify_ws_key

_VALID_KEY = "a" * 32


@pytest.fixture(autouse=True)
def api_secret_key(monkeypatch):
    monkeypatch.setenv("API_SECRET_KEY", _VALID_KEY)
    yield


class TestVerifyApiKey:
    def test_correct_key_passes(self):
        verify_api_key(_VALID_KEY)

    def test_missing_key_raises_401(self):
        with pytest.raises(HTTPException) as exc_info:
            verify_api_key(None)
        assert exc_info.value.status_code == 401

    def test_wrong_key_raises_401(self):
        with pytest.raises(HTTPException) as exc_info:
            verify_api_key("b" * 32)
        assert exc_info.value.status_code == 401

    def test_empty_key_raises_401(self):
        with pytest.raises(HTTPException) as exc_info:
            verify_api_key("")
        assert exc_info.value.status_code == 401

    def test_unconfigured_key_raises_503(self, monkeypatch):
        monkeypatch.delenv("API_SECRET_KEY", raising=False)
        with pytest.raises(HTTPException) as exc_info:
            verify_api_key(_VALID_KEY)
        assert exc_info.value.status_code == 503

    def test_short_key_raises_503(self, monkeypatch):
        monkeypatch.setenv("API_SECRET_KEY", "short")
        with pytest.raises(HTTPException) as exc_info:
            verify_api_key("short")
        assert exc_info.value.status_code == 503


class TestVerifyWsKey:
    async def test_correct_key_passes(self):
        ws = MagicMock()
        ws.headers = {"x-api-key": _VALID_KEY}
        ws.close = AsyncMock()
        await verify_ws_key(ws)
        ws.close.assert_not_called()

    async def test_missing_key_closes_4401(self):
        ws = MagicMock()
        ws.headers = {}
        ws.close = AsyncMock()
        with pytest.raises(HTTPException) as exc_info:
            await verify_ws_key(ws)
        assert exc_info.value.status_code == 401
        ws.close.assert_awaited_once_with(code=4401)

    async def test_wrong_key_closes_4401(self):
        ws = MagicMock()
        ws.headers = {"x-api-key": "b" * 32}
        ws.close = AsyncMock()
        with pytest.raises(HTTPException):
            await verify_ws_key(ws)
        ws.close.assert_awaited_once_with(code=4401)

    async def test_unconfigured_key_closes_4503(self, monkeypatch):
        monkeypatch.delenv("API_SECRET_KEY", raising=False)
        ws = MagicMock()
        ws.headers = {"x-api-key": _VALID_KEY}
        ws.close = AsyncMock()
        with pytest.raises(RuntimeError):
            await verify_ws_key(ws)
        ws.close.assert_awaited_once_with(code=4503)
