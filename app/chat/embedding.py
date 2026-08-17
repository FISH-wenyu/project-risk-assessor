from __future__ import annotations

import hashlib
import importlib
import math
from collections.abc import Callable, Sequence
from typing import Any


class NullEmbeddingProvider:
    def __init__(self, reason: str = "embedding provider unavailable"):
        self.reason = str(reason or "embedding provider unavailable")

    @property
    def available(self) -> bool:
        return False

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise RuntimeError(self.reason)


class DeterministicEmbeddingProvider:
    """Stable, local embeddings intended only for tests."""

    def __init__(self, dimension: int = 16):
        if int(dimension) <= 0:
            raise ValueError("dimension must be positive")
        self.dimension = int(dimension)
        self.reason = ""

    @property
    def available(self) -> bool:
        return True

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            seed = hashlib.sha256(str(text).encode("utf-8")).digest()
            raw = bytearray()
            counter = 0
            while len(raw) < self.dimension:
                raw.extend(hashlib.sha256(seed + counter.to_bytes(4, "big")).digest())
                counter += 1
            vector = [(value - 127.5) / 127.5 for value in raw[: self.dimension]]
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            vectors.append([value / norm for value in vector])
        return vectors


class FastEmbedProvider:
    def __init__(
        self,
        model_name: str,
        *,
        expected_dimension: int | None = None,
        model_factory: Callable[[str], Any] | None = None,
    ):
        self.model_name = str(model_name).strip()
        self.reason = ""
        self._model: Any | None = None
        if expected_dimension is not None and int(expected_dimension) <= 0:
            raise ValueError("expected_dimension must be positive")
        self._dimension = (
            int(expected_dimension) if expected_dimension is not None else None
        )
        try:
            if model_factory is None:
                module = importlib.import_module("fastembed")
                model_factory = module.TextEmbedding
            self._model = model_factory(self.model_name)
        except Exception as exc:
            self.reason = f"{type(exc).__name__}: {exc}"

    @property
    def available(self) -> bool:
        return self._model is not None

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not self.available:
            raise RuntimeError(self.reason or "fastembed unavailable")
        normalized_texts = [str(text) for text in texts]
        if not normalized_texts:
            return []
        rows = list(self._model.embed(normalized_texts))
        if len(rows) != len(normalized_texts):
            raise ValueError("embedding result count does not match input count")
        vectors: list[list[float]] = []
        for row in rows:
            vector = [float(value) for value in row]
            if not vector or not all(math.isfinite(value) for value in vector):
                raise ValueError("embedding vectors must contain finite values")
            if self._dimension is None:
                self._dimension = len(vector)
            if len(vector) != self._dimension:
                raise ValueError("embedding dimensions are inconsistent")
            vectors.append(vector)
        return vectors


def create_embedding_provider(
    model_name: str, *, expected_dimension: int | None = None
) -> FastEmbedProvider | NullEmbeddingProvider:
    provider = FastEmbedProvider(
        model_name, expected_dimension=expected_dimension
    )
    if provider.available:
        return provider
    return NullEmbeddingProvider(provider.reason)


build_embedding_provider = create_embedding_provider
