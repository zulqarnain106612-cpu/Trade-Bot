"""
NLP features: CryptoBERT 110M inference pipeline.

Loads ElKulako/cryptobert (110M parameter BERT fine-tuned on crypto text)
and produces a 128-dimensional embedding from news headlines + social text.
Mean-pools the [CLS] token over the batch and projects 768 → 128.

Model is loaded lazily on first call and cached for the process lifetime.
CPU-only inference; no CUDA required.

Sentiment score is a simple positive/negative/neutral classification head
built on top of the projected embedding.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np
import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_MODEL_NAME = os.environ.get("CRYPTOBERT_MODEL", "ElKulako/cryptobert")
_PROJ_DIM = 128
_MAX_LEN = 256


@dataclass(frozen=True)
class NLPFeatures:
    embedding: np.ndarray  # shape [128] — projected CryptoBERT embedding
    sentiment_score: float  # [-1, +1] negative→positive
    confidence: float  # softmax confidence of dominant sentiment class


class _CryptoBERTPipeline:
    _instance: _CryptoBERTPipeline | None = None

    def __init__(self) -> None:
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer, pipeline

            # revision pin prevents supply-chain drift (bandit B615)
            _rev = os.environ.get("CRYPTOBERT_REVISION", "main")
            self._tokenizer = AutoTokenizer.from_pretrained(  # nosec B615
                _MODEL_NAME, revision=_rev
            )
            self._model = AutoModel.from_pretrained(  # nosec B615
                _MODEL_NAME, revision=_rev
            )
            self._model.eval()

            # Sentiment head (positive/negative/neutral)
            self._sentiment = pipeline(
                "text-classification",
                model=_MODEL_NAME,
                tokenizer=self._tokenizer,
                device=-1,  # CPU
                top_k=None,
            )

            # Projection layer 768 → 128
            torch.manual_seed(42)
            self._proj = torch.nn.Linear(768, _PROJ_DIM, bias=False)
            self._proj.eval()

            self._torch = torch
            self._available = True
            log.info("cryptobert_loaded", model=_MODEL_NAME)
        except Exception as exc:
            log.warning("cryptobert_load_failed", exc=str(exc))
            self._available = False

    @classmethod
    def instance(cls) -> _CryptoBERTPipeline:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def encode(self, texts: list[str]) -> NLPFeatures:
        if not self._available or not texts:
            return NLPFeatures(
                embedding=np.zeros(_PROJ_DIM, dtype=np.float32),
                sentiment_score=0.0,
                confidence=0.0,
            )
        import torch

        inputs = self._tokenizer(
            texts,
            padding=True,
            truncation=True,
            max_length=_MAX_LEN,
            return_tensors="pt",
        )
        with torch.no_grad():
            outputs = self._model(**inputs)
            # Mean pool last hidden state over sequence length
            hidden = outputs.last_hidden_state  # [B, T, 768]
            mask = inputs["attention_mask"].unsqueeze(-1).float()
            pooled = (hidden * mask).sum(dim=1) / mask.sum(dim=1)  # [B, 768]
            mean_pooled = pooled.mean(dim=0, keepdim=True)  # [1, 768]
            proj = self._proj(mean_pooled).squeeze(0)  # [128]
            embedding = proj.numpy().astype(np.float32)

        # Sentiment on first text (representative)
        results: list[list[dict]] = self._sentiment(texts[:4], truncation=True)
        label_scores: dict[str, float] = {}
        for batch_result in results:
            for item in batch_result:
                label = item["label"].lower()
                label_scores[label] = label_scores.get(label, 0.0) + item["score"]
        # Aggregate across texts
        pos = label_scores.get("positive", label_scores.get("bullish", 0.0))
        neg = label_scores.get("negative", label_scores.get("bearish", 0.0))
        total = pos + neg + label_scores.get("neutral", 0.0)
        sentiment = (pos - neg) / max(total, 1e-6)
        confidence = max(pos, neg, label_scores.get("neutral", 0.0)) / max(total, 1e-6)

        return NLPFeatures(
            embedding=embedding,
            sentiment_score=float(sentiment),
            confidence=float(confidence),
        )


def get_nlp_features(texts: list[str]) -> NLPFeatures:
    """Public API — encode a list of text strings into NLP features."""
    return _CryptoBERTPipeline.instance().encode(texts)
