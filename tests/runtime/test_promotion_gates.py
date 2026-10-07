"""RES-016: the PROMOTION_GATE and SHADOW stages are decided by the existing
promotion gauntlet and model-registry evaluation, not re-implemented.

Decides: RES-016"""

from __future__ import annotations

from src.models.model_registry import ModelRegistry
from src.runtime.adaptive import gauntlet_evidence, shadow_evidence
from src.tuning.promotion_gauntlet import GauntletCriteria, GauntletObservation


def test_gauntlet_evidence_passes_and_fails_with_the_gauntlet() -> None:
    good = GauntletObservation(
        trade_count=40, days_running=20, realized_sharpe=1.1, realized_max_drawdown_pct=0.05
    )
    assert gauntlet_evidence(good) == (True, "passed")
    thin = GauntletObservation(
        trade_count=3, days_running=20, realized_sharpe=1.1, realized_max_drawdown_pct=0.05
    )
    passed, detail = gauntlet_evidence(thin)
    assert not passed and detail == "trade_count 3 < min_trades 30"
    lenient = GauntletCriteria(min_trades=2)
    assert gauntlet_evidence(thin, lenient) == (True, "passed")


def test_shadow_evidence_is_the_model_registrys_verdict() -> None:
    models = ModelRegistry(min_evaluations=5)
    models.register_shadow("m")
    assert shadow_evidence(models, "m") == (False, "insufficient evaluations (0 < 5)")
