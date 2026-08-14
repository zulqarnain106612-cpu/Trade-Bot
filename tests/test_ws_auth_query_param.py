"""
The dashboard's WebSocket could never authenticate.

frontend/src/App.jsx connects with ``?api_key=...`` and says why in its own
comment: browsers cannot set headers on a WebSocket upgrade. verify_ws_key
read only the header, so every browser connection was closed 4401 -- which
went unnoticed because the REST polling that fills most of the UI does set
the header and kept working.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from src.api.access_control import Role
from src.api.auth import verify_ws_key


_PRIMARY = "a" * 40
_READONLY = "b" * 40


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_SECRET_KEY", _PRIMARY)
    monkeypatch.delenv("API_READONLY_KEY", raising=False)


def _ws(header: str | None = None, query: str | None = None) -> MagicMock:
    ws = MagicMock()
    ws.headers = {"x-api-key": header} if header else {}
    ws.query_params = {"api_key": query} if query else {}
    ws.close = AsyncMock()
    return ws


class TestQueryParamFallback:
    @pytest.mark.asyncio
    async def test_a_browser_style_query_param_authenticates(self) -> None:
        """The exact shape frontend/src/App.jsx sends."""
        assert await verify_ws_key(_ws(query=_PRIMARY)) is Role.TRADE_AUTHORIZING

    @pytest.mark.asyncio
    async def test_the_socket_is_not_closed_on_a_valid_query_param(self) -> None:
        ws = _ws(query=_PRIMARY)
        await verify_ws_key(ws)
        ws.close.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_wrong_query_param_is_still_rejected(self) -> None:
        ws = _ws(query="z" * 40)
        with pytest.raises(HTTPException):
            await verify_ws_key(ws)
        ws.close.assert_awaited_once_with(code=4401)

    @pytest.mark.asyncio
    async def test_a_read_only_key_resolves_to_its_role_over_the_query(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("API_READONLY_KEY", _READONLY)
        assert await verify_ws_key(_ws(query=_READONLY)) is Role.READ_ONLY


class TestHeaderStillPreferred:
    @pytest.mark.asyncio
    async def test_the_header_path_is_unchanged(self) -> None:
        assert await verify_ws_key(_ws(header=_PRIMARY)) is Role.TRADE_AUTHORIZING

    @pytest.mark.asyncio
    async def test_the_header_wins_when_both_are_present(self) -> None:
        """
        Query strings appear in proxy and access logs where headers do not,
        so a client that can set a header must keep using it.
        """
        assert await verify_ws_key(_ws(header=_PRIMARY, query="z" * 40)) is (Role.TRADE_AUTHORIZING)

    @pytest.mark.asyncio
    async def test_neither_supplied_is_rejected(self) -> None:
        ws = _ws()
        with pytest.raises(HTTPException):
            await verify_ws_key(ws)
        ws.close.assert_awaited_once_with(code=4401)


def test_the_frontend_still_sends_the_parameter_the_server_reads() -> None:
    """
    The break was a name/mechanism mismatch across the boundary, so pin the
    contract from the frontend side too — the server's own tests could not
    have caught it.
    """
    from pathlib import Path

    app_jsx = Path("frontend/src/App.jsx").read_text(encoding="utf-8")
    assert "api_key=" in app_jsx, "frontend no longer sends ?api_key= — update the server"
