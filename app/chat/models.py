from __future__ import annotations

from dataclasses import dataclass


class ChatDomainError(Exception):
    """Base class for chat and memory domain failures."""


class ChatNotFound(ChatDomainError):
    pass


class SessionScopeMismatch(ChatDomainError):
    pass


class IdempotencyConflict(ChatDomainError):
    pass


class ValidationError(ChatDomainError):
    pass


class OutboxLeaseLost(ValidationError):
    pass


def _nullable_scope_value(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


@dataclass(frozen=True)
class PrincipalScope:
    user_id: str | None = None
    org_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "user_id", _nullable_scope_value(self.user_id))
        object.__setattr__(self, "org_id", _nullable_scope_value(self.org_id))


@dataclass(frozen=True)
class ChatSession:
    session_id: str
    project_id: str
    title: str
    status: str
    scope: PrincipalScope
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ChatMessage:
    message_id: str
    session_id: str
    sequence_no: int
    role: str
    content: str
    content_hash: str
    token_estimate: int
    created_at: str
    reply_to_message_id: str | None = None


@dataclass(frozen=True)
class MessageWriteResult:
    message: ChatMessage
    created: bool


@dataclass(frozen=True)
class MemorySummary:
    summary_id: str
    session_id: str
    version: int
    content: str
    content_hash: str
    message_start_id: str
    message_end_id: str
    created_at: str


@dataclass(frozen=True)
class MemoryItem:
    memory_id: str
    project_id: str
    session_id: str | None
    scope: PrincipalScope
    source_type: str
    canonical_text: str
    content_hash: str
    status: str
    confidence: float
    confidentiality: str
    expires_at: str | None
    supersedes_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Citation:
    citation_id: str
    message_id: str
    source_type: str
    source_id: str
    label: str
    locator: str = ""


@dataclass(frozen=True)
class RetrievalHit:
    memory_id: str
    score: float
    match_type: str
    project_id: str
    scope: PrincipalScope
    session_id: str | None = None


@dataclass(frozen=True)
class RetrievalStatus:
    available: bool
    degraded: bool = False
    reason: str = ""
    hit_count: int = 0
