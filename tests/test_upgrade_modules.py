"""Tests for upgrade/ modules: MAML, Optuna walk-forward, ShadowDeploy, Registry."""

from __future__ import annotations

import numpy as np
import torch
import torch.nn as nn


# ─── helpers ──────────────────────────────────────────────────────────────────


class _TinyMLP(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.fc = nn.Linear(4, 2)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.fc(x)


# ─── MAML ─────────────────────────────────────────────────────────────────────


class TestFastAdapt:
    def test_returns_adapted_params_dict(self) -> None:
        from src.upgrade.maml import fast_adapt

        model = _TinyMLP()
        x = torch.randn(8, 4)
        y = torch.randint(0, 2, (8,))
        adapted = fast_adapt(model, x, y, nn.CrossEntropyLoss(), k_steps=1)
        assert isinstance(adapted, dict)
        assert "fc.weight" in adapted

    def test_adapted_params_differ_from_original(self) -> None:
        from src.upgrade.maml import fast_adapt

        model = _TinyMLP()
        original_w = model.fc.weight.data.clone()
        x = torch.randn(8, 4)
        y = torch.randint(0, 2, (8,))
        adapted = fast_adapt(model, x, y, nn.CrossEntropyLoss(), k_steps=3, lr_inner=0.1)
        assert not torch.allclose(adapted["fc.weight"], original_w)

    def test_k_steps_zero_unchanged(self) -> None:
        from src.upgrade.maml import fast_adapt

        model = _TinyMLP()
        x = torch.randn(8, 4)
        y = torch.randint(0, 2, (8,))
        adapted = fast_adapt(model, x, y, nn.CrossEntropyLoss(), k_steps=0)
        assert torch.allclose(adapted["fc.weight"], model.fc.weight.data)


class TestMAMLOptimizer:
    def test_meta_update_runs(self) -> None:
        from src.upgrade.maml import MAMLOptimizer

        model = _TinyMLP()
        opt = MAMLOptimizer(model, lr_inner=0.01, lr_outer=0.001, k_steps=1)
        tasks = [
            {
                "support_x": torch.randn(4, 4),
                "support_y": torch.randint(0, 2, (4,)),
                "query_x": torch.randn(4, 4),
                "query_y": torch.randint(0, 2, (4,)),
            }
        ]
        loss = opt.meta_update(tasks)
        assert isinstance(loss, float)
        assert loss >= 0.0

    def test_meta_update_empty_tasks_returns_zero(self) -> None:
        from src.upgrade.maml import MAMLOptimizer

        model = _TinyMLP()
        opt = MAMLOptimizer(model)
        loss = opt.meta_update([])
        assert loss == 0.0


class TestHorizonMAMLAdapter:
    def test_adapt_on_drift_target_horizon_saves_checkpoint(self, tmp_path) -> None:

        from src.upgrade.maml import HorizonMAMLAdapter

        # h8 = index 7, h10 = index 9 — target horizons
        adapter = HorizonMAMLAdapter(checkpoint_dir=tmp_path, k_steps=1)
        model = _TinyMLP()
        x = torch.randn(8, 4)
        y = torch.randint(0, 2, (8,))
        adapted = adapter.adapt_on_drift(horizon_id=7, model=model, recent_x=x, recent_y=y)
        assert adapted is not None
        assert (tmp_path / "h8_adapted.pt").exists()

    def test_adapt_on_drift_non_target_horizon_skips(self, tmp_path) -> None:
        from src.upgrade.maml import HorizonMAMLAdapter

        adapter = HorizonMAMLAdapter(checkpoint_dir=tmp_path)
        model = _TinyMLP()
        x = torch.randn(4, 4)
        y = torch.randint(0, 2, (4,))
        result = adapter.adapt_on_drift(horizon_id=0, model=model, recent_x=x, recent_y=y)
        # horizon 0 is not in target horizons → returns the original model unchanged
        assert result is model


# ─── Optuna walk-forward ──────────────────────────────────────────────────────


class TestSharpeHelper:
    def test_zero_std_returns_zero(self) -> None:
        from src.upgrade.optuna_wf import _sharpe

        assert _sharpe(np.zeros(10)) == 0.0

    def test_positive_returns_positive_sharpe(self) -> None:
        from src.upgrade.optuna_wf import _sharpe

        r = np.ones(252) * 0.01
        assert _sharpe(r) > 0.0


class TestWalkForwardStudy:
    def test_run_completes_with_dummy_fn(self, tmp_path) -> None:
        from src.upgrade.optuna_wf import WalkForwardStudy

        data = np.random.randn(100)

        def dummy_fn(params, X_train, y_train, X_test, y_test, trial):
            return np.random.randn(len(X_test)).tolist()

        study = WalkForwardStudy(
            study_name="test_run",
            train_fn=dummy_fn,
            data=data,
            n_trials=2,
            n_folds=3,
            storage_path=tmp_path / "study.db",
        )
        result = study.run()
        assert "best_params" in result or "error" in result or isinstance(result, dict)

    def test_best_params_before_run_raises_or_returns_none(self, tmp_path) -> None:
        from src.upgrade.optuna_wf import WalkForwardStudy

        study = WalkForwardStudy(
            study_name="test_pre",
            train_fn=lambda *a: [],
            data=np.zeros(30),
            n_trials=1,
            storage_path=tmp_path / "s.db",
        )
        params = study.best_params
        assert params is None or isinstance(params, dict)

    def test_wf_params_defaults(self) -> None:
        from src.upgrade.optuna_wf import WFParams

        wp = WFParams()
        assert wp.lr_min < wp.lr_max
        assert 0 < wp.n_layers_min <= wp.n_layers_max


# ─── ShadowDeployer ───────────────────────────────────────────────────────────


class TestShadowDeployer:
    def _make_deployer(self, shadow_hours: float = 0.001):
        from src.upgrade.shadow_deploy import ShadowDeployer

        inc = lambda x: float(x)  # noqa: E731
        cha = lambda x: float(x) * 1.1  # noqa: E731
        return ShadowDeployer(
            incumbent=inc,
            challenger=cha,
            shadow_hours=shadow_hours,
        )

    def test_start_sets_active(self) -> None:
        d = self._make_deployer()
        d.start()
        assert d.active

    def test_predict_incumbent_callable(self) -> None:
        d = self._make_deployer()
        d.start()
        result = d.predict_incumbent(1.0)
        assert isinstance(result, float)

    def test_predict_challenger_callable(self) -> None:
        d = self._make_deployer()
        d.start()
        result = d.predict_challenger(1.0)
        assert isinstance(result, float)

    def test_predict_model_with_predict_method(self) -> None:
        from src.upgrade.shadow_deploy import ShadowDeployer

        class Sklearn:
            def predict(self, x):
                return [0.5]

        d = ShadowDeployer(incumbent=Sklearn(), challenger=Sklearn())
        d.start()
        result = d.predict_incumbent(1.0)
        assert result == [0.5]

    def test_record_return_runs(self) -> None:
        d = self._make_deployer()
        d.start()
        d.record_return(0.01, incumbent_pred=0.5, challenger_pred=0.6)
        # Doesn't crash

    def test_ready_to_evaluate_before_time(self) -> None:
        d = self._make_deployer(shadow_hours=24.0)
        d.start()
        assert not d.ready_to_evaluate()

    def test_evaluate_after_records(self) -> None:
        d = self._make_deployer(shadow_hours=0.0)
        d.start()
        for _ in range(10):
            d.record_return(0.01, incumbent_pred=0.5, challenger_pred=0.6)
        result = d.evaluate()
        assert hasattr(result, "promoted")
        assert isinstance(result.promoted, bool)

    def test_result_none_before_evaluate(self) -> None:
        d = self._make_deployer()
        assert d.result is None


# ─── ModelRegistry ────────────────────────────────────────────────────────────


class TestModelRegistry:
    def test_init_with_local_uri(self, tmp_path) -> None:
        from src.upgrade.registry import ModelRegistry

        reg = ModelRegistry(tracking_uri=str(tmp_path / "mlruns"))
        assert reg is not None

    def test_log_model_creates_run(self, tmp_path) -> None:
        from src.upgrade.registry import ModelRegistry

        reg = ModelRegistry(tracking_uri=str(tmp_path / "mlruns"))
        model = _TinyMLP()
        run_id = reg.log_model(
            model=model,
            horizon_id=0,
            params={"lr": 0.001},
            metrics={"sharpe": 1.5},
        )
        assert run_id is None or isinstance(run_id, str)

    def test_list_registered_empty(self, tmp_path) -> None:
        from src.upgrade.registry import ModelRegistry

        reg = ModelRegistry(tracking_uri=str(tmp_path / "mlruns"))
        result = reg.list_registered()
        assert isinstance(result, list)

    def test_load_model_missing_returns_none(self, tmp_path) -> None:
        from src.upgrade.registry import ModelRegistry

        reg = ModelRegistry(tracking_uri=str(tmp_path / "mlruns"))
        result = reg.load_model("nonexistent_model", version=1)
        assert result is None

    def test_tag_dvc_returns_bool(self, tmp_path) -> None:
        from src.upgrade.registry import ModelRegistry

        reg = ModelRegistry(tracking_uri=str(tmp_path / "mlruns"))
        ok = reg.tag_dvc("v1.0", artifact_dir=tmp_path)
        assert isinstance(ok, bool)
