"""CryptoBERT pipeline paths, with the model faked rather than downloaded.

CI used to reach 90%+ on src/features/nlp.py only because the runner had a
route out and actually pulled ElKulako/cryptobert from HuggingFace mid-test.
With the network guard in place that download fails, the pipeline records
itself unavailable, and everything past the guard went uncovered. These tests
inject a fake `transformers` (real torch, fake weights) so the loaded path is
exercised without fetching a 110M-parameter model.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import patch

import numpy as np
import pytest


torch = pytest.importorskip("torch")


_HIDDEN = 768


class _FakeTokenizer:
    def __call__(self, texts, padding=True, truncation=True, max_length=256, return_tensors="pt"):
        batch, seq = len(texts), 5
        return {
            "input_ids": torch.ones(batch, seq, dtype=torch.long),
            "attention_mask": torch.ones(batch, seq),
        }


class _FakeOutput:
    def __init__(self, hidden):
        self.last_hidden_state = hidden


class _FakeModel:
    def eval(self):
        return self

    def __call__(self, **inputs):
        batch, seq = inputs["input_ids"].shape
        return _FakeOutput(torch.full((batch, seq, _HIDDEN), 0.5))


def _fake_transformers(sentiment_results):
    mod = types.ModuleType("transformers")
    mod.AutoTokenizer = types.SimpleNamespace(from_pretrained=lambda *a, **k: _FakeTokenizer())
    mod.AutoModel = types.SimpleNamespace(from_pretrained=lambda *a, **k: _FakeModel())
    mod.pipeline = lambda *a, **k: lambda texts, truncation=True: sentiment_results
    return mod


@pytest.fixture(autouse=True)
def _fresh_pipeline():
    """The pipeline caches itself for the process lifetime."""
    from src.features import nlp

    nlp._CryptoBERTPipeline._instance = None
    yield
    nlp._CryptoBERTPipeline._instance = None


def _encode(texts, sentiment_results):
    """One encode against a freshly built pipeline.

    The pipeline caches itself for the process lifetime, so a test that
    encodes twice would otherwise get the first call's sentiment head back.
    """
    from src.features import nlp

    nlp._CryptoBERTPipeline._instance = None
    with patch.dict(sys.modules, {"transformers": _fake_transformers(sentiment_results)}):
        return nlp.get_nlp_features(texts)


def test_a_model_that_will_not_load_yields_neutral_features():
    """No network, no model — the caller still gets a usable, zeroed answer."""
    from src.features.nlp import get_nlp_features

    broken = types.ModuleType("transformers")

    def _raise(*_a, **_k):
        raise OSError("no route to huggingface.co")

    broken.AutoTokenizer = types.SimpleNamespace(from_pretrained=_raise)
    broken.AutoModel = types.SimpleNamespace(from_pretrained=_raise)
    broken.pipeline = _raise

    with patch.dict(sys.modules, {"transformers": broken}):
        feats = get_nlp_features(["bitcoin rips"])

    assert feats.sentiment_score == 0.0
    assert feats.confidence == 0.0
    assert feats.embedding.shape == (128,)
    assert not feats.embedding.any()


def test_an_empty_text_list_short_circuits():
    feats = _encode([], [])
    assert feats.sentiment_score == 0.0
    assert not feats.embedding.any()


def test_a_loaded_model_projects_to_128_dimensions():
    feats = _encode(
        ["bitcoin rips", "eth lags"],
        [[{"label": "Positive", "score": 0.9}, {"label": "Negative", "score": 0.1}]],
    )

    assert feats.embedding.shape == (128,)
    assert feats.embedding.dtype == np.float32
    assert np.isfinite(feats.embedding).all()
    assert feats.embedding.any()  # a real projection, not the zero fallback


def test_positive_and_negative_labels_set_the_sentiment_sign():
    bullish = _encode(["up"], [[{"label": "Positive", "score": 1.0}]])
    bearish = _encode(["down"], [[{"label": "Negative", "score": 1.0}]])

    assert bullish.sentiment_score == pytest.approx(1.0)
    assert bearish.sentiment_score == pytest.approx(-1.0)
    assert bullish.confidence == pytest.approx(1.0)


def test_the_bullish_and_bearish_label_names_are_accepted_too():
    """The head's labels vary by checkpoint; both vocabularies must work."""
    feats = _encode(
        ["moon"], [[{"label": "Bullish", "score": 0.8}, {"label": "Bearish", "score": 0.2}]]
    )

    assert feats.sentiment_score == pytest.approx(0.6)


def test_a_neutral_reading_scores_zero_but_stays_confident():
    feats = _encode(["flat"], [[{"label": "Neutral", "score": 1.0}]])

    assert feats.sentiment_score == pytest.approx(0.0)
    assert feats.confidence == pytest.approx(1.0)


def test_the_pipeline_is_built_once_and_reused():
    from src.features import nlp

    with patch.dict(
        sys.modules, {"transformers": _fake_transformers([[{"label": "Neutral", "score": 1.0}]])}
    ):
        first = nlp._CryptoBERTPipeline.instance()
        second = nlp._CryptoBERTPipeline.instance()

    assert first is second
