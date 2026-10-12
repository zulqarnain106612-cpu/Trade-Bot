"""Small CPU horizon expert trained incrementally from matured outcomes."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import torch

from src.models.architectures import build_architecture


def _atomic_write(path: Path, data: bytes) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


class HorizonMLPExpert:
    """Horizon-isolated incremental return model with an MLP feature map."""

    def __init__(
        self,
        feature_names: tuple[str, ...],
        seed: int = 7,
        *,
        horizon_id: int | None = None,
        instrument_hash: str | None = None,
    ) -> None:
        if not feature_names or len(set(feature_names)) != len(feature_names):
            raise ValueError("feature_names must be a non-empty unique sequence")
        self.feature_names = tuple(feature_names)
        self.seed = int(seed)
        self.horizon_id = None if horizon_id is None else int(horizon_id)
        self.instrument_hash = instrument_hash
        self.schema_hash = hashlib.sha256("\0".join(self.feature_names).encode()).hexdigest()
        torch.manual_seed(self.seed)
        self.encoder = build_architecture(
            "mlp",
            input_dim=len(self.feature_names),
            hidden_dim=32,
            n_layers=1,
            dropout=0.0,
            d_model=16,
        ).cpu()
        self.regressor = torch.nn.Linear(16, 1).cpu()
        self.optimizer = torch.optim.SGD(
            (*self.encoder.parameters(), *self.regressor.parameters()), lr=0.01
        )
        self.n_samples = 0
        self.oos_samples = 0
        self.oos_directional_correct = 0
        self.oos_squared_error = 0.0
        self.seen_sample_ids: set[str] = set()
        self.training_data_hash = hashlib.sha256(b"").hexdigest()
        self.artifact_sha256: str | None = None

    def _input_tensor(self, features: dict[str, Any]) -> torch.Tensor:
        if set(features) != set(self.feature_names):
            raise ValueError("feature names do not match the trained horizon schema")
        values = np.asarray([features[name] for name in self.feature_names], dtype=np.float32)
        if not np.isfinite(values).all():
            raise ValueError("features must all be finite numeric values")
        # A changing online scaler makes old SGD coefficients refer to a
        # different coordinate system after every update. Use one stable,
        # causal transform at both fit and serve time instead.
        values = np.sign(values) * np.log1p(np.abs(values))
        return torch.from_numpy(values).reshape(1, -1)

    def partial_fit(
        self, features: dict[str, Any], matured_return: float, sample_id: str | None = None
    ) -> bool:
        """Consume one already matured return label exactly once at the caller."""
        if sample_id is not None and sample_id in self.seen_sample_ids:
            return False
        target = float(matured_return)
        if not np.isfinite(target):
            raise ValueError("matured_return must be finite")
        inputs = self._input_tensor(features)
        if self.n_samples:
            self.encoder.eval()
            self.regressor.eval()
            with torch.no_grad():
                prior = float(self.regressor(self.encoder(inputs)).item())
            residual = target - prior
            self.oos_squared_error += residual * residual
            self.oos_samples += 1
            if (prior > 0 and target > 0) or (prior < 0 and target < 0):
                self.oos_directional_correct += 1
        self.encoder.train()
        self.regressor.train()
        self.optimizer.zero_grad(set_to_none=True)
        prediction = self.regressor(self.encoder(inputs))
        loss = torch.nn.functional.mse_loss(
            prediction, torch.tensor([[target]], dtype=torch.float32)
        )
        loss.backward()
        self.optimizer.step()
        self.encoder.eval()
        self.regressor.eval()
        self.n_samples += 1
        record = hashlib.sha256()
        record.update(bytes.fromhex(self.training_data_hash))
        record.update(inputs.numpy().tobytes())
        record.update(np.asarray([target], dtype=np.float64).tobytes())
        self.training_data_hash = record.hexdigest()
        if sample_id is not None:
            self.seen_sample_ids.add(sample_id)
        return True

    def predict(self, features: dict[str, Any]) -> float:
        if self.n_samples == 0:
            raise RuntimeError("horizon model is UNTRAINED")
        inputs = self._input_tensor(features)
        self.encoder.eval()
        self.regressor.eval()
        with torch.no_grad():
            return float(self.regressor(self.encoder(inputs)).item())

    @property
    def predictive_sigma(self) -> float:
        """Prequential RMSE; unavailable until at least one forecast matured."""
        if self.oos_samples == 0:
            return 0.0
        return float(np.sqrt(self.oos_squared_error / self.oos_samples))

    @property
    def oos_directional_accuracy(self) -> float | None:
        if self.oos_samples == 0:
            return None
        return self.oos_directional_correct / self.oos_samples

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        versions_dir = path.parent / f".{path.name}.versions"
        versions_dir.mkdir(parents=True, exist_ok=True)
        temporary_dir = Path(tempfile.mkdtemp(prefix=".candidate-", dir=versions_dir))
        artifact = {
            "feature_names": self.feature_names,
            "horizon_id": self.horizon_id,
            "instrument_hash": self.instrument_hash,
            "schema_hash": self.schema_hash,
            "encoder_state": self.encoder.state_dict(),
            "regressor_state": self.regressor.state_dict(),
            "optimizer_state": self.optimizer.state_dict(),
            "n_samples": self.n_samples,
            "oos_samples": self.oos_samples,
            "oos_directional_correct": self.oos_directional_correct,
            "oos_squared_error": self.oos_squared_error,
            "seen_sample_ids": self.seen_sample_ids,
            "training_data_hash": self.training_data_hash,
            "seed": self.seed,
            "architecture": "mlp",
            "format_version": 3,
        }
        try:
            candidate_path = temporary_dir / path.name
            joblib.dump(artifact, candidate_path)
            from src.models.provenance import (
                ModelProvenance,
                build_manifest,
                code_commit,
                hash_artifact,
                library_versions,
                manifest_path_for,
            )

            payload = candidate_path.read_bytes()
            digest = hash_artifact(payload)
            provenance = ModelProvenance(
                model_id=(
                    f"h{self.horizon_id}-mlp-{self.instrument_hash or 'unscoped'}"
                    f"-{self.schema_hash[:8]}"
                ),
                training_data_hash=self.training_data_hash,
                feature_schema_hash=self.schema_hash,
                code_commit=code_commit(),
                hyperparameters={"encoder": "MLPHead", "optimizer": "SGD", "learning_rate": 0.01},
                random_seed=self.seed,
                library_versions=library_versions(("numpy", "scikit-learn", "torch", "joblib")),
                metrics={
                    "matured_samples": float(self.n_samples),
                    "oos_evaluated": float(self.oos_samples),
                    "oos_directional_accuracy": float(self.oos_directional_accuracy or 0.0),
                    "oos_rmse": self.predictive_sigma,
                },
                validation_methodology="prequential forecasts scored before each matured-label update",
                artifact_sha256=digest,
                feature_columns=self.feature_names,
            )
            manifest = build_manifest(candidate_path, payload, provenance)
            manifest_path_for(candidate_path).write_text(
                json.dumps(manifest, sort_keys=True), encoding="utf-8"
            )
            version_dir = versions_dir / digest
            if version_dir.exists():
                import shutil

                shutil.rmtree(temporary_dir)
            else:
                os.replace(temporary_dir, version_dir)
            versioned_path = version_dir / path.name
            pointer = path.with_suffix(path.suffix + ".active.json")
            pointer_payload = json.dumps(
                {
                    "artifact": str(versioned_path.relative_to(path.parent)),
                    "sha256": digest,
                    "format_version": 1,
                },
                sort_keys=True,
            ).encode()
            _atomic_write(pointer, pointer_payload)
            self.artifact_sha256 = digest
        finally:
            if temporary_dir.exists():
                import shutil

                shutil.rmtree(temporary_dir)

    @staticmethod
    def active_artifact_path(path: Path) -> Path:
        """Resolve the atomic active-version pointer, refusing path escape."""
        pointer = path.with_suffix(path.suffix + ".active.json")
        if not pointer.is_file():
            return path
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        if payload.get("format_version") != 1:
            raise ValueError("unsupported horizon model pointer")
        resolved = (path.parent / str(payload["artifact"])).resolve()
        versions_root = (path.parent / f".{path.name}.versions").resolve()
        try:
            resolved.relative_to(versions_root)
        except ValueError as exc:
            raise ValueError("horizon model pointer escapes its version directory") from exc
        if not resolved.is_file():
            raise ValueError("active horizon model artifact is missing")
        return resolved

    @staticmethod
    def activation_pointer(path: Path) -> dict[str, Any] | None:
        pointer = path.with_suffix(path.suffix + ".active.json")
        if not pointer.is_file():
            return None
        value = json.loads(pointer.read_text(encoding="utf-8"))
        if value.get("format_version") != 1 or not isinstance(value.get("artifact"), str):
            raise ValueError("invalid horizon model activation pointer")
        return value

    @classmethod
    def activation_state(cls, path: Path) -> str:
        pointer = cls.activation_pointer(path)
        return (
            "UNPROMOTED" if pointer is None else str(pointer.get("lifecycle_status", "UNPROMOTED"))
        )

    @staticmethod
    def write_activation_pointer(path: Path, payload: dict[str, Any]) -> None:
        pointer = path.with_suffix(path.suffix + ".active.json")
        _atomic_write(pointer, json.dumps(payload, sort_keys=True).encode("utf-8"))

    @classmethod
    def artifact_available(cls, path: Path) -> bool:
        try:
            return cls.active_artifact_path(path).is_file()
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return False

    @classmethod
    def load(
        cls,
        path: Path,
        expected_schema_hash: str | None = None,
        *,
        expected_horizon_id: int | None = None,
        expected_instrument_hash: str | None = None,
    ) -> HorizonMLPExpert:
        from src.models.provenance import hash_artifact, read_manifest, read_provenance

        artifact_path = cls.active_artifact_path(path)
        payload = artifact_path.read_bytes()
        manifest = read_manifest(artifact_path)
        provenance = read_provenance(artifact_path)
        if manifest.get("sha256") != hash_artifact(payload):
            raise ValueError("horizon model artifact checksum mismatch")
        if provenance is None or not provenance.is_complete:
            raise ValueError("horizon model artifact provenance is incomplete")
        if provenance.artifact_sha256 != manifest.get("sha256"):
            raise ValueError("horizon model provenance checksum mismatch")
        artifact = joblib.load(artifact_path)
        if artifact.get("format_version") != 3 or artifact.get("architecture") != "mlp":
            raise ValueError("unsupported horizon model artifact")
        expert = cls(
            tuple(artifact["feature_names"]),
            seed=int(artifact.get("seed", 7)),
            horizon_id=artifact.get("horizon_id"),
            instrument_hash=artifact.get("instrument_hash"),
        )
        if expected_horizon_id is not None and expert.horizon_id != expected_horizon_id:
            raise ValueError("horizon model artifact belongs to a different horizon")
        if (
            expected_instrument_hash is not None
            and expert.instrument_hash != expected_instrument_hash
        ):
            raise ValueError("horizon model artifact belongs to a different instrument")
        if expert.schema_hash != artifact.get("schema_hash"):
            raise ValueError("horizon model artifact has an invalid feature schema hash")
        if provenance.feature_schema_hash != expert.schema_hash:
            raise ValueError("horizon model provenance feature schema mismatch")
        if expected_schema_hash and expert.schema_hash != expected_schema_hash:
            raise ValueError("horizon model artifact feature schema mismatch")
        expert.encoder.load_state_dict(artifact["encoder_state"], strict=True)
        expert.regressor.load_state_dict(artifact["regressor_state"], strict=True)
        expert.optimizer.load_state_dict(artifact["optimizer_state"])
        expert.n_samples = int(artifact["n_samples"])
        expert.oos_samples = int(artifact.get("oos_samples", 0))
        expert.oos_directional_correct = int(artifact.get("oos_directional_correct", 0))
        expert.oos_squared_error = float(artifact.get("oos_squared_error", 0.0))
        expert.seen_sample_ids = set(artifact.get("seen_sample_ids", ()))
        expert.training_data_hash = str(artifact.get("training_data_hash", ""))
        expert.artifact_sha256 = hash_artifact(payload)
        if expert.n_samples < 1:
            raise ValueError("horizon model artifact has no training samples")
        return expert
