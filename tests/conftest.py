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
    connection already raises: a suite that skips when its container is not
    running (tests/test_timescale_storage.py) keeps skipping instead of
    erroring, and code whose job is to fail open on an unreachable endpoint
    still behaves the way it would on a machine with no route out.
    """


_REAL_CONNECT = _socket.socket.connect
_REAL_CONNECT_EX = _socket.socket.connect_ex


def _denier(real):
    """Wrap one socket method so only AF_UNIX reaches the real one."""

    def _deny(self, address, *args, **kwargs):
        if getattr(self, "family", None) == getattr(_socket, "AF_UNIX", None):
            return real(self, address, *args, **kwargs)
        raise NetworkAccessDenied(
            f"a test tried to connect to {address!r}. Patch the transport instead: "
            "a real connection means the test is exercising the failure branch, "
            "not the one it claims to cover."
        )

    return _deny


def _deny_create_connection(address, *_args, **_kwargs):
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
