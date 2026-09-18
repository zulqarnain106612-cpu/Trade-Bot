"""
MODL-001, MODL-005 — model artifacts carry full provenance and round-trip.

A model in production that cannot be traced to the data and the code that made
it is a model that cannot be rolled back to, reproduced, or argued about after
it loses money. The source document's list, all ten fields:

```
model ID · training data hash · feature schema hash · code commit
hyperparameters · random seed · library versions · metrics
validation methodology · artifact hash
```

The hashes carry most of the weight, so they get the most tests. Two
properties matter and they are different: a hash must **change** when the
thing it describes changes (or it detects nothing), and it must **not change**
when irrelevant things change (or it is noise and gets ignored).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from src.models.provenance import (
    COMMIT_ENV_VAR,
    REQUIRED_FIELDS,
    TRACKED_LIBRARIES,
    ModelProvenance,
    ProvenanceError,
    build_manifest,
    code_commit,
    hash_artifact,
    hash_feature_schema,
    hash_training_data,
    library_versions,
    manifest_path_for,
    read_manifest,
    read_provenance,
)


def make_record(**overrides) -> dict:
    base = {
        "model_id": "direction:BTC/USDT:15m",
        "training_data_hash": "a" * 64,
        "feature_schema_hash": "b" * 64,
        "code_commit": "c" * 40,
        "hyperparameters": {"max_depth": 4, "n_estimators": 200},
        "random_seed": 42,
        "library_versions": {"numpy": "2.0.0"},
        "metrics": {"oos_sharpe": 1.8},
        "validation_methodology": "CPCV(n_splits=6, n_test_splits=2)",
        "artifact_sha256": "d" * 64,
        "created_at": "2026-09-11T12:00:00+00:00",
        "symbol": "BTC/USDT",
        "timeframe": "15m",
        "feature_columns": ["frac_diff", "ofi"],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# The hashes
# ---------------------------------------------------------------------------


class TestTrainingDataHash:
    def test_the_same_data_hashes_the_same(self):
        X = np.arange(60, dtype=np.float64).reshape(20, 3)
        y = np.arange(20, dtype=np.int8)
        assert hash_training_data(X, y) == hash_training_data(X.copy(), y.copy())

    def test_one_changed_value_changes_the_hash(self):
        X = np.arange(60, dtype=np.float64).reshape(20, 3)
        y = np.arange(20, dtype=np.int8)
        other = X.copy()
        other[7, 1] += 1e-12
        assert hash_training_data(X, y) != hash_training_data(other, y)

    def test_a_changed_shape_changes_the_hash(self):
        values = np.arange(60, dtype=np.float64)
        assert hash_training_data(values.reshape(20, 3)) != hash_training_data(
            values.reshape(3, 20)
        )

    def test_a_changed_dtype_changes_the_hash(self):
        values = np.arange(20)
        assert hash_training_data(values.astype(np.float32)) != hash_training_data(
            values.astype(np.float64)
        )

    def test_a_transposed_view_is_not_the_same_matrix(self):
        # Same bytes, different logical order. Without the forced
        # C-contiguity these would collide and a model trained on the
        # transpose would claim the original's provenance.
        X = np.arange(60, dtype=np.float64).reshape(20, 3)
        assert hash_training_data(X) != hash_training_data(X.T)

    def test_the_split_between_arrays_matters(self):
        # Concatenating the arrays before hashing would make these equal, and
        # a different train/label partition of the same bytes is a different
        # training run.
        values = np.arange(20, dtype=np.float64)
        assert hash_training_data(values[:10], values[10:]) != hash_training_data(
            values[:15], values[15:]
        )

    def test_the_order_of_arrays_matters(self):
        a = np.arange(10, dtype=np.float64)
        b = np.arange(10, 20, dtype=np.float64)
        assert hash_training_data(a, b) != hash_training_data(b, a)

    def test_it_is_a_sha256_hex_digest(self):
        digest = hash_training_data(np.zeros(3))
        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")


class TestFeatureSchemaHash:
    def test_the_same_columns_hash_the_same(self):
        assert hash_feature_schema(["a", "b"]) == hash_feature_schema(["a", "b"])

    def test_order_is_part_of_the_schema(self):
        # A model fitted on [a, b] and fed [b, a] produces confident nonsense.
        # That is exactly what this hash exists to catch at load time.
        assert hash_feature_schema(["a", "b"]) != hash_feature_schema(["b", "a"])

    def test_an_added_column_changes_the_hash(self):
        assert hash_feature_schema(["a", "b"]) != hash_feature_schema(["a", "b", "c"])

    def test_an_empty_schema_still_hashes(self):
        assert len(hash_feature_schema([])) == 64


class TestArtifactHash:
    def test_the_same_bytes_hash_the_same(self):
        assert hash_artifact(b"model") == hash_artifact(b"model")

    def test_one_changed_byte_changes_the_hash(self):
        assert hash_artifact(b"model") != hash_artifact(b"modeL")


# ---------------------------------------------------------------------------
# The environment
# ---------------------------------------------------------------------------


class TestCodeCommit:
    def test_the_environment_variable_wins(self, monkeypatch):
        monkeypatch.setenv(COMMIT_ENV_VAR, "deadbeef")
        assert code_commit() == "deadbeef"

    def test_a_blank_environment_variable_is_ignored(self, monkeypatch, tmp_path):
        monkeypatch.setenv(COMMIT_ENV_VAR, "   ")
        assert code_commit(root=tmp_path) == ""

    def test_a_detached_head_holds_the_sha_directly(self, monkeypatch, tmp_path):
        monkeypatch.delenv(COMMIT_ENV_VAR, raising=False)
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("f" * 40, encoding="utf-8")
        assert code_commit(root=tmp_path) == "f" * 40

    def test_a_symbolic_head_is_followed(self, monkeypatch, tmp_path):
        monkeypatch.delenv(COMMIT_ENV_VAR, raising=False)
        git = tmp_path / ".git"
        (git / "refs" / "heads").mkdir(parents=True)
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git / "refs" / "heads" / "main").write_text("a" * 40 + "\n", encoding="utf-8")
        assert code_commit(root=tmp_path) == "a" * 40

    def test_a_packed_ref_is_found(self, monkeypatch, tmp_path):
        monkeypatch.delenv(COMMIT_ENV_VAR, raising=False)
        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git / "packed-refs").write_text(
            "# pack-refs with: peeled\n" + "b" * 40 + " refs/heads/main\n", encoding="utf-8"
        )
        assert code_commit(root=tmp_path) == "b" * 40

    def test_no_git_at_all_reports_nothing_rather_than_guessing(self, monkeypatch, tmp_path):
        # An empty string is honest. A fabricated commit is worse than none.
        monkeypatch.delenv(COMMIT_ENV_VAR, raising=False)
        assert code_commit(root=tmp_path) == ""

    def test_a_dangling_ref_reports_nothing(self, monkeypatch, tmp_path):
        monkeypatch.delenv(COMMIT_ENV_VAR, raising=False)
        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/gone\n", encoding="utf-8")
        assert code_commit(root=tmp_path) == ""

    def test_a_packed_refs_without_the_branch_reports_nothing(self, monkeypatch, tmp_path):
        monkeypatch.delenv(COMMIT_ENV_VAR, raising=False)
        git = tmp_path / ".git"
        git.mkdir()
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git / "packed-refs").write_text("c" * 40 + " refs/heads/other\n", encoding="utf-8")
        assert code_commit(root=tmp_path) == ""

    def test_a_linked_worktree_is_followed(self, monkeypatch, tmp_path):
        # Found by running this suite: in a linked worktree `.git` is a
        # *file* holding `gitdir: <path>`, not a directory, and this project
        # is developed in worktrees. Treating it as a directory reported "no
        # commit" for every build made in one -- a silently blank provenance
        # field rather than an error anyone would notice.
        monkeypatch.delenv(COMMIT_ENV_VAR, raising=False)
        main = tmp_path / "main"
        (main / ".git" / "refs" / "heads").mkdir(parents=True)
        (main / ".git" / "refs" / "heads" / "feature").write_text("e" * 40, encoding="utf-8")

        linked_git = main / ".git" / "worktrees" / "wt"
        linked_git.mkdir(parents=True)
        (linked_git / "HEAD").write_text("ref: refs/heads/feature\n", encoding="utf-8")
        (linked_git / "commondir").write_text("../..\n", encoding="utf-8")

        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / ".git").write_text(f"gitdir: {linked_git}\n", encoding="utf-8")

        assert code_commit(root=worktree) == "e" * 40

    def test_a_worktree_pointing_at_a_packed_ref_is_followed(self, monkeypatch, tmp_path):
        monkeypatch.delenv(COMMIT_ENV_VAR, raising=False)
        main = tmp_path / "main"
        (main / ".git").mkdir(parents=True)
        (main / ".git" / "packed-refs").write_text(
            "f" * 40 + " refs/heads/feature\n", encoding="utf-8"
        )
        linked_git = main / ".git" / "worktrees" / "wt"
        linked_git.mkdir(parents=True)
        (linked_git / "HEAD").write_text("ref: refs/heads/feature\n", encoding="utf-8")
        (linked_git / "commondir").write_text("../..\n", encoding="utf-8")

        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / ".git").write_text(f"gitdir: {linked_git}\n", encoding="utf-8")

        assert code_commit(root=worktree) == "f" * 40

    def test_a_git_directory_without_a_head_reports_nothing(self, monkeypatch, tmp_path):
        # An interrupted clone, or a directory someone created by hand.
        monkeypatch.delenv(COMMIT_ENV_VAR, raising=False)
        (tmp_path / ".git").mkdir()
        assert code_commit(root=tmp_path) == ""

    def test_a_dot_git_file_that_is_not_a_pointer_reports_nothing(self, monkeypatch, tmp_path):
        monkeypatch.delenv(COMMIT_ENV_VAR, raising=False)
        (tmp_path / ".git").write_text("something else entirely\n", encoding="utf-8")
        assert code_commit(root=tmp_path) == ""

    def test_a_relative_gitdir_pointer_is_resolved(self, monkeypatch, tmp_path):
        monkeypatch.delenv(COMMIT_ENV_VAR, raising=False)
        real = tmp_path / "elsewhere"
        real.mkdir()
        (real / "HEAD").write_text("a" * 40, encoding="utf-8")
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: ../elsewhere\n", encoding="utf-8")
        assert code_commit(root=worktree) == "a" * 40

    def test_the_real_repository_reports_a_commit(self):
        # The repository this runs in has a .git directory, a .git file
        # pointing at a worktree, or a CI-provided environment variable, so an
        # empty result here means the lookup is broken rather than that
        # provenance is genuinely unavailable.
        assert code_commit() != ""


class TestLibraryVersions:
    def test_the_tracked_libraries_are_reported(self):
        versions = library_versions()
        assert set(versions) == set(TRACKED_LIBRARIES)
        assert all(isinstance(v, str) and v for v in versions.values())

    def test_an_absent_package_is_named_rather_than_omitted(self):
        # Omitting it would make "numpy missing" and "numpy not tracked"
        # indistinguishable in the record.
        assert library_versions(["a-package-that-does-not-exist"]) == {
            "a-package-that-does-not-exist": "absent"
        }

    def test_the_list_is_deliberately_short(self):
        # A manifest that records the whole environment changes on every
        # unrelated upgrade, and a record that always differs tells you
        # nothing when it differs.
        assert len(TRACKED_LIBRARIES) < 10


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


class TestTheRecord:
    def test_every_required_field_is_present(self):
        record = ModelProvenance.from_dict(make_record())
        for name in REQUIRED_FIELDS:
            assert hasattr(record, name)

    def test_it_round_trips_through_a_dict(self):
        original = ModelProvenance.from_dict(make_record())
        assert ModelProvenance.from_dict(original.to_dict()) == original

    def test_it_round_trips_through_json(self):
        original = ModelProvenance.from_dict(make_record())
        revived = ModelProvenance.from_dict(json.loads(json.dumps(original.to_dict())))
        assert revived == original

    @pytest.mark.parametrize("field", REQUIRED_FIELDS)
    def test_a_missing_required_field_is_refused(self, field):
        raw = make_record()
        del raw[field]
        with pytest.raises(ProvenanceError, match=field):
            ModelProvenance.from_dict(raw)

    def test_it_is_frozen(self):
        # Provenance that can be edited after the fact is not provenance.
        record = ModelProvenance.from_dict(make_record())
        with pytest.raises(AttributeError):
            record.code_commit = "tampered"

    def test_a_complete_record_reports_no_gaps(self):
        assert ModelProvenance.from_dict(make_record()).is_complete

    @pytest.mark.parametrize(
        ("field", "empty"),
        [
            ("code_commit", ""),
            ("training_data_hash", ""),
            ("hyperparameters", {}),
            ("metrics", {}),
            ("library_versions", {}),
        ],
    )
    def test_an_empty_field_is_reported_as_a_gap(self, field, empty):
        # Present-but-empty is what an artifact built by a job that could not
        # determine its commit looks like. `from_dict` accepts it; this is
        # how a caller finds out.
        record = ModelProvenance.from_dict(make_record(**{field: empty}))
        assert not record.is_complete
        assert field in record.missing_fields()

    def test_the_optional_fields_do_not_affect_completeness(self):
        record = ModelProvenance.from_dict(make_record(created_at="", symbol="", timeframe=""))
        assert record.is_complete


# ---------------------------------------------------------------------------
# The manifest file
# ---------------------------------------------------------------------------


class TestTheManifest:
    @pytest.fixture
    def artifact(self, tmp_path: Path) -> Path:
        path = tmp_path / "model.joblib"
        path.write_bytes(b"pretend-this-is-a-model")
        return path

    def test_the_manifest_sits_beside_the_artifact(self, artifact):
        assert manifest_path_for(artifact) == artifact.with_suffix(".sha256")

    def test_the_legacy_keys_keep_their_meaning(self, artifact):
        manifest = build_manifest(artifact, artifact.read_bytes(), None)
        assert manifest["file"] == "model.joblib"
        assert manifest["sha256"] == hash_artifact(artifact.read_bytes())
        assert "provenance" not in manifest

    def test_provenance_is_nested_so_its_absence_is_visible(self, artifact):
        record = ModelProvenance.from_dict(make_record())
        manifest = build_manifest(artifact, artifact.read_bytes(), record)
        assert manifest["provenance"]["model_id"] == "direction:BTC/USDT:15m"
        assert manifest["sha256"] == hash_artifact(artifact.read_bytes())

    def test_reading_a_manifest_that_is_not_there(self, artifact):
        with pytest.raises(ProvenanceError, match="manifest missing"):
            read_manifest(artifact)

    def test_reading_a_manifest_that_is_not_json(self, artifact):
        manifest_path_for(artifact).write_text("{ not json", encoding="utf-8")
        with pytest.raises(ProvenanceError, match="not valid JSON"):
            read_manifest(artifact)

    def test_an_old_format_manifest_reports_no_provenance(self, artifact):
        # Not an error: models saved before the record existed still load,
        # and None is the answer a rollback needs to be able to see.
        manifest_path_for(artifact).write_text(
            json.dumps({"file": artifact.name, "sha256": "x" * 64}), encoding="utf-8"
        )
        assert read_provenance(artifact) is None

    def test_a_full_manifest_round_trips(self, artifact):
        record = ModelProvenance.from_dict(make_record())
        manifest_path_for(artifact).write_text(
            json.dumps(build_manifest(artifact, artifact.read_bytes(), record)), encoding="utf-8"
        )
        assert read_provenance(artifact) == record

    def test_a_truncated_provenance_block_is_refused_rather_than_half_read(self, artifact):
        raw = make_record()
        del raw["artifact_sha256"]
        manifest_path_for(artifact).write_text(
            json.dumps({"file": artifact.name, "sha256": "x" * 64, "provenance": raw}),
            encoding="utf-8",
        )
        with pytest.raises(ProvenanceError, match="artifact_sha256"):
            read_provenance(artifact)
