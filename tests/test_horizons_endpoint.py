"""
Coverage for the horizon term-structure surface: config/horizons.yaml ->
CryptoIntelligence.horizon_status() -> GET /horizons -> HorizonsPanel.

The point of this surface is that it reports every DECLARED horizon, not
only the ones that answered — a worker that dies must show up as a gap,
not disappear. Most of these tests pin exactly that.
"""

from __future__ import annotations

import time

import pytest

from src.intel import (
    horizon_entry,
    load_horizon_specs,
    sorted_horizon_entries,
)
from src.workers.orchestrator import WorkerResult


def test_config_declares_ten_horizons():
    # The shipped config/horizons.yaml. If a horizon is added or removed
    # this should be a deliberate edit, not a silent drift.
    assert len(load_horizon_specs()) == 10


def test_entries_are_ordered_shortest_term_first():
    entries = sorted_horizon_entries()
    assert [e["label"] for e in entries] == [
        "30s", "2m", "5m", "15m", "1h", "4h", "1D", "3D", "1W", "1M",
    ]
    # Ordering is by the horizon's declared id, and must be strictly
    # increasing — the panel plots them in this order as a term structure.
    ids = [e["id"] for e in entries]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids), "horizon ids must be unique"


def test_seconds_increase_monotonically_with_id():
    """A term structure that is not ordered in time is not a term structure."""
    secs = [e["seconds"] for e in sorted_horizon_entries()]
    # strict=False is deliberate: secs[1:] is one shorter by construction,
    # which is the whole point of pairing a list with its own tail.
    assert all(a < b for a, b in zip(secs, secs[1:], strict=False)), secs


def test_every_entry_carries_its_models():
    for e in sorted_horizon_entries():
        assert e["models"], f"horizon {e['key']} declares no models"


def test_static_entry_has_no_prediction():
    entry = horizon_entry("h1", {"id": 0, "label": "30s", "seconds": 30})
    assert entry["last_prediction"] is None
    assert entry["age_seconds"] is None


def test_entry_tolerates_a_missing_spec():
    # A malformed YAML block yields None for the spec; the loader must
    # still produce a usable row rather than raising on the signal path.
    entry = horizon_entry("h9", None)
    assert entry["key"] == "h9"
    assert entry["label"] == "h9"
    assert entry["models"] == []


# ---------------------------------------------------------------------------
# horizon_status() — the live path
# ---------------------------------------------------------------------------


class _IntelStub:
    """CryptoIntelligence.horizon_status bound to a bare object.

    Constructing the real class spawns worker processes and opens a DuckDB
    file, none of which this behaviour depends on.
    """

    def __init__(self, recorded):
        self._last_horizon_results = recorded

    horizon_status = None  # replaced below


@pytest.fixture
def intel_stub():
    from src.intel import CryptoIntelligence

    _IntelStub.horizon_status = CryptoIntelligence.horizon_status
    return _IntelStub


def _result(horizon_id, direction=1, confidence=0.7):
    return WorkerResult(
        task_id=f"t{horizon_id}",
        horizon_id=horizon_id,
        direction=direction,
        confidence=confidence,
        magnitude_mu=0.0123,
        magnitude_sigma=0.004,
        timing=0.5,
        algo="IOC",
    )


def test_status_reports_every_declared_horizon_even_when_none_reported(intel_stub):
    status = intel_stub({}).horizon_status()
    assert len(status) == 10
    assert all(h["last_prediction"] is None for h in status)


def test_status_merges_a_reported_prediction(intel_stub):
    now = time.time()
    status = intel_stub({0: (_result(0), now)}).horizon_status()

    first = next(h for h in status if h["id"] == 0)
    assert first["last_prediction"]["direction"] == 1
    assert first["last_prediction"]["confidence"] == pytest.approx(0.7)
    assert first["last_prediction"]["algo"] == "IOC"
    assert first["age_seconds"] == pytest.approx(0.0, abs=2.0)


def test_a_silent_horizon_stays_visible_with_no_prediction(intel_stub):
    """
    The whole reason this reports declared rather than reporting horizons:
    if only h1 answers, the other nine must still be listed so a dead
    worker is visible instead of silently absent.
    """
    status = intel_stub({0: (_result(0), time.time())}).horizon_status()

    assert len(status) == 10
    reported = [h for h in status if h["last_prediction"] is not None]
    silent = [h for h in status if h["last_prediction"] is None]
    assert len(reported) == 1
    assert len(silent) == 9


def test_age_reflects_staleness(intel_stub):
    stale_ts = time.time() - 300
    status = intel_stub({0: (_result(0), stale_ts)}).horizon_status()

    first = next(h for h in status if h["id"] == 0)
    # The panel flags anything older than 120s; the value must be real
    # elapsed time, not a fixed placeholder.
    assert first["age_seconds"] > 120


def test_status_preserves_a_worker_error(intel_stub):
    bad = _result(1)
    bad.error = "model load failed"
    status = intel_stub({1: (bad, time.time())}).horizon_status()

    entry = next(h for h in status if h["id"] == 1)
    assert entry["last_prediction"]["error"] == "model load failed"
