from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException

from .models import (
    ChatDomainError,
    ChatMessage,
    ChatNotFound,
    ChatSession,
    Citation,
    IdempotencyConflict,
    MemoryItem,
    PrincipalScope,
    ValidationError,
)


MAX_SESSION_LIMIT = 100
MAX_MESSAGE_LIMIT = 200
MAX_MEMORY_LIMIT = 100
MAX_PROJECT_ID_CHARS = 128
MAX_TITLE_CHARS = 120
MAX_MESSAGE_CHARS = 4000
DEFAULT_SESSION_TITLE = "风险对话"


def build_chat_router(
    service_provider: Callable[[], Any],
    auth_dependency: Any,
    *,
    repository_provider: Callable[[], Any] | None = None,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/chat",
        tags=["chat"],
        dependencies=[auth_dependency] if auth_dependency is not None else [],
    )

    @router.post("/sessions")
    def create_session(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            project_id = _required_text(
                payload.get("project_id") if isinstance(payload, dict) else "",
                "project_id",
                max_chars=MAX_PROJECT_ID_CHARS,
            )
            title = _optional_text(
                payload.get("title") if isinstance(payload, dict) else "",
                DEFAULT_SESSION_TITLE,
                max_chars=MAX_TITLE_CHARS,
            )
            session = _repository_from_provider(
                service_provider, repository_provider
            ).create_session(project_id, title, _scope())
            return {"session": _session_for_response(session)}
        except Exception as exc:
            raise _public_http_error(exc) from exc

    @router.get("/sessions")
    def list_sessions(project_id: str = "", limit: int = MAX_SESSION_LIMIT) -> dict[str, Any]:
        try:
            clean_project_id = _required_text(
                project_id, "project_id", max_chars=MAX_PROJECT_ID_CHARS
            )
            clean_limit = _clamp_limit(limit, MAX_SESSION_LIMIT)
            sessions = _repository_from_provider(
                service_provider, repository_provider
            ).list_sessions(
                clean_project_id, _scope(), limit=clean_limit
            )
            return {
                "sessions": [_session_for_response(session) for session in sessions],
                "limit": clean_limit,
            }
        except Exception as exc:
            raise _public_http_error(exc) from exc

    @router.get("/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, Any]:
        try:
            session = _repository_from_provider(
                service_provider, repository_provider
            ).get_session_by_id(
                _required_text(session_id, "session_id"), _scope()
            )
            return {"session": _session_for_response(session)}
        except Exception as exc:
            raise _public_http_error(exc) from exc

    @router.get("/sessions/{session_id}/messages")
    def list_messages(
        session_id: str, limit: int = MAX_MESSAGE_LIMIT
    ) -> dict[str, Any]:
        try:
            clean_limit = _clamp_limit(limit, MAX_MESSAGE_LIMIT)
            repository = _repository_from_provider(service_provider, repository_provider)
            scope = _scope()
            session = repository.get_session_by_id(
                _required_text(session_id, "session_id"), scope
            )
            messages = repository.list_messages(
                session.session_id,
                scope,
                project_id=session.project_id,
                limit=clean_limit,
            )
            return {
                "session": _session_for_response(session),
                "messages": [
                    _message_with_citations_for_response(
                        repository, message, scope, session.project_id
                    )
                    for message in messages
                ],
                "limit": clean_limit,
            }
        except Exception as exc:
            raise _public_http_error(exc) from exc

    @router.post("/sessions/{session_id}/messages")
    def send_message(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            content = _required_text(
                payload.get("content") if isinstance(payload, dict) else "",
                "content",
                max_chars=MAX_MESSAGE_CHARS,
            )
            idempotency_key = _optional_text(
                payload.get("idempotency_key") if isinstance(payload, dict) else "",
                str(uuid4()),
                max_chars=160,
            )
            remember = bool(payload.get("remember")) if isinstance(payload, dict) else False
            result = service_provider().send_message(
                session_id=_required_text(session_id, "session_id"),
                content=content,
                idempotency_key=idempotency_key,
                remember=remember,
                scope=_scope(),
            )
            return {
                "session": _session_for_response(result.session),
                "user_message": _message_for_response(result.user_message),
                "assistant_message": {
                    **_message_for_response(result.assistant_message),
                    "citations": [
                        _citation_for_response(citation)
                        for citation in result.citations
                    ],
                },
                "citations": [
                    _citation_for_response(citation) for citation in result.citations
                ],
                "fallback_used": bool(result.fallback_used),
                "retrieval_degraded": bool(result.retrieval_degraded),
                "retrieval_reason": _public_retrieval_reason(
                    getattr(result, "retrieval_reason", "")
                ),
            }
        except Exception as exc:
            raise _public_http_error(exc) from exc

    @router.get("/sessions/{session_id}/memories")
    def list_memories(
        session_id: str, limit: int = MAX_MEMORY_LIMIT
    ) -> dict[str, Any]:
        try:
            clean_limit = _clamp_limit(limit, MAX_MEMORY_LIMIT)
            repository = _repository_from_provider(service_provider, repository_provider)
            scope = _scope()
            session = repository.get_session_by_id(
                _required_text(session_id, "session_id"), scope
            )
            memories = repository.list_memories(
                session.project_id, scope, limit=clean_limit
            )
            return {
                "session": _session_for_response(session),
                "memories": [_memory_for_response(memory) for memory in memories],
                "limit": clean_limit,
            }
        except Exception as exc:
            raise _public_http_error(exc) from exc

    return router


def _repository(service: Any) -> Any:
    repository = getattr(service, "repository", None)
    if repository is None:
        raise ValidationError("chat repository unavailable")
    return repository


def _repository_from_provider(
    service_provider: Callable[[], Any],
    repository_provider: Callable[[], Any] | None,
) -> Any:
    if repository_provider is not None:
        return repository_provider()
    return _repository(service_provider())


def _scope() -> PrincipalScope:
    return PrincipalScope()


def _required_text(value: Any, field: str, *, max_chars: int | None = None) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValidationError(f"{field} is required")
    if max_chars is not None and len(text) > max_chars:
        raise ValidationError(f"{field} is too long")
    return text


def _optional_text(value: Any, default: str, *, max_chars: int) -> str:
    text = str(value or "").strip() or default
    if len(text) > max_chars:
        text = text[:max_chars].strip() or default
    return text


def _clamp_limit(value: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        number = maximum
    return max(1, min(number, maximum))


def _session_for_response(session: ChatSession) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "project_id": session.project_id,
        "title": session.title,
        "status": session.status,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


def _message_for_response(message: ChatMessage) -> dict[str, Any]:
    return {
        "message_id": message.message_id,
        "session_id": message.session_id,
        "sequence_no": message.sequence_no,
        "role": message.role,
        "content": message.content,
        "created_at": message.created_at,
        "reply_to_message_id": message.reply_to_message_id,
    }


def _message_with_citations_for_response(
    repository: Any,
    message: ChatMessage,
    scope: PrincipalScope,
    project_id: str,
) -> dict[str, Any]:
    citations = repository.list_citations(
        message.message_id, scope, project_id=project_id
    )
    return {
        **_message_for_response(message),
        "citations": [_citation_for_response(citation) for citation in citations],
    }


def _citation_for_response(citation: Citation) -> dict[str, Any]:
    return {
        "citation_id": citation.citation_id,
        "message_id": citation.message_id,
        "source_type": citation.source_type,
        "source_id": citation.source_id,
        "label": citation.label,
        "locator": citation.locator,
    }


def _memory_for_response(memory: MemoryItem) -> dict[str, Any]:
    return {
        "memory_id": memory.memory_id,
        "project_id": memory.project_id,
        "session_id": memory.session_id,
        "source_type": memory.source_type,
        "status": memory.status,
        "confidence": memory.confidence,
        "confidentiality": memory.confidentiality,
        "expires_at": memory.expires_at,
        "created_at": memory.created_at,
        "updated_at": memory.updated_at,
    }


def _public_retrieval_reason(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if "vector" in lowered:
        return "vector retrieval degraded"
    if "embedding" in lowered:
        return "embedding retrieval degraded"
    return "context retrieval degraded"


def _public_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, ChatNotFound):
        return HTTPException(status_code=404, detail="Chat session not found")
    if isinstance(exc, IdempotencyConflict):
        return HTTPException(status_code=409, detail="Chat idempotency conflict")
    if isinstance(exc, ValidationError):
        return HTTPException(status_code=400, detail="Invalid chat request")
    if isinstance(exc, ChatDomainError):
        return HTTPException(status_code=400, detail="Chat request failed")
    return HTTPException(status_code=500, detail="Chat request failed")
