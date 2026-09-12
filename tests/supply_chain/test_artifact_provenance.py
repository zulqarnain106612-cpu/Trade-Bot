"""
SUP-004 — an artifact that can say where it came from, and be caught lying.

The write half of provenance is easy and nearly worthless on its own: any
build can drop a JSON file next to its outputs. The value is entirely in the
verify half, because the failures are all cases where the file is *present*
and wrong — a commit carried over from a cached checkout, a digest list that
no longer matches the bytes beside it, a file added to the bundle after the
record was written.

So the tests below are mostly corruption: mutate the bundle, mutate the
record, and require `verify()` to say so. `build_record` gets exactly enough
coverage to prove the round trip works.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.write_provenance import REQUIRED_FIELDS, SCHEMA_VERSION, build_record, verify

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    (tmp_path / "trade_bot-1.0.0.tar.gz").write_bytes(b"a source distribution")
    (tmp_path / "trade_bot-1.0.0-py3-none-any.whl").write_bytes(b"a wheel")
    (tmp_path / "sbom.cdx.json").write_text('{"components": []}')
    return tmp_path


@pytest.fixture
def record_path(bundle: Path) -> Path:
    path = bundle / "provenance.json"
    record = build_record(bundle)
    # The real commit comes from GITHUB_SHA or git; in a tmp_path there is
    # neither, so supply one rather than testing the environment.
    record["commit"] = "a" * 40
    path.write_text(json.dumps(record, indent=2, sort_keys=True))
    return path


class TestTheRoundTrip:
    def test_a_freshly_written_record_verifies(self, record_path):
        assert verify(record_path) == []

    def test_it_describes_every_file_except_itself(self, record_path):
        record = json.loads(record_path.read_text())
        names = {entry["name"] for entry in record["files"]}
        assert names == {
            "trade_bot-1.0.0.tar.gz",
            "trade_bot-1.0.0-py3-none-any.whl",
            "sbom.cdx.json",
        }

    def test_every_required_field_is_populated(self, record_path):
        record = json.loads(record_path.read_text())
        for field in REQUIRED_FIELDS:
            assert record.get(field) not in ("", None, [])

    def test_each_file_carries_a_digest_and_a_size(self, record_path):
        record = json.loads(record_path.read_text())
        for entry in record["files"]:
            assert len(entry["sha256"]) == 64
            assert entry["bytes"] > 0


class TestVerifyCatchesTampering:
    def test_a_modified_artifact(self, bundle, record_path):
        # The case that matters most: bytes replaced after the record was
        # written, which is what a compromised build step produces.
        (bundle / "trade_bot-1.0.0.tar.gz").write_bytes(b"something else entirely")
        assert any("digest mismatch" in p for p in verify(record_path))

    def test_a_missing_artifact(self, bundle, record_path):
        (bundle / "sbom.cdx.json").unlink()
        assert any("missing from the bundle" in p for p in verify(record_path))

    def test_an_undescribed_extra_file(self, bundle, record_path):
        # A file that appears in the bundle after the record was written is
        # the one nobody audits, because nothing points at it.
        (bundle / "extra-payload.bin").write_bytes(b"where did this come from")
        assert any("not described" in p for p in verify(record_path))

    def test_a_commit_that_is_not_a_full_sha(self, bundle, record_path):
        record = json.loads(record_path.read_text())
        record["commit"] = "abc1234"
        record_path.write_text(json.dumps(record))
        assert any("not a full SHA" in p for p in verify(record_path))

    def test_a_commit_from_a_different_run(self, bundle, record_path, monkeypatch):
        # A cached or shallow checkout in the verifying job, or a record
        # carried over from a previous build.
        monkeypatch.setenv("GITHUB_SHA", "b" * 40)
        assert any("does not match this run" in p for p in verify(record_path))

    @pytest.mark.parametrize("field", REQUIRED_FIELDS)
    def test_any_emptied_field(self, bundle, record_path, field):
        record = json.loads(record_path.read_text())
        record[field] = "" if isinstance(record[field], str) else None
        record_path.write_text(json.dumps(record))
        assert verify(record_path)

    def test_a_wrong_schema_version(self, bundle, record_path):
        record = json.loads(record_path.read_text())
        record["schema_version"] = SCHEMA_VERSION + 1
        record_path.write_text(json.dumps(record))
        assert any("schema_version" in p for p in verify(record_path))

    def test_an_unreadable_record(self, bundle):
        path = bundle / "provenance.json"
        path.write_text("{not json")
        assert any("unreadable" in p for p in verify(path))

    def test_a_missing_record(self, tmp_path):
        assert verify(tmp_path / "absent.json")


class TestTheReleaseWorkflowUsesIt:
    @pytest.fixture(scope="class")
    def workflow(self) -> str:
        return (REPO / ".github/workflows/release.yml").read_text(encoding="utf-8")

    def test_the_record_is_written(self, workflow):
        assert "write_provenance.py --out" in workflow

    def test_the_record_is_verified_in_a_separate_job(self, workflow):
        # Verified from the *uploaded* artifact, which is the only way to
        # notice that what was uploaded is not what was described.
        assert "write_provenance.py --verify" in workflow
        assert "download-artifact" in workflow

    def test_an_sbom_is_produced(self, workflow):
        assert "cyclonedx" in workflow.lower()

    def test_the_build_is_attested_with_oidc(self, workflow):
        assert "attest-build-provenance@" in workflow
        assert "id-token: write" in workflow
        assert "attestations: write" in workflow

    def test_no_signing_key_is_stored(self, workflow):
        # The point of OIDC here: a short-lived workload identity, so there is
        # no signing secret to leak or rotate.
        assert "secrets." not in workflow

    def test_the_workflow_has_a_gate(self, workflow):
        assert "assert_jobs_green.py" in workflow
