"""Multilingual embedding backends (local sentence-transformers + deterministic test backend)."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Any, Protocol

_WORD_RE = re.compile(r"[a-z0-9]+")


class EmbeddingBackend(Protocol):
    """Protocol implemented by real multilingual models and deterministic offline test backends."""

    @property
    def model_name(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class DeterministicHashEmbedder:
    """Fast, offline, zero-download normalized vector backend for unit/integration tests.

    Hashes lowercase words and character trigrams into signed buckets so semantically
    overlapping Malay/English queries produce meaningful cosine similarity in tests.
    """

    def __init__(self, *, dimension: int = 384, model_name: str = "deterministic-hash-384") -> None:
        self._dimension = dimension
        self._model_name = model_name

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return self._dimension

    def _encode_one(self, text: str) -> list[float]:
        vec = [0.0] * self._dimension
        cleaned = text.lower().strip()
        words = _WORD_RE.findall(cleaned)
        features: list[str] = []
        for w in words:
            features.append(f"w:{w}")
            if len(w) >= 3:
                for i in range(len(w) - 2):
                    features.append(f"g:{w[i : i + 3]}")
        if not features:
            features.append("empty")

        for feat in features:
            digest = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(digest[:4], "little") % self._dimension
            sign = 1.0 if (digest[4] & 1) == 0 else -1.0
            weight = 2.0 if feat.startswith("w:") else 0.5
            vec[idx] += sign * weight

        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0.0:
            vec = [v / norm for v in vec]
        return vec

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._encode_one(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._encode_one(text)


class SentenceTransformerEmbedder:
    """Local open-weights multilingual embedder powered by sentence-transformers."""

    def __init__(
        self,
        model_name: str = "intfloat/multilingual-e5-small",
        *,
        device: str | None = None,
        batch_size: int = 32,
        max_seq_length: int = 512,
    ) -> None:
        import torch
        from sentence_transformers import SentenceTransformer

        self._model_name = model_name
        self._batch_size = batch_size
        self._is_e5 = "e5" in model_name.lower()

        resolved_device = device
        if resolved_device is None:
            if torch.backends.mps.is_available():
                resolved_device = "mps"
            elif torch.cuda.is_available():
                resolved_device = "cuda"
            else:
                resolved_device = "cpu"

        model_kwargs: dict[str, Any] = {}
        if resolved_device in {"mps", "cuda"}:
            model_kwargs["dtype"] = torch.float16

        self._model: Any = SentenceTransformer(
            model_name,
            device=resolved_device,
            model_kwargs=model_kwargs,
        )
        self._model.max_seq_length = max_seq_length
        dim = (
            self._model.get_embedding_dimension()
            if hasattr(self._model, "get_embedding_dimension")
            else self._model.get_sentence_embedding_dimension()
        )
        if dim is None:
            probe = self._model.encode(["probe"], normalize_embeddings=True)
            dim = len(probe[0])
        self._dimension = int(dim)

    def release(self) -> None:
        """Release model weights and accelerator cache cleanly (e.g. between benchmarks)."""
        import gc

        self._model = None
        gc.collect()
        try:
            import torch

            if torch.backends.mps.is_available():
                torch.mps.empty_cache()
            elif torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        prepared = [f"passage: {t}" if self._is_e5 else str(t) for t in texts]
        if not self._is_e5 and hasattr(self._model, "encode_document"):
            arr = self._model.encode_document(
                prepared,
                batch_size=self._batch_size,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        else:
            arr = self._model.encode(
                prepared,
                batch_size=self._batch_size,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        return [[float(x) for x in row] for row in arr.tolist()]

    def embed_query(self, text: str) -> list[float]:
        prepared = f"query: {text}" if self._is_e5 else str(text)
        if not self._is_e5 and hasattr(self._model, "encode_query"):
            arr = self._model.encode_query(
                prepared,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        else:
            arr = self._model.encode(
                prepared,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        return [float(x) for x in arr.tolist()]


def create_embedder(
    model_name: str,
    *,
    device: str | None = None,
    batch_size: int = 32,
) -> EmbeddingBackend:
    """Factory creating either the deterministic test embedder or a SentenceTransformer model."""
    if model_name.startswith("deterministic-hash"):
        return DeterministicHashEmbedder(model_name=model_name)
    return SentenceTransformerEmbedder(model_name=model_name, device=device, batch_size=batch_size)
