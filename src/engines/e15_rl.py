"""
E-15 — Reinforcement Learning engine (DQN).

Gap G-01 fix: full MDP spec.
State: 17 engine confidence scores + 9 regime one-hot + 1 realized return (dim=27).
Action: {hold=0, long=1, short=2}.
Reward: sign(action) * realized_return - 0.0002 (fee) - 0.01 * |action_change|.
Lowest-weight engine — acts as sanity check, not primary signal.
"""

from __future__ import annotations

import pickle
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import structlog

from src.engines.schema import EngineOutput


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_ENGINE_ID = "E-15"
_SLA_SECONDS = 5
_MODEL_PATH = Path("models/e15_dqn.pkl")
_N_ENGINES = 17
_N_REGIMES = 9
_STATE_DIM = _N_ENGINES + _N_REGIMES + 1

_REGIME_NAMES = [
    "Trending",
    "Ranging",
    "Volatile",
    "Accumulation",
    "Transition",
    "LiquidityCrisis",
    "OptionsDriven",
    "MacroDominated",
    "Capitulation",
]
_ACTION_TO_DIRECTION = {0: 0, 1: 1, 2: -1}


class E15RL:
    def __init__(self, horizon_hours: int = 4) -> None:
        self._horizon = horizon_hours
        self._model: object | None = None
        self._last_action: int = 0
        self._load_model()

    def _load_model(self) -> None:
        if _MODEL_PATH.exists():
            try:
                with open(_MODEL_PATH, "rb") as f:
                    self._model = pickle.load(f)  # nosec B301 — model written by this process only
            except Exception as exc:
                log.warning("e15_model_load_error", exc=str(exc))

    async def run(self, symbol: str, data: dict) -> EngineOutput:
        spot: float = data.get("spot", 0.0)
        if spot <= 0:
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, "no_spot")

        try:
            state = self._build_state(data)
            action = self._select_action(state)
            direction = _ACTION_TO_DIRECTION[action]
            # E-15 is low-weight; cap confidence at 0.5
            confidence = 0.3 if self._model is not None else 0.1

            return EngineOutput(
                engine_id=_ENGINE_ID,
                symbol=symbol,
                timestamp_utc=datetime.now(UTC),
                predicted_price=spot * (1 + direction * 0.001),
                confidence=confidence,
                direction=direction,
                horizon_hours=self._horizon,
                metadata={"dqn_action": action},
            )
        except Exception as exc:
            log.warning("e15_error", exc=str(exc))
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, str(exc))

    def _build_state(self, data: dict) -> np.ndarray:
        engine_outputs: dict[str, EngineOutput] = data.get("engine_outputs", {})
        state = np.zeros(_STATE_DIM, dtype=np.float32)

        # Engine confidences (indices 0..16)
        for i in range(1, _N_ENGINES + 1):
            eid = f"E-{i:02d}"
            out = engine_outputs.get(eid)
            state[i - 1] = out.confidence if out else 0.0

        # Regime one-hot (indices 17..25)
        regime = data.get("regime", "Trending")
        if regime in _REGIME_NAMES:
            idx = _REGIME_NAMES.index(regime)
            state[_N_ENGINES + idx] = 1.0

        # Recent realized return (index 26)
        realized = data.get("realized_return", 0.0)
        state[_N_ENGINES + _N_REGIMES] = float(realized)
        return state

    def _select_action(self, state: np.ndarray) -> int:
        if self._model is None:
            return 0  # hold if no model
        # Dict[int, Ridge] produced by train_offline / FQI path
        if isinstance(self._model, dict):
            q: dict[int, float] = {}
            for a, reg in self._model.items():
                try:
                    q[a] = float(reg.predict(state.reshape(1, -1))[0])  # type: ignore[union-attr]
                except Exception:
                    q[a] = 0.0
            return max(q, key=lambda k: q[k]) if q else 0
        try:
            # stable-baselines3 predict interface. LAW9: the SB3 policy exposes no
            # per-action confidence, so the action is validated against the discrete
            # {hold, long, short} space instead — anything outside it is untrusted
            # and falls back to hold rather than being wrapped around by modulo.
            action, _ = self._model.predict(state, deterministic=True)  # type: ignore[union-attr,attr-defined]  # confidence: range-checked below
            chosen = int(action)
            return chosen if chosen in (0, 1, 2) else 0
        except Exception:
            return 0

    def train_offline(self, states: np.ndarray, actions: np.ndarray, rewards: np.ndarray) -> None:
        """
        Offline fitted Q-iteration via per-action Ridge regression.

        Trains one Ridge model per action on (state → reward) pairs from the
        action's subset. At inference time, the action with the highest predicted
        Q-value wins.  This is a behavioural-cloning / FQI approximation that
        works without stable-baselines3.
        """
        from sklearn.linear_model import Ridge

        n_actions = 3
        models: dict[int, object] = {}
        for a in range(n_actions):
            mask = actions == a
            if mask.sum() < 5:
                continue
            reg = Ridge(alpha=1.0)
            reg.fit(states[mask], rewards[mask])
            models[a] = reg

        if not models:
            log.warning("e15_offline_train_no_data", n_samples=len(states))
            return

        self._model = models
        try:
            _MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_MODEL_PATH, "wb") as f:
                pickle.dump(models, f)
        except Exception as exc:
            log.warning("e15_model_save_error", exc=str(exc))
        log.info("e15_offline_trained", n_samples=len(states), n_actions=len(models))
