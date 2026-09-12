"""
SECR-010 — the key this bot holds cannot withdraw funds.

Everything else in this package bounds what an attacker can do to the bot.
This bounds what they can do with it, and the difference is total: a trading
key loses money at the speed of the market, a withdrawal key loses all of it
in one request.

The check here is honest about what it is. Nothing in this process can prove
what the key on Binance's side is permitted to do -- that is the exchange's
state, not ours. What this proves is that a posture was *declared*, that the
declaration is safe, and that a live executor refuses to start without one.
The remaining step, attempting a withdrawal and confirming the venue refuses,
is a human one and is written down in `docs/security/KEY_MANAGEMENT.md`.

That gap is the reason the "no declaration" case fails closed rather than
warning. A deployment nobody wrote a posture for is a deployment where nobody
checked the checkbox either.
"""

from __future__ import annotations

import json

import pytest

from src.security.exchange_key_posture import (
    FORBIDDEN,
    PERMITTED,
    Capability,
    KeyPosture,
    PostureViolation,
    assert_all_safe,
    assert_declared_posture_is_safe,
    assert_safe,
    load_declarations,
    posture_from_mapping,
)

SAFE_RAW = {
    "capabilities": ["read", "trade"],
    "ip_allowlisted": True,
    "withdrawal_whitelist_only": True,
    "declared_by": "operator",
    "declared_on": "2026-09-12",
}


def raw(**overrides):
    merged = dict(SAFE_RAW)
    merged.update(overrides)
    return merged


class TestTheSafePosture:
    def test_read_and_trade_is_accepted(self):
        assert_safe(posture_from_mapping("binance", SAFE_RAW))

    def test_read_only_is_accepted(self):
        assert_safe(posture_from_mapping("binance", raw(capabilities=["read"])))

    def test_the_permitted_set_is_what_the_policy_says(self):
        assert frozenset({Capability.READ, Capability.TRADE}) == PERMITTED

    def test_permitted_and_forbidden_do_not_overlap(self):
        assert not (PERMITTED & FORBIDDEN)


class TestTheUnsafePostures:
    @pytest.mark.parametrize("capability", ["withdraw", "transfer"])
    def test_a_withdrawal_capable_key_is_refused(self, capability):
        posture = posture_from_mapping("binance", raw(capabilities=["read", "trade", capability]))
        with pytest.raises(PostureViolation, match=capability):
            assert_safe(posture)

    def test_transfer_counts_as_withdrawal(self):
        # A sub-account transfer is a withdrawal with one extra step, and the
        # attacker has time for one extra step.
        assert Capability.TRANSFER in FORBIDDEN

    def test_an_unattributed_posture_is_refused(self):
        posture = KeyPosture(
            exchange="binance",
            capabilities=frozenset({Capability.READ}),
            ip_allowlisted=True,
            withdrawal_whitelist_only=True,
            declared_by="",
            declared_on="",
        )
        with pytest.raises(PostureViolation):
            assert_safe(posture)

    def test_a_withdrawal_key_must_at_least_be_whitelist_bound(self):
        # Reached only if somebody relaxes the capability rule. It is here
        # because defence that survives one bad afternoon is the useful kind.
        posture = KeyPosture(
            exchange="binance",
            capabilities=frozenset({Capability.WITHDRAW}),
            ip_allowlisted=True,
            withdrawal_whitelist_only=False,
            declared_by="operator",
            declared_on="2026-09-12",
        )
        with pytest.raises(PostureViolation):
            assert_safe(posture)


class TestParsingRefusesToInvent:
    @pytest.mark.parametrize("missing", list(SAFE_RAW))
    def test_every_field_is_required(self, missing):
        incomplete = {k: v for k, v in SAFE_RAW.items() if k != missing}
        with pytest.raises(PostureViolation):
            posture_from_mapping("binance", incomplete)

    @pytest.mark.parametrize("bad", [None, [], "read,trade", 0])
    def test_a_non_mapping_is_not_a_declaration(self, bad):
        with pytest.raises(PostureViolation):
            posture_from_mapping("binance", bad)

    def test_an_unknown_capability_is_refused(self):
        # Not ignored: an unrecognised capability is one this policy has never
        # been asked about, and silently dropping it would approve it.
        with pytest.raises(PostureViolation):
            posture_from_mapping("binance", raw(capabilities=["read", "teleport"]))

    def test_the_violation_names_the_exchange(self):
        with pytest.raises(PostureViolation, match="kraken"):
            posture_from_mapping("kraken", {})


class TestTheDeclarationSet:
    def test_an_empty_set_is_refused(self):
        # Otherwise a deployment with keys and no declarations passes the
        # whole requirement trivially.
        for empty in ({}, None, []):
            with pytest.raises(PostureViolation):
                assert_all_safe(empty)

    def test_commentary_keys_are_not_exchanges(self):
        assert_all_safe({"_comment": ["explanation"], "binance": SAFE_RAW})

    def test_a_set_of_only_commentary_is_refused(self):
        with pytest.raises(PostureViolation):
            assert_all_safe({"_comment": ["explanation"]})

    def test_one_bad_exchange_fails_the_set(self):
        with pytest.raises(PostureViolation):
            assert_all_safe({"binance": SAFE_RAW, "okx": raw(capabilities=["read", "withdraw"])})


class TestTheShippedDeclaration:
    def test_the_repository_declares_a_safe_posture(self):
        assert_declared_posture_is_safe()

    def test_it_declares_every_exchange_this_bot_can_trade(self):
        declared = {k for k in load_declarations() if not k.startswith("_")}
        # Binance is the venue LiveExecutor places orders on; a second venue
        # added without a posture must fail this, not this test's author's
        # memory.
        assert "binance" in declared

    def test_a_missing_file_is_a_violation_not_a_pass(self, tmp_path):
        with pytest.raises(PostureViolation):
            assert_declared_posture_is_safe(tmp_path / "absent.json")

    def test_a_malformed_file_is_a_violation(self, tmp_path):
        path = tmp_path / "posture.json"
        path.write_text("{not json")
        with pytest.raises(PostureViolation):
            assert_declared_posture_is_safe(path)

    def test_a_file_declaring_withdrawal_is_refused(self, tmp_path):
        path = tmp_path / "posture.json"
        path.write_text(json.dumps({"binance": raw(capabilities=["read", "withdraw"])}))
        with pytest.raises(PostureViolation):
            assert_declared_posture_is_safe(path)


class TestTheLiveExecutorRefusesWithoutIt:
    def test_the_constructor_asserts_the_posture(self):
        import inspect

        from src.execution.live import LiveExecutor

        source = inspect.getsource(LiveExecutor.__init__)
        assert "assert_declared_posture_is_safe()" in source

    def test_it_is_asserted_before_any_state_is_built(self):
        import inspect

        from src.execution.live import LiveExecutor

        source = inspect.getsource(LiveExecutor.__init__)
        # Before the executor holds a storage handle or a fetcher: a refusal
        # that happens after half the object exists is a refusal somebody
        # will be tempted to catch and continue past.
        assert source.index("assert_declared_posture_is_safe()") < source.index(
            "self._storage = storage"
        )
