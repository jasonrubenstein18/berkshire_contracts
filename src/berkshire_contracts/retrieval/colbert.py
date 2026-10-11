"""Late-interaction (ColBERT-style) encoder and exact MaxSim scoring."""

from __future__ import annotations

import logging
import os
import threading
from functools import lru_cache

import numpy as np

from berkshire_contracts.config import get_settings

log = logging.getLogger(__name__)
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def _best_device() -> str:
    forced = os.environ.get("COLBERT_DEVICE")
    if forced:
        return forced
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


class LateInteractionEncoder:
    """Wraps a PyLate ColBERT model; every token keeps its own 96-d vector, scored with MaxSim."""

    def __init__(self, model_name: str | None = None, device: str | None = None) -> None:
        from pylate import models

        self.model_name = model_name or get_settings().colbert_model
        self.device = device or _best_device()
        self.model = models.ColBERT(model_name_or_path=self.model_name, device=self.device)
        self._lock = threading.Lock()

    @property
    def document_length(self) -> int:
        return int(self.model.document_length)

    def encode_documents(
        self, texts: list[str], batch_size: int = 32, progress: bool = False
    ) -> list[np.ndarray]:
        with self._lock:
            out = self.model.encode(texts, is_query=False, batch_size=batch_size, show_progress_bar=progress)
        return [np.asarray(e, dtype=np.float32) for e in out]

    def encode_query(self, text: str) -> np.ndarray:
        with self._lock:
            out = self.model.encode([text], is_query=True, batch_size=1, show_progress_bar=False)
        return np.asarray(out[0], dtype=np.float32)

    def content_tokens(self, text: str) -> int:
        """Leading query positions that come from the text ([CLS], query marker, words), before [MASK] padding."""
        return len(self.model.tokenizer(text, add_special_tokens=False)["input_ids"]) + 2


_encoder_lock = threading.Lock()


@lru_cache(maxsize=1)
def _cached_encoder() -> LateInteractionEncoder:
    return LateInteractionEncoder()


def get_encoder() -> LateInteractionEncoder:
    """Shared encoder. The lock keeps a question asked during app warm-up from loading a second copy."""
    with _encoder_lock:
        return _cached_encoder()


def maxsim(query: np.ndarray, document: np.ndarray) -> float:
    """Sum over query tokens of the best cosine match among document tokens (vectors are L2-normalized)."""
    if document.size == 0:
        return 0.0
    return float((document @ query.T).max(axis=0).sum())


def maxsim_many(query: np.ndarray, documents: list[np.ndarray]) -> np.ndarray:
    return np.array([maxsim(query, d) for d in documents], dtype=np.float32)


def maxsim_segments(query: np.ndarray, embeddings: np.ndarray, offsets: np.ndarray) -> np.ndarray:
    """Exact MaxSim for every chunk at once; `embeddings` is all chunk tokens concatenated."""
    similarities = embeddings @ query.T
    per_chunk_max = np.maximum.reduceat(similarities, offsets[:-1], axis=0)
    return per_chunk_max.sum(axis=1)
