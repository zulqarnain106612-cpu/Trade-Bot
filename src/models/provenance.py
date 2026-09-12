"""
Model provenance — what a model artifact must be able to say about itself.

MODL-001. A model in production that cannot be traced to the data and the code
that made it is a model that cannot be rolled back to, reproduced, or argued
about after it loses money. The source document lists what an artifact must
carry:

```
model ID
training data hash
feature schema hash
code commit
hyperparameters
random seed
library versions
metrics
validation methodology
artifact hash
```

This module defines that record, the hashes that go into it, and the manifest
file it is written to. `src/models/trainer.py` produces one per saved model and
verifies the artifact hash on load.

Backwards compatibility is deliberate: the manifest was previously
`{"file": ..., "sha256": ...}` and models saved before this change still load.
`ModelProvenance.missing_fields()` is how a caller finds out that an artifact
predates the record rather than discovering it at rollback time.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any

import numpy as np

#: Every field the source document requires. Order is the document's.
REQUIRED_FIELDS: tuple[str, ...] = (
    "model_id",
    "training_data_hash",
    "feature_schema_hash",
    "code_commit",
    "hyperparameters",
    "random_seed",
    "library_versions",
    "metrics",
    "validation_methodology",
    "artifact_sha256",
)

#: Packages whose version can change a model's output. Not "everything
#: installed": a manifest that records the whole environment is one that
#: changes on every unrelated upgrade, and a record that always differs tells
#: you nothing when it differs.
TRACKED_LIBRARIES: tuple[str, ...] = (
    "numpy",
    "pandas",
    "scikit-learn",
    "scipy",
    "xgboost",
)

#: Set in CI, where there is no .git directory to read.
COMMIT_ENV_VAR = "TRADE_BOT_GIT_COMMIT"

MANIFEST_SUFFIX = ".sha256"


class ProvenanceError(RuntimeError):
    """Raised when an artifact's provenance is missing or inconsistent."""


# ---------------------------------------------------------------------------
# The hashes
# ---------------------------------------------------------------------------


def hash_training_data(*arrays: np.ndarray) -> str:
    """
    A stable digest of the exact training inputs.

    Hashes the dtype, shape and raw bytes of each array in order, with a
    separator between them, so two different partitions of the same bytes do
    not collide. C-contiguity is forced because a transposed view has the same
    bytes in a different logical order, and the model would be trained on a
    different matrix.
    """
    digest = hashlib.sha256()
    for array in arrays:
        contiguous = np.ascontiguousarray(array)
        digest.update(str(contiguous.dtype).encode("utf-8"))
        digest.update(b"|")
        digest.update(str(contiguous.shape).encode("utf-8"))
        digest.update(b"|")
        digest.update(contiguous.tobytes())
        digest.update(b"||")
    return digest.hexdigest()


def hash_feature_schema(columns: Sequence[str]) -> str:
    """
    A digest of the feature column names **in order**.

    Order matters and is deliberately part of the hash: a model fitted on
    `[a, b]` and fed `[b, a]` produces confident nonsense, and that is exactly
    the failure this hash exists to catch at load time.
    """
    payload = "\n".join(columns).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def hash_artifact(data: bytes) -> str:
    """A digest of the serialised model bytes."""
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# The environment
# ---------------------------------------------------------------------------


def _git_dir(base: Path) -> Path | None:
    """
    The directory holding HEAD for `base`, or None.

    In an ordinary clone `.git` is a directory. **In a linked worktree it is a
    file** containing `gitdir: <path>`, and this project is developed in
    worktrees -- so treating `.git` as a directory reported "no commit" for
    every build made in one, which is a silently blank provenance field rather
    than an error anybody would notice.
    """
    dot_git = base / ".git"
    if dot_git.is_dir():
        return dot_git
    try:
        pointer = dot_git.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not pointer.startswith("gitdir:"):
        return None
    target = Path(pointer.split(":", 1)[1].strip())
    return target if target.is_absolute() else (base / target).resolve()


def _common_dir(git_dir: Path) -> Path:
    """
    Where a worktree's shared refs live.

    A linked worktree has its own HEAD but shares `refs/` and `packed-refs`
    with the main repository, named by the `commondir` file.
    """
    try:
        pointer = (git_dir / "commondir").read_text(encoding="utf-8").strip()
    except OSError:
        return git_dir
    target = Path(pointer)
    return target if target.is_absolute() else (git_dir / target).resolve()


def _resolve_ref(git_dir: Path, ref: str) -> str:
    """Follow a symbolic ref to a sha, checking loose then packed refs."""
    for candidate in (git_dir, _common_dir(git_dir)):
        try:
            return (candidate / ref).read_text(encoding="utf-8").strip()
        except OSError:
            continue
    try:
        packed = (_common_dir(git_dir) / "packed-refs").read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in packed.splitlines():
        if line.endswith(f" {ref}"):
            return line.split(" ", 1)[0]
    return ""


def code_commit(root: Path | None = None) -> str:
    """
    The git commit the artifact was built from.

    Read from `.git` directly rather than by shelling out to `git`: this runs
    inside a training job that may have no git binary, and a subprocess here
    is a cost with no benefit. `TRADE_BOT_GIT_COMMIT` overrides, because a
    container build has the commit in its environment and no `.git` at all.

    Returns "" when the commit genuinely cannot be determined. An empty string
    is honest; a fabricated one is worse than nothing, and
    `ModelProvenance.missing_fields()` is how a caller finds out.
    """
    from_env = os.environ.get(COMMIT_ENV_VAR, "").strip()
    if from_env:
        return from_env

    base = root or Path(__file__).resolve().parents[2]
    git_dir = _git_dir(base)
    if git_dir is None:
        return ""

    try:
        pointer = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return ""

    if not pointer.startswith("ref:"):
        return pointer  # detached HEAD: the file already holds the sha

    return _resolve_ref(git_dir, pointer.removeprefix("ref:").strip())


def library_versions(packages: Iterable[str] = TRACKED_LIBRARIES) -> dict[str, str]:
    """Installed versions of the packages that can change a model's output."""
    versions: dict[str, str] = {}
    for name in packages:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = "absent"
    return versions


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelProvenance:
    """
    Everything a model artifact must be able to say about its own origin.

    Frozen: provenance that can be edited after the fact is not provenance.
    """

    model_id: str
    training_data_hash: str
    feature_schema_hash: str
    code_commit: str
    hyperparameters: dict[str, Any]
    random_seed: int
    library_versions: dict[str, str]
    metrics: dict[str, float]
    validation_methodology: str
    artifact_sha256: str
    created_at: str = ""
    symbol: str = ""
    timeframe: str = ""
    feature_columns: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "training_data_hash": self.training_data_hash,
            "feature_schema_hash": self.feature_schema_hash,
            "code_commit": self.code_commit,
            "hyperparameters": dict(self.hyperparameters),
            "random_seed": self.random_seed,
            "library_versions": dict(self.library_versions),
            "metrics": dict(self.metrics),
            "validation_methodology": self.validation_methodology,
            "artifact_sha256": self.artifact_sha256,
            "created_at": self.created_at,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "feature_columns": list(self.feature_columns),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ModelProvenance:
        missing = [f for f in REQUIRED_FIELDS if f not in raw]
        if missing:
            raise ProvenanceError(f"provenance record is missing: {', '.join(missing)}")
        return cls(
            model_id=raw["model_id"],
            training_data_hash=raw["training_data_hash"],
            feature_schema_hash=raw["feature_schema_hash"],
            code_commit=raw["code_commit"],
            hyperparameters=dict(raw["hyperparameters"]),
            random_seed=int(raw["random_seed"]),
            library_versions=dict(raw["library_versions"]),
            metrics=dict(raw["metrics"]),
            validation_methodology=raw["validation_methodology"],
            artifact_sha256=raw["artifact_sha256"],
            created_at=raw.get("created_at", ""),
            symbol=raw.get("symbol", ""),
            timeframe=raw.get("timeframe", ""),
            feature_columns=tuple(raw.get("feature_columns", [])),
        )

    def missing_fields(self) -> tuple[str, ...]:
        """
        Required fields that are present but empty.

        `from_dict` refuses a record with a *missing* key; this reports one
        that carries the key with nothing in it -- which is what an artifact
        built by a job that could not determine its commit looks like.
        """
        empty: list[str] = []
        for name in REQUIRED_FIELDS:
            value = getattr(self, name)
            if value == "" or value == {} or value is None:
                empty.append(name)
        return tuple(empty)

    @property
    def is_complete(self) -> bool:
        return not self.missing_fields()


# ---------------------------------------------------------------------------
# The manifest file
# ---------------------------------------------------------------------------


def manifest_path_for(artifact: Path) -> Path:
    return artifact.with_suffix(MANIFEST_SUFFIX)


def build_manifest(artifact: Path, data: bytes, provenance: ModelProvenance | None) -> dict:
    """
    The manifest's on-disk shape.

    `file` and `sha256` stay at the top level and keep their original meaning,
    so a reader written against the old format still works. Provenance is a
    nested object, so its absence is visible rather than inferred from a
    handful of missing keys.
    """
    manifest: dict[str, Any] = {"file": artifact.name, "sha256": hash_artifact(data)}
    if provenance is not None:
        manifest["provenance"] = provenance.to_dict()
    return manifest


def read_manifest(artifact: Path) -> dict:
    """Read and parse an artifact's manifest, or raise."""
    path = manifest_path_for(artifact)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProvenanceError(
            f"Model manifest missing for {artifact}. Re-train the model to regenerate it."
        ) from exc
    except json.JSONDecodeError as exc:
        raise ProvenanceError(f"Model manifest for {artifact} is not valid JSON: {exc}") from exc


def read_provenance(artifact: Path) -> ModelProvenance | None:
    """
    The artifact's provenance record, or None when it predates the record.

    None and a record with empty fields are different answers to different
    questions, and a rollback needs to be able to tell them apart.
    """
    raw = read_manifest(artifact).get("provenance")
    if raw is None:
        return None
    return ModelProvenance.from_dict(raw)
