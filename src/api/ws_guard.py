"""
API-005 — an established WebSocket connection confers no trust.

The upgrade is authenticated (`verify_ws_key`), and that is where most
WebSocket security stops: the handshake is treated as a login and every frame
afterwards as trusted. It is not a login. The socket outlives the request that
opened it, the key that opened it may since have been revoked, and a frame is
a caller-supplied byte string like any request body -- with none of the
framework's parsing, size limits or validation around it, because none of
FastAPI's request machinery is on that path.

So every inbound frame is re-checked here, independently, in this order:

  size -> parse -> schema -> freshness -> replay -> rate

The order is the point. Parsing before bounding the size means a 50 MB frame
is decoded before it is rejected; checking replay before freshness means the
nonce cache absorbs arbitrarily old frames an attacker chose to keep. Each
check is cheap and each one is only correct behind the previous one.

This bot's ``/ws`` endpoint is push-only: it has no inbound command a client
is *supposed* to send. That makes the guard stricter, not redundant -- the
correct answer to an unsolicited frame is to close the connection, and
"there's nothing to validate" is precisely the assumption that makes the first
inbound command someone adds a hole.
"""

from __future__ import annotations

import collections
import json
import time
from typing import Any, Final

# Close codes. 1008 (policy violation) and 1009 (message too big) are the
# RFC 6455 codes; 4429 is application-private and mirrors HTTP 429, matching
# the code the capacity limiter already uses for a rejected connect.
CLOSE_POLICY_VIOLATION: Final[int] = 1008
CLOSE_MESSAGE_TOO_BIG: Final[int] = 1009
CLOSE_RATE_LIMITED: Final[int] = 4429

# A control frame is small. The bound is on the *encoded* bytes, checked
# before json.loads, because a decoder is an attacker-controlled cost.
MAX_FRAME_BYTES: Final[int] = 4096

# Per-connection budget. Generous for a heartbeat client, useless for a
# flooder: sustained traffic on a socket that is meant to be push-only is
# itself the anomaly.
MAX_FRAMES_PER_WINDOW: Final[int] = 20
FRAME_WINDOW_S: Final[float] = 10.0

# How far a frame's timestamp may be from the server's clock. Two minutes
# covers ordinary drift on an operator's laptop and bounds how long a captured
# frame stays replayable even if the nonce cache has rolled over.
MAX_CLOCK_SKEW_S: Final[float] = 120.0

# Bounded replay memory. A set alone grows without limit on a long-lived
# socket, which is a memory exhaustion primitive handed to the caller; the
# deque evicts the oldest nonce once the cache is full, and MAX_CLOCK_SKEW_S
# is what keeps an evicted nonce from becoming replayable again.
NONCE_CACHE_SIZE: Final[int] = 512

MIN_NONCE_LEN: Final[int] = 8
MAX_NONCE_LEN: Final[int] = 64

# The complete inbound vocabulary. An allowlist, so adding a command is a
# deliberate edit here rather than a side effect of a handler appearing
# somewhere else.
ALLOWED_INBOUND_TYPES: Final[frozenset[str]] = frozenset({"ping", "subscribe"})

# Every frame carries these, whatever its type.
_REQUIRED_FIELDS: Final[tuple[str, ...]] = ("type", "nonce", "ts")


class WSFrameError(Exception):
    """
    A frame that must not be processed, carrying the close code to send.

    The message is for the server log. What goes back to the client is the
    close code and nothing else: a descriptive close reason on a rejected
    frame tells a prober which of the checks it tripped.
    """

    def __init__(self, reason: str, close_code: int = CLOSE_POLICY_VIOLATION) -> None:
        super().__init__(reason)
        self.reason = reason
        self.close_code = close_code


class WSFrameGuard:
    """
    Per-connection validator. One instance per socket, never shared.

    Sharing one across connections would let a client exhaust another's frame
    budget and would make one client's nonces collide with another's -- both
    of which turn a protection into a denial of service.
    """

    def __init__(self, now: Any = time.time, monotonic: Any = time.monotonic) -> None:
        # Injected clocks: the freshness window is wall-clock (it is compared
        # against a caller's timestamp) and the rate window is monotonic (it
        # must not move when the system clock is stepped). Conflating them is
        # the bug where an NTP correction silently empties the rate limiter.
        self._now = now
        self._monotonic = monotonic
        self._nonces: collections.OrderedDict[str, None] = collections.OrderedDict()
        self._frame_ts: collections.deque[float] = collections.deque(maxlen=MAX_FRAMES_PER_WINDOW)

    def check(self, raw: str | bytes) -> dict[str, Any]:
        """
        Validate one inbound frame and return its decoded payload.

        Raises
        ------
        WSFrameError
            With the close code the caller should close the socket with.
        """
        payload = self._decode(raw)
        self._check_schema(payload)
        self._check_freshness(payload["ts"])
        self._check_rate()
        # Replay last among the stateful checks: a frame that failed any
        # earlier check must not consume a nonce slot, or a flood of garbage
        # evicts the real nonces and re-opens the replay window.
        self._check_replay(payload["nonce"])
        return payload

    # -- individual checks ------------------------------------------------

    def _decode(self, raw: str | bytes) -> dict[str, Any]:
        encoded = raw.encode("utf-8", errors="surrogatepass") if isinstance(raw, str) else raw
        if len(encoded) > MAX_FRAME_BYTES:
            raise WSFrameError("frame exceeds the maximum size", CLOSE_MESSAGE_TOO_BIG)
        try:
            payload = json.loads(encoded)
        except (ValueError, UnicodeDecodeError) as exc:
            raise WSFrameError("frame is not valid JSON") from exc
        if not isinstance(payload, dict):
            # A bare list or scalar parses fine and then fails on the first
            # field access several layers deeper, where the error is a 500.
            raise WSFrameError("frame is not a JSON object")
        return payload

    def _check_schema(self, payload: dict[str, Any]) -> None:
        for field in _REQUIRED_FIELDS:
            if field not in payload:
                raise WSFrameError(f"frame is missing the {field!r} field")
        msg_type = payload["type"]
        if not isinstance(msg_type, str) or msg_type not in ALLOWED_INBOUND_TYPES:
            raise WSFrameError("frame type is not in the permitted set")
        nonce = payload["nonce"]
        if not isinstance(nonce, str) or not MIN_NONCE_LEN <= len(nonce) <= MAX_NONCE_LEN:
            raise WSFrameError("frame nonce is missing or the wrong length")
        ts = payload["ts"]
        # bool is an int in Python and would otherwise sail through as 0/1,
        # i.e. a timestamp in 1970 that the freshness check then rejects for
        # the wrong reason.
        if isinstance(ts, bool) or not isinstance(ts, int | float):
            raise WSFrameError("frame timestamp is not a number")

    def _check_freshness(self, ts: float) -> None:
        skew = abs(float(self._now()) - float(ts))
        if skew > MAX_CLOCK_SKEW_S:
            # Both directions: a frame from the future is as much a forgery
            # signal as a stale one, and accepting it would let an attacker
            # pre-date captures past the replay window.
            raise WSFrameError("frame timestamp is outside the permitted window")

    def _check_rate(self) -> None:
        now = float(self._monotonic())
        while self._frame_ts and now - self._frame_ts[0] >= FRAME_WINDOW_S:
            self._frame_ts.popleft()
        if len(self._frame_ts) >= MAX_FRAMES_PER_WINDOW:
            raise WSFrameError("frame rate limit exceeded", CLOSE_RATE_LIMITED)
        self._frame_ts.append(now)

    def _check_replay(self, nonce: str) -> None:
        if nonce in self._nonces:
            raise WSFrameError("frame nonce has already been used")
        self._nonces[nonce] = None
        while len(self._nonces) > NONCE_CACHE_SIZE:
            self._nonces.popitem(last=False)
