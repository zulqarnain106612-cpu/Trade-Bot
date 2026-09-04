"""Tests for src/execution/rl_agent.py -- PPO/SAC execution agent.

stable_baselines3 and gymnasium are optional deps not installed in CI, so
_load_or_init and _ExecutionEnv naturally exercise their ImportError
branches here; fake modules via sys.modules cover the installed paths.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import numpy as np

from src.execution.rl_agent import (
    _N_ACTIONS,
    _STATE_DIM,
    RLExecutionAgent,
    RLExecutionState,
    _ExecutionEnv,
)


def test_state_build_produces_fixed_state_dim():
    builder = RLExecutionState()
    state = builder.build(
        horizon_confidences=[0.5] * 10,
        regime_id=3,
        ecc_features={"cluster_flow_score": 0.2},
        realized_pnl=1.0,
        drawdown=0.05,
        kyle_lambda=0.001,
    )
    assert state.shape == (_STATE_DIM,)
    assert state.dtype == np.float32


def test_state_build_pads_short_confidence_list():
    builder = RLExecutionState()
    state = builder.build([0.9], 0, {}, 0.0, 0.0, 0.0)
    assert state.shape == (_STATE_DIM,)
    assert state[0] == np.float32(0.9)
    assert state[1] == 0.0  # padded


def test_state_build_truncates_long_confidence_list():
    builder = RLExecutionState()
    state = builder.build([0.1] * 25, 0, {}, 0.0, 0.0, 0.0)
    assert state.shape == (_STATE_DIM,)


def test_state_build_pads_when_fewer_horizons_configured():
    # n_horizons < 10 makes the raw vector shorter than _STATE_DIM, exercising
    # the np.pad branch rather than the truncating one.
    builder = RLExecutionState(n_horizons=3)
    state = builder.build([0.5, 0.5, 0.5], 0, {}, 0.0, 0.0, 0.0)
    assert state.shape == (_STATE_DIM,)


def test_state_build_clamps_regime_id_to_last_onehot_slot():
    builder = RLExecutionState()
    state = builder.build(
        [0.0] * 10, regime_id=99, ecc_features={}, realized_pnl=0.0, drawdown=0.0, kyle_lambda=0.0
    )
    # regime one-hot occupies indices 10..18; index 18 is the clamped slot
    assert state[18] == 1.0


def test_agent_init_without_sb3_falls_back():
    with patch.dict(sys.modules, {"stable_baselines3": None}):
        agent = RLExecutionAgent()
    assert agent._sb3_available is False
    assert agent._model is None


def test_agent_init_with_sb3_but_no_saved_model(tmp_path):
    fake_sb3 = MagicMock()
    with patch.dict(sys.modules, {"stable_baselines3": fake_sb3}):
        agent = RLExecutionAgent(model_path=tmp_path)
    assert agent._sb3_available is True
    assert agent._model is None


def test_agent_init_loads_existing_model(tmp_path):
    model_file = tmp_path / "ppo_execution.zip"
    model_file.write_bytes(b"not-a-real-model")
    fake_sb3 = MagicMock()
    fake_sb3.PPO.load.return_value = "loaded-model"

    with patch.dict(sys.modules, {"stable_baselines3": fake_sb3}):
        agent = RLExecutionAgent(model_path=tmp_path, algo="PPO")
    assert agent._model == "loaded-model"


def test_agent_init_loads_sac_variant(tmp_path):
    model_file = tmp_path / "sac_execution.zip"
    model_file.write_bytes(b"x")
    fake_sb3 = MagicMock()
    fake_sb3.SAC.load.return_value = "sac-model"

    with patch.dict(sys.modules, {"stable_baselines3": fake_sb3}):
        agent = RLExecutionAgent(model_path=tmp_path, algo="SAC")
    assert agent._model == "sac-model"


def test_predict_rule_based_long_on_high_confidence():
    with patch.dict(sys.modules, {"stable_baselines3": None}):
        agent = RLExecutionAgent()
    action, prob = agent.predict([0.9] * 10)
    assert action == 1
    assert prob > 0.7


def test_predict_rule_based_short_on_low_confidence():
    with patch.dict(sys.modules, {"stable_baselines3": None}):
        agent = RLExecutionAgent()
    action, _prob = agent.predict([0.2] * 10)
    assert action == 2


def test_predict_rule_based_hold_in_middle_band():
    with patch.dict(sys.modules, {"stable_baselines3": None}):
        agent = RLExecutionAgent()
    action, prob = agent.predict([0.5] * 10)
    assert action == 0
    assert prob == 0.5


def test_predict_rule_based_empty_confidences_defaults_to_hold():
    with patch.dict(sys.modules, {"stable_baselines3": None}):
        agent = RLExecutionAgent()
    action, _ = agent.predict([])
    assert action == 0


def test_predict_uses_loaded_model_when_available(tmp_path):
    model_file = tmp_path / "ppo_execution.zip"
    model_file.write_bytes(b"x")
    fake_model = MagicMock()
    fake_model.predict.return_value = (2, None)
    fake_sb3 = MagicMock()
    fake_sb3.PPO.load.return_value = fake_model

    with patch.dict(sys.modules, {"stable_baselines3": fake_sb3}):
        agent = RLExecutionAgent(model_path=tmp_path)
        action, prob = agent.predict([0.9] * 10)

    assert action == 2
    assert prob == 0.9


def test_train_skips_when_sb3_unavailable():
    with patch.dict(sys.modules, {"stable_baselines3": None}):
        agent = RLExecutionAgent()
        agent.train([{"realized_return": 0.01}])
    assert agent._model is None


def test_train_happy_path_saves_model(tmp_path):
    fake_model = MagicMock()
    fake_sb3 = MagicMock()
    fake_sb3.PPO.return_value = fake_model
    save_dir = tmp_path / "out"

    with patch.dict(sys.modules, {"stable_baselines3": fake_sb3, "gymnasium": None}):
        agent = RLExecutionAgent(model_path=tmp_path)
        agent.train([{"realized_return": 0.01}], n_timesteps=10, save_path=save_dir)

    fake_model.learn.assert_called_once_with(total_timesteps=10)
    fake_model.save.assert_called_once()
    assert save_dir.exists()


def test_train_swallows_failure(tmp_path):
    fake_sb3 = MagicMock()
    fake_sb3.PPO.side_effect = RuntimeError("training blew up")

    with patch.dict(sys.modules, {"stable_baselines3": fake_sb3, "gymnasium": None}):
        agent = RLExecutionAgent(model_path=tmp_path)
        agent.train([{"realized_return": 0.01}])  # must not raise


def test_execution_env_without_gymnasium_has_no_spaces():
    with patch.dict(sys.modules, {"gymnasium": None}):
        env = _ExecutionEnv([{"realized_return": 0.01}])
    assert not hasattr(env, "observation_space")


def test_execution_env_with_gymnasium_sets_spaces():
    fake_gym = MagicMock()
    with patch.dict(sys.modules, {"gymnasium": fake_gym}):
        env = _ExecutionEnv([{"realized_return": 0.01}])
    assert env.observation_space is fake_gym.spaces.Box.return_value
    fake_gym.spaces.Discrete.assert_called_once_with(_N_ACTIONS)


def test_execution_env_reset_returns_zero_obs():
    with patch.dict(sys.modules, {"gymnasium": None}):
        env = _ExecutionEnv([{"realized_return": 0.01}])
    obs, info = env.reset()
    assert obs.shape == (_STATE_DIM,)
    assert info == {}


def test_execution_env_reset_with_no_episodes_does_not_divide_by_zero():
    with patch.dict(sys.modules, {"gymnasium": None}):
        env = _ExecutionEnv([])
    obs, _ = env.reset()
    assert obs.shape == (_STATE_DIM,)


def test_execution_env_step_returns_reward_and_terminates_after_50():
    with patch.dict(sys.modules, {"gymnasium": None}):
        env = _ExecutionEnv([{"realized_return": 0.10}])
    _obs, reward, done, truncated, _info = env.step(1)
    assert done is False
    assert truncated is False
    assert reward > 0  # long on a positive return, net of fee+impact

    for _ in range(49):
        _, _, done, _, _ = env.step(1)
    assert done is True


def test_execution_env_step_with_no_episodes():
    with patch.dict(sys.modules, {"gymnasium": None}):
        env = _ExecutionEnv([])
    _, reward, _, _, _ = env.step(0)
    assert reward == 0.0  # hold -> direction 0 -> no fee/impact either


def test_compute_reward_short_direction():
    with patch.dict(sys.modules, {"gymnasium": None}):
        env = _ExecutionEnv([])
    reward = env._compute_reward(2, {"realized_return": -0.10})
    # short a falling market -> positive gross, minus fee and impact
    assert reward > 0


def test_compute_reward_hold_has_no_costs():
    with patch.dict(sys.modules, {"gymnasium": None}):
        env = _ExecutionEnv([])
    assert env._compute_reward(0, {"realized_return": 0.5}) == 0.0
