"""Test-suite wide isolation.

`DuckDBStore(path=None)` falls back to the module-level `_DB_PATH`, which
defaults to the repository's real `data/crypto_intel.duckdb`. Without this
the suite writes production rows into a checked-out working tree and
row-count assertions depend on whatever previous runs left behind.

`_DB_PATH` is read from `DUCKDB_PATH` at import time, so this must run
before any test imports `src.data.duckdb_store` — a `tests/conftest.py`
module body is imported first, which is why this is not a fixture.
"""

from __future__ import annotations

import os
import socket as _socket
import tempfile
from pathlib import Path
from urllib.parse import urlparse

_TMP_DB_DIR = Path(tempfile.mkdtemp(prefix="trade-bot-tests-duckdb-"))
os.environ.setdefault("DUCKDB_PATH", str(_TMP_DB_DIR / "crypto_intel.duckdb"))


def settings_double():
    """A `get_settings()` stand-in whose sub-configs are real, not MagicMocks.

    A bare `MagicMock()` hands back a MagicMock for every attribute, so code
    that compares a config value against a number (`lookback_days <= 0`)
    raises TypeError instead of exercising the path under test. Sub-configs
    with real defaults are substituted; everything else stays auto-specced.
    """
    from unittest.mock import MagicMock

    from src.config import StrategyPortfolioSettings

    cfg = MagicMock()
    cfg.strategy_portfolio = StrategyPortfolioSettings()
    return cfg


# ---------------------------------------------------------------------------
# No test may open a real network connection.
# ---------------------------------------------------------------------------
#
# The suite's standing rule is that nothing contacts a real exchange, node or
# database. Nothing enforced it, so a test that forgot to patch its transport
# silently made a real connection: it passed anyway, because the code under
# test fails open, but it was exercising the "unreachable" branch rather than
# the branch the test claimed to cover -- and it paid a DNS timeout per call
# on a machine with no route out.
#
# Blocking the socket layer catches every client (requests, aiohttp, httpx,
# raw sockets) in one place. AF_UNIX is left alone: it is how a local test
# database or a subprocess pipe talks, and it never leaves the machine.


class NetworkAccessDenied(OSError):
    """Raised when a test tries to open a real network connection.

    An OSError, not a bare RuntimeError, because that is what a refused
    connection already raises: code whose job is to fail open on an
    unreachable endpoint still behaves the way it would on a machine with no
    route out.
    """


# The one exception, and it is narrow: a TimescaleDB the operator deliberately
# provisioned and named in STORAGE_TIMESCALE_DSN. tests/test_timescale_storage.py
# is an integration suite -- it exists to exercise a real database -- so denying
# it a socket did not make it hermetic, it made it disappear: 92 tests skipped
# on a laptop and in CI alike, and the storage backend reported green by
# absence. Only a loopback address on the configured port is let through, so
# this can never become a route to an exchange, a node, or anything off-box;
# with the variable unset (a plain `pytest` run) nothing is allowed at all.
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _allowed_endpoints() -> frozenset[tuple[str, int]]:
    dsn = os.environ.get("STORAGE_TIMESCALE_DSN")
    if not dsn:
        return frozenset()
    try:
        parsed = urlparse(dsn)
        host, port = parsed.hostname, parsed.port
    except ValueError:  # pragma: no cover - a malformed DSN allows nothing
        return frozenset()
    if host not in _LOOPBACK_HOSTS:
        return frozenset()
    return frozenset((h, port or 5432) for h in _LOOPBACK_HOSTS)


_ALLOWED_ENDPOINTS = _allowed_endpoints()


def _is_allowed(address) -> bool:
    return (
        isinstance(address, tuple)
        and len(address) >= 2
        and (address[0], address[1]) in _ALLOWED_ENDPOINTS
    )


_REAL_CONNECT = _socket.socket.connect
_REAL_CONNECT_EX = _socket.socket.connect_ex
_REAL_CREATE_CONNECTION = _socket.create_connection


def _denier(real):
    """Wrap one socket method so only AF_UNIX reaches the real one."""

    def _deny(self, address, *args, **kwargs):
        if getattr(self, "family", None) == getattr(_socket, "AF_UNIX", None):
            return real(self, address, *args, **kwargs)
        if _is_allowed(address):
            return real(self, address, *args, **kwargs)
        raise NetworkAccessDenied(
            f"a test tried to connect to {address!r}. Patch the transport instead: "
            "a real connection means the test is exercising the failure branch, "
            "not the one it claims to cover."
        )

    return _deny


def _deny_create_connection(address, *_args, **_kwargs):
    if _is_allowed(address):
        return _REAL_CREATE_CONNECTION(address, *_args, **_kwargs)
    raise NetworkAccessDenied(
        f"a test tried to connect to {address!r}. Patch the transport instead."
    )


# Installed at import, not as a fixture: a fixture only covers the window
# between setup and teardown of one test, and the calls that motivated this
# came from a worker thread that outlived it and from module import time.
# Patching once here covers collection, threads and teardown as well.
_socket.socket.connect = _denier(_REAL_CONNECT)
_socket.socket.connect_ex = _denier(_REAL_CONNECT_EX)
_socket.create_connection = _deny_create_connection
