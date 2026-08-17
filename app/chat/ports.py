from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from .models import (
    ChatMessage,
    ChatSession,
    Citation,
    MemoryItem,
    MemorySummary,
    MessageWriteResult,
    PrincipalScope,
    RetrievalHit,
)


class ChatRepository(Protocol):
    def create_session(
        self, project_id: str, title: str, scope: PrincipalScope
    ) -> ChatSession: ...

    def get_session(
        self,
        session_id: str,
        scope: PrincipalScope,
        *,
        project_id: str,
    ) -> ChatSession: ...

    def get_session_by_id(
        self,
        session_id: str,
        scope: PrincipalScope,
    ) -> ChatSession: ...

    def list_sessions(
        self, project_id: str, scope: PrincipalScope, limit: int = 100
    ) -> list[ChatSession]: ...

    def update_session(
        self,
        session_id: str,
        scope: PrincipalScope,
        *,
        project_id: str,
        title: str | None = None,
        status: str | None = None,
    ) -> ChatSession: ...

    def archive_session(
        self, session_id: str, scope: PrincipalScope, *, project_id: str
    ) -> ChatSession: ...

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        idempotency_key: str,
        scope: PrincipalScope,
        *,
        project_id: str,
    ) -> MessageWriteResult: ...

    def get_message_by_idempotency_key(
        self,
        session_id: str,
        idempotency_key: str,
        scope: PrincipalScope,
        *,
        project_id: str,
    ) -> ChatMessage | None: ...

    def list_messages(
        self,
        session_id: str,
        scope: PrincipalScope,
        *,
        project_id: str,
        limit: int | None = None,
    ) -> list[ChatMessage]: ...

    def save_assistant_message_with_citations(
        self,
        session_id: str,
        content: str,
        idempotency_key: str,
        citations: Sequence[Citation],
        scope: PrincipalScope,
        *,
        project_id: str,
        reply_to_message_id: str | None = None,
        audit: Mapping[str, Any],
    ) -> MessageWriteResult: ...

    def list_citations(
        self, message_id: str, scope: PrincipalScope, *, project_id: str
    ) -> list[Citation]: ...

    def get_llm_audit(
        self, message_id: str, scope: PrincipalScope, *, project_id: str
    ) -> Mapping[str, Any] | None: ...

    def save_summary(
        self,
        session_id: str,
        content: str,
        message_start_id: str,
        message_end_id: str,
        scope: PrincipalScope,
        *,
        project_id: str,
    ) -> MemorySummary: ...

    def get_latest_summary(
        self, session_id: str, scope: PrincipalScope, *, project_id: str
    ) -> MemorySummary | None: ...

    def save_memory(
        self,
        project_id: str,
        canonical_text: str,
        source_type: str,
        scope: PrincipalScope,
        *,
        session_id: str | None = None,
        confidence: float = 1.0,
        confidentiality: str = "sanitized",
        expires_at: str | None = None,
    ) -> MemoryItem: ...

    def supersede_memory(
        self,
        memory_id: str,
        canonical_text: str,
        scope: PrincipalScope,
        *,
        project_id: str,
        source_type: str | None = None,
        confidence: float | None = None,
        confidentiality: str | None = None,
        expires_at: str | None = None,
    ) -> MemoryItem: ...

    def expire_memory(
        self, memory_id: str, scope: PrincipalScope, *, project_id: str
    ) -> MemoryItem: ...

    def get_memory(
        self, memory_id: str, scope: PrincipalScope, *, project_id: str
    ) -> MemoryItem: ...

    def list_memories(
        self,
        project_id: str,
        scope: PrincipalScope,
        limit: int = 100,
        *,
        now: str | None = None,
    ) -> list[MemoryItem]: ...

    def search_active_memories(
        self,
        project_id: str,
        scope: PrincipalScope,
        query: str,
        limit: int,
    ) -> list[MemoryItem]: ...

    def list_pending_outbox(self, limit: int = 100) -> list[dict[str, Any]]: ...

    def claim_outbox(
        self,
        worker_id: str,
        limit: int = 100,
        lease_seconds: int = 60,
        *,
        now: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def mark_outbox_processed(self, outbox_id: str, worker_id: str) -> None:
        """Mark success or raise OutboxLeaseLost when ownership changed."""
        ...

    def mark_outbox_failed(
        self,
        outbox_id: str,
        worker_id: str,
        error_type: str,
        *,
        now: str | None = None,
        permanent: bool = False,
        max_attempts: int = 5,
    ) -> None:
        """Mark failure or raise OutboxLeaseLost when ownership changed."""
        ...


class EmbeddingProvider(Protocol):
    @property
    def available(self) -> bool: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class VectorIndex(Protocol):
    @property
    def available(self) -> bool: ...

    def upsert(self, record_id: str, vector: Sequence[float], payload: Mapping[str, Any]) -> None: ...

    def delete(self, record_id: str) -> None: ...

    def search(
        self,
        vector: Sequence[float],
        *,
        project_id: str,
        scope: PrincipalScope,
        limit: int,
    ) -> list[RetrievalHit]: ...


class RiskContextProvider(Protocol):
    def get_sanitized_context(self, project_id: str) -> Mapping[str, Any] | None: ...


class ChatLlmClient(Protocol):
    @property
    def available(self) -> bool: ...

    def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        model: str | None = None,
    ) -> Mapping[str, Any]: ...
