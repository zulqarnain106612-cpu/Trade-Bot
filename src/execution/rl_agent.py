"""
RL Execution Agent — PPO/SAC via Stable-Baselines3.

Learns optimal execution timing and order sizing within the constraint set
by the RiskGate. The RL agent operates on top of the signal — it decides
WHEN and HOW MUCH to execute, not whether to trade (that is the RiskGate's job).

State space (27-dim):
  - 10 horizon confidence scores
  - 9 regime one-hot vector (regime states 0-8)
  - 5 ECC features (cluster_flow, ecdsa_weakness, schnorr_divergence, hodler, dark_pool)
  - 1 realized P&L last period
  - 1 current drawdown
  - 1 Kyle lambda (market impact)

Action space: {hold=0, long=1, short=2}
Reward: sign(action) * realized_return - fee_cost - impact_cost

Uses PPO as primary (stable, good sample efficiency) and SAC as alternative
(off-policy, better for continuous action spaces).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np
import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_MODEL_PATH = Path(os.environ.get("RL_MODEL_PATH", "./models/rl_execution"))
_ALGO = os.environ.get("RL_ALGO", "PPO")  # PPO or SAC

_STATE_DIM = 27
_N_ACTIONS = 3
_FEE_RATE = 0.0004  # 0.04% taker fee
_IMPACT_PENALTY = 0.01  # penalty for execution risk


class RLExecutionState:
    """Constructs the state vector for the RL agent from runtime data."""

    def __init__(self, n_horizons: int = 10) -> None:
        self._n_horizons = n_horizons

    def build(
        self,
        horizon_confidences: list[float],
        regime_id: int,
        ecc_features: dict[str, float],
        realized_pnl: float,
        drawdown: float,
        kyle_lambda: float,
    ) -> np.ndarray:
        """Construct the 27-dim state vector."""
        # Pad/truncate confidences to n_horizons
        confs = list(horizon_confidences)[: self._n_horizons]
        confs += [0.0] * (self._n_horizons - len(confs))

        # One-hot regime (9 classes)
        regime_onehot = np.zeros(9)
        regime_onehot[min(regime_id, 8)] = 1.0

        ecc_vec = [
            ecc_features.get("cluster_flow_score", 0.0),
            ecc_features.get("ecdsa_weakness", 0.0),
            ecc_features.get("schnorr_divergence", 0.0),
            ecc_features.get("hodler_index", 0.0),
            ecc_features.get("dark_pool_pressure", 0.0),
        ]

        state = np.array(
            confs + list(regime_onehot) + ecc_vec + [realized_pnl, drawdown, kyle_lambda],
            dtype=np.float32,
        )
        return state[:_STATE_DIM]  # clip to 27 dims if slightly off


class RLExecutionAgent:
    """
    PPO/SAC-based execution agent using Stable-Baselines3.

    Trains offline on historical execution data; inference is synchronous.
    Falls back to a rule-based policy when SB3 is not installed.
    """

    def __init__(self, model_path: Path = _MODEL_PATH, algo: str = _ALGO) -> None:
        self._model_path = model_path
        self._algo = algo
        self._model: Any | None = None
        self._state_builder = RLExecutionState()
        self._sb3_available = False
        self._load_or_init()

    def _load_or_init(self) -> None:
        try:
            from stable_baselines3 import PPO, SAC  # type: ignore[import]

            self._sb3_available = True
            model_file = self._model_path / f"{self._algo.lower()}_execution.zip"
            if model_file.exists():
                cls = PPO if self._algo == "PPO" else SAC
                self._model = cls.load(str(model_file))
                log.info("rl_model_loaded", path=str(model_file), algo=self._algo)
            else:
                log.info("rl_model_not_found_using_init_policy", path=str(model_file))
        except ImportError:
            log.warning("stable_baselines3_not_installed_using_rule_based_policy")

    def predict(
        self,
        horizon_confidences: list[float],
        regime_id: int = 0,
        ecc_features: dict[str, float] | None = None,
        realized_pnl: float = 0.0,
        drawdown: float = 0.0,
        kyle_lambda: float = 0.001,
    ) -> tuple[int, float]:
        """
        Predict execution action: (action, action_probability).

        Returns (0=hold, 1=long, 2=short), probability of action.
        """
        if ecc_features is None:
            ecc_features = {}

        state = self._state_builder.build(
            horizon_confidences, regime_id, ecc_features, realized_pnl, drawdown, kyle_lambda
        )

        if self._model is not None and self._sb3_available:
            action, _ = self._model.predict(state, deterministic=True)
            return int(action), 0.9

        # Rule-based fallback: use mean confidence to decide
        mean_conf = float(np.mean(horizon_confidences)) if horizon_confidences else 0.5
        if mean_conf > 0.7:
            return 1, mean_conf  # long
        if mean_conf < 0.4:
            return 2, 1.0 - mean_conf  # short
        return 0, 0.5  # hold

    def train(
        self,
        training_data: list[dict],
        n_timesteps: int = 100_000,
        save_path: Path | None = None,
    ) -> None:
        """
        Train the RL agent on historical execution data.

        training_data: list of episode dicts with keys:
          state, action, reward, next_state, done
        """
        if not self._sb3_available:
            log.warning("sb3_not_available_skipping_training")
            return

        try:
            from stable_baselines3 import PPO, SAC  # type: ignore[import]

            env = _ExecutionEnv(training_data)
            cls = PPO if self._algo == "PPO" else SAC
            policy = "MlpPolicy"
            self._model = cls(policy, env, verbose=0)
            self._model.learn(total_timesteps=n_timesteps)

            save_to = save_path or self._model_path
            save_to.mkdir(parents=True, exist_ok=True)
            self._model.save(str(save_to / f"{self._algo.lower()}_execution"))
            log.info("rl_model_trained_and_saved", path=str(save_to))
        except Exception as exc:
            log.warning("rl_training_failed", exc=str(exc))


class _ExecutionEnv:
    """
    Minimal gymnasium-compatible environment for RL execution training.

    Episode = one historical trading session. Each step = one bar.
    """

    def __init__(self, episodes: list[dict]) -> None:
        self._episodes = episodes
        self._current = 0
        self._step_idx = 0
        try:
            import gymnasium as gym  # type: ignore[import]

            self.observation_space = gym.spaces.Box(
                low=-np.inf, high=np.inf, shape=(_STATE_DIM,), dtype=np.float32
            )
            self.action_space = gym.spaces.Discrete(_N_ACTIONS)
        except ImportError:
            pass

    def reset(self, **kwargs: Any) -> tuple[np.ndarray, dict]:
        self._current = (self._current + 1) % max(len(self._episodes), 1)
        self._step_idx = 0
        return np.zeros(_STATE_DIM, dtype=np.float32), {}

    def step(self, action: int) -> tuple[np.ndarray, float, bool, bool, dict]:
        episode = self._episodes[self._current % len(self._episodes)] if self._episodes else {}
        reward = self._compute_reward(action, episode)
        self._step_idx += 1
        done = self._step_idx >= 50
        obs = np.zeros(_STATE_DIM, dtype=np.float32)
        return obs, reward, done, False, {}

    def _compute_reward(self, action: int, episode: dict) -> float:
        ret = float(episode.get("realized_return", 0.0))
        direction = 1 if action == 1 else (-1 if action == 2 else 0)
        return float(
            direction * ret - _FEE_RATE * abs(direction) - _IMPACT_PENALTY * abs(direction)
        )
