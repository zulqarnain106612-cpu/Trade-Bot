from pathlib import Path

from src.tuning.audit import TuningAuditLog, TuningEventType


def test_record_and_read_all(tmp_path: Path) -> None:
    log = TuningAuditLog(tmp_path / "audit.jsonl")
    log.record("hmm.entropy_threshold", TuningEventType.PROPOSED, {"candidate": 0.6})
    log.record("hmm.entropy_threshold", TuningEventType.EVALUATED, {"oos_sharpe_delta": 0.12})
    entries = log.read_all()
    assert len(entries) == 2
    assert entries[0].event_type == TuningEventType.PROPOSED
    assert entries[1].details["oos_sharpe_delta"] == 0.12


def test_read_for_param_filters(tmp_path: Path) -> None:
    log = TuningAuditLog(tmp_path / "audit.jsonl")
    log.record("hmm.entropy_threshold", TuningEventType.PROPOSED)
    log.record("features.vwap_window", TuningEventType.PROPOSED)
    entries = log.read_for_param("hmm.entropy_threshold")
    assert len(entries) == 1
    assert entries[0].param_name == "hmm.entropy_threshold"


def test_read_all_empty_when_no_file(tmp_path: Path) -> None:
    log = TuningAuditLog(tmp_path / "audit.jsonl")
    assert log.read_all() == []


def test_entries_persist_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    TuningAuditLog(path).record("hmm.entropy_threshold", TuningEventType.PROMOTED, {"v": 1})
    reloaded = TuningAuditLog(path).read_all()
    assert len(reloaded) == 1
    assert reloaded[0].event_type == TuningEventType.PROMOTED


def test_all_event_types_round_trip(tmp_path: Path) -> None:
    log = TuningAuditLog(tmp_path / "audit.jsonl")
    for event_type in TuningEventType:
        log.record("p", event_type)
    entries = log.read_all()
    assert [e.event_type for e in entries] == list(TuningEventType)


def test_details_defaults_to_empty_dict(tmp_path: Path) -> None:
    log = TuningAuditLog(tmp_path / "audit.jsonl")
    entry = log.record("hmm.entropy_threshold", TuningEventType.PAUSED)
    assert entry.details == {}


def test_to_json_from_dict_roundtrip(tmp_path: Path) -> None:
    from src.tuning.audit import TuningAuditEntry

    entry = TuningAuditEntry(
        param_name="hmm.entropy_threshold",
        event_type=TuningEventType.PROMOTED,
        timestamp="2026-01-01T00:00:00+00:00",
        details={"v": 1, "delta": 0.05},
    )
    json_str = entry.to_json()
    assert "promoted" in json_str
    recovered = TuningAuditEntry.from_dict(__import__("json").loads(json_str))
    assert recovered.param_name == entry.param_name
    assert recovered.event_type == TuningEventType.PROMOTED
    assert recovered.details == {"v": 1, "delta": 0.05}


def test_path_property(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = TuningAuditLog(path)
    assert log.path == path
