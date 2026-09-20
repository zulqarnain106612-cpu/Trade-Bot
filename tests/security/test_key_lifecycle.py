"""
SECR-003 — environment separation and rotation.

The bug this file is about does not look like a bug. Everything works: the
key is valid, the exchange accepts it, orders fill. The only thing wrong is
*which account* they filled in, and that is invisible from inside the process
that placed them.

Separation therefore cannot be tested by checking that somebody configured
two different values -- the whole failure is that they configured one value
twice. It is tested by handing the *same* master seed to every environment
and asserting the derived material still differs, which is a property of the
derivation rather than of anyone's discipline.

Rotation is tested for the thing that decides whether a rotation contains
anything: whether the superseded key keeps working. Scheduled and rebuild
rotations overlap so the bot stays up; compromise and departure do not,
because in both cases somebody else knows the old key.
"""

from __future__ import annotations

import pytest

from src.security.key_lifecycle import (
    LIVE_ENVIRONMENTS,
    Environment,
    KeyIdentity,
    KeySeparationError,
    RotationReason,
    assert_environments_are_separated,
    assert_usable_in,
    derive_key,
    previous_generation_still_valid,
    rotate,
)

SEED = bytes(range(64))
OTHER_SEED = bytes(range(1, 65))


class TestEnvironmentSeparation:
    def test_one_seed_still_yields_four_distinct_keys(self):
        # The heart of it: even a deployment that copies the production seed
        # into a developer's .env cannot produce the production key.
        keys = {env: derive_key(SEED, KeyIdentity(env, "binance", 0)) for env in Environment}
        assert len(set(keys.values())) == len(Environment)

    def test_the_runtime_assertion_agrees(self):
        assert_environments_are_separated(SEED, "binance")

    @pytest.mark.parametrize("environment", list(Environment))
    def test_a_different_seed_gives_a_different_key(self, environment):
        identity = KeyIdentity(environment, "binance", 0)
        assert derive_key(SEED, identity) != derive_key(OTHER_SEED, identity)

    def test_exchanges_are_separated_too(self):
        # A key that works on two venues doubles the blast radius of one leak.
        a = derive_key(SEED, KeyIdentity(Environment.PRODUCTION, "binance", 0))
        b = derive_key(SEED, KeyIdentity(Environment.PRODUCTION, "kraken", 0))
        assert a != b

    def test_the_derivation_is_deterministic(self):
        # Rotation depends on it: a key that cannot be re-derived from the
        # seed is a key that has to be stored somewhere else as well.
        identity = KeyIdentity(Environment.PAPER, "binance", 3)
        assert derive_key(SEED, identity) == derive_key(SEED, identity)

    def test_the_exchange_name_is_case_insensitive(self):
        a = derive_key(SEED, KeyIdentity(Environment.PAPER, "Binance", 0))
        b = derive_key(SEED, KeyIdentity(Environment.PAPER, "binance", 0))
        assert a == b


class TestUseSiteEnforcement:
    def test_a_key_is_usable_in_its_own_environment(self):
        identity = KeyIdentity(Environment.PAPER, "binance", 0)
        assert_usable_in(identity, Environment.PAPER)

    @pytest.mark.parametrize("wrong", list(Environment))
    def test_a_key_is_refused_anywhere_else(self, wrong):
        identity = KeyIdentity(Environment.PRODUCTION, "binance", 0)
        if wrong is Environment.PRODUCTION:
            return
        with pytest.raises(KeySeparationError):
            assert_usable_in(identity, wrong)

    def test_the_production_key_is_refused_in_paper(self):
        # Named separately from the parametrised case because it is *the*
        # incident: a paper run signing with the live key trades real money
        # while every dashboard says it is simulating.
        with pytest.raises(KeySeparationError):
            assert_usable_in(KeyIdentity(Environment.PRODUCTION, "binance", 0), Environment.PAPER)

    def test_only_production_is_a_live_environment(self):
        assert frozenset({Environment.PRODUCTION}) == LIVE_ENVIRONMENTS

    def test_the_error_names_the_key_without_its_material(self):
        identity = KeyIdentity(Environment.PRODUCTION, "binance", 2)
        with pytest.raises(KeySeparationError) as exc:
            assert_usable_in(identity, Environment.TEST)
        assert identity.label() in str(exc.value)
        assert derive_key(SEED, identity) not in str(exc.value)


class TestRotation:
    def test_rotating_advances_the_generation(self):
        identity = KeyIdentity(Environment.PRODUCTION, "binance", 0)
        assert rotate(identity, RotationReason.SCHEDULED).generation == 1

    def test_a_rotated_key_is_new_material(self):
        identity = KeyIdentity(Environment.PRODUCTION, "binance", 0)
        rotated = rotate(identity, RotationReason.SCHEDULED)
        assert derive_key(SEED, identity) != derive_key(SEED, rotated)

    def test_rotation_does_not_move_the_key_between_environments(self):
        identity = KeyIdentity(Environment.PAPER, "binance", 0)
        assert rotate(identity, RotationReason.SCHEDULED).environment is Environment.PAPER

    @pytest.mark.parametrize("reason", [RotationReason.COMPROMISE, RotationReason.DEPARTURE])
    def test_the_old_key_dies_immediately_when_somebody_else_knows_it(self, reason):
        assert previous_generation_still_valid(reason) is False

    @pytest.mark.parametrize("reason", [RotationReason.SCHEDULED, RotationReason.SERVER_REBUILD])
    def test_the_old_key_overlaps_when_nobody_else_does(self, reason):
        # No overlap here would mean every routine rotation is an outage,
        # which is how routine rotations stop happening.
        assert previous_generation_still_valid(reason) is True

    def test_every_reason_is_decided(self):
        # A new reason added to the enum must be classified deliberately;
        # this fails rather than letting it default to "keep the old key".
        for reason in RotationReason:
            assert isinstance(previous_generation_still_valid(reason), bool)

    def test_a_string_reason_is_refused(self):
        # "compromise" as a string would take the overlap branch by default,
        # i.e. the compromised key keeps working.
        with pytest.raises(TypeError):
            rotate(KeyIdentity(Environment.PRODUCTION, "binance", 0), "compromise")


class TestBadInput:
    def test_a_short_seed_is_refused(self):
        with pytest.raises(ValueError):
            derive_key(b"tooshort", KeyIdentity(Environment.TEST, "binance", 0))

    def test_a_negative_generation_is_refused(self):
        with pytest.raises(ValueError):
            derive_key(SEED, KeyIdentity(Environment.TEST, "binance", -1))

    def test_an_unnamed_exchange_is_refused(self):
        with pytest.raises(ValueError):
            derive_key(SEED, KeyIdentity(Environment.TEST, "", 0))


class TestTheLabelIsSafeToLog:
    def test_it_identifies_the_key(self):
        identity = KeyIdentity(Environment.PRODUCTION, "binance", 7)
        assert identity.label() == "production:binance:g7"

    def test_it_contains_no_key_material(self):
        identity = KeyIdentity(Environment.PRODUCTION, "binance", 7)
        assert derive_key(SEED, identity) not in identity.label()
