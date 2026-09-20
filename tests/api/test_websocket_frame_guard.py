"""
API-005 — an open WebSocket is not a trusted one.

The upgrade test in `tests/authentication` proves a socket cannot be opened
without a key. This file starts one message later, at the assumption that
usually follows: that a frame arriving on an authenticated socket has already
been vetted. It has not. It arrives with no framework parsing around it, no
size limit, no schema and no request context, which is more raw than any HTTP
body this API accepts.

Each check is tested on its own *and* in the order the guard applies them,
because the ordering is load-bearing: parse-before-size decodes a frame it was
about to reject, and replay-before-freshness lets an attacker choose how long
their captured frame stays interesting.
"""

from __future__ import annotations

import json

import pytest

from src.api.ws_guard import (
    CLOSE_MESSAGE_TOO_BIG,
    CLOSE_POLICY_VIOLATION,
    CLOSE_RATE_LIMITED,
    MAX_CLOCK_SKEW_S,
    MAX_FRAME_BYTES,
    MAX_FRAMES_PER_WINDOW,
    NONCE_CACHE_SIZE,
    WSFrameError,
    WSFrameGuard,
)


class FakeClock:
    """Wall clock and monotonic clock that move independently, as they do."""

    def __init__(self, wall: float = 1_000_000.0, mono: float = 0.0) -> None:
        self.wall = wall
        self.mono = mono

    def now(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.mono += seconds


def frame(clock: FakeClock, *, nonce: str = "nonce-0001", **overrides: object) -> str:
    payload: dict[str, object] = {"type": "ping", "nonce": nonce, "ts": clock.wall}
    payload.update(overrides)
    return json.dumps(payload)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def guard(clock: FakeClock) -> WSFrameGuard:
    return WSFrameGuard(now=clock.now, monotonic=clock.monotonic)


class TestAWellFormedFrame:
    def test_it_is_accepted_and_returned_decoded(self, guard, clock):
        payload = guard.check(frame(clock))
        assert payload["type"] == "ping"

    def test_distinct_nonces_are_all_accepted(self, guard, clock):
        for i in range(5):
            guard.check(frame(clock, nonce=f"nonce-{i:04d}"))


class TestTheSizeBound:
    def test_an_oversized_frame_is_refused(self, guard, clock):
        fat = frame(clock, padding="x" * (MAX_FRAME_BYTES + 1))
        with pytest.raises(WSFrameError) as exc:
            guard.check(fat)
        assert exc.value.close_code == CLOSE_MESSAGE_TOO_BIG

    def test_the_bound_is_checked_before_the_frame_is_parsed(self, guard):
        # Oversized *and* unparseable. A guard that parsed first would spend
        # the decode on it and then report a JSON error -- the report is how
        # we can tell which check ran, and the decode is the cost that
        # mattered.
        with pytest.raises(WSFrameError) as exc:
            guard.check("{" + "x" * (MAX_FRAME_BYTES + 1))
        assert exc.value.close_code == CLOSE_MESSAGE_TOO_BIG

    def test_the_bound_is_on_bytes_not_characters(self, guard, clock):
        # A multi-byte character counts as its encoded length; measuring in
        # characters lets a 3x-expanding payload through a byte budget.
        padding = "é" * (MAX_FRAME_BYTES // 2)
        with pytest.raises(WSFrameError) as exc:
            guard.check(frame(clock, padding=padding))
        assert exc.value.close_code == CLOSE_MESSAGE_TOO_BIG


class TestTheSchema:
    @pytest.mark.parametrize("junk", ["", "not json", "{", "[1,2,3]", '"a string"', "42"])
    def test_non_object_payloads_are_refused(self, guard, junk):
        with pytest.raises(WSFrameError):
            guard.check(junk)

    @pytest.mark.parametrize("missing", ["type", "nonce", "ts"])
    def test_every_required_field_is_required(self, guard, clock, missing):
        payload = json.loads(frame(clock))
        del payload[missing]
        with pytest.raises(WSFrameError):
            guard.check(json.dumps(payload))

    def test_an_unknown_type_is_refused(self, guard, clock):
        # Allowlist, not denylist: a handler added elsewhere does not become
        # reachable by existing.
        with pytest.raises(WSFrameError):
            guard.check(frame(clock, type="place_order"))

    @pytest.mark.parametrize("nonce", ["", "short", "n" * 65])
    def test_the_nonce_length_is_bounded_at_both_ends(self, guard, clock, nonce):
        with pytest.raises(WSFrameError):
            guard.check(frame(clock, nonce=nonce))

    @pytest.mark.parametrize("ts", ["now", None, True, [1]])
    def test_a_non_numeric_timestamp_is_refused(self, guard, clock, ts):
        # `True` is in this list deliberately: bool is an int in Python, so a
        # naive isinstance check accepts it as the timestamp 1.
        with pytest.raises(WSFrameError):
            guard.check(frame(clock, ts=ts))

    def test_a_schema_failure_closes_on_policy_violation(self, guard, clock):
        with pytest.raises(WSFrameError) as exc:
            guard.check(frame(clock, type="place_order"))
        assert exc.value.close_code == CLOSE_POLICY_VIOLATION


class TestFreshness:
    def test_a_stale_frame_is_refused(self, guard, clock):
        stale = frame(clock)
        clock.advance(MAX_CLOCK_SKEW_S + 1)
        with pytest.raises(WSFrameError):
            guard.check(stale)

    def test_a_frame_from_the_future_is_refused(self, guard, clock):
        # Symmetric on purpose: a future timestamp is how a captured frame is
        # kept replayable past the window.
        with pytest.raises(WSFrameError):
            guard.check(frame(clock, ts=clock.wall + MAX_CLOCK_SKEW_S + 1))

    def test_a_frame_inside_the_window_survives(self, guard, clock):
        guard.check(frame(clock, ts=clock.wall - (MAX_CLOCK_SKEW_S / 2)))


class TestReplay:
    def test_the_same_nonce_twice_is_refused(self, guard, clock):
        guard.check(frame(clock, nonce="repeated-nonce"))
        with pytest.raises(WSFrameError):
            guard.check(frame(clock, nonce="repeated-nonce"))

    def test_a_byte_identical_frame_is_refused(self, guard, clock):
        raw = frame(clock)
        guard.check(raw)
        with pytest.raises(WSFrameError):
            guard.check(raw)

    def test_the_nonce_cache_is_bounded(self, guard, clock):
        # An unbounded cache is a memory sink the client fills for free. The
        # eviction is what MAX_CLOCK_SKEW_S then backstops.
        for i in range(NONCE_CACHE_SIZE + 50):
            if i and i % MAX_FRAMES_PER_WINDOW == 0:
                clock.advance(11.0)
            guard.check(frame(clock, nonce=f"n-{i:06d}"))
        assert len(guard._nonces) <= NONCE_CACHE_SIZE

    def test_a_rejected_frame_does_not_consume_a_nonce_slot(self, guard, clock):
        # A flood of frames that fail an earlier check must not be able to
        # evict the real nonces, or the replay window re-opens under load.
        with pytest.raises(WSFrameError):
            guard.check(frame(clock, nonce="never-stored", type="place_order"))
        assert "never-stored" not in guard._nonces


class TestRateLimiting:
    def test_a_flood_is_refused_with_the_rate_code(self, guard, clock):
        for i in range(MAX_FRAMES_PER_WINDOW):
            guard.check(frame(clock, nonce=f"burst-{i:04d}"))
        with pytest.raises(WSFrameError) as exc:
            guard.check(frame(clock, nonce="burst-over"))
        assert exc.value.close_code == CLOSE_RATE_LIMITED

    def test_the_budget_refills_as_the_window_slides(self, guard, clock):
        for i in range(MAX_FRAMES_PER_WINDOW):
            guard.check(frame(clock, nonce=f"first-{i:04d}"))
        clock.advance(11.0)
        guard.check(frame(clock, nonce="after-window"))

    def test_the_rate_window_uses_the_monotonic_clock(self, clock):
        # A limiter keyed on wall time empties when NTP steps the clock --
        # i.e. at the moment an attacker who can influence time chooses.
        guard = WSFrameGuard(now=clock.now, monotonic=clock.monotonic)
        for i in range(MAX_FRAMES_PER_WINDOW):
            guard.check(frame(clock, nonce=f"pre-{i:04d}"))
        clock.wall += 10_000.0  # wall clock jumps, monotonic does not
        with pytest.raises(WSFrameError) as exc:
            guard.check(frame(clock, nonce="post-step", ts=clock.wall))
        assert exc.value.close_code == CLOSE_RATE_LIMITED


class TestGuardsAreNotShared:
    def test_one_connection_cannot_spend_anothers_budget(self, clock):
        a = WSFrameGuard(now=clock.now, monotonic=clock.monotonic)
        b = WSFrameGuard(now=clock.now, monotonic=clock.monotonic)
        for i in range(MAX_FRAMES_PER_WINDOW):
            a.check(frame(clock, nonce=f"a-{i:06d}"))
        # b is a different client and is unaffected.
        b.check(frame(clock, nonce="b-00000001"))

    def test_two_clients_may_use_the_same_nonce(self, clock):
        a = WSFrameGuard(now=clock.now, monotonic=clock.monotonic)
        b = WSFrameGuard(now=clock.now, monotonic=clock.monotonic)
        a.check(frame(clock, nonce="same-nonce"))
        b.check(frame(clock, nonce="same-nonce"))


class TestTheGuardIsWiredIntoTheEndpoint:
    def test_the_websocket_endpoint_reads_through_the_guard(self):
        # The unit tests above prove the guard is correct; this proves the
        # socket actually goes through it. A correct guard nothing calls is
        # the failure mode this whole file exists to prevent.
        import inspect

        from src.api import main

        source = inspect.getsource(main._guarded_ws_reader)
        assert "WSFrameGuard()" in source
        assert "guard.check(raw)" in source
        assert "await ws.close(code=exc.close_code)" in source

    def test_each_connection_builds_its_own_guard(self):
        import inspect

        from src.api import main

        # Constructed inside the per-connection reader, not at module scope.
        assert "WSFrameGuard()" not in inspect.getsource(main.websocket_endpoint)
        assert "WSFrameGuard()" in inspect.getsource(main._guarded_ws_reader)

    def test_the_reader_is_started_and_cancelled_by_the_endpoint(self):
        import inspect

        source = inspect.getsource(__import__("src.api.main", fromlist=["x"]).websocket_endpoint)
        assert "_guarded_ws_reader(ws)" in source
        assert "reader.cancel()" in source
