"""
GaussianHMM regime detector — Hamilton (1989) 3-state switching model.

States:
  0 = ranging   (low volatility, mean-reverting)
  1 = trending  (directional, sustained momentum)
  2 = volatile  (high volatility, risk-off — blocks new positions)

The detector:
  1. Trains a GaussianHMM on a feature observation matrix
  2. Assigns canonical state labels by volatility ranking
     (lowest vol → ranging, highest → volatile) so state indices
     are stable across re-trains regardless of HMM initialisation order
  3. Exposes predict_current() for live regime classification
  4. Persists / loads the fitted model via joblib

Authority:
  Hamilton (1989) "A New Approach to the Economic Analysis of
    Nonstationary Time Series and the Business Cycle", Econometrica 57(2).
  López de Prado (2018) AFML — regime as a signal gate (Ch.17).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import joblib
import numpy as np
import pandas as pd
import structlog
from hmmlearn.hmm import GaussianHMM
from sklearn.preprocessing import StandardScaler

from src.config import (
    REGIME_RANGING,
    REGIME_TRENDING,
    REGIME_VOLATILE,
    HMMSettings,
)
from src.tuning.live_overrides import effective_hmm_settings


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# HMM observation features — subset of the full feature matrix.
# Using vol ratio, frac diff, ATR momentum gives the HMM the clearest
# signal to separate the three regimes without redundancy.
# ---------------------------------------------------------------------------

HMM_FEATURE_COLS: Final[list[str]] = [
    "frac_diff",
    "realized_vol_ratio",
    "atr_momentum",
    "rolling_sharpe",
    "volume_zscore",
    "garch_vol_forecast",
]

_MODEL_FILENAME: Final[str] = "hmm_{symbol}_{timeframe}.joblib"
_MANIFEST_SUFFIX: Final[str] = ".sha256"


def _write_manifest(path: Path) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix(_MANIFEST_SUFFIX).write_text(json.dumps({"file": path.name, "sha256": digest}))


def _verify_manifest(path: Path) -> None:
    import hmac as _hmac

    manifest_path = path.with_suffix(_MANIFEST_SUFFIX)
    if not manifest_path.exists():
        raise RuntimeError(f"HMM model manifest missing for {path}. Re-train to regenerate.")
    manifest = json.loads(manifest_path.read_text())
    expected = manifest.get("sha256", "")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if not _hmac.compare_digest(actual.encode(), expected.encode()):
        raise RuntimeError(
            f"HMM model integrity check FAILED for {path}. "
            "File may be tampered. Re-train to replace."
        )


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegimePrediction:
    """
    Single-bar regime prediction.

    state       : canonical regime index (0=ranging, 1=trending, 2=volatile)
    prob_ranging   : posterior probability of ranging state
    prob_trending  : posterior probability of trending state
    prob_volatile  : posterior probability of volatile state
    is_volatile    : True when state == REGIME_VOLATILE — used by risk gate
    entropy        : GAP-002 — normalized Shannon entropy of the posterior
                     distribution, in [0, 1]. 0 = certain (one state has
                     prob≈1), 1 = maximal uncertainty (uniform over states).
                     Normalized by log(n_states) so the range is fixed
                     regardless of n_components.
    """

    state: int
    prob_ranging: float
    prob_trending: float
    prob_volatile: float
    entropy: float = 0.0

    @property
    def is_volatile(self) -> bool:
        return self.state == REGIME_VOLATILE

    @property
    def dominant_prob(self) -> float:
        return max(self.prob_ranging, self.prob_trending, self.prob_volatile)

    @property
    def confidence(self) -> float:
        """GAP-002: confidence = 1 - entropy. 1.0 = fully confident."""
        return 1.0 - self.entropy

    def position_scalar(self, cfg: HMMSettings | None = None) -> float:
        """
        GAP-002: continuous entropy-based position-size scalar in
        [entropy_scalar_floor, 1.0].

        Below `entropy_threshold`, full confidence -> scalar = 1.0.
        Above it, scalar decays linearly from 1.0 to `entropy_scalar_floor`
        as entropy rises from threshold to 1.0 (max uncertainty). A linear
        ramp (rather than a hard step at the threshold) avoids a
        discontinuous 2x position-size jump from an infinitesimal entropy
        change at the boundary.
        """
        if cfg is None:
            cfg = effective_hmm_settings()
        threshold = cfg.entropy_threshold
        floor = cfg.entropy_scalar_floor
        if self.entropy <= threshold:
            return 1.0
        span = 1.0 - threshold
        if span <= 0.0:
            return floor
        t = min(1.0, (self.entropy - threshold) / span)
        return 1.0 - t * (1.0 - floor)

    def as_dict(self) -> dict[str, object]:
        return {
            "regime_state": self.state,
            "prob_ranging": round(self.prob_ranging, 6),
            "prob_trending": round(self.prob_trending, 6),
            "prob_volatile": round(self.prob_volatile, 6),
            "is_volatile": self.is_volatile,
            "entropy": round(self.entropy, 6),
            "confidence": round(self.confidence, 6),
        }


# ---------------------------------------------------------------------------
# State labeling — Hamilton (1989) stable canonical assignment
# ---------------------------------------------------------------------------


def _assign_canonical_states(
    model: GaussianHMM,
) -> dict[int, int]:
    """
    Map raw HMM state indices to canonical {ranging=0, trending=1, volatile=2}.

    Hamilton (1989) HMM states have no guaranteed ordering — two runs on
    the same data may swap indices.  We break this degeneracy by sorting
    states on their mean realised-volatility observation (column 1 of means,
    which is 'realized_vol_ratio').

    Ranking by vol_ratio mean:
      lowest  → REGIME_RANGING   (0)
      middle  → REGIME_TRENDING  (1)
      highest → REGIME_VOLATILE  (2)

    Returns
    -------
    dict mapping raw HMM state index → canonical regime constant.
    """
    # means shape: (n_components, n_features)
    # col 1 = realized_vol_ratio (index 1 in HMM_FEATURE_COLS)
    vol_col: int = HMM_FEATURE_COLS.index("realized_vol_ratio")
    vol_means: np.ndarray = model.means_[:, vol_col]
    sorted_indices: np.ndarray = np.argsort(vol_means)  # ascending vol

    canonical = [REGIME_RANGING, REGIME_TRENDING, REGIME_VOLATILE]
    return {int(raw_idx): canonical[rank] for rank, raw_idx in enumerate(sorted_indices)}


# ---------------------------------------------------------------------------
# RegimeDetector
# ---------------------------------------------------------------------------


class RegimeDetector:
    """
    GaussianHMM 3-state regime detector.

    Lifecycle::

        detector = RegimeDetector()
        detector.fit(feature_df)  # train on historical features
        pred = detector.predict_current(feature_df.iloc[-lookback:])
        detector.save(model_dir)  # persist
        detector.load(model_dir, symbol, timeframe)  # restore

    Parameters
    ----------
    symbol    : trading symbol (e.g. "BTC/USDT") — used for model filename
    timeframe : bar timeframe string (e.g. "15m")
    cfg       : HMMSettings; loaded from global config if None
    """

    def __init__(
        self,
        symbol: str,
        timeframe: str,
        cfg: HMMSettings | None = None,
    ) -> None:
        self._symbol = symbol
        self._timeframe = timeframe
        self._cfg: HMMSettings = cfg or effective_hmm_settings()
        self._model: GaussianHMM | None = None
        self._scaler: StandardScaler | None = None
        self._state_map: dict[int, int] = {}  # raw HMM index → canonical
        self._fitted: bool = False
        self._train_hash: str = ""
        # M-08: initialise explicitly so mypy and predict_current() don't need getattr fallback
        self._convergence_failed: bool = False
        self._log = log.bind(
            component="regime_detector",
            symbol=symbol,
            timeframe=timeframe,
        )

    # ------------------------------------------------------------------
    # Fit
    # ------------------------------------------------------------------

    def fit(
        self,
        features: pd.DataFrame,
        lengths: list[int] | None = None,
    ) -> RegimeDetector:
        """
        Fit GaussianHMM on a feature DataFrame.

        Parameters
        ----------
        features : DataFrame containing at least HMM_FEATURE_COLS columns,
                   rows ordered ascending in time, no NaN values.
        lengths  : optional list of sequence lengths for multiple episodes
                   (passed directly to hmmlearn).  If None, treated as a
                   single sequence.

        Returns
        -------
        self — allows method chaining.

        Raises
        ------
        ValueError : if required feature columns are missing or data has NaN.
        """
        missing = set(HMM_FEATURE_COLS) - set(features.columns)
        if missing:
            raise ValueError(f"HMM fit: missing observation columns {missing}")

        # H-11: enforce exactly 3 states
        cfg = self._cfg
        if cfg.n_components != 3:
            raise ValueError(
                f"RegimeDetector requires exactly n_components=3 "
                f"(ranging/trending/volatile). Got n_components={cfg.n_components}. "
                "Set HMM_N_COMPONENTS=3 or update the canonical state mapping."
            )

        # M-14: prevent fit() on an already-fitted instance — concurrency hazard.
        # Always create a new RegimeDetector() for retraining.
        if self._fitted:
            raise RuntimeError(
                "RegimeDetector.fit() called on an already-fitted instance. "
                "Create a new RegimeDetector() for retraining to prevent "
                "concurrency issues with predict_current()."
            )

        obs_df = features[HMM_FEATURE_COLS].copy()

        if obs_df.isna().any().any():
            raise ValueError(
                f"HMM fit: observation matrix contains NaN — "
                f"drop NaN rows before calling fit(). "
                f"NaN count per col: {obs_df.isna().sum().to_dict()}"
            )

        n = len(obs_df)
        if n < cfg.n_components * 20:
            raise ValueError(f"HMM fit: need at least {cfg.n_components * 20} rows, " f"got {n}")

        # Scale to zero-mean unit-variance — HMM diagonal / full covariance
        # is sensitive to feature magnitude differences
        self._scaler = StandardScaler()
        X: np.ndarray = self._scaler.fit_transform(obs_df.to_numpy(dtype=np.float64))

        lengths_arg = lengths or [len(X)]

        # VUL-025: Multi-init HMM — run HMM_N_INIT fits with different seeds,
        # keep the one with the highest log-likelihood to avoid local optima.
        # Fixed random_state=42 would consistently converge to the same poor
        # local optimum on certain market regimes.
        _HMM_N_INIT: int = getattr(cfg, "n_init", 5)
        # VF-020: guard against n_init=0 which would leave best_model=None
        # and raise a misleading RuntimeError at the score check below.
        if _HMM_N_INIT < 1:
            raise ValueError(
                f"HMM_N_INIT must be >= 1, got {_HMM_N_INIT}. "
                "Set HMM_N_INIT=5 (or higher) in environment."
            )
        best_model = None
        best_score: float = float("-inf")

        t0 = time.perf_counter()
        for seed in range(_HMM_N_INIT):
            candidate = GaussianHMM(
                n_components=cfg.n_components,
                covariance_type=cfg.covariance_type,
                n_iter=cfg.n_iter,
                tol=cfg.tol,
                random_state=cfg.random_state + seed,
                verbose=False,
            )
            candidate.fit(X, lengths=lengths_arg)
            try:
                score = candidate.score(X, lengths=lengths_arg)
            except Exception:
                continue
            if score > best_score:
                best_score = score
                best_model = candidate

        elapsed = time.perf_counter() - t0

        if best_model is None:
            raise RuntimeError("HMM multi-init: all candidate fits failed to score.")

        self._model = best_model
        converged = bool(best_model.monitor_.converged)

        # VUL-025: If the best model still did not converge, default regime
        # to VOLATILE so downstream gates block new positions.
        if not converged:
            # SCAN2-009: REGIME_VOLATILE already at module level — inline import removed
            self._log.error(
                "hmm.fit_not_converged",
                best_score=round(best_score, 4),
                n_init=_HMM_N_INIT,
                action="defaulting_regime_to_VOLATILE_until_retrain",
            )
            self._convergence_failed = True
        else:
            self._convergence_failed = False

        self._state_map = _assign_canonical_states(self._model)
        self._fitted = True

        # H-07: SHA-256 replaces MD5 — more collision-resistant; 16 hex chars = 64 bits
        self._train_hash = hashlib.sha256(obs_df.to_numpy(dtype=np.float64).tobytes()).hexdigest()[
            :16
        ]

        self._log.info(
            "hmm.fitted",
            n_rows=n,
            n_iter=self._model.monitor_.iter,
            converged=bool(self._model.monitor_.converged),
            elapsed_s=round(elapsed, 3),
            state_map=self._state_map,
            train_hash=self._train_hash,
        )
        return self

    # ------------------------------------------------------------------
    # Predict sequence
    # ------------------------------------------------------------------

    def predict_sequence(
        self,
        features: pd.DataFrame,
    ) -> pd.Series:
        """
        Decode the most likely hidden state sequence via Viterbi.

        Returns canonical regime labels (0/1/2) aligned to features.index.
        """
        model, _ = self._require_fitted()
        X = self._transform(features)
        raw_states: np.ndarray = model.predict(X)
        canonical = np.array([self._state_map[int(s)] for s in raw_states])
        return pd.Series(canonical, index=features.index, dtype=np.int8, name="regime")

    def predict_proba_sequence(
        self,
        features: pd.DataFrame,
    ) -> pd.DataFrame:
        """
        Return posterior state probabilities for every bar via forward-backward.

        Returns DataFrame with columns ['prob_ranging', 'prob_trending', 'prob_volatile']
        aligned to features.index.  Columns are reordered to canonical indices.
        """
        model, _ = self._require_fitted()
        X = self._transform(features)
        posteriors: np.ndarray = model.predict_proba(X)

        # Reorder columns from raw HMM order to canonical
        canonical_posteriors = np.zeros_like(posteriors)
        for raw_idx, canon_idx in self._state_map.items():
            canonical_posteriors[:, canon_idx] = posteriors[:, raw_idx]

        cols = ["prob_ranging", "prob_trending", "prob_volatile"]
        return pd.DataFrame(canonical_posteriors, index=features.index, columns=cols)

    # ------------------------------------------------------------------
    # Single-bar inference — hot path for live trading
    # ------------------------------------------------------------------

    def predict_current(
        self,
        history: pd.DataFrame,
        lookback: int = 100,
    ) -> RegimePrediction:
        """
        Predict regime for the most recent bar in history.

        Runs the full forward-backward algorithm on the last `lookback` bars
        and returns the posterior distribution of the final bar.

        Parameters
        ----------
        history  : DataFrame with HMM_FEATURE_COLS, most-recent bar last.
                   Must have at least `lookback` non-NaN rows.
        lookback : number of bars to feed into the HMM for context.
                   More bars → more accurate smoothed posterior.
                   100 bars is a practical balance of accuracy vs latency.

        Returns
        -------
        RegimePrediction for the last bar.

        Raises
        ------
        ValueError : if fitted model is unavailable or data is insufficient.
        """
        model, _ = self._require_fitted()

        # VUL-025: If the last fit did not converge, return VOLATILE prediction
        # so the regime gate blocks all new positions until a successful retrain.
        if getattr(self, "_convergence_failed", False):
            # SCAN2-009: REGIME_VOLATILE imported at module level — no per-call inline import
            # VUL-REGIME-001: RegimePrediction is a frozen dataclass with exactly 4 fields;
            # do not pass symbol/timeframe kwargs — they don't exist on the dataclass.
            return RegimePrediction(
                state=REGIME_VOLATILE,
                prob_ranging=0.0,
                prob_trending=0.0,
                prob_volatile=1.0,
                # GAP-002: a non-convergent HMM has no meaningful posterior;
                # report entropy=0.0 since prob_volatile=1.0 is a degenerate
                # (zero-entropy) distribution. The VOLATILE state assignment
                # itself is what blocks new positions in this fail-safe path,
                # not the entropy gate.
                entropy=0.0,
            )

        missing = set(HMM_FEATURE_COLS) - set(history.columns)
        if missing:
            raise ValueError(f"predict_current: missing columns {missing}")

        obs_df = history[HMM_FEATURE_COLS].dropna()
        if len(obs_df) < self._cfg.n_components * 5:
            raise ValueError(
                f"predict_current: need at least {self._cfg.n_components * 5} "
                f"non-NaN rows, got {len(obs_df)}"
            )

        window = obs_df.iloc[-lookback:]
        X = self._transform(window)
        posteriors: np.ndarray = model.predict_proba(X)

        last_posterior: np.ndarray = posteriors[-1]  # shape (n_components,)

        # Map to canonical order
        prob_canonical = np.zeros(self._cfg.n_components)
        for raw_idx, canon_idx in self._state_map.items():
            prob_canonical[canon_idx] = float(last_posterior[raw_idx])

        dominant_raw: int = int(np.argmax(last_posterior))
        state: int = self._state_map[dominant_raw]

        # GAP-002: normalized Shannon entropy of the posterior over the
        # n_components states. Using natural log normalized by log(n_states)
        # fixes the range to [0, 1] regardless of n_components, so the
        # entropy_threshold config value is comparable across configurations.
        # Clip probabilities away from exactly 0 to avoid log(0) = -inf;
        # this has no observable effect since p*log(p) -> 0 as p -> 0.
        _eps = 1e-12
        _p = np.clip(last_posterior, _eps, 1.0)
        _raw_entropy = float(-np.sum(_p * np.log(_p)))
        _max_entropy = float(np.log(self._cfg.n_components))
        entropy = _raw_entropy / _max_entropy if _max_entropy > 0.0 else 0.0
        entropy = max(0.0, min(1.0, entropy))

        pred = RegimePrediction(
            state=state,
            prob_ranging=float(prob_canonical[REGIME_RANGING]),
            prob_trending=float(prob_canonical[REGIME_TRENDING]),
            prob_volatile=float(prob_canonical[REGIME_VOLATILE]),
            entropy=entropy,
        )

        self._log.debug(
            "hmm.predict_current",
            state=state,
            prob_ranging=round(pred.prob_ranging, 4),
            prob_trending=round(pred.prob_trending, 4),
            prob_volatile=round(pred.prob_volatile, 4),
            is_volatile=pred.is_volatile,
            entropy=round(pred.entropy, 4),
        )
        return pred

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, model_dir: str | Path) -> Path:
        """
        Serialize the fitted model + scaler + state_map to disk via joblib.

        Returns path of the saved file.

        Raises
        ------
        RuntimeError : if model has not been fitted.
        """
        model, scaler = self._require_fitted()
        path = Path(model_dir) / _MODEL_FILENAME.format(
            symbol=self._symbol.replace("/", "_"),
            timeframe=self._timeframe,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "model": model,
            "scaler": scaler,
            "state_map": self._state_map,
            "cfg": self._cfg,
            "train_hash": self._train_hash,
            "symbol": self._symbol,
            "timeframe": self._timeframe,
            # VF-018: persist convergence flag so a non-convergent model loaded
            # from disk still defaults regime to VOLATILE in predict_current().
            "convergence_failed": self._convergence_failed,
        }
        joblib.dump(payload, path, compress=3)
        _write_manifest(path)
        self._log.info("hmm.saved", path=str(path), train_hash=self._train_hash)
        return path

    @classmethod
    def load(
        cls,
        model_dir: str | Path,
        symbol: str,
        timeframe: str,
    ) -> RegimeDetector:
        """
        Restore a previously saved RegimeDetector from disk.

        Raises
        ------
        FileNotFoundError : if no saved model exists for symbol/timeframe.
        """
        path = Path(model_dir) / _MODEL_FILENAME.format(
            symbol=symbol.replace("/", "_"),
            timeframe=timeframe,
        )
        if not path.exists():
            raise FileNotFoundError(f"No saved HMM model at {path} — call fit() first.")
        _verify_manifest(path)
        payload: dict = joblib.load(path)
        detector = cls(
            symbol=payload["symbol"],
            timeframe=payload["timeframe"],
            cfg=payload["cfg"],
        )
        detector._model = payload["model"]
        detector._scaler = payload["scaler"]
        detector._state_map = payload["state_map"]
        detector._train_hash = payload["train_hash"]
        detector._fitted = True
        # VF-018: restore convergence flag — defaults False for old payloads without
        # the key (backward compatible), correct for new payloads.
        detector._convergence_failed = bool(payload.get("convergence_failed", False))
        log.info(
            "hmm.loaded",
            path=str(path),
            state_map=detector._state_map,
            train_hash=detector._train_hash,
        )
        return detector

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def regime_statistics(
        self,
        features: pd.DataFrame,
    ) -> pd.DataFrame:
        """
        Return per-regime summary statistics over a feature DataFrame.

        Useful for post-training sanity checks: confirms state separation,
        verifies volatility ordering matches canonical assignment.

        Returns DataFrame indexed by regime name with columns:
          count, pct, mean_vol_ratio, mean_atr_momentum, mean_rolling_sharpe
        """
        self._require_fitted()
        states = self.predict_sequence(features)
        obs = features[HMM_FEATURE_COLS].copy()
        obs["regime"] = states

        rows = []
        name_map = {
            REGIME_RANGING: "ranging",
            REGIME_TRENDING: "trending",
            REGIME_VOLATILE: "volatile",
        }
        total = len(obs)
        for canon_idx, name in name_map.items():
            sub = obs[obs["regime"] == canon_idx]
            if len(sub) == 0:
                rows.append(
                    {
                        "regime": name,
                        "count": 0,
                        "pct": 0.0,
                        "mean_vol_ratio": np.nan,
                        "mean_atr_momentum": np.nan,
                        "mean_rolling_sharpe": np.nan,
                    }
                )
                continue
            rows.append(
                {
                    "regime": name,
                    "count": len(sub),
                    "pct": round(len(sub) / total * 100, 2),
                    "mean_vol_ratio": round(float(sub["realized_vol_ratio"].mean()), 6),
                    "mean_atr_momentum": round(float(sub["atr_momentum"].mean()), 6),
                    "mean_rolling_sharpe": round(float(sub["rolling_sharpe"].mean()), 6),
                }
            )

        return pd.DataFrame(rows).set_index("regime")

    def is_fitted(self) -> bool:
        """Return True if the model has been trained or loaded."""
        return self._fitted

    def state_map(self) -> dict[int, int]:
        """Return a copy of raw HMM index → canonical regime mapping."""
        return dict(self._state_map)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_fitted(self) -> tuple[GaussianHMM, StandardScaler]:
        if not self._fitted or self._model is None or self._scaler is None:
            raise RuntimeError("RegimeDetector is not fitted — call fit() or load() first.")
        return self._model, self._scaler

    def _transform(self, features: pd.DataFrame) -> np.ndarray:
        """Scale observation DataFrame using the fitted StandardScaler."""
        obs = features[HMM_FEATURE_COLS]
        if obs.isna().any().any():
            raise ValueError("Observation matrix contains NaN — drop NaN rows before inference.")
        assert self._scaler is not None
        return self._scaler.transform(obs.to_numpy(dtype=np.float64))
