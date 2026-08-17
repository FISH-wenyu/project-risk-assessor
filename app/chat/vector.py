from __future__ import annotations

import importlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.risk.time_utils import beijing_now_text

from .models import PrincipalScope, RetrievalHit


_PAYLOAD_FIELDS = (
    "record_id",
    "project_id",
    "session_id",
    "user_id",
    "org_id",
    "source_type",
    "confidentiality",
    "embedding_model",
    "embedding_version",
    "content_hash",
    "created_at",
    "expires_at",
)


class NullVectorIndex:
    def __init__(self, reason: str = "vector index unavailable"):
        self.reason = str(reason or "vector index unavailable")

    @property
    def available(self) -> bool:
        return False

    def upsert(
        self, record_id: str, vector: Sequence[float], payload: Mapping[str, Any]
    ) -> None:
        raise RuntimeError(self.reason)

    def delete(self, record_id: str) -> None:
        raise RuntimeError(self.reason)

    def search(
        self,
        vector: Sequence[float],
        *,
        project_id: str,
        scope: PrincipalScope,
        limit: int,
    ) -> list[RetrievalHit]:
        raise RuntimeError(self.reason)


class InMemoryVectorIndex:
    """Small cosine index for tests; canonical text is never retained."""

    def __init__(self, *, now_factory=beijing_now_text):
        self.reason = ""
        self._now_factory = now_factory
        self._records: dict[str, tuple[list[float], dict[str, Any]]] = {}

    @property
    def available(self) -> bool:
        return True

    def upsert(
        self, record_id: str, vector: Sequence[float], payload: Mapping[str, Any]
    ) -> None:
        values = _vector(vector)
        safe_payload = _safe_payload(record_id, payload)
        self._records[str(record_id)] = (values, safe_payload)

    def delete(self, record_id: str) -> None:
        self._records.pop(str(record_id), None)

    def search(
        self,
        vector: Sequence[float],
        *,
        project_id: str,
        scope: PrincipalScope,
        limit: int,
    ) -> list[RetrievalHit]:
        query = _vector(vector)
        now = self._now_factory()
        hits: list[RetrievalHit] = []
        for record_id, (candidate, payload) in self._records.items():
            if payload["project_id"] != str(project_id):
                continue
            if payload["user_id"] != scope.user_id or payload["org_id"] != scope.org_id:
                continue
            expires_at = payload.get("expires_at")
            if expires_at and str(expires_at) <= now:
                continue
            if len(candidate) != len(query):
                continue
            hits.append(
                RetrievalHit(
                    memory_id=record_id,
                    score=_cosine(query, candidate),
                    match_type="semantic",
                    project_id=str(payload["project_id"]),
                    scope=PrincipalScope(payload["user_id"], payload["org_id"]),
                    session_id=payload.get("session_id"),
                )
            )
        hits.sort(key=lambda hit: (hit.score, hit.memory_id), reverse=True)
        return hits[: max(0, int(limit))]

    def get_payload(self, record_id: str) -> dict[str, Any]:
        return dict(self._records[str(record_id)][1])


@dataclass(frozen=True)
class _MatchValue:
    value: Any


@dataclass(frozen=True)
class _FieldCondition:
    key: str
    match: Any


@dataclass(frozen=True)
class _PayloadField:
    key: str


@dataclass(frozen=True)
class _IsNullCondition:
    is_null: _PayloadField


@dataclass(frozen=True)
class _Filter:
    must: list[Any]


@dataclass(frozen=True)
class _PointStruct:
    id: str
    vector: list[float]
    payload: dict[str, Any]


@dataclass(frozen=True)
class _PointIdsList:
    points: list[str]


@dataclass(frozen=True)
class _VectorParams:
    size: int
    distance: Any


class _Distance:
    COSINE = "Cosine"


class _FallbackModels:
    MatchValue = _MatchValue
    FieldCondition = _FieldCondition
    PayloadField = _PayloadField
    IsNullCondition = _IsNullCondition
    Filter = _Filter
    PointStruct = _PointStruct
    PointIdsList = _PointIdsList
    VectorParams = _VectorParams
    Distance = _Distance


class QdrantLocalIndex:
    def __init__(
        self,
        path: str | Path,
        collection: str,
        *,
        vector_size: int,
        embedding_model: str,
        embedding_version: str,
        client: Any | None = None,
        models_module: Any | None = None,
    ):
        self.path = Path(path)
        self.collection = str(collection)
        self.vector_size = int(vector_size)
        self.embedding_model = str(embedding_model).strip()
        self.embedding_version = str(embedding_version).strip()
        self.reason = ""
        self._client = None
        self._models = models_module
        try:
            if client is None:
                qdrant_module = importlib.import_module("qdrant_client")
                self._models = self._models or importlib.import_module(
                    "qdrant_client.models"
                )
                self.path.parent.mkdir(parents=True, exist_ok=True)
                client = qdrant_module.QdrantClient(path=str(self.path))
            elif self._models is None:
                try:
                    self._models = importlib.import_module("qdrant_client.models")
                except ModuleNotFoundError:
                    self._models = _FallbackModels
            self._client = client
            self._ensure_collection()
        except Exception as exc:
            self._client = None
            self.reason = f"{type(exc).__name__}: {exc}"

    @property
    def available(self) -> bool:
        return self._client is not None

    def _ensure_collection(self) -> None:
        if not self.collection.strip():
            raise ValueError("collection must not be empty")
        if self.vector_size <= 0:
            raise ValueError("vector_size must be positive")
        if not self.embedding_model:
            raise ValueError("embedding_model must not be empty")
        if not self.embedding_version:
            raise ValueError("embedding_version must not be empty")
        if self._client.collection_exists(self.collection):
            info = self._client.get_collection(collection_name=self.collection)
            vectors = info.config.params.vectors
            if isinstance(vectors, Mapping) or not hasattr(vectors, "size"):
                raise ValueError("collection must use one unnamed vector schema")
            if int(vectors.size) != self.vector_size:
                raise ValueError(
                    "collection dimension mismatch: "
                    f"expected {self.vector_size}, got {vectors.size}"
                )
            if not _is_cosine_distance(vectors.distance):
                raise ValueError("collection distance must be COSINE")
        else:
            self._client.create_collection(
                collection_name=self.collection,
                vectors_config=self._models.VectorParams(
                    size=self.vector_size, distance=self._models.Distance.COSINE
                ),
            )

    def upsert(
        self, record_id: str, vector: Sequence[float], payload: Mapping[str, Any]
    ) -> None:
        self._require_available()
        values = _vector(vector)
        if len(values) != self.vector_size:
            raise ValueError("vector dimension does not match collection")
        safe_payload = _safe_payload(
            record_id,
            payload,
            embedding_model=self.embedding_model,
            embedding_version=self.embedding_version,
        )
        point = self._models.PointStruct(
            id=str(record_id), vector=values, payload=safe_payload
        )
        self._client.upsert(
            collection_name=self.collection, points=[point], wait=True
        )

    def delete(self, record_id: str) -> None:
        self._require_available()
        selector = self._models.PointIdsList(points=[str(record_id)])
        self._client.delete(
            collection_name=self.collection, points_selector=selector, wait=True
        )

    def search(
        self,
        vector: Sequence[float],
        *,
        project_id: str,
        scope: PrincipalScope,
        limit: int,
    ) -> list[RetrievalHit]:
        self._require_available()
        query_filter = self._scope_filter(str(project_id), scope)
        response = self._client.query_points(
            collection_name=self.collection,
            query=_vector(vector),
            query_filter=query_filter,
            limit=max(0, int(limit)),
            with_payload=True,
        )
        now = beijing_now_text()
        hits: list[RetrievalHit] = []
        for point in getattr(response, "points", response or []):
            payload = dict(getattr(point, "payload", {}) or {})
            if str(payload.get("project_id")) != str(project_id):
                continue
            if payload.get("user_id") != scope.user_id:
                continue
            if payload.get("org_id") != scope.org_id:
                continue
            if payload.get("embedding_model") != self.embedding_model:
                continue
            if payload.get("embedding_version") != self.embedding_version:
                continue
            expires_at = payload.get("expires_at")
            if expires_at and str(expires_at) <= now:
                continue
            hits.append(
                RetrievalHit(
                    memory_id=str(payload.get("record_id") or point.id),
                    score=float(point.score),
                    match_type="semantic",
                    project_id=str(payload.get("project_id") or project_id),
                    scope=PrincipalScope(payload.get("user_id"), payload.get("org_id")),
                    session_id=payload.get("session_id"),
                )
            )
        return hits

    def _scope_filter(self, project_id: str, scope: PrincipalScope) -> Any:
        return self._models.Filter(
            must=[
                self._equality_condition("project_id", project_id),
                self._equality_condition("user_id", scope.user_id),
                self._equality_condition("org_id", scope.org_id),
                self._equality_condition("embedding_model", self.embedding_model),
                self._equality_condition("embedding_version", self.embedding_version),
            ]
        )

    def _equality_condition(self, key: str, value: str | None) -> Any:
        if value is None and hasattr(self._models, "IsNullCondition"):
            return self._models.IsNullCondition(
                is_null=self._models.PayloadField(key=key)
            )
        return self._models.FieldCondition(
            key=key, match=self._models.MatchValue(value=value)
        )

    def _require_available(self) -> None:
        if not self.available:
            raise RuntimeError(self.reason or "qdrant unavailable")


def create_vector_index(
    path: str | Path,
    collection: str,
    *,
    vector_size: int,
    embedding_model: str,
    embedding_version: str,
    client: Any | None = None,
    models_module: Any | None = None,
) -> QdrantLocalIndex | NullVectorIndex:
    index = QdrantLocalIndex(
        path,
        collection,
        vector_size=vector_size,
        embedding_model=embedding_model,
        embedding_version=embedding_version,
        client=client,
        models_module=models_module,
    )
    if index.available:
        return index
    return NullVectorIndex(index.reason)


build_vector_index = create_vector_index


def _safe_payload(
    record_id: str,
    payload: Mapping[str, Any],
    *,
    embedding_model: str | None = None,
    embedding_version: str | None = None,
) -> dict[str, Any]:
    missing = [field for field in _PAYLOAD_FIELDS if field not in payload]
    if missing:
        raise ValueError(f"missing vector payload fields: {', '.join(missing)}")
    safe = {field: payload.get(field) for field in _PAYLOAD_FIELDS}
    safe["record_id"] = str(record_id)
    safe["project_id"] = str(safe["project_id"])
    if embedding_model is not None and safe["embedding_model"] != embedding_model:
        raise ValueError("embedding_model does not match index schema")
    if embedding_version is not None and safe["embedding_version"] != embedding_version:
        raise ValueError("embedding_version does not match index schema")
    return safe


def _is_cosine_distance(value: Any) -> bool:
    normalized = str(getattr(value, "value", value)).split(".")[-1].lower()
    return normalized == "cosine"


def _vector(values: Sequence[float]) -> list[float]:
    vector = [float(value) for value in values]
    if not vector or not all(math.isfinite(value) for value in vector):
        raise ValueError("vector must contain finite values")
    return vector


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)
