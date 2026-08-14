"""
Tests for MetaNetworkLoss.

Every defect pinned here shares a property: it produced a loss curve that
descended convincingly while optimising something other than the documented
objective. None of them raised, so the existing forward-pass tests could not
have caught any of them — these exercise the loss and the backward pass.
"""

from __future__ import annotations

import pytest
import torch

from src.fusion.meta_network import HorizonOutput, MetaNetwork, MetaNetworkLoss


def _targets(batch: int = 4) -> dict:
    return {
        "direction_label": torch.zeros(batch, dtype=torch.long),
        "magnitude_y": torch.randn(batch),
        "timing_label": torch.randint(0, 2, (batch,)),
    }


def _model_and_outputs(n_horizons: int = 3, batch: int = 4, d_in: int = 64):
    model = MetaNetwork(n_horizons=n_horizons, d_in=d_in)
    return model, model(torch.randn(batch, d_in))


# ------------------------------------------------------------ logits vs probs


def test_forward_exposes_both_logits_and_probabilities() -> None:
    _, outputs = _model_and_outputs()
    for out in outputs:
        assert out.direction_logits is not None
        assert out.direction_logits.shape == out.direction.shape
        # direction is the softmax of the logits, not a second independent head.
        assert torch.allclose(torch.softmax(out.direction_logits, dim=-1), out.direction)


def test_loss_consumes_logits_not_probabilities() -> None:
    # nn.CrossEntropyLoss applies its own log-softmax. Feeding it the softmaxed
    # head squashes the range and trains a different objective while raising
    # nothing, so the two must not produce the same number.
    _, outputs = _model_and_outputs(n_horizons=1)
    targets = [_targets()]
    loss_fn = MetaNetworkLoss()

    real = loss_fn(outputs, targets)

    squashed = [
        HorizonOutput(
            direction=outputs[0].direction,
            magnitude=outputs[0].magnitude,
            timing=outputs[0].timing,
            direction_logits=outputs[0].direction,  # the old, buggy behaviour
        )
    ]
    assert not torch.isclose(real, loss_fn(squashed, targets))


def test_missing_logits_is_refused_rather_than_falling_back() -> None:
    # A silent fallback to `direction` is exactly the double-softmax this
    # field exists to prevent.
    _, outputs = _model_and_outputs(n_horizons=1)
    stripped = [
        HorizonOutput(
            direction=outputs[0].direction,
            magnitude=outputs[0].magnitude,
            timing=outputs[0].timing,
        )
    ]
    with pytest.raises(ValueError, match="direction_logits"):
        MetaNetworkLoss()(stripped, [_targets()])


# ------------------------------------------------------------ log_s vs log_var


def test_magnitude_is_treated_as_log_std_not_log_variance() -> None:
    # The head emits log_s. GaussianNLLLoss takes a variance, so the loss must
    # use exp(2*log_s). Using exp(log_s) never raises — it just makes every
    # uncertainty the model reports wrong by a square.
    batch = 4
    log_s = torch.full((batch,), 0.5)
    mu = torch.zeros(batch)
    magnitude = torch.stack([mu, log_s], dim=1)
    out = HorizonOutput(
        direction=torch.full((batch, 3), 1 / 3),
        magnitude=magnitude,
        timing=torch.full((batch, 1), 0.5),
        direction_logits=torch.zeros(batch, 3),
    )
    target = {
        "direction_label": torch.zeros(batch, dtype=torch.long),
        "magnitude_y": torch.zeros(batch),
        "timing_label": torch.zeros(batch),
    }
    actual = MetaNetworkLoss()([out], [target])

    nll = torch.nn.GaussianNLLLoss()
    as_log_std = nll(mu, target["magnitude_y"], torch.exp(2.0 * log_s))
    as_log_var = nll(mu, target["magnitude_y"], torch.exp(log_s))

    ce = torch.nn.CrossEntropyLoss()(out.direction_logits, target["direction_label"])
    bce = torch.nn.functional.binary_cross_entropy(
        out.timing.squeeze(-1), target["timing_label"].float()
    )
    assert torch.isclose(actual, ce + as_log_std + bce, atol=1e-5)
    assert not torch.isclose(as_log_std, as_log_var)


def test_extreme_log_s_does_not_overflow() -> None:
    batch = 2
    out = HorizonOutput(
        direction=torch.full((batch, 3), 1 / 3),
        magnitude=torch.stack([torch.zeros(batch), torch.full((batch,), 200.0)], dim=1),
        timing=torch.full((batch, 1), 0.5),
        direction_logits=torch.zeros(batch, 3),
    )
    loss = MetaNetworkLoss()([out], [_targets(batch)])
    assert torch.isfinite(loss)


# ------------------------------------------------------------ target handling


def test_sparse_supervision_via_none_is_supported() -> None:
    _, outputs = _model_and_outputs(n_horizons=3)
    loss = MetaNetworkLoss()(outputs, [_targets(), None, _targets()])
    assert torch.isfinite(loss)


def test_target_length_mismatch_raises_instead_of_truncating() -> None:
    # zip() previously dropped the tail horizons silently: they trained on
    # nothing and the run looked healthy.
    _, outputs = _model_and_outputs(n_horizons=3)
    with pytest.raises(ValueError, match="one target per horizon"):
        MetaNetworkLoss()(outputs, [_targets()])


def test_all_none_targets_raises_rather_than_returning_zero() -> None:
    # Returning 0.0 makes optimizer.step() a no-op that looks like a
    # completed training step.
    _, outputs = _model_and_outputs(n_horizons=2)
    with pytest.raises(ValueError, match="nothing to train on"):
        MetaNetworkLoss()(outputs, [None, None])


# ------------------------------------------------------------ backward pass


def test_loss_backward_reaches_every_head() -> None:
    model, outputs = _model_and_outputs(n_horizons=2)
    loss = MetaNetworkLoss()(outputs, [_targets(), _targets()])
    loss.backward()

    for i in range(2):
        assert model.direction_heads[i].weight.grad is not None
        assert model.magnitude_heads[i].weight.grad is not None
        assert model.timing_heads[i].weight.grad is not None
    assert model.shared[0].weight.grad is not None
    assert torch.isfinite(model.shared[0].weight.grad).all()


def test_accumulator_follows_the_model_dtype() -> None:
    # A bare torch.tensor(0.0) is CPU-float32 and would raise as soon as this
    # trains in any other dtype or device.
    model = MetaNetwork(n_horizons=1, d_in=64).double()
    outputs = model(torch.randn(2, 64, dtype=torch.float64))
    targets = [
        {
            "direction_label": torch.zeros(2, dtype=torch.long),
            "magnitude_y": torch.randn(2, dtype=torch.float64),
            "timing_label": torch.zeros(2, dtype=torch.float64),
        }
    ]
    loss = MetaNetworkLoss()(outputs, targets)
    assert loss.dtype == torch.float64
