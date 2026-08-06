"""
Shadow Deploy — 24-hour A/B Sharpe gating for model upgrades.

Runs a challenger model alongside the incumbent in shadow mode for
24 hours (configurable). Both receive the same signals. At evaluation
time, if challenger OOS Sharpe > incumbent Sharpe + epsilon, it
promotes the challenger to production.

Also generates Evidently data drift reports for each A/B evaluation.

Architecture:
  - ShadowDeployer maintains two model references: incumbent + challenger
  - On each inference call, both models produce predictions
  - Predictions are tracked in rolling arrays for Sharpe computation
  - _evaluate() promotes challenger or discards it
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np
import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_SHADOW_HOURS = float(__import__("os").environ.get("SHADOW_HOURS", "24"))
_SHARPE_EPSILON = 0.05  # challenger must exceed incumbent by 5% Sharpe


@dataclass
class ModelRecord:
    """Tracks predictions and returns for one model in the A/B test."""

    name: str
    model: Any
    predictions: list[float] = field(default_factory=list)
    actuals: list[float] = field(default_factory=list)
    returns: list[float] = field(default_factory=list)
    start_ts: float = field(default_factory=time.time)

    def sharpe(self) -> float:
        arr = np.array(self.returns)
        if len(arr) < 5:
            return -999.0
        std = float(np.std(arr))
        if std < 1e-9:
            return 0.0
        return float(np.mean(arr) / std * np.sqrt(252))

    def age_hours(self) -> float:
        return (time.time() - self.start_ts) / 3600.0


@dataclass
class ABResult:
    promoted: bool
    incumbent_sharpe: float
    challenger_sharpe: float
    incumbent_name: str
    challenger_name: str
    reason: str
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class ShadowDeployer:
    """
    Manages a 24-hour shadow deployment for candidate model upgrades.

    Usage:
        deployer = ShadowDeployer(incumbent_model, challenger_model)
        deployer.start()
        # on each bar:
        inc_pred = deployer.predict_incumbent(x)
        cha_pred = deployer.predict_challenger(x)
        deployer.record_return(actual_return)
        # after 24h:
        result = deployer.evaluate()
    """

    def __init__(
        self,
        incumbent: Any,
        challenger: Any,
        incumbent_name: str = "incumbent",
        challenger_name: str = "challenger",
        shadow_hours: float = _SHADOW_HOURS,
        sharpe_epsilon: float = _SHARPE_EPSILON,
        mlflow_tracking_uri: str | None = None,
    ) -> None:
        self._shadow_hours = shadow_hours
        self._sharpe_epsilon = sharpe_epsilon
        self._mlflow_uri = mlflow_tracking_uri

        self._incumbent = ModelRecord(name=incumbent_name, model=incumbent)
        self._challenger = ModelRecord(name=challenger_name, model=challenger)
        self._active = False
        self._result: ABResult | None = None

    def start(self) -> None:
        self._incumbent.start_ts = time.time()
        self._challenger.start_ts = time.time()
        self._active = True
        log.info(
            "shadow_deploy_started",
            incumbent=self._incumbent.name,
            challenger=self._challenger.name,
            shadow_hours=self._shadow_hours,
        )

    def predict_incumbent(self, x: Any) -> Any:
        return self._call_model(self._incumbent.model, x)

    def predict_challenger(self, x: Any) -> Any:
        return self._call_model(self._challenger.model, x)

    def _call_model(self, model: Any, x: Any) -> Any:
        """Call the model with whatever interface it presents."""
        if callable(model):
            return model(x)
        if hasattr(model, "predict"):
            return model.predict(x)
        return 0.0

    def record_return(
        self, actual_return: float, incumbent_pred: float = 0.0, challenger_pred: float = 0.0
    ) -> None:
        """
        Record realized return and compute per-model P&L attribution.

        The sign of the prediction determines direction;
        the realized_return is the market return over the horizon.
        """
        inc_dir = np.sign(incumbent_pred) if incumbent_pred else 0.0
        cha_dir = np.sign(challenger_pred) if challenger_pred else 0.0

        self._incumbent.returns.append(float(inc_dir * actual_return))
        self._challenger.returns.append(float(cha_dir * actual_return))

    def ready_to_evaluate(self) -> bool:
        return self._active and self._incumbent.age_hours() >= self._shadow_hours

    def evaluate(self) -> ABResult:
        """
        Compare Sharpe ratios and decide whether to promote challenger.

        If challenger Sharpe > incumbent Sharpe + epsilon, challenger is
        promoted (caller is responsible for swapping the live model).
        """
        inc_sharpe = self._incumbent.sharpe()
        cha_sharpe = self._challenger.sharpe()
        promote = cha_sharpe > inc_sharpe + self._sharpe_epsilon

        reason = (
            f"challenger {cha_sharpe:.3f} > incumbent {inc_sharpe:.3f} + eps {self._sharpe_epsilon}"
            if promote
            else f"challenger {cha_sharpe:.3f} <= incumbent {inc_sharpe:.3f} + eps {self._sharpe_epsilon}"
        )

        self._result = ABResult(
            promoted=promote,
            incumbent_sharpe=inc_sharpe,
            challenger_sharpe=cha_sharpe,
            incumbent_name=self._incumbent.name,
            challenger_name=self._challenger.name,
            reason=reason,
        )

        self._active = False
        log.info("shadow_deploy_evaluated", promoted=promote, reason=reason)
        self._generate_evidently_report()

        return self._result

    def _generate_evidently_report(self) -> None:
        """Generate Evidently data drift report comparing incumbent vs challenger predictions."""
        try:
            import pandas as pd
            from evidently.metric_preset import DataDriftPreset  # type: ignore[import]
            from evidently.report import Report  # type: ignore[import]

            ref = pd.DataFrame({"prediction": self._incumbent.returns})
            cur = pd.DataFrame({"prediction": self._challenger.returns})
            report = Report(metrics=[DataDriftPreset()])
            report.run(reference_data=ref, current_data=cur)
            report.save_html(
                f"./models/shadow_report_{self._incumbent.name}_vs_{self._challenger.name}.html"
            )
            log.info("evidently_report_saved")
        except ImportError:
            log.debug("evidently_not_installed_skipping_report")
        except Exception as exc:
            log.warning("evidently_report_failed", exc=str(exc))

    @property
    def result(self) -> ABResult | None:
        return self._result

    @property
    def active(self) -> bool:
        return self._active
