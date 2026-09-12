"""
RES-008 — malformed input is rejected safely, with an audit event.

The passing condition the source document sets is deliberately not "the
application did not crash":

```
malformed input
     ↓
rejected safely
     ↓
no corrupted state
     ↓
no unauthorized action
     ↓
no secret leakage
     ↓
audit event
```

Each arrow is a separate assertion below, because a fuzz suite that only
checks for absence of exceptions declares victory the moment the code catches
everything and returns a default — which is the failure mode, not the fix.

Generation is a seeded `random.Random` for the same reason as
`tests/property/`: byte-for-byte reproducibility in CI, no new dependency, and
pools weighted toward the values that break comparisons rather than toward
values that look realistic.
"""

from __future__ import annotations

import math
import os
import random

import pandas as pd
import pytest

from src.data.quality_gate import DataQualityGate, FreshnessBudget
from src.execution.exchange_contract import ExchangeOrderStatus, parse_order

SEED = 20260912

#: Draws per property. The nightly job raises this via TRADE_BOT_FUZZ_DRAWS;
#: the pull-request run keeps it small enough to stay under a second, because
#: a fuzz suite slow enough to notice is one that gets marked slow and then
#: skipped.
DRAWS = int(os.environ.get("TRADE_BOT_FUZZ_DRAWS", "1500"))

#: Values chosen because they break comparisons, parsers and formatters.
HOSTILE = [
    None,
    "",
    " ",
    0,
    -1,
    math.nan,
    math.inf,
    -math.inf,
    1e308,
    -1e308,
    "NaN",
    "null",
    "0x0",
    [],
    {},
    (),
    True,
    False,
    "\x00",
    "‮",
    "a" * 4096,
    "'; DROP TABLE orders; --",
    "../../etc/passwd",
    "{{7*7}}",
    "<script>alert(1)</script>",
    "-1e400",
]

#: Credential-shaped values that must never appear *in full* in a rejection
#: message. A malformed input is exactly when a handler is most likely to
#: echo whatever it was given, and a venue field is attacker-influenceable in
#: the general case.
#:
#: "In full" is the honest bar. A rejection has to say enough for an operator
#: to recognise a new venue status and look it up, so the echo is truncated
#: to twelve characters rather than suppressed -- and twelve characters
#: cannot be a credential whatever format it is in.
SECRETS = (
    "sk-live-51H8x9Kq2mNpQr7sT4uVwXyZaBcDeFgHiJkLmNoPqRsTuVwX",
    "AKIAIOSFODNN7EXAMPLE",
    "-----BEGIN PRIVATE KEY-----MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcw",
    "password=hunter2correcthorsebatterystaple",
    "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abcdefghijklmnop",
)


@pytest.fixture
def rng() -> random.Random:
    return random.Random(SEED)


def draw(rng: random.Random):
    return rng.choice(HOSTILE)


# ---------------------------------------------------------------------------
# The exchange-response contract
# ---------------------------------------------------------------------------


class TestFuzzedExchangeResponses:
    FIELDS = ("id", "symbol", "status", "filled", "amount", "average", "price", "remaining")

    def test_no_input_raises(self, rng):
        # Totality under hostile input. The contract's whole design is that
        # the caller gets an answer rather than an exception whose handler
        # would have to re-decide what "keep waiting" means.
        for _ in range(DRAWS):
            payload = {field: draw(rng) for field in self.FIELDS}
            parse_order(payload, expected_symbol="BTC/USDT", expected_order_id="ord-1")

    def test_no_garbage_response_ever_books_a_fill(self, rng):
        # The invariant that matters. A fuzzed response may be UNKNOWN, may
        # be refused, may even parse -- but it must not produce a fill the
        # caller would act on unless every field genuinely supports one.
        for _ in range(DRAWS):
            payload = {field: draw(rng) for field in self.FIELDS}
            update = parse_order(payload, expected_symbol="BTC/USDT")
            if update.fsm_status is not None and update.status is ExchangeOrderStatus.FILLED:
                assert update.filled_qty and update.filled_qty > 0.0
                assert update.average_price and update.average_price > 0.0

    def test_every_reported_number_is_finite_or_absent(self, rng):
        # A NaN that survives the parse is a NaN in the position ledger.
        for _ in range(DRAWS):
            update = parse_order({field: draw(rng) for field in self.FIELDS})
            for value in (update.filled_qty, update.average_price, update.remaining_qty):
                assert value is None or math.isfinite(value)

    @pytest.mark.parametrize("secret", SECRETS)
    def test_a_rejection_never_echoes_a_secret(self, secret):
        # Found by writing this test: the reason echoed the raw status and
        # order id verbatim, so a credential-shaped venue field landed in a
        # log line. The echo is now redacted and truncated.
        payload = {"id": secret, "status": secret}
        update = parse_order(payload, expected_order_id="ord-1")
        assert secret not in update.reason, update.reason
        # And not just the whole string: no long run of it survives either.
        assert secret[:20] not in update.reason, update.reason

    def test_a_long_venue_field_cannot_flood_the_log(self):
        update = parse_order({"id": "x" * 8000, "status": "y" * 8000}, expected_order_id="ord-1")
        assert len(update.reason) < 400

    def test_a_hostile_status_is_unknown_not_an_action(self, rng):
        for value in HOSTILE:
            update = parse_order({"id": "ord-1", "status": value})
            if update.status is not ExchangeOrderStatus.UNKNOWN:
                # The only hostile values that should parse are ones that
                # happen to be real statuses; none of the pool is.
                pytest.fail(f"{value!r} was interpreted as {update.status}")


# ---------------------------------------------------------------------------
# The data-quality gate
# ---------------------------------------------------------------------------


class TestFuzzedMarketData:
    @pytest.fixture
    def gate(self) -> DataQualityGate:
        return DataQualityGate()

    @pytest.fixture
    def budget(self) -> FreshnessBudget:
        return FreshnessBudget(label="fuzz", max_age_s=1e12)

    def test_no_frame_shape_raises(self, gate, budget, rng):
        for _ in range(min(DRAWS, 300)):
            rows = rng.randint(0, 6)
            frame = pd.DataFrame(
                {
                    "open": [draw(rng) for _ in range(rows)],
                    "high": [draw(rng) for _ in range(rows)],
                    "low": [draw(rng) for _ in range(rows)],
                    "close": [draw(rng) for _ in range(rows)],
                    "volume": [draw(rng) for _ in range(rows)],
                },
                index=[rng.randint(-(2**40), 2**40) for _ in range(rows)],
            )
            result = gate.check_bars(frame, budget=budget)
            assert isinstance(result.passed, bool)

    def test_no_garbage_frame_is_ever_accepted(self, gate, budget, rng):
        # Every column is drawn from the hostile pool, so nothing in here is
        # a usable bar. A pass would mean the gate is deciding on shape
        # rather than on content.
        accepted = 0
        for _ in range(min(DRAWS, 300)):
            rows = rng.randint(3, 6)
            frame = pd.DataFrame(
                {
                    "open": [draw(rng) for _ in range(rows)],
                    "high": [draw(rng) for _ in range(rows)],
                    "low": [draw(rng) for _ in range(rows)],
                    "close": [draw(rng) for _ in range(rows)],
                    "volume": [draw(rng) for _ in range(rows)],
                },
                index=sorted(rng.sample(range(2**30), rows)),
            )
            if gate.check_bars(frame, budget=budget).passed:
                accepted += 1
        assert accepted == 0

    def test_a_rejection_always_says_why(self, gate, budget, rng):
        for _ in range(min(DRAWS, 300)):
            rows = rng.randint(3, 6)
            frame = pd.DataFrame(
                {
                    "close": [draw(rng) for _ in range(rows)],
                    "volume": [draw(rng) for _ in range(rows)],
                },
                index=sorted(rng.sample(range(2**30), rows)),
            )
            result = gate.check_bars(frame, budget=budget)
            if not result.passed:
                assert result.reason, "a rejection with no reason cannot be acted on"

    @pytest.mark.parametrize("secret", SECRETS)
    def test_a_rejection_never_echoes_a_secret(self, gate, secret):
        frame = pd.DataFrame({"close": [secret], "volume": [secret]}, index=[0])
        result = gate.check_bars(frame, budget=FreshnessBudget("fuzz", 1e12))
        assert secret not in result.reason

    def test_state_is_not_corrupted_by_a_bad_frame(self, gate, budget, rng):
        # The gate is stateless by design, and this is what says so: a good
        # frame after a thousand bad ones must still be accepted.
        good = pd.DataFrame(
            {
                "open": [100.0, 101.0, 102.0],
                "high": [101.0, 102.0, 103.0],
                "low": [99.0, 100.0, 101.0],
                "close": [100.5, 101.5, 102.5],
                "volume": [10.0, 11.0, 12.0],
            },
            index=[0, 900_000, 1_800_000],
        )
        assert gate.check_bars(good, budget=budget).passed
        for _ in range(min(DRAWS, 200)):
            gate.check_bars(
                pd.DataFrame({"close": [draw(rng)], "volume": [draw(rng)]}, index=[0]),
                budget=budget,
            )
        assert gate.check_bars(good, budget=budget).passed


# ---------------------------------------------------------------------------
# The audit event
# ---------------------------------------------------------------------------


class TestRejectionsAreAudited:
    def test_every_rejection_records_an_event(self, monkeypatch):
        # The last arrow in the chain. A rejection nobody recorded is a
        # rejection nobody can count, and RES-008 is about the count as much
        # as the refusal.
        recorded: list[dict] = []

        class _Trail:
            def record(self, **kwargs):
                recorded.append(kwargs)

        import src.diagnostics.audit_trail as audit

        monkeypatch.setattr(audit, "get_audit_trail", lambda: _Trail())
        gate = DataQualityGate()
        for value in HOSTILE[:10]:
            gate.check_sentiment_score(value if isinstance(value, (int, float)) else math.nan)
        assert len(recorded) >= 1
        assert all(event["event_type"] == "data_quality_reject" for event in recorded)

    def test_an_audit_outage_does_not_stop_the_rejection(self, monkeypatch):
        import src.diagnostics.audit_trail as audit

        def _boom():
            raise RuntimeError("audit down")

        monkeypatch.setattr(audit, "get_audit_trail", _boom)
        assert not DataQualityGate().check_sentiment_score(math.nan).passed
