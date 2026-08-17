from __future__ import annotations

import hashlib
import re
import textwrap
from dataclasses import dataclass, replace
from typing import Any

from app.risk.time_utils import beijing_now_text

from .models import (
    ChatMessage,
    MemoryItem,
    MemorySummary,
    OutboxLeaseLost,
    PrincipalScope,
)
from .ports import ChatRepository, EmbeddingProvider, VectorIndex


_AUTHORITY = {
    "source_fact": 3,
    "user_confirmed": 3,
    "decision": 2,
    "preference": 2,
    "assistant_inference": 1,
}
_MIN_SUMMARY_CHAR_LIMIT = 256
_SUMMARY_LINE_CHAR_LIMIT = 80
_DATABASE_URL = re.compile(
    r"(?i)\b(?:mysql|mariadb|postgres(?:ql)?|sqlserver|oracle|redis|mongodb)"
    r"(?:\+[A-Za-z0-9_.-]+)?://[^\s,;，；'\"<>]+"
)
_HTTP_URL = re.compile(r"(?i)\bhttps?://[^\s,;，；'\"<>]+")
_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
    r"[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]+=*")
_BASIC_AUTH = re.compile(
    r"(?i)\bauthorization\s*[:=：＝]\s*basic\s+[^\s,;，；]+"
)
_TOKEN_PREFIX = re.compile(r"(?i)\btoken\s+[A-Za-z0-9._~+/-]{8,}=*")
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(?<![\w-])[\"']?"
    r"(x[-_]api[-_]key|x[-_]auth[-_]token|api[_-]?key|access[_-]?key|"
    r"secret[_-]?key|token|password|passwd|"
    r"secret|cookie|credential|account(?:[_-]?(?:id|no|number))?|"
    r"username|user[_-]?name|user[_-]?id|login(?:[_-]?name)?|"
    r"账号|账户|用户名|登录名|密码|口令|密钥)[\"']?"
    r"\s*[:=：＝]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;，；]+)"
)
_EMAIL = re.compile(r"(?<![\w.-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PHONE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
_BUSINESS_NUMBER_LABEL = re.compile(
    r"[\w-]{0,20}(?:编号|编码)\s*[:=：＝]\s*$"
)
_CHINESE_ID = re.compile(
    r"(身份证号|身份证号码|公民身份号码)\s*[:=：＝]\s*"
    r"(?:\d{17}[0-9Xx]|\d{15})"
)
_BANK_CARD = re.compile(r"(银行卡号|银行卡)\s*[:=：＝]\s*\d{16,19}")


@dataclass(frozen=True)
class ContextMemory:
    item: MemoryItem
    score: float
    match_type: str

    def __getattr__(self, name: str) -> Any:
        return getattr(self.item, name)


@dataclass(frozen=True)
class MemoryContext:
    summary: MemorySummary | None
    recent_messages: list[ChatMessage]
    memories: list[ContextMemory]
    estimated_chars: int
    retrieval_degraded: bool = False
    retrieval_reason: str = ""


@dataclass(frozen=True)
class OutboxProcessResult:
    claimed: int
    processed: int
    failed: int


class MemoryOrchestrator:
    def __init__(
        self,
        repository: ChatRepository,
        embedding_provider: EmbeddingProvider,
        vector_index: VectorIndex,
        *,
        recent_message_limit: int = 12,
        context_char_budget: int = 12000,
        retrieval_limit: int = 6,
        summary_threshold: int = 24,
        summary_keep_messages: int = 12,
        summary_char_limit: int = 4000,
        embedding_model: str = "intfloat/multilingual-e5-small",
        embedding_version: str = "1",
    ):
        self.repository = repository
        self.embedding_provider = embedding_provider
        self.vector_index = vector_index
        self.recent_message_limit = max(1, int(recent_message_limit))
        self.context_char_budget = max(1, int(context_char_budget))
        self.retrieval_limit = max(0, int(retrieval_limit))
        self.summary_keep_messages = max(1, int(summary_keep_messages))
        self.summary_threshold = max(self.summary_keep_messages, int(summary_threshold))
        self.requested_summary_char_limit = max(1, int(summary_char_limit))
        self.effective_summary_char_limit = max(
            _MIN_SUMMARY_CHAR_LIMIT,
            self.requested_summary_char_limit,
        )
        self.summary_line_char_limit = _SUMMARY_LINE_CHAR_LIMIT
        self.embedding_model = str(embedding_model)
        self.embedding_version = str(embedding_version)

    def build_context(
        self,
        session_id: str,
        *,
        project_id: str,
        scope: PrincipalScope,
        query: str,
    ) -> MemoryContext:
        self._refresh_summary(session_id, project_id=project_id, scope=scope)
        summary = self.repository.get_latest_summary(
            session_id, scope, project_id=project_id
        )
        recent = self.repository.list_messages(
            session_id,
            scope,
            project_id=project_id,
            limit=self.recent_message_limit,
        )
        exact = self.repository.search_active_memories(
            project_id, scope, query, self.retrieval_limit
        )
        candidates = [ContextMemory(item, 1.0, "exact") for item in exact]
        degraded = False
        reasons: list[str] = []

        if self.retrieval_limit and query.strip():
            if not getattr(self.embedding_provider, "available", False):
                degraded = True
                reasons.append(getattr(self.embedding_provider, "reason", "embedding unavailable"))
            elif not getattr(self.vector_index, "available", False):
                degraded = True
                reasons.append(getattr(self.vector_index, "reason", "vector unavailable"))
            else:
                try:
                    vectors = self.embedding_provider.embed([query])
                    if len(vectors) != 1:
                        raise ValueError("query embedding count must be one")
                    hits = self.vector_index.search(
                        vectors[0],
                        project_id=project_id,
                        scope=scope,
                        limit=self.retrieval_limit,
                    )
                    for hit in hits:
                        if hit.project_id != project_id or hit.scope != scope:
                            continue
                        try:
                            item = self.repository.get_memory(
                                hit.memory_id, scope, project_id=project_id
                            )
                        except Exception:
                            continue
                        if not _memory_is_active(item):
                            continue
                        candidates.append(ContextMemory(item, float(hit.score), "semantic"))
                except Exception as exc:
                    degraded = True
                    reasons.append(f"{type(exc).__name__}: {exc}")

        memories = self._rank_and_deduplicate(candidates)[: self.retrieval_limit]
        summary, recent, memories = self._fit_budget(summary, recent, memories)
        return MemoryContext(
            summary=summary,
            recent_messages=recent,
            memories=memories,
            estimated_chars=_estimated_chars(summary, recent, memories),
            retrieval_degraded=degraded,
            retrieval_reason="; ".join(reason for reason in reasons if reason),
        )

    def promote_user_confirmed(
        self,
        session_id: str,
        *,
        project_id: str,
        scope: PrincipalScope,
        canonical_text: str,
        confidence: float = 1.0,
        expires_at: str | None = None,
    ) -> MemoryItem:
        _, contains_sensitive_text = _sanitize_local_text(canonical_text)
        return self.repository.save_memory(
            project_id,
            canonical_text,
            "user_confirmed",
            scope,
            session_id=session_id,
            confidence=confidence,
            confidentiality=(
                "local_only" if contains_sensitive_text else "sanitized"
            ),
            expires_at=expires_at,
        )

    def vector_payload(self, item: MemoryItem) -> dict[str, Any]:
        return {
            "record_id": item.memory_id,
            "project_id": item.project_id,
            "session_id": item.session_id,
            "user_id": item.scope.user_id,
            "org_id": item.scope.org_id,
            "source_type": item.source_type,
            "confidentiality": item.confidentiality,
            "embedding_model": self.embedding_model,
            "embedding_version": self.embedding_version,
            "content_hash": item.content_hash,
            "created_at": item.created_at,
            "expires_at": item.expires_at,
        }

    def process_outbox_once(
        self,
        worker_id: str,
        *,
        limit: int = 100,
        lease_seconds: int = 60,
        max_attempts: int = 5,
    ) -> OutboxProcessResult:
        rows = self.repository.claim_outbox(
            worker_id, limit=limit, lease_seconds=lease_seconds
        )
        processed = 0
        failed = 0
        for row in rows:
            try:
                if not getattr(self.vector_index, "available", False):
                    raise RuntimeError(
                        getattr(self.vector_index, "reason", "vector unavailable")
                    )
                if row["action"] == "delete":
                    self.vector_index.delete(str(row["memory_id"]))
                elif row["action"] == "upsert":
                    scope = PrincipalScope(row.get("user_id"), row.get("org_id"))
                    item = self.repository.get_memory(
                        str(row["memory_id"]),
                        scope,
                        project_id=str(row["project_id"]),
                    )
                    if not _memory_is_active(item):
                        self.vector_index.delete(item.memory_id)
                    else:
                        if not getattr(self.embedding_provider, "available", False):
                            raise RuntimeError(
                                getattr(
                                    self.embedding_provider,
                                    "reason",
                                    "embedding unavailable",
                                )
                            )
                        vectors = self.embedding_provider.embed([item.canonical_text])
                        if len(vectors) != 1:
                            raise ValueError("memory embedding count must be one")
                        self.vector_index.upsert(
                            item.memory_id, vectors[0], self.vector_payload(item)
                        )
                else:
                    raise ValueError("unsupported outbox action")
                self.repository.mark_outbox_processed(str(row["outbox_id"]), worker_id)
                processed += 1
            except Exception as exc:
                if isinstance(exc, OutboxLeaseLost):
                    failed += 1
                    continue
                self.repository.mark_outbox_failed(
                    str(row["outbox_id"]),
                    worker_id,
                    type(exc).__name__,
                    max_attempts=max_attempts,
                )
                failed += 1
        return OutboxProcessResult(len(rows), processed, failed)

    def _refresh_summary(
        self, session_id: str, *, project_id: str, scope: PrincipalScope
    ) -> MemorySummary | None:
        messages = self.repository.list_messages(
            session_id, scope, project_id=project_id
        )
        if len(messages) <= self.summary_threshold:
            return self.repository.get_latest_summary(
                session_id, scope, project_id=project_id
            )
        cutoff = len(messages) - self.summary_keep_messages
        eligible = messages[:cutoff]
        latest = self.repository.get_latest_summary(
            session_id, scope, project_id=project_id
        )
        start_index = 0
        if latest is not None:
            by_id = {message.message_id: index for index, message in enumerate(messages)}
            covered_end = by_id.get(latest.message_end_id)
            if covered_end is not None:
                start_index = covered_end + 1
        new_messages = eligible[start_index:]
        if not new_messages:
            return latest
        extracted = _extract_summary(new_messages)
        content = _merge_summary(
            latest.content if latest is not None else "",
            extracted,
            self.effective_summary_char_limit,
        )
        start_id = latest.message_start_id if latest is not None else new_messages[0].message_id
        return self.repository.save_summary(
            session_id,
            content,
            start_id,
            new_messages[-1].message_id,
            scope,
            project_id=project_id,
        )

    def _rank_and_deduplicate(
        self, candidates: list[ContextMemory]
    ) -> list[ContextMemory]:
        by_memory: dict[str, ContextMemory] = {}
        for candidate in candidates:
            current = by_memory.get(candidate.memory_id)
            if current is None or candidate.score > current.score:
                by_memory[candidate.memory_id] = candidate
        ranked = sorted(by_memory.values(), key=_memory_sort_key, reverse=True)
        result: list[ContextMemory] = []
        hashes: set[str] = set()
        for candidate in ranked:
            if candidate.content_hash in hashes:
                continue
            hashes.add(candidate.content_hash)
            result.append(candidate)
        return result

    def _fit_budget(
        self,
        summary: MemorySummary | None,
        recent: list[ChatMessage],
        memories: list[ContextMemory],
    ) -> tuple[MemorySummary | None, list[ChatMessage], list[ContextMemory]]:
        recent = list(recent)
        memories = list(memories)
        while memories and _estimated_chars(summary, recent, memories) > self.context_char_budget:
            memories.pop()
        while len(recent) > 1 and _estimated_chars(summary, recent, memories) > self.context_char_budget:
            recent.pop(0)
        if summary is not None and _estimated_chars(summary, recent, memories) > self.context_char_budget:
            remaining = self.context_char_budget - _estimated_chars(None, recent, memories)
            if remaining > 0:
                content = summary.content[:remaining]
                summary = replace(
                    summary,
                    content=content,
                    content_hash=_content_hash(content),
                )
            else:
                summary = None
        if recent and _estimated_chars(summary, recent, memories) > self.context_char_budget:
            others = _estimated_chars(summary, [], memories)
            allowed = max(0, self.context_char_budget - others)
            content = recent[-1].content[:allowed]
            recent[-1] = replace(
                recent[-1],
                content=content,
                content_hash=_content_hash(content),
                token_estimate=_token_estimate(content),
            )
        return summary, recent, memories


def _memory_sort_key(candidate: ContextMemory) -> tuple[Any, ...]:
    return (
        _AUTHORITY.get(candidate.source_type, 0),
        candidate.created_at,
        float(candidate.confidence),
        float(candidate.score),
        candidate.memory_id,
    )


def _memory_is_active(item: MemoryItem) -> bool:
    if item.status != "active":
        return False
    if not item.expires_at:
        return True
    return str(item.expires_at) > beijing_now_text()


def _estimated_chars(
    summary: MemorySummary | None,
    recent: list[ChatMessage],
    memories: list[ContextMemory],
) -> int:
    return (
        (len(summary.content) if summary is not None else 0)
        + sum(len(message.content) for message in recent)
        + sum(len(memory.canonical_text) for memory in memories)
    )


def _extract_summary(messages: list[ChatMessage]) -> str:
    lines = []
    for message in messages:
        text = _redact(message.content).replace("\r", " ").replace("\n", " ").strip()
        if text:
            prefix = f"{message.role}: "
            width = max(1, _SUMMARY_LINE_CHAR_LIMIT - len(prefix))
            chunks = _summary_chunks(text, width)
            lines.extend(f"{prefix}{chunk}" for chunk in chunks)
    return "\n".join(lines)


def _merge_summary(previous: str, extracted: str, limit: int) -> str:
    previous_lines = _bounded_summary_lines(previous)
    extracted_lines = _bounded_summary_lines(extracted)
    if not previous_lines and not extracted_lines:
        return ""

    selected_previous: set[int] = set()
    selected_extracted: set[int] = set()
    if previous_lines:
        selected_previous.update((0, len(previous_lines) - 1))
    if extracted_lines:
        selected_extracted.add(len(extracted_lines) - 1)

    def selected_lines() -> list[str]:
        return [
            *(line for index, line in enumerate(previous_lines) if index in selected_previous),
            *(line for index, line in enumerate(extracted_lines) if index in selected_extracted),
        ]

    effective_limit = max(_MIN_SUMMARY_CHAR_LIMIT, int(limit))
    optional = [
        *(("extracted", index) for index in range(len(extracted_lines) - 2, -1, -1)),
        *(("previous", index) for index in range(len(previous_lines) - 2, 0, -1)),
    ]
    for source, index in optional:
        selected = selected_extracted if source == "extracted" else selected_previous
        selected.add(index)
        if len("\n".join(selected_lines())) > effective_limit:
            selected.remove(index)
    return "\n".join(selected_lines())


def _bounded_summary_lines(value: str) -> list[str]:
    lines: list[str] = []
    for line in value.splitlines():
        if not line:
            continue
        lines.extend(
            textwrap.wrap(
                line,
                width=_SUMMARY_LINE_CHAR_LIMIT,
                break_long_words=True,
                break_on_hyphens=False,
                replace_whitespace=False,
            )
        )
    return lines


def _summary_chunks(value: str, width: int) -> list[str]:
    chunks: list[str] = []
    segments = [
        segment.strip()
        for segment in re.split(r"(?<=[;；])\s*", value)
        if segment.strip()
    ]
    for segment in segments:
        chunks.extend(
            textwrap.wrap(
                segment,
                width=width,
                break_long_words=True,
                break_on_hyphens=False,
                replace_whitespace=False,
            )
        )
    return chunks or [""]


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _token_estimate(content: str) -> int:
    return max(1, (len(content) + 3) // 4)


def _redact(text: str) -> str:
    return _sanitize_local_text(text)[0]


def _sanitize_local_text(text: str) -> tuple[str, bool]:
    value = str(text)
    sensitive = False

    def substitute(pattern: re.Pattern[str], replacement: Any) -> None:
        nonlocal value, sensitive
        if pattern.search(value) is None:
            return
        sensitive = True
        value = pattern.sub(replacement, value)

    substitute(_DATABASE_URL, "[REDACTED_DATABASE_URL]")
    substitute(_HTTP_URL, "[REDACTED_URL]")
    substitute(_JWT, "[REDACTED_JWT]")
    substitute(_BASIC_AUTH, "Authorization: Basic [REDACTED]")
    substitute(_BEARER, "Bearer [REDACTED]")
    substitute(_TOKEN_PREFIX, "Token [REDACTED]")
    substitute(
        _CHINESE_ID,
        lambda match: f"{match.group(1)}=[REDACTED_ID]",
    )
    substitute(
        _BANK_CARD,
        lambda match: f"{match.group(1)}=[REDACTED_BANK_CARD]",
    )
    substitute(
        _SENSITIVE_ASSIGNMENT,
        lambda match: f"{match.group(1)}=[REDACTED]",
    )
    substitute(_EMAIL, "[REDACTED_EMAIL]")

    def redact_phone(match: re.Match[str]) -> str:
        nonlocal sensitive
        if _BUSINESS_NUMBER_LABEL.search(value[: match.start()]):
            return match.group(0)
        sensitive = True
        return "[REDACTED_PHONE]"

    value = _PHONE.sub(redact_phone, value)
    return value, sensitive
