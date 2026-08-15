"""
Risk/sizing module — actual professional formulas, not placeholders.

1. Probability calibration: raw classifier outputs (XGBoost predict_proba)
   are NOT true probabilities out of the box — they're often over/under-
   confident. Platt scaling (logistic calibration) or isotonic regression
   corrects this. Uncalibrated probabilities fed into Kelly will oversize
   bets — this is a real, common, costly mistake.

2. Kelly Criterion (Kelly, 1956; popularized in trading by Ed Thorp):
   f* = (b*p - q) / b
   where:
     f* = fraction of capital to wager
     b  = net odds received (payoff ratio, win_amount / loss_amount)
     p  = probability of winning
     q  = 1 - p (probability of losing)

   Full Kelly is provably capital-growth-optimal in the infinite-horizon
   limit IF p and b are known exactly — but real-world probability
   estimates carry error, so professional practice (Thorp included) is to
   use FRACTIONAL Kelly (typically 1/4 to 1/2) to reduce variance from
   estimation error. This module defaults to quarter-Kelly.
"""

import numpy as np
from sklearn.calibration import CalibratedClassifierCV


def calibrate_probabilities(model, X_train, y_train, X_test, method: str = "isotonic"):
    """
    Wraps a fitted model with probability calibration.
    method: 'isotonic' (non-parametric, needs more data) or 'sigmoid' (Platt scaling).
    """
    calibrated = CalibratedClassifierCV(model, method=method, cv=3)
    calibrated.fit(X_train, y_train)
    calibrated_probs = calibrated.predict_proba(X_test)[:, 1]
    return calibrated_probs, calibrated


def kelly_fraction(
    p_win: float, avg_win_pct: float, avg_loss_pct: float, kelly_multiplier: float = 0.25
) -> float:
    """
    Real Kelly formula, fractional (default quarter-Kelly for safety
    against probability estimation error — standard professional practice).

    p_win:        calibrated probability of a winning trade (0-1)
    avg_win_pct:  average % gain on winning trades (positive number, e.g. 0.02 for 2%)
    avg_loss_pct: average % loss on losing trades (positive number, e.g. 0.015 for 1.5%)
    kelly_multiplier: fraction of full Kelly to actually use (0.25 = quarter-Kelly)

    Returns: fraction of capital to risk on this trade (clamped to [0, 0.25]
    as a hard safety ceiling regardless of what the formula outputs — no
    single crypto trade should ever risk more than 25% of capital, formula
    or not).
    """
    if avg_loss_pct <= 0:
        raise ValueError("avg_loss_pct must be a positive number (magnitude of loss)")

    b = avg_win_pct / avg_loss_pct  # net odds
    q = 1 - p_win
    f_star = (b * p_win - q) / b

    f_star = max(0.0, f_star)  # never bet on a negative-edge signal
    f_fractional = f_star * kelly_multiplier
    f_capped = min(f_fractional, 0.25)  # hard safety ceiling

    return f_capped


def confidence_interval_from_ensemble(model_probs: list, z: float = 1.645) -> tuple:
    """
    Given probability outputs from multiple sub-models (or bootstrap
    resamples of one model), returns (mean_prob, lower_90, upper_90).
    z=1.645 corresponds to a 90% confidence interval under a normal
    approximation of the sub-model disagreement.
    """
    probs = np.array(model_probs)
    mean_p = probs.mean()
    se = probs.std(ddof=1) / np.sqrt(len(probs)) if len(probs) > 1 else probs.std()
    return mean_p, max(0.0, mean_p - z * se), min(1.0, mean_p + z * se)


if __name__ == "__main__":
    # Worked example using realistic, not idealized, numbers
    p = 0.56  # calibrated probability of an up-move (realistic per architecture doc ceiling)
    avg_win = 0.018  # 1.8% average win
    avg_loss = 0.016  # 1.6% average loss
    f = kelly_fraction(p, avg_win, avg_loss, kelly_multiplier=0.25)
    print(f"Calibrated p_win: {p}")
    print(f"Full Kelly fraction: {((avg_win/avg_loss)*p - (1-p)) / (avg_win/avg_loss):.4f}")
    print(f"Quarter-Kelly (actually used): {f:.4f} → risk {f*100:.2f}% of capital per trade")

    mean_p, lo, hi = confidence_interval_from_ensemble([0.58, 0.54, 0.61, 0.52])
    print(f"\nEnsemble mean probability: {mean_p:.3f}, 90% CI: [{lo:.3f}, {hi:.3f}]")
