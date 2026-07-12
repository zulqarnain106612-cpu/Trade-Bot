#!/usr/bin/env python3
"""
Manual operator entrypoint for one self-tuning attempt cycle.

Design: docs/SELF_TUNING_IMPLEMENTATION_PLAN.md Phase 4 / Phase 8.

This is intentionally a manual script, not a cron job or an orchestrator
call site -- Phase 4's exit criteria require a human to read the audit
trail after each attempt during the shadow-mode soak period.
AutoTuningScheduler (src/tuning/scheduler.py) is the automated counterpart
for later, once that soak is reviewed; both share the same
src/tuning/state.py singletons, so a manual run here and the scheduler
write to one audit trail / version store.

Supports all four self-tuning parameter groups:
  - hmm.entropy_threshold / hmm.entropy_scalar_floor   (--trades)
  - risk.slippage_impact_coeff_bps                      (--slippage-samples)
  - features.<vwap_window|ofi_window|atr_window|sharpe_window|volume_zscore_window>
                                                         (--bars, --model-dir)
  - xgboost.<n_estimators|max_depth|learning_rate|subsample|colsample_bytree|
             min_child_weight|reg_alpha|reg_lambda>      (--bars)

Usage:
    uv run python scripts/run_tuning_attempt.py --param hmm.entropy_threshold --trades trades.json
    uv run python scripts/run_tuning_attempt.py --param risk.slippage_impact_coeff_bps --slippage-samples slippage.json
    uv run python scripts/run_tuning_attempt.py --param features.atr_window --bars bars.json --model-dir models/artifacts
    uv run python scripts/run_tuning_attempt.py --param xgboost.max_depth --bars bars.json

File formats:
  trades.json    : JSON list of {"entropy": float, "raw_return": float}
                   (historical closed trades enriched with the regime
                   posterior entropy recorded at signal time).
  slippage.json  : JSON list of {"reference_price", "fill_price", "qty",
                   "adv_20d", "spread_bps", "direction"} -- see
                   src.tuning.backtest_harness.SlippageFillSample.
  bars.json      : JSON list of {"ts", "open", "high", "low", "close",
                   "volume"} (Unix-ms timestamps).

Wiring any of these to a real trade-history / bar-history export is a
separate, later step; this script only consumes the file formats above.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from src.config import Settings, get_settings
from src.features.pipeline import build_feature_matrix
from src.models.trainer import ModelTrainer
from src.tuning.backtest_harness import (
    XGBOOST_INT_FIELDS,
    SlippageFillSample,
    TradeSample,
    run_entropy_threshold_backtest,
    run_feature_window_backtest,
    run_slippage_coeff_backtest,
    run_xgboost_hyperparam_backtest,
)
from src.tuning.bootstrap import (
    FEATURE_WINDOW_FIELDS,
    XGBOOST_HYPERPARAM_FIELDS,
    register_feature_window_param,
    register_hmm_entropy_scalar_floor,
    register_hmm_entropy_threshold,
    register_slippage_impact_coeff,
    register_xgboost_hyperparam_param,
)
from src.tuning.evaluator import MetricComparison
from src.tuning.proposer import Proposal
from src.tuning.registry import TunableParameter
from src.tuning.state import audit_log, parameter_registry, runner, version_store


def _load_trade_samples(path: Path) -> list[TradeSample]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        TradeSample(entropy=float(d["entropy"]), raw_return=float(d["raw_return"])) for d in data
    ]


def _load_slippage_samples(path: Path) -> list[SlippageFillSample]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        SlippageFillSample(
            reference_price=float(d["reference_price"]),
            fill_price=float(d["fill_price"]),
            qty=float(d["qty"]),
            adv_20d=float(d["adv_20d"]),
            spread_bps=float(d["spread_bps"]),
            direction=int(d["direction"]),
        )
        for d in data
    ]


def _load_bars_df(path: Path) -> pd.DataFrame:
    data = json.loads(path.read_text(encoding="utf-8"))
    df = pd.DataFrame(data)
    return df.set_index("ts")[["open", "high", "low", "close", "volume"]].sort_index()


def _register_param(param_name: str, settings: Settings) -> None:
    if parameter_registry.is_registered(param_name):
        return
    if param_name == "hmm.entropy_threshold":
        register_hmm_entropy_threshold(parameter_registry, settings, version_store)
    elif param_name == "hmm.entropy_scalar_floor":
        register_hmm_entropy_scalar_floor(parameter_registry, settings, version_store)
    elif param_name == "risk.slippage_impact_coeff_bps":
        register_slippage_impact_coeff(parameter_registry, settings, version_store)
    elif param_name.startswith("features."):
        field_name = param_name.removeprefix("features.")
        if field_name not in FEATURE_WINDOW_FIELDS:
            raise ValueError(f"unknown feature-window field: {field_name!r}")
        register_feature_window_param(parameter_registry, field_name, settings, version_store)
    elif param_name.startswith("xgboost."):
        field_name = param_name.removeprefix("xgboost.")
        if field_name not in XGBOOST_HYPERPARAM_FIELDS:
            raise ValueError(f"unknown XGBoost hyperparameter field: {field_name!r}")
        register_xgboost_hyperparam_param(parameter_registry, field_name, settings, version_store)
    else:
        raise ValueError(f"no bootstrap registrar for {param_name!r}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--param",
        default="hmm.entropy_threshold",
        help="Parameter to attempt, e.g. hmm.entropy_threshold, "
        "risk.slippage_impact_coeff_bps, features.atr_window, xgboost.max_depth "
        "(default: hmm.entropy_threshold)",
    )
    parser.add_argument("--trades", type=Path, help="Path to trades.json (hmm.* params)")
    parser.add_argument(
        "--slippage-samples", type=Path, help="Path to slippage.json (slippage param)"
    )
    parser.add_argument(
        "--bars", type=Path, help="Path to bars.json (features.* / xgboost.* params)"
    )
    parser.add_argument(
        "--model-dir",
        type=Path,
        help="Directory containing the saved direction model (features.* params only)",
    )
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--timeframe", default="15m")
    args = parser.parse_args()

    settings = get_settings()
    param_name = args.param

    try:
        _register_param(param_name, settings)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if param_name in ("hmm.entropy_threshold", "hmm.entropy_scalar_floor"):
        if args.trades is None:
            print("--trades is required for hmm.* parameters", file=sys.stderr)
            return 2
        samples = _load_trade_samples(args.trades)
        # This script registers only the ONE requested --param per
        # invocation, so the companion hmm.* value is never guaranteed to
        # be in parameter_registry -- read the durable version_store
        # directly (populated regardless of this invocation's registration)
        # instead, falling back to the raw .env default when nothing has
        # been promoted yet. Without this, the "held constant" companion
        # value in this single-parameter evaluation would silently ignore
        # any prior promotion of that companion parameter.
        champion_threshold = (
            version_store.current("hmm.entropy_threshold").value
            if version_store.has_versions("hmm.entropy_threshold")
            else settings.hmm.entropy_threshold
        )
        champion_floor = (
            version_store.current("hmm.entropy_scalar_floor").value
            if version_store.has_versions("hmm.entropy_scalar_floor")
            else settings.hmm.entropy_scalar_floor
        )

        def evaluate(param: TunableParameter, proposal: Proposal) -> list[MetricComparison]:
            # Evaluates the ACTUAL proposed challenger value, produced by the
            # runner's proposer immediately before this callback is invoked --
            # not a value chosen independently by this script.
            if param_name == "hmm.entropy_threshold":
                return run_entropy_threshold_backtest(
                    samples,
                    champion_threshold=proposal.champion_value,
                    champion_floor=champion_floor,
                    challenger_threshold=proposal.challenger_value,
                    challenger_floor=champion_floor,
                    features_cfg=settings.features,
                )
            return run_entropy_threshold_backtest(
                samples,
                champion_threshold=champion_threshold,
                champion_floor=proposal.champion_value,
                challenger_threshold=champion_threshold,
                challenger_floor=proposal.challenger_value,
                features_cfg=settings.features,
            )

        primary_metric = "oos_sharpe"

    elif param_name == "risk.slippage_impact_coeff_bps":
        if args.slippage_samples is None:
            print(
                "--slippage-samples is required for risk.slippage_impact_coeff_bps",
                file=sys.stderr,
            )
            return 2
        slippage_samples = _load_slippage_samples(args.slippage_samples)

        def evaluate(param: TunableParameter, proposal: Proposal) -> list[MetricComparison]:
            return run_slippage_coeff_backtest(
                slippage_samples,
                champion_coeff=proposal.champion_value,
                challenger_coeff=proposal.challenger_value,
                features_cfg=settings.features,
            )

        primary_metric = "slippage_prediction_accuracy"

    elif param_name.startswith("features."):
        if args.bars is None or args.model_dir is None:
            print(
                "--bars and --model-dir are required for features.* parameters",
                file=sys.stderr,
            )
            return 2
        field_name = param_name.removeprefix("features.")
        bars_df = _load_bars_df(args.bars)
        try:
            direction_model = ModelTrainer.load_direction(
                args.model_dir, args.symbol, args.timeframe
            )
        except FileNotFoundError:
            print(
                f"no direction model found at {args.model_dir} for "
                f"{args.symbol}/{args.timeframe}",
                file=sys.stderr,
            )
            return 2

        def evaluate(param: TunableParameter, proposal: Proposal) -> list[MetricComparison]:
            return run_feature_window_backtest(
                bars_df,
                field_name=field_name,
                champion_window=max(2, round(proposal.champion_value)),
                challenger_window=max(2, round(proposal.challenger_value)),
                direction_model=direction_model,
                features_cfg=settings.features,
            )

        primary_metric = "oos_sharpe"

    elif param_name.startswith("xgboost."):
        if args.bars is None:
            print("--bars is required for xgboost.* parameters", file=sys.stderr)
            return 2
        field_name = param_name.removeprefix("xgboost.")
        bars_df = _load_bars_df(args.bars)
        fm = build_feature_matrix(bars_df, cfg=settings.features)

        def evaluate(param: TunableParameter, proposal: Proposal) -> list[MetricComparison]:
            champion_value: float = proposal.champion_value
            challenger_value: float = proposal.challenger_value
            if field_name in XGBOOST_INT_FIELDS:
                champion_value = round(champion_value)
                challenger_value = round(challenger_value)
            return run_xgboost_hyperparam_backtest(
                fm,
                field_name=field_name,
                champion_value=champion_value,
                challenger_value=challenger_value,
                base_xgb_cfg=settings.xgboost,
                symbol=args.symbol,
                timeframe=args.timeframe,
                feature_cfg=settings.features,
            )

        primary_metric = "oos_sharpe"

    else:
        print(f"no backtest harness wired for {param_name!r}", file=sys.stderr)
        return 2

    result = runner.attempt(param_name, evaluate, primary_metric=primary_metric)

    print(f"attempted={result.attempted} accepted={result.accepted} promoted={result.promoted}")
    print(f"challenger_value={result.challenger_value}")
    print(f"reasons={list(result.reasons)}")
    print(f"audit_log_path={audit_log.path}")
    print(f"version_store_path={version_store.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
