"""
Model Registry — MLflow + DVC integration for crypto-intel-v6 models.

Handles:
  - log_model(): log a PyTorch model artifact to MLflow with metrics
  - register_model(): promote model to Production stage in MLflow registry
  - load_model(): load a registered model by name and stage
  - tag_dvc(): create a DVC tag for the model artifact directory

All writes are idempotent: if the run already exists, a new run is created
under the same experiment.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_TRACKING_URI = os.environ.get("MLFLOW_TRACKING_URI", "http://127.0.0.1:5000")
_EXPERIMENT_NAME = os.environ.get("MLFLOW_EXPERIMENT", "crypto-intel-v6")
_DVC_REMOTE = os.environ.get("DVC_REMOTE", "origin")


class ModelRegistry:
    """
    MLflow model registry facade for crypto-intel-v6.

    Logs model checkpoints as MLflow artifacts and registers them in the
    MLflow Model Registry. Optionally tags a DVC commit for reproducibility.
    """

    def __init__(
        self,
        tracking_uri: str = _TRACKING_URI,
        experiment_name: str = _EXPERIMENT_NAME,
    ) -> None:
        self._tracking_uri = tracking_uri
        self._experiment_name = experiment_name
        self._mlflow: Any = None
        self._client: Any = None
        self._experiment_id: str | None = None
        self._init()

    def _init(self) -> None:
        try:
            import mlflow  # type: ignore[import]

            self._mlflow = mlflow
            mlflow.set_tracking_uri(self._tracking_uri)
            exp = mlflow.get_experiment_by_name(self._experiment_name)
            if exp is None:
                self._experiment_id = mlflow.create_experiment(self._experiment_name)
            else:
                self._experiment_id = exp.experiment_id
            self._client = mlflow.tracking.MlflowClient(tracking_uri=self._tracking_uri)
            log.info(
                "mlflow_connected",
                tracking_uri=self._tracking_uri,
                experiment=self._experiment_name,
            )
        except ImportError:
            log.warning("mlflow_not_installed_registry_disabled")

    def log_model(
        self,
        model: Any,
        model_name: str,
        horizon_idx: int,
        metrics: dict[str, float],
        params: dict[str, Any] | None = None,
        artifact_path: str | None = None,
    ) -> str | None:
        """
        Log a PyTorch model to MLflow with associated metrics and params.

        Returns the MLflow run_id or None if MLflow is unavailable.
        """
        if self._mlflow is None:
            log.debug("mlflow_unavailable_skipping_log", model_name=model_name)
            return None

        artifact_path = artifact_path or f"h{horizon_idx + 1}/{model_name}"
        tags = {"horizon_idx": str(horizon_idx), "model_name": model_name}

        with self._mlflow.start_run(experiment_id=self._experiment_id, tags=tags) as run:
            if params:
                self._mlflow.log_params(params)
            self._mlflow.log_metrics(metrics)

            try:
                self._mlflow.pytorch.log_model(model, artifact_path)  # type: ignore[attr-defined]
            except (ImportError, Exception) as exc:
                log.warning("mlflow_pytorch_log_failed", exc=str(exc))

            run_id = run.info.run_id
            log.info("mlflow_model_logged", model_name=model_name, run_id=run_id, metrics=metrics)
            return run_id

    def register_model(
        self,
        run_id: str,
        model_name: str,
        artifact_path: str,
        stage: str = "Production",
    ) -> str | None:
        """
        Register a logged model artifact into the MLflow Model Registry.

        Transitions the latest version to `stage`.
        Returns the model version string.
        """
        if self._client is None:
            return None

        try:
            model_uri = f"runs:/{run_id}/{artifact_path}"
            mv = self._mlflow.register_model(model_uri, model_name)
            self._client.transition_model_version_stage(
                name=model_name,
                version=mv.version,
                stage=stage,
                archive_existing_versions=True,
            )
            log.info("model_registered", model_name=model_name, version=mv.version, stage=stage)
            return mv.version
        except Exception as exc:
            log.warning("model_registration_failed", exc=str(exc))
            return None

    def load_model(
        self,
        model_name: str,
        stage: str = "Production",
    ) -> Any | None:
        """Load a model from the MLflow registry by name and stage."""
        if self._mlflow is None:
            return None

        try:
            model_uri = f"models:/{model_name}/{stage}"
            model = self._mlflow.pytorch.load_model(model_uri)  # type: ignore[attr-defined]
            log.info("model_loaded_from_registry", model_name=model_name, stage=stage)
            return model
        except Exception as exc:
            log.warning("model_load_failed", model_name=model_name, stage=stage, exc=str(exc))
            return None

    def tag_dvc(self, tag_name: str, artifact_dir: Path | None = None) -> bool:
        """
        Create a DVC tag for the artifact directory.

        The tag makes the model checkpoint reproducible from a git commit.
        Returns True if the DVC tag was created successfully.
        """
        try:
            import subprocess

            cmd = ["dvc", "commit", "--quiet"]
            if artifact_dir:
                cmd.append(str(artifact_dir))
            subprocess.run(cmd, check=True, capture_output=True)

            # Create git tag pointing at this DVC state
            import subprocess as sp

            sp.run(
                ["git", "tag", "-a", tag_name, "-m", f"DVC model tag: {tag_name}"],
                check=True,
                capture_output=True,
            )
            log.info("dvc_tag_created", tag=tag_name)
            return True
        except Exception as exc:
            log.warning("dvc_tag_failed", tag=tag_name, exc=str(exc))
            return False

    def list_registered(self, model_name_prefix: str = "") -> list[dict]:
        """List all registered models (optionally filtered by name prefix)."""
        if self._client is None:
            return []
        try:
            models = self._client.search_registered_models()
            return [
                {
                    "name": m.name,
                    "latest_version": m.latest_versions[-1].version
                    if m.latest_versions
                    else "none",
                }
                for m in models
                if m.name.startswith(model_name_prefix)
            ]
        except Exception:
            return []
