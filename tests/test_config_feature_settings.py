"""Tests for FeatureSettings.validate_purge_gap_covers_label_horizon (UI-005)."""

import pytest

from src.config import FeatureSettings


def test_default_purge_gap_covers_default_label_horizon() -> None:
    cfg = FeatureSettings()
    assert cfg.purge_gap_bars >= cfg.triple_barrier_max_holding_bars


def test_purge_gap_equal_to_horizon_is_valid() -> None:
    cfg = FeatureSettings(purge_gap_bars=30, triple_barrier_max_holding_bars=30)
    assert cfg.purge_gap_bars == 30


def test_purge_gap_greater_than_horizon_is_valid() -> None:
    cfg = FeatureSettings(purge_gap_bars=100, triple_barrier_max_holding_bars=30)
    assert cfg.purge_gap_bars == 100


def test_purge_gap_smaller_than_horizon_raises() -> None:
    """AFML Ch.7: a purge gap shorter than the label horizon lets a training
    sample's triple-barrier label overlap with the test fold -- this must
    fail fast at config-load time, not silently leak during training."""
    with pytest.raises(ValueError, match="purge_gap_bars"):
        FeatureSettings(purge_gap_bars=5, triple_barrier_max_holding_bars=60)
