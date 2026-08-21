"""
Repeated rollbacks must not re-promote a value the watchdog already rejected.

rollback() took existing[-2] unconditionally. After one rollback that entry
is the bad value the rollback was escaping, so the second rollback promoted
it again -- the store oscillated between the good and bad value forever while
the design doc's headline invariant is "never regress".

Also pins the append-only file's crash behaviour: a truncated final line is a
promotion that never completed and must be recoverable, while corruption
earlier in the file means history was rewritten and must fail loudly.
"""

from __future__ import annotations

import json

import pytest

from src.tuning.store import NoPriorVersionError, VersionedConfigStore


def _store(tmp_path):
    return VersionedConfigStore(tmp_path / "versions.jsonl")


def test_second_rollback_does_not_restore_the_rejected_value(tmp_path) -> None:
    s = _store(tmp_path)
    s.promote("p", 1.0, {})
    s.promote("p", 2.0, {})  # rejected below
    assert s.rollback("p").value == 1.0

    s.promote("p", 3.0, {})  # also rejected
    second = s.rollback("p")

    assert second.value == 1.0
    assert second.value != 3.0


def test_rollback_walks_back_past_every_rejected_value(tmp_path) -> None:
    s = _store(tmp_path)
    for v in (1.0, 2.0, 3.0):
        s.promote("p", v, {})

    assert s.rollback("p").value == 2.0  # rejects 3.0
    assert s.rollback("p").value == 1.0  # rejects 2.0


def test_rollback_raises_when_nothing_safe_remains(tmp_path) -> None:
    s = _store(tmp_path)
    s.promote("p", 1.0, {})
    s.promote("p", 2.0, {})
    s.rollback("p")  # back to 1.0, 2.0 quarantined

    # Only 1.0 (current) and 2.0 (rejected) exist; there is no safe target.
    with pytest.raises(NoPriorVersionError):
        s.rollback("p")


def test_rollback_still_needs_two_versions(tmp_path) -> None:
    s = _store(tmp_path)
    s.promote("p", 1.0, {})
    with pytest.raises(NoPriorVersionError):
        s.rollback("p")


def test_history_is_never_edited_by_a_rollback(tmp_path) -> None:
    s = _store(tmp_path)
    s.promote("p", 1.0, {})
    s.promote("p", 2.0, {})
    s.rollback("p")

    versions = [r.version for r in s.history("p")]
    assert versions == [1, 2, 3]
    assert s.history("p")[-1].is_rollback is True


def test_a_truncated_final_line_is_dropped_not_fatal(tmp_path) -> None:
    path = tmp_path / "versions.jsonl"
    s = VersionedConfigStore(path)
    s.promote("p", 1.0, {})
    with path.open("a", encoding="utf-8") as f:
        f.write('{"param_name": "p", "value": 2.')  # crash mid-append

    reloaded = VersionedConfigStore(path)
    assert reloaded.truncated_tail is True
    assert reloaded.current("p").value == 1.0


def test_corruption_before_the_last_line_is_fatal(tmp_path) -> None:
    path = tmp_path / "versions.jsonl"
    s = VersionedConfigStore(path)
    s.promote("p", 1.0, {})
    s.promote("p", 2.0, {})

    lines = path.read_text(encoding="utf-8").splitlines()
    lines[0] = "{not json"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ValueError):
        VersionedConfigStore(path)


def test_a_clean_file_reloads_with_no_truncation_flag(tmp_path) -> None:
    path = tmp_path / "versions.jsonl"
    s = VersionedConfigStore(path)
    s.promote("p", 1.0, {"metric": "sharpe"})

    reloaded = VersionedConfigStore(path)
    assert reloaded.truncated_tail is False
    assert reloaded.current("p").evidence == {"metric": "sharpe"}
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])["value"] == 1.0
