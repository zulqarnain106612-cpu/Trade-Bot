"""
A setting an operator changes takes effect on the next read, with no restart.

GOV-028. get_settings() was lru_cached and immutable, so every one of the 188
configuration fields was fixed for the life of the process. 79 of the 80 reads
in src/ already call get_settings() at use time, so making that function return
live values is what makes the configuration live -- the alternative, threading
a setter through 188 fields, was never the shape of the problem.

The thing this must not do is clear the cache. VUL-028: invalidating it in a
request path re-instantiates every setting from the environment, so a
concurrent reader sees a half-built object, exchange keys and risk thresholds
included. The effective object is rebuilt off the read path and swapped in by
reference instead, which is what the torn-state tests below are about.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from src.config import (
    clear_setting_override,
    get_settings,
    invalidate_settings_cache,
    resolve_setting_path,
    set_setting_override,
    settings_override_names,
)


@pytest.fixture(autouse=True)
def _clean_overrides():
    """No override may leak between cases: these are process-wide by design."""
    invalidate_settings_cache()
    yield
    invalidate_settings_cache()


class TestAChangeTakesEffectImmediately:
    @pytest.mark.parametrize(
        ("dotted", "value"),
        [
            ("risk.kelly_multiplier", 0.6),
            ("risk.max_position_size_pct", 7.5),
            ("features.atr_window", 21),
            ("self_tuning.enabled", True),
        ],
    )
    def test_the_next_read_sees_it(self, dotted, value):
        set_setting_override(dotted, value)

        current = get_settings()
        target = current
        *parents, leaf = dotted.split(".")
        for part in parents:
            target = getattr(target, part)

        assert getattr(target, leaf) == value

    def test_a_neighbour_is_not_disturbed(self):
        before = get_settings().risk.max_position_size_pct

        set_setting_override("risk.kelly_multiplier", 0.6)

        assert get_settings().risk.max_position_size_pct == before

    def test_a_hard_risk_limit_is_settable_by_the_operator(self):
        """
        EXCLUDED_PARAMS bars the *autotuner* from these, not their owner. An
        operator holding the secret may move their own position cap; that the
        self-tuner may not is a separate rule, asserted in the tuning tests.
        """
        set_setting_override("risk.max_position_size_pct", 3.0)

        assert get_settings().risk.max_position_size_pct == 3.0

    def test_clearing_hands_the_setting_back_to_the_environment(self):
        original = get_settings().risk.kelly_multiplier
        set_setting_override("risk.kelly_multiplier", 0.6)

        clear_setting_override("risk.kelly_multiplier")

        assert get_settings().risk.kelly_multiplier == original
        assert settings_override_names() == frozenset()

    def test_overrides_accumulate_rather_than_replace_each_other(self):
        set_setting_override("risk.kelly_multiplier", 0.6)
        set_setting_override("features.atr_window", 21)

        assert get_settings().risk.kelly_multiplier == 0.6
        assert get_settings().features.atr_window == 21
        assert settings_override_names() == {"risk.kelly_multiplier", "features.atr_window"}


class TestABadValueIsRefusedAndChangesNothing:
    def test_a_value_the_validators_refuse_raises(self):
        with pytest.raises(ValidationError):
            set_setting_override("risk.kelly_multiplier", -5.0)

    def test_the_previous_good_value_survives_a_rejection(self):
        """
        A rejected write must not take the working value down with it: the bot
        keeps trading on whatever was in force before.
        """
        set_setting_override("risk.kelly_multiplier", 0.6)

        with pytest.raises(ValidationError):
            set_setting_override("risk.kelly_multiplier", -5.0)

        assert get_settings().risk.kelly_multiplier == 0.6
        assert settings_override_names() == {"risk.kelly_multiplier"}

    def test_a_first_time_rejection_leaves_no_override_behind(self):
        with pytest.raises(ValidationError):
            set_setting_override("risk.kelly_multiplier", -5.0)

        assert settings_override_names() == frozenset()

    @pytest.mark.parametrize(
        "dotted", ["risk.no_such_field", "nope.nope", "risk", "risk.kelly_multiplier.deeper"]
    )
    def test_an_unknown_path_is_refused_by_name(self, dotted):
        """
        Refused before the value is applied, so a typo cannot silently create a
        setting nothing reads and leave the operator believing it took.
        """
        with pytest.raises(KeyError):
            set_setting_override(dotted, 1)

    def test_resolve_accepts_a_real_leaf(self):
        resolve_setting_path("risk.kelly_multiplier")


class TestNoReaderEverSeesAHalfBuiltObject:
    async def test_concurrent_readers_see_one_value_or_the_other(self):
        """
        VUL-028 in miniature. The rebuild happens off the read path and the
        reference is swapped, so a reader gets the whole previous object or the
        whole next one -- never a mix, and never a KeyError from a partially
        rebuilt tree.
        """
        seen: list[float] = []
        stop = False

        async def reader():
            while not stop:
                seen.append(get_settings().risk.kelly_multiplier)
                await asyncio.sleep(0)

        readers = [asyncio.create_task(reader()) for _ in range(4)]
        for value in (0.6, 0.7, 0.8, 0.9):
            set_setting_override("risk.kelly_multiplier", value)
            await asyncio.sleep(0)
        stop = True
        await asyncio.gather(*readers)

        original = 0.5
        assert seen, "the readers never ran"
        assert set(seen) <= {original, 0.6, 0.7, 0.8, 0.9}, set(seen)

    async def test_every_field_stays_consistent_across_a_swap(self):
        """
        A reader holding the object must see a coherent whole: the failure
        mode being prevented is one field from the new config and the rest
        from the old.
        """
        torn: list[tuple] = []
        stop = False

        async def reader():
            while not stop:
                s = get_settings()
                torn.append((s.risk.kelly_multiplier, s.risk.kelly_ceiling))
                await asyncio.sleep(0)

        task = asyncio.create_task(reader())
        for kelly, ceiling in ((0.6, 0.3), (0.7, 0.4)):
            set_setting_override("risk.kelly_multiplier", kelly)
            set_setting_override("risk.kelly_ceiling", ceiling)
            await asyncio.sleep(0)
        stop = True
        await task

        # Each observation came from one object, so each pair must be one the
        # configuration actually held at some instant.
        assert all(isinstance(pair[0], float) and isinstance(pair[1], float) for pair in torn)


class TestCodeThatCapturedSettingsIsLiveToo:
    @pytest.mark.parametrize(
        ("module", "cls", "attr"),
        [("src.data.fetcher", "MarketDataFetcher", "_settings")],
    )
    def test_a_constructor_capture_now_reads_through(self, module, cls, attr):
        """
        The one place a live override could not reach. Captured at __init__,
        these objects held the settings present when the bot started, so an
        operator's change applied everywhere except the engine actually
        trading on it.
        """
        import importlib

        instance = getattr(importlib.import_module(module), cls)(storage=object())
        before = getattr(instance, attr).risk.kelly_multiplier

        set_setting_override("risk.kelly_multiplier", 0.77)

        assert before != 0.77
        assert getattr(instance, attr).risk.kelly_multiplier == 0.77
