#!/usr/bin/env python3
"""
Manual operator entrypoint for one self-tuning attempt cycle.

Design: docs/SELF_TUNING_IMPLEMENTATION_PLAN.md Phase 4.

This is intentionally a manual script, not a cron job or an orchestrator
call site -- Phase 4's exit criteria require a human to read the audit
trail after each attempt during the shadow-mode soak period before this
is ever wired to run unattended. Do not add automated scheduling for
this script until Phase 4's soak criteria (see design doc) are met and
reviewed.

Usage:
    uv run python scripts/run_tuning_attempt.py --trades path/to/trades.json

trades.json format: a JSON list of {"entropy": float, "raw_return": float}
objects -- historical closed trades enriched with the regime posterior
entropy recorded at signal time. Wiring this up to a real trade-history
export is a separate, later step; this script only consumes the format.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.config import get_settings
from src.tuning.backtest_harness import TradeSample, run_entropy_threshold_backtest
from src.tuning.bootstrap import register_hmm_entropy_threshold
from src.tuning.evaluator import MetricComparison
from src.tuning.proposer import Proposal
from src.tuning.registry import TunableParameter
from src.tuning.state import audit_log, parameter_registry, runner, version_store


def _load_trade_samples(path: Path) -> list[TradeSample]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        TradeSample(entropy=float(d["entropy"]), raw_return=float(d["raw_return"])) for d in data
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trades", type=Path, required=True, help="Path to trades.json")
    parser.add_argument(
        "--param",
        default="hmm.entropy_threshold",
        help="Parameter to attempt (default: hmm.entropy_threshold)",
    )
    args = parser.parse_args()

    if not parameter_registry.is_registered(args.param):
        if args.param == "hmm.entropy_threshold":
            register_hmm_entropy_threshold(parameter_registry)
        else:
            print(
                f"Parameter {args.param!r} is not registered and has no known bootstrap.",
                file=sys.stderr,
            )
            return 2

    if args.param != "hmm.entropy_threshold":
        print(f"No backtest harness wired for {args.param!r} yet.", file=sys.stderr)
        return 2

    samples = _load_trade_samples(args.trades)
    settings = get_settings()

    def evaluate(param: TunableParameter, proposal: Proposal) -> list[MetricComparison]:
        # Evaluates the ACTUAL proposed challenger value, produced by the
        # runner's proposer immediately before this callback is invoked --
        # not a value chosen independently by this script.
        return run_entropy_threshold_backtest(
            samples,
            champion_threshold=proposal.champion_value,
            champion_floor=settings.hmm.entropy_scalar_floor,
            challenger_threshold=proposal.challenger_value,
            challenger_floor=settings.hmm.entropy_scalar_floor,
        )

    result = runner.attempt(args.param, evaluate, primary_metric="oos_sharpe")

    print(f"attempted={result.attempted} accepted={result.accepted} promoted={result.promoted}")
    print(f"challenger_value={result.challenger_value}")
    print(f"reasons={list(result.reasons)}")
    print(f"audit_log_path={audit_log.path}")
    print(f"version_store_path={version_store.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
