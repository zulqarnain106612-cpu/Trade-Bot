"""
Hidden Markov Model regime detector — 3 states.
State 0 = ranging/low-vol
State 1 = trending
State 2 = volatile/crisis

Reference: Hamilton (1989), "A New Approach to the Economic Analysis of
Nonstationary Time Series and the Business Cycle", Econometrica.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from hmmlearn import hmm
import joblib
import os
import structlog

log = structlog.get_logger()

REGIME_LABELS = {0: "ranging", 1: "trending", 2: "volatile"}

class RegimeDetector:
    def __init__(self, n_states: int = 3, model_path: str = "./models/hmm.pkl"):
        self._n_states   = n_states
        self._model_path = model_path
        self._model: hmm.GaussianHMM | None = None

    def _features(self, df: pd.DataFrame) -> np.ndarray:
        """2-feature observation: [log_return, realized_vol]."""
        ret = np.log(df["close"] / df["close"].shift(1)).fillna(0).values
        vol = pd.Series(ret).rolling(20).std().fillna(0).values
        return np.column_stack([ret, vol])

    def fit(self, df: pd.DataFrame) -> "RegimeDetector":
        obs = self._features(df)
        self._model = hmm.GaussianHMM(
            n_components=self._n_states,
            covariance_type="diag",
            n_iter=200,
            random_state=42,
        )
        self._model.fit(obs)
        # Relabel so state with highest mean return variance = volatile (state 2)
        means = self._model.means_[:, 1]  # realized vol column
        order = np.argsort(means)         # ascending vol
        # order[0]=ranging, order[1]=trending, order[2]=volatile
        self._remap = {order[i]: i for i in range(self._n_states)}
        os.makedirs(os.path.dirname(self._model_path), exist_ok=True)
        joblib.dump((self._model, self._remap), self._model_path)
        log.info("regime model fitted and saved", path=self._model_path)
        return self

    def load(self) -> bool:
        if os.path.exists(self._model_path):
            self._model, self._remap = joblib.load(self._model_path)
            log.info("regime model loaded", path=self._model_path)
            return True
        return False

    def predict(self, df: pd.DataFrame) -> pd.Series:
        """Returns Series of regime labels aligned with df.index."""
        if self._model is None:
            raise RuntimeError("RegimeDetector not fitted")
        obs    = self._features(df)
        states = self._model.predict(obs)
        remapped = np.array([self._remap[s] for s in states])
        return pd.Series(
            [REGIME_LABELS[r] for r in remapped],
            index=df.index,
            name="regime",
        )

    def current_regime(self, df: pd.DataFrame, window: int = 50) -> str:
        """Fast regime for the most recent bar."""
        tail = df.tail(window)
        regimes = self.predict(tail)
        return regimes.iloc[-1]

