"""`Settings.strategy` must be a single, unambiguous field.

It had been declared twice in the class body -- once as
RegimeStrategySettings, once as StrategySettings -- so the second silently
shadowed the first and one whole settings class was dead. Both carried the
same rs_* fields with the same defaults under the same STRATEGY_ env prefix,
so nothing crashed; it just meant two declarations where a reader could not
tell which one was live.
"""

from __future__ import annotations

import ast
import pathlib
from collections import Counter

from src.config import Settings, StrategySettings, get_settings


_CONFIG_SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "config.py"


def test_no_settings_field_is_declared_twice():
    """A duplicate annotation is legal Python and silently keeps the last one."""
    tree = ast.parse(_CONFIG_SRC.read_text())
    settings_cls = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Settings"
    )
    names = Counter(
        node.target.id
        for node in settings_cls.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    )
    assert [name for name, n in names.items() if n > 1] == []


def test_strategy_carries_the_selector_thresholds():
    strategy = get_settings().strategy
    assert isinstance(strategy, StrategySettings)
    for knob in ("rs_min_confidence", "rs_max_entropy", "rs_transition_guard"):
        assert hasattr(strategy, knob)


def test_strategy_also_carries_the_mean_reversion_and_breakout_knobs():
    """These were only ever reachable through the field that happened to win."""
    strategy = Settings().strategy
    for knob in ("mr_lookback", "mr_entry_z", "bo_entry_period", "bo_atr_period"):
        assert hasattr(strategy, knob)


def test_the_config_aware_selector_reads_those_thresholds():
    from src.strategies.regime_strategy_selector import select_strategy_from_config

    selection = select_strategy_from_config(regime_state=0, confidence=0.9, entropy=0.1)
    assert selection is not None
