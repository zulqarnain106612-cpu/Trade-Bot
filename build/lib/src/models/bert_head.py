"""
BERT head — CryptoBERT 110M mean-pool [CLS] → 128-dim projection.

Uses the pre-loaded CryptoBERT pipeline from src/features/nlp.py and wraps
the embedding in a trainable linear projection for end-to-end fine-tuning.

Input:  pre-computed embedding [B, 768] from CryptoBERT
Output: [B, 128]
"""

from __future__ import annotations

import torch
import torch.nn as nn


class BERTHead(nn.Module):
    """
    Projection head that maps CryptoBERT 768-dim CLS embedding → 128-dim.

    The CryptoBERT model itself runs in the NLP feature pipeline (nlp.py)
    and its output is passed as a feature vector here. This module provides
    the trainable projection layer that adapts the frozen/fine-tuned
    CryptoBERT output for the ensemble.
    """

    def __init__(self, bert_dim: int = 768, d_model: int = 128) -> None:
        super().__init__()
        self.proj = nn.Sequential(
            nn.Linear(bert_dim, 256),
            nn.GELU(),
            nn.Dropout(0.1),
            nn.Linear(256, d_model),
            nn.LayerNorm(d_model),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """x: [B, 768] CryptoBERT embedding → [B, 128]"""
        return self.proj(x)
