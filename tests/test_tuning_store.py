from pathlib import Path

import pytest

from src.tuning.store import (
    NoPriorVersionError,
    NoVersionsError,
    VersionedConfigStore,
)


def test_promote_creates_first_version(tmp_path: Path) -> None:
    store = VersionedConfigStore(tmp_path / "versions.jsonl")
    record = store.promote("hmm.entropy_threshold", 0.55, {"oos_sharpe_delta": 0.1})
    assert record.version == 1
    assert record.value == 0.55
    assert store.current("hmm.entropy_threshold").value == 0.55


def test_promote_appends_new_version(tmp_path: Path) -> None:
    store = VersionedConfigStore(tmp_path / "versions.jsonl")
    store.promote("hmm.entropy_threshold", 0.55, {})
    second = store.promote("hmm.entropy_threshold", 0.60, {"oos_sharpe_delta": 0.2})
    assert second.version == 2
    assert store.current("hmm.entropy_threshold").value == 0.60
    assert len(store.history("hmm.entropy_threshold")) == 2


def test_current_raises_when_no_versions(tmp_path: Path) -> None:
    store = VersionedConfigStore(tmp_path / "versions.jsonl")
    with pytest.raises(NoVersionsError):
        store.current("hmm.entropy_threshold")


def test_has_versions(tmp_path: Path) -> None:
    store = VersionedConfigStore(tmp_path / "versions.jsonl")
    assert not store.has_versions("hmm.entropy_threshold")
    store.promote("hmm.entropy_threshold", 0.5, {})
    assert store.has_versions("hmm.entropy_threshold")


def test_rollback_reverts_to_prior_value(tmp_path: Path) -> None:
    store = VersionedConfigStore(tmp_path / "versions.jsonl")
    store.promote("hmm.entropy_threshold", 0.50, {})
    store.promote("hmm.entropy_threshold", 0.65, {})
    reverted = store.rollback("hmm.entropy_threshold")
    assert reverted.value == 0.50
    assert reverted.is_rollback is True
    assert reverted.version == 3
    assert store.current("hmm.entropy_threshold").value == 0.50


def test_rollback_requires_at_least_two_versions(tmp_path: Path) -> None:
    store = VersionedConfigStore(tmp_path / "versions.jsonl")
    with pytest.raises(NoPriorVersionError):
        store.rollback("hmm.entropy_threshold")
    store.promote("hmm.entropy_threshold", 0.5, {})
    with pytest.raises(NoPriorVersionError):
        store.rollback("hmm.entropy_threshold")


def test_history_survives_reload_from_disk(tmp_path: Path) -> None:
    path = tmp_path / "versions.jsonl"
    store1 = VersionedConfigStore(path)
    store1.promote("hmm.entropy_threshold", 0.5, {"note": "first"})
    store1.promote("hmm.entropy_threshold", 0.6, {"note": "second"})

    store2 = VersionedConfigStore(path)
    history = store2.history("hmm.entropy_threshold")
    assert [v.value for v in history] == [0.5, 0.6]
    assert store2.current("hmm.entropy_threshold").value == 0.6


def test_multiple_parameters_are_independent(tmp_path: Path) -> None:
    store = VersionedConfigStore(tmp_path / "versions.jsonl")
    store.promote("hmm.entropy_threshold", 0.5, {})
    store.promote("features.vwap_window", 20, {})
    assert store.current("hmm.entropy_threshold").value == 0.5
    assert store.current("features.vwap_window").value == 20


def test_load_ignores_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "versions.jsonl"
    path.write_text("\n\n")
    store = VersionedConfigStore(path)
    assert not store.has_versions("hmm.entropy_threshold")
