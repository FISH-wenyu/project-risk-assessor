from __future__ import annotations

import hashlib
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from .llm import build_prompt, parse_llm_answer, sanitize_outbound_text
from .memory import MemoryContext, MemoryOrchestrator
from .models import ChatMessage, ChatSession, Citation, PrincipalScope, ValidationError
from .ports import ChatLlmClient, ChatRepository, RiskContextProvider


@dataclass(frozen=True)
class ChatSendResult:
    session: ChatSession
    user_message: ChatMessage
    assistant_message: ChatMessage
    citations: list[Citation]
    fallback_used: bool
    retrieval_degraded: bool = False
    retrieval_reason: str = ""


class ChatService:
    def __init__(
        self,
        *,
        repository: ChatRepository,
        memory: MemoryOrchestrator,
        risk_context_provider: RiskContextProvider,
        llm_client: ChatLlmClient,
        model: str = "",
        context_char_limit: int = 12000,
    ):
        self.repository = repository
        self.memory = memory
        self.risk_context_provider = risk_context_provider
        self.llm_client = llm_client
        self.model = str(model or "")
        self.context_char_limit = max(1000, int(context_char_limit or 12000))
        self._idempotency_locks: dict[tuple[str, str], _LockEntry] = {}
        self._idempotency_locks_guard = threading.Lock()

    def send_message(
        self,
        *,
        session_id: str,
        content: str,
        idempotency_key: str,
        remember: bool,
        scope: PrincipalScope,
    ) -> ChatSendResult:
        content = _required_content(content)
        idempotency_key = _required_content(idempotency_key, field="idempotency_key")
        if not isinstance(scope, PrincipalScope):
            raise ValidationError("scope must be PrincipalScope")
        session = self._get_session(session_id, scope)
        key = (session.session_id, idempotency_key)
        entry = self._acquire_idempotency_entry(key)
        try:
            with entry.lock:
                return self._send_message_locked(
                    session=session,
                    content=content,
                    idempotency_key=idempotency_key,
                    remember=remember,
                    scope=scope,
                )
        finally:
            self._release_idempotency_entry(key, entry)

    def _send_message_locked(
        self,
        *,
        session: ChatSession,
        content: str,
        idempotency_key: str,
        remember: bool,
        scope: PrincipalScope,
    ) -> ChatSendResult:
        user_key = f"user:{idempotency_key}"
        assistant_key = f"assistant:{idempotency_key}"

        user_write = self.repository.append_message(
            session.session_id,
            "user",
            content,
            user_key,
            scope,
            project_id=session.project_id,
        )
        existing_assistant = self.repository.get_message_by_idempotency_key(
            session.session_id,
            assistant_key,
            scope,
            project_id=session.project_id,
        )
        if existing_assistant is not None:
            if remember:
                self._promote_user_memory_best_effort(session, scope, content)
            citations = self.repository.list_citations(
                existing_assistant.message_id,
                scope,
                project_id=session.project_id,
            )
            return ChatSendResult(
                session=session,
                user_message=user_write.message,
                assistant_message=existing_assistant,
                citations=citations,
                fallback_used=self._audit_fallback_used(
                    existing_assistant.message_id, scope, session.project_id
                ),
            )

        risk_context = self._risk_context_best_effort(session.project_id)
        memory_context = self._memory_context_best_effort(
            session, scope, content, user_write.message
        )
        prompt = self._prompt_best_effort(
            content,
            risk_context,
            memory_context,
            project_id=session.project_id,
            scope=session.scope,
        )

        fallback_used = False
        error_type = ""
        try:
            if risk_context is None and memory_context.retrieval_reason == "context degraded":
                raise RuntimeError("context unavailable")
            if not getattr(self.llm_client, "available", False):
                raise RuntimeError("llm unavailable")
            response = self.llm_client.complete(prompt.messages, model=self.model or None)
            answer = parse_llm_answer(response, requested_model=self.model)
        except Exception as exc:
            fallback_used = True
            error_type = type(exc).__name__
            answer = _fallback_answer(prompt.payload.get("risk_summary"))

        citations = _allowed_citations(answer.citation_ids, prompt.allowed_citations)
        audit = {
            "request_hash": _sha256(prompt.payload_json),
            "response_hash": _sha256(answer.raw_text or answer.answer),
            "prompt_chars": len(prompt.payload_json),
            "response_chars": len(answer.answer),
            "model": answer.model or self.model,
            "fallback_used": fallback_used,
            "status": "fallback" if fallback_used else "success",
            "error_type": error_type,
        }
        assistant_write = self.repository.save_assistant_message_with_citations(
            session.session_id,
            answer.answer,
            assistant_key,
            citations,
            scope,
            project_id=session.project_id,
            reply_to_message_id=user_write.message.message_id,
            audit=audit,
        )
        stored_citations = self.repository.list_citations(
            assistant_write.message.message_id,
            scope,
            project_id=session.project_id,
        )
        if remember:
            self._promote_user_memory_best_effort(session, scope, content)
        self._refresh_summary_best_effort(session, scope, content)
        return ChatSendResult(
            session=session,
            user_message=user_write.message,
            assistant_message=assistant_write.message,
            citations=stored_citations,
            fallback_used=fallback_used,
            retrieval_degraded=memory_context.retrieval_degraded,
            retrieval_reason=memory_context.retrieval_reason,
        )

    def _acquire_idempotency_entry(self, key: tuple[str, str]) -> "_LockEntry":
        with self._idempotency_locks_guard:
            entry = self._idempotency_locks.get(key)
            if entry is None:
                entry = _LockEntry(threading.Lock())
                self._idempotency_locks[key] = entry
            entry.ref_count += 1
            return entry

    def _release_idempotency_entry(
        self, key: tuple[str, str], entry: "_LockEntry"
    ) -> None:
        with self._idempotency_locks_guard:
            current = self._idempotency_locks.get(key)
            if current is not entry:
                return
            entry.ref_count -= 1
            if entry.ref_count <= 0 and not entry.lock.locked():
                self._idempotency_locks.pop(key, None)

    def idempotency_lock_count(self) -> int:
        with self._idempotency_locks_guard:
            return len(self._idempotency_locks)

    def _get_session(self, session_id: str, scope: PrincipalScope) -> ChatSession:
        getter = getattr(self.repository, "get_session_by_id", None)
        if getter is None:
            raise ValidationError("repository does not support session lookup")
        return getter(session_id, scope)

    def _risk_context_best_effort(self, project_id: str) -> Mapping[str, Any] | None:
        try:
            return self.risk_context_provider.get_sanitized_context(project_id)
        except Exception:
            return None

    def _audit_fallback_used(
        self, message_id: str, scope: PrincipalScope, project_id: str
    ) -> bool:
        getter = getattr(self.repository, "get_llm_audit", None)
        if getter is None:
            return False
        try:
            audit = getter(message_id, scope, project_id=project_id)
        except Exception:
            return False
        return bool((audit or {}).get("fallback_used"))

    def _memory_context_best_effort(
        self,
        session: ChatSession,
        scope: PrincipalScope,
        query: str,
        fallback_message: ChatMessage,
    ) -> MemoryContext:
        try:
            return self.memory.build_context(
                session.session_id,
                project_id=session.project_id,
                scope=scope,
                query=query,
            )
        except Exception:
            return MemoryContext(
                summary=None,
                recent_messages=[fallback_message],
                memories=[],
                estimated_chars=len(fallback_message.content),
                retrieval_degraded=True,
                retrieval_reason="context degraded",
            )

    def _prompt_best_effort(
        self,
        content: str,
        risk_context: Mapping[str, Any] | None,
        memory_context: MemoryContext,
        *,
        project_id: str,
        scope: PrincipalScope,
    ):
        try:
            return build_prompt(
                question=content,
                risk_context=risk_context,
                conversation_summary=(
                    memory_context.summary.content if memory_context.summary else ""
                ),
                recent_messages=[
                    {"role": message.role, "content": message.content}
                    for message in memory_context.recent_messages
                ],
                memories=_prompt_memories(
                    memory_context,
                    project_id=project_id,
                    scope=scope,
                ),
                total_char_limit=self.context_char_limit,
            )
        except Exception:
            return build_prompt(
                question=content,
                risk_context=None,
                conversation_summary="",
                recent_messages=[{"role": "user", "content": content}],
                memories=[],
                total_char_limit=self.context_char_limit,
            )

    def _promote_user_memory_best_effort(
        self, session: ChatSession, scope: PrincipalScope, content: str
    ) -> None:
        content_hash = _sha256(content)
        try:
            for item in self.repository.list_memories(session.project_id, scope):
                if (
                    item.session_id == session.session_id
                    and item.source_type == "user_confirmed"
                    and item.content_hash == content_hash
                ):
                    return
            self.memory.promote_user_confirmed(
                session.session_id,
                project_id=session.project_id,
                scope=scope,
                canonical_text=content,
            )
        except Exception:
            return

    def _refresh_summary_best_effort(
        self, session: ChatSession, scope: PrincipalScope, query: str
    ) -> None:
        try:
            self.memory.build_context(
                session.session_id,
                project_id=session.project_id,
                scope=scope,
                query=query,
            )
        except Exception:
            return


def _prompt_memories(
    memory_context: MemoryContext, *, project_id: str, scope: PrincipalScope
) -> list[dict[str, Any]]:
    result = []
    for memory in memory_context.memories:
        if memory.project_id != project_id or memory.scope != scope:
            continue
        if memory.confidentiality == "local_only":
            continue
        try:
            UUID(memory.memory_id)
        except (TypeError, ValueError):
            continue
        result.append(
            {
                "citation_id": f"memory:{memory.memory_id}",
                "memory_type": memory.source_type,
                "canonical_text": memory.canonical_text,
                "confidence": memory.confidence,
            }
        )
    return result


def _allowed_citations(
    requested: list[str], allowed: Mapping[str, Mapping[str, str]]
) -> list[Citation]:
    citations: list[Citation] = []
    seen: set[str] = set()
    for citation_id in requested:
        if citation_id in seen or citation_id not in allowed:
            continue
        seen.add(citation_id)
        metadata = allowed[citation_id]
        citations.append(
            Citation(
                citation_id=str(uuid4()),
                message_id="",
                source_type=str(metadata["source_type"]),
                source_id=str(metadata["source_id"]),
                label=str(metadata["label"]),
            )
        )
    return citations


@dataclass
class _LockEntry:
    lock: threading.Lock
    ref_count: int = 0


def _fallback_answer(risk_summary: Mapping[str, Any] | None):
    latest = risk_summary if isinstance(risk_summary, Mapping) else {}
    level = sanitize_outbound_text(latest.get("level"), limit=20) or "unknown"
    score = latest.get("score")
    hits = latest.get("risk_hits") or []
    suggestions = latest.get("suggestions") or []
    hit_text = "; ".join(
        _fallback_hit_text(hit)
        for hit in hits[:3]
        if _fallback_hit_text(hit)
    )
    suggestion_text = "; ".join(
        sanitize_outbound_text(item, limit=120)
        for item in suggestions[:3]
        if isinstance(item, str)
    )
    parts = [
        "Local risk summary: "
        f"current level is {level}, score is {score if score is not None else 'unknown'}."
    ]
    if hit_text:
        parts.append(f" Main hits: {hit_text}.")
    if suggestion_text:
        parts.append(f" Suggested actions: {suggestion_text}.")
    parts.append(" External LLM is unavailable; this is a local fallback answer.")
    from .llm import LlmAnswer

    history_id = latest.get("history_id") or latest.get("id")
    citation_ids = [f"risk-history:{history_id}"] if history_id is not None else []
    answer = "".join(parts)
    return LlmAnswer(answer, citation_ids, answer, "")


def _fallback_hit_text(value: Any) -> str:
    if isinstance(value, Mapping):
        parts = [
            sanitize_outbound_text(value.get("rule"), limit=80),
            sanitize_outbound_text(value.get("severity"), limit=40),
            sanitize_outbound_text(value.get("summary"), limit=120),
        ]
        return " ".join(part for part in parts if part)
    return sanitize_outbound_text(value, limit=120)


def _required_content(value: str, *, field: str = "content") -> str:
    text = str(value or "").strip()
    if not text:
        raise ValidationError(f"{field} must not be blank")
    return text


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
