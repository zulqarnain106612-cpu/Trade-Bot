"""Tests for src/models/model_registry.py"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src.models.model_registry import ModelRegistry, ModelVersion, _cache_key


SYM = "BTC/USDT"
TF = "15m"
MT = "direction"


def _make_registry(tmp_path: Path) -> ModelRegistry:
    return ModelRegistry(model_dir=tmp_path)


def _register_one(
    reg: ModelRegistry,
    version: str = "v1",
    live_gate: bool = True,
    oos_sharpe: float = 1.0,
) -> ModelVersion:
    return reg.register(
        symbol=SYM,
        timeframe=TF,
        model_type=MT,
        version=version,
        file_path=Path(f"models/xgb_{version}.joblib"),
        metrics={"live_gate_pass": live_gate, "oos_sharpe": oos_sharpe},
    )


# ---------------------------------------------------------------------------
# ModelVersion
# ---------------------------------------------------------------------------


def test_model_version_live_gate():
    mv = ModelVersion(
        symbol=SYM,
        timeframe=TF,
        model_type=MT,
        version="v1",
        file_path="models/x.joblib",
        registered_at=time.time(),
        metrics={"live_gate_pass": True},
    )
    assert mv.live_gate_pass is True


def test_model_version_oos_sharpe():
    mv = ModelVersion(
        symbol=SYM,
        timeframe=TF,
        model_type=MT,
        version="v1",
        file_path="models/x.joblib",
        registered_at=time.time(),
        metrics={"oos_sharpe": 1.23},
    )
    assert mv.oos_sharpe == pytest.approx(1.23)


def test_model_version_to_dict_has_iso():
    mv = ModelVersion(
        symbol=SYM,
        timeframe=TF,
        model_type=MT,
        version="v1",
        file_path="models/x.joblib",
        registered_at=time.time(),
    )
    d = mv.to_dict()
    assert "registered_at_iso" in d


def test_model_version_roundtrip():
    mv = ModelVersion(
        symbol=SYM,
        timeframe=TF,
        model_type=MT,
        version="v1",
        file_path="models/x.joblib",
        registered_at=time.time(),
        metrics={"oos_sharpe": 0.9, "live_gate_pass": False},
        notes="test",
    )
    mv2 = ModelVersion.from_dict(mv.to_dict())
    assert mv2.symbol == mv.symbol
    assert mv2.version == mv.version
    assert mv2.metrics == mv.metrics


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


def test_register_returns_model_version(tmp_path):
    reg = _make_registry(tmp_path)
    mv = _register_one(reg)
    assert isinstance(mv, ModelVersion)
    assert mv.symbol == SYM
    assert mv.version == "v1"


def test_register_persists_to_file(tmp_path):
    reg = _make_registry(tmp_path)
    _register_one(reg, version="v1")
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert len(data["versions"]) == 1


def test_register_multiple_versions(tmp_path):
    reg = _make_registry(tmp_path)
    _register_one(reg, version="v1")
    _register_one(reg, version="v2")
    versions = reg.list_versions(SYM, TF, MT)
    assert len(versions) == 2


def test_register_prunes_old_non_pinned(tmp_path):
    from src.models.model_registry import _MAX_VERSIONS_PER_KEY

    reg = _make_registry(tmp_path)
    for i in range(_MAX_VERSIONS_PER_KEY + 5):
        _register_one(reg, version=f"v{i:03d}")
    versions = reg.list_versions(SYM, TF, MT)
    assert len(versions) <= _MAX_VERSIONS_PER_KEY


def test_register_does_not_prune_pinned(tmp_path):
    from src.models.model_registry import _MAX_VERSIONS_PER_KEY

    reg = _make_registry(tmp_path)
    _register_one(reg, version="v_pinned")
    reg.pin(SYM, TF, MT, "v_pinned")
    for i in range(_MAX_VERSIONS_PER_KEY + 5):
        _register_one(reg, version=f"v{i:03d}")
    versions = reg.list_versions(SYM, TF, MT)
    pinned_versions = [v for v in versions if v.is_pinned]
    assert len(pinned_versions) == 1
    assert pinned_versions[0].version == "v_pinned"


# ---------------------------------------------------------------------------
# active_version
# ---------------------------------------------------------------------------


def test_active_version_none_if_empty(tmp_path):
    reg = _make_registry(tmp_path)
    assert reg.active_version(SYM, TF, MT) is None


def test_active_version_prefers_live_gate(tmp_path):
    reg = _make_registry(tmp_path)
    _register_one(reg, version="v_fail", live_gate=False)
    _register_one(reg, version="v_pass", live_gate=True)
    av = reg.active_version(SYM, TF, MT)
    assert av is not None
    assert av.version == "v_pass"


def test_active_version_falls_back_to_latest(tmp_path):
    reg = _make_registry(tmp_path)
    _register_one(reg, version="v1", live_gate=False)
    _register_one(reg, version="v2", live_gate=False)
    av = reg.active_version(SYM, TF, MT)
    assert av is not None
    assert av.version == "v2"


def test_active_version_prefers_pin_over_gate(tmp_path):
    reg = _make_registry(tmp_path)
    _register_one(reg, version="v_old", live_gate=True)
    _register_one(reg, version="v_new", live_gate=True)
    reg.pin(SYM, TF, MT, "v_old")
    av = reg.active_version(SYM, TF, MT)
    assert av is not None
    assert av.version == "v_old"


# ---------------------------------------------------------------------------
# pin / unpin
# ---------------------------------------------------------------------------


def test_pin_returns_true_if_found(tmp_path):
    reg = _make_registry(tmp_path)
    _register_one(reg, version="v1")
    assert reg.pin(SYM, TF, MT, "v1") is True


def test_pin_returns_false_if_missing(tmp_path):
    reg = _make_registry(tmp_path)
    assert reg.pin(SYM, TF, MT, "nonexistent") is False


def test_pin_clears_previous_pin(tmp_path):
    reg = _make_registry(tmp_path)
    _register_one(reg, version="v1")
    _register_one(reg, version="v2")
    reg.pin(SYM, TF, MT, "v1")
    reg.pin(SYM, TF, MT, "v2")
    versions = reg.list_versions(SYM, TF, MT)
    pinned = [v for v in versions if v.is_pinned]
    assert len(pinned) == 1
    assert pinned[0].version == "v2"


def test_unpin_removes_pin(tmp_path):
    reg = _make_registry(tmp_path)
    _register_one(reg, version="v1")
    reg.pin(SYM, TF, MT, "v1")
    reg.unpin(SYM, TF, MT)
    versions = reg.list_versions(SYM, TF, MT)
    assert all(not v.is_pinned for v in versions)


# ---------------------------------------------------------------------------
# list_versions
# ---------------------------------------------------------------------------


def test_list_versions_empty(tmp_path):
    reg = _make_registry(tmp_path)
    assert reg.list_versions(SYM, TF) == []


def test_list_versions_filter_by_type(tmp_path):
    reg = _make_registry(tmp_path)
    _register_one(reg, version="v1")
    reg.register(SYM, TF, "meta_label", "vm1", "f.joblib")
    direction = reg.list_versions(SYM, TF, "direction")
    meta = reg.list_versions(SYM, TF, "meta_label")
    assert len(direction) == 1
    assert len(meta) == 1


def test_list_versions_ordered_by_registered_at(tmp_path):
    reg = _make_registry(tmp_path)
    _register_one(reg, version="v1")
    time.sleep(0.01)
    _register_one(reg, version="v2")
    versions = reg.list_versions(SYM, TF)
    assert versions[0].version == "v1"
    assert versions[1].version == "v2"


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


def test_summary_structure(tmp_path):
    reg = _make_registry(tmp_path)
    _register_one(reg, version="v1")
    s = reg.summary(SYM, TF)
    assert s["symbol"] == SYM
    assert s["n_versions"] == 1
    assert "by_type" in s
    assert "active" in s


# ---------------------------------------------------------------------------
# persistence — reload across instances
# ---------------------------------------------------------------------------


def test_persisted_versions_survive_reload(tmp_path):
    reg1 = _make_registry(tmp_path)
    _register_one(reg1, version="v1", live_gate=True)

    reg2 = _make_registry(tmp_path)  # new instance, reads from disk
    versions = reg2.list_versions(SYM, TF, MT)
    assert len(versions) == 1
    assert versions[0].version == "v1"


def test_corrupted_registry_returns_empty(tmp_path):
    path = tmp_path / "model_registry_BTC_USDT_15m.json"
    path.write_text("not valid json", encoding="utf-8")
    reg = _make_registry(tmp_path)
    assert reg.list_versions(SYM, TF) == []


# ---------------------------------------------------------------------------
# _cache_key
# ---------------------------------------------------------------------------


def test_cache_key_includes_both_parts():
    key = _cache_key("BTC/USDT", "15m")
    assert "BTC/USDT" in key
    assert "15m" in key
