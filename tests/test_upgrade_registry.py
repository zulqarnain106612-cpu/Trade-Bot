"""Tests for src/upgrade/registry.py -- the MLflow model registry facade.

mlflow is a real (optional) dependency, so `_init()`'s `import mlflow` would
otherwise try to reach a real tracking server at 127.0.0.1:5000. Every test
injects a fake module via sys.modules instead of touching the network.
"""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock, patch

from src.upgrade.registry import ModelRegistry


def _fake_mlflow(existing_experiment=None):
    fake = MagicMock()
    fake.get_experiment_by_name.return_value = existing_experiment
    fake.create_experiment.return_value = "new-exp-id"
    return fake


def test_init_mlflow_not_installed_disables_registry():
    with patch.dict(sys.modules, {"mlflow": None}):
        reg = ModelRegistry()
    assert reg._mlflow is None
    assert reg._client is None


def test_init_creates_experiment_when_missing():
    fake = _fake_mlflow(existing_experiment=None)
    with patch.dict(sys.modules, {"mlflow": fake}):
        reg = ModelRegistry()
    assert reg._mlflow is fake
    assert reg._experiment_id == "new-exp-id"
    fake.create_experiment.assert_called_once()


def test_init_reuses_existing_experiment():
    existing = MagicMock(experiment_id="exp-42")
    fake = _fake_mlflow(existing_experiment=existing)
    with patch.dict(sys.modules, {"mlflow": fake}):
        reg = ModelRegistry()
    assert reg._experiment_id == "exp-42"
    fake.create_experiment.assert_not_called()


def _registry_with_client():
    fake = _fake_mlflow(existing_experiment=MagicMock(experiment_id="exp-1"))
    with patch.dict(sys.modules, {"mlflow": fake}):
        reg = ModelRegistry()
    return reg, fake


def test_log_model_returns_none_when_mlflow_unavailable():
    with patch.dict(sys.modules, {"mlflow": None}):
        reg = ModelRegistry()
    assert reg.log_model(model=object(), model_name="m", horizon_idx=0, metrics={}) is None


def test_log_model_happy_path_with_params():
    reg, fake = _registry_with_client()
    run_ctx = MagicMock()
    run_ctx.info.run_id = "run-123"
    fake.start_run.return_value.__enter__.return_value = run_ctx

    run_id = reg.log_model(
        model=object(),
        model_name="m1",
        horizon_idx=2,
        metrics={"acc": 0.9},
        params={"lr": 0.01},
    )

    assert run_id == "run-123"
    fake.log_params.assert_called_once_with({"lr": 0.01})
    fake.log_metrics.assert_called_once_with({"acc": 0.9})
    fake.pytorch.log_model.assert_called_once_with(
        reg._mlflow.pytorch.log_model.call_args[0][0], "h3/m1"
    )


def test_log_model_without_params_skips_log_params():
    reg, fake = _registry_with_client()
    run_ctx = MagicMock()
    run_ctx.info.run_id = "run-456"
    fake.start_run.return_value.__enter__.return_value = run_ctx

    reg.log_model(model=object(), model_name="m2", horizon_idx=0, metrics={"acc": 0.5})
    fake.log_params.assert_not_called()


def test_log_model_pytorch_log_failure_is_caught_and_run_id_still_returned():
    reg, fake = _registry_with_client()
    run_ctx = MagicMock()
    run_ctx.info.run_id = "run-789"
    fake.start_run.return_value.__enter__.return_value = run_ctx
    fake.pytorch.log_model.side_effect = RuntimeError("boom")

    run_id = reg.log_model(model=object(), model_name="m3", horizon_idx=0, metrics={})
    assert run_id == "run-789"


def test_register_model_returns_none_without_client():
    with patch.dict(sys.modules, {"mlflow": None}):
        reg = ModelRegistry()
    assert reg.register_model(run_id="r", model_name="m", artifact_path="p") is None


def test_register_model_happy_path():
    reg, fake = _registry_with_client()
    mv = MagicMock(version="3")
    fake.register_model.return_value = mv

    version = reg.register_model(run_id="r1", model_name="m1", artifact_path="h1/m1")

    assert version == "3"
    reg._client.transition_model_version_stage.assert_called_once_with(
        name="m1", version="3", stage="Production", archive_existing_versions=True
    )


def test_register_model_exception_returns_none():
    reg, fake = _registry_with_client()
    fake.register_model.side_effect = RuntimeError("registry down")
    assert reg.register_model(run_id="r", model_name="m", artifact_path="p") is None


def test_load_model_returns_none_when_mlflow_unavailable():
    with patch.dict(sys.modules, {"mlflow": None}):
        reg = ModelRegistry()
    assert reg.load_model("m") is None


def test_load_model_happy_path():
    reg, fake = _registry_with_client()
    fake.pytorch.load_model.return_value = "the-model"
    assert reg.load_model("m1", stage="Staging") == "the-model"
    fake.pytorch.load_model.assert_called_once_with("models:/m1/Staging")


def test_load_model_exception_returns_none():
    reg, fake = _registry_with_client()
    fake.pytorch.load_model.side_effect = RuntimeError("not found")
    assert reg.load_model("missing") is None


def test_tag_dvc_success_without_artifact_dir():
    reg, _ = _registry_with_client()
    with patch.object(subprocess, "run") as mock_run:
        assert reg.tag_dvc("v1") is True
    dvc_call = mock_run.call_args_list[0]
    assert dvc_call.args[0] == ["dvc", "commit", "--quiet"]


def test_tag_dvc_success_with_artifact_dir():
    reg, _ = _registry_with_client()
    with patch.object(subprocess, "run") as mock_run:
        assert reg.tag_dvc("v2", artifact_dir="models/h1") is True
    dvc_call = mock_run.call_args_list[0]
    assert dvc_call.args[0] == ["dvc", "commit", "--quiet", "models/h1"]


def test_tag_dvc_failure_returns_false():
    reg, _ = _registry_with_client()
    with patch.object(subprocess, "run", side_effect=subprocess.CalledProcessError(1, "dvc")):
        assert reg.tag_dvc("v3") is False


def test_list_registered_returns_empty_without_client():
    with patch.dict(sys.modules, {"mlflow": None}):
        reg = ModelRegistry()
    assert reg.list_registered() == []


def test_list_registered_with_and_without_latest_versions():
    reg, _ = _registry_with_client()
    with_versions = MagicMock(name="modelA")
    with_versions.name = "modelA"
    with_versions.latest_versions = [MagicMock(version="1"), MagicMock(version="2")]
    no_versions = MagicMock(name="modelB")
    no_versions.name = "modelB"
    no_versions.latest_versions = []
    reg._client.search_registered_models.return_value = [with_versions, no_versions]

    result = reg.list_registered()

    assert result == [
        {"name": "modelA", "latest_version": "2"},
        {"name": "modelB", "latest_version": "none"},
    ]


def test_list_registered_filters_by_prefix():
    reg, _ = _registry_with_client()
    a = MagicMock(latest_versions=[])
    a.name = "alpha-1"
    b = MagicMock(latest_versions=[])
    b.name = "beta-1"
    reg._client.search_registered_models.return_value = [a, b]

    result = reg.list_registered(model_name_prefix="alpha")

    assert [r["name"] for r in result] == ["alpha-1"]


def test_list_registered_exception_returns_empty():
    reg, _ = _registry_with_client()
    reg._client.search_registered_models.side_effect = RuntimeError("down")
    assert reg.list_registered() == []
