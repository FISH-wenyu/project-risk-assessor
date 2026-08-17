from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from app.risk.time_utils import beijing_now_text
from app.sqlite_support import SQLiteStoreMixin

from .models import (
    ChatMessage,
    ChatNotFound,
    ChatSession,
    Citation,
    IdempotencyConflict,
    MemoryItem,
    MemorySummary,
    MessageWriteResult,
    OutboxLeaseLost,
    PrincipalScope,
    SessionScopeMismatch,
    ValidationError,
)


_SESSION_STATUSES = {"active", "archived"}
_MESSAGE_ROLES = {"user", "assistant", "system"}
_MEMORY_STATUSES = {"active", "superseded", "expired"}


class SQLiteAuthorityStore(SQLiteStoreMixin):
    def __init__(self, db_path: str | Path):
        self._prepare_database(db_path)
        self._init_db()

    def _connect(self, *, row_factory: bool = True) -> sqlite3.Connection:
        return super()._connect(row_factory=row_factory)

    def create_session(
        self, project_id: str, title: str, scope: PrincipalScope
    ) -> ChatSession:
        project_id = _required_text(project_id, "project_id")
        title = _required_text(title, "title")
        scope = _scope(scope)
        session_id = _new_id()
        now = beijing_now_text()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO chat_sessions (
                    session_id, project_id, title, status, user_id, org_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?)
                """,
                (session_id, project_id, title, scope.user_id, scope.org_id, now, now),
            )
        return ChatSession(session_id, project_id, title, "active", scope, now, now)

    def get_session(
        self,
        session_id: str,
        scope: PrincipalScope,
        *,
        project_id: str,
    ) -> ChatSession:
        with closing(self._connect()) as conn:
            row = self._require_session_row(conn, session_id, scope, project_id)
        return _session_from_row(row)

    def get_session_by_id(
        self,
        session_id: str,
        scope: PrincipalScope,
    ) -> ChatSession:
        session_id = _required_text(session_id, "session_id")
        scope = _scope(scope)
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM chat_sessions
                WHERE session_id = ? AND user_id IS ? AND org_id IS ?
                """,
                (session_id, scope.user_id, scope.org_id),
            ).fetchone()
        if row is None:
            raise ChatNotFound("Chat session not found")
        return _session_from_row(row)

    def list_sessions(
        self, project_id: str, scope: PrincipalScope, limit: int = 100
    ) -> list[ChatSession]:
        project_id = _required_text(project_id, "project_id")
        scope = _scope(scope)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM chat_sessions
                WHERE project_id = ? AND user_id IS ? AND org_id IS ?
                ORDER BY updated_at DESC, created_at DESC, session_id DESC
                LIMIT ?
                """,
                (project_id, scope.user_id, scope.org_id, _limit(limit)),
            ).fetchall()
        return [_session_from_row(row) for row in rows]

    def update_session(
        self,
        session_id: str,
        scope: PrincipalScope,
        *,
        project_id: str,
        title: str | None = None,
        status: str | None = None,
    ) -> ChatSession:
        if title is None and status is None:
            return self.get_session(session_id, scope, project_id=project_id)
        if title is not None:
            title = _required_text(title, "title")
        if status is not None and status not in _SESSION_STATUSES:
            raise ValidationError(f"Unsupported session status: {status}")
        session_id = _required_text(session_id, "session_id")
        project_id = _required_text(project_id, "project_id")
        scope = _scope(scope)
        assignments: list[str] = []
        params: list[Any] = []
        if title is not None:
            assignments.append("title = ?")
            params.append(title)
        if status is not None:
            assignments.append("status = ?")
            params.append(status)
        now = beijing_now_text()
        assignments.append("updated_at = ?")
        params.append(now)
        params.extend((session_id, project_id, scope.user_id, scope.org_id))
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                f"""
                UPDATE chat_sessions SET {', '.join(assignments)}
                WHERE session_id = ? AND project_id = ?
                  AND user_id IS ? AND org_id IS ?
                """,
                params,
            )
            if cursor.rowcount == 0:
                self._require_session_row(conn, session_id, scope, project_id)
            updated = conn.execute(
                "SELECT * FROM chat_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return _session_from_row(updated)

    def archive_session(
        self, session_id: str, scope: PrincipalScope, *, project_id: str
    ) -> ChatSession:
        return self.update_session(
            session_id, scope, project_id=project_id, status="archived"
        )

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        idempotency_key: str,
        scope: PrincipalScope,
        *,
        project_id: str,
    ) -> MessageWriteResult:
        role, content, idempotency_key = _message_input(role, content, idempotency_key)
        operation_hash = _operation_hash(
            {"operation": "append_message", "role": role, "content": content}
        )
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                message, created = self._append_message_in_transaction(
                    conn,
                    session_id,
                    role,
                    content,
                    idempotency_key,
                    operation_hash,
                    None,
                    scope,
                    project_id,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return MessageWriteResult(message, created)

    def get_message_by_idempotency_key(
        self,
        session_id: str,
        idempotency_key: str,
        scope: PrincipalScope,
        *,
        project_id: str,
    ) -> ChatMessage | None:
        idempotency_key = _required_text(idempotency_key, "idempotency_key")
        with closing(self._connect()) as conn:
            self._require_session_row(conn, session_id, scope, project_id)
            row = conn.execute(
                """
                SELECT * FROM chat_messages
                WHERE session_id = ? AND idempotency_key = ?
                """,
                (session_id, idempotency_key),
            ).fetchone()
        return _message_from_row(row) if row else None

    def list_messages(
        self,
        session_id: str,
        scope: PrincipalScope,
        *,
        project_id: str,
        limit: int | None = None,
    ) -> list[ChatMessage]:
        with closing(self._connect()) as conn:
            self._require_session_row(conn, session_id, scope, project_id)
            if limit is None:
                rows = conn.execute(
                    "SELECT * FROM chat_messages WHERE session_id = ? ORDER BY sequence_no",
                    (session_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM (
                        SELECT * FROM chat_messages
                        WHERE session_id = ? ORDER BY sequence_no DESC LIMIT ?
                    ) ORDER BY sequence_no
                    """,
                    (session_id, _limit(limit)),
                ).fetchall()
        return [_message_from_row(row) for row in rows]

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
    ) -> MessageWriteResult:
        role, content, idempotency_key = _message_input(
            "assistant", content, idempotency_key
        )
        reply_to_message_id = _optional_text(reply_to_message_id)
        normalized_citations = _normalize_citations(citations)
        safe_audit = _normalize_llm_audit(audit, len(content))
        operation_hash = _operation_hash(
            {
                "operation": "assistant_bundle",
                "role": role,
                "content": content,
                "reply_to_message_id": reply_to_message_id,
                "citations": [
                    {
                        "source_type": item.source_type,
                        "source_id": item.source_id,
                        "label": item.label,
                        "locator": item.locator,
                    }
                    for item in normalized_citations
                ],
                "audit": safe_audit,
            }
        )
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                session_row = self._require_session_row(
                    conn, session_id, scope, project_id
                )
                reply_to_message_id = self._validate_reply_target(
                    conn, session_id, reply_to_message_id
                )
                message, created = self._append_message_in_transaction(
                    conn,
                    session_id,
                    role,
                    content,
                    idempotency_key,
                    operation_hash,
                    reply_to_message_id,
                    scope,
                    project_id,
                )
                if created:
                    for citation in normalized_citations:
                        conn.execute(
                            """
                            INSERT INTO chat_message_citations (
                                citation_id, message_id, source_type, source_id, label, locator
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                _required_text(citation.citation_id, "citation_id"),
                                message.message_id,
                                _required_text(citation.source_type, "citation source_type"),
                                _required_text(citation.source_id, "citation source_id"),
                                str(citation.label or ""),
                                str(citation.locator or ""),
                            ),
                        )
                    self._insert_llm_audit(
                        conn,
                        message,
                        str(session_row["project_id"]),
                        safe_audit,
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return MessageWriteResult(message, created)

    def list_citations(
        self, message_id: str, scope: PrincipalScope, *, project_id: str
    ) -> list[Citation]:
        with closing(self._connect()) as conn:
            message_row = conn.execute(
                "SELECT session_id FROM chat_messages WHERE message_id = ?", (message_id,)
            ).fetchone()
            if message_row is None:
                raise ChatNotFound(f"Message not found: {message_id}")
            self._require_session_row(
                conn, str(message_row["session_id"]), scope, project_id
            )
            rows = conn.execute(
                "SELECT * FROM chat_message_citations WHERE message_id = ? ORDER BY rowid",
                (message_id,),
            ).fetchall()
        return [_citation_from_row(row) for row in rows]

    def get_llm_audit(
        self, message_id: str, scope: PrincipalScope, *, project_id: str
    ) -> dict[str, Any] | None:
        message_id = _required_text(message_id, "message_id")
        project_id = _required_text(project_id, "project_id")
        scope = _scope(scope)
        with closing(self._connect()) as conn:
            message_row = conn.execute(
                "SELECT session_id FROM chat_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if message_row is None:
                raise ChatNotFound("Chat message not found")
            self._require_session_row(
                conn, str(message_row["session_id"]), scope, project_id
            )
            row = conn.execute(
                """
                SELECT * FROM chat_llm_audit
                WHERE message_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (message_id,),
            ).fetchone()
        return dict(row) if row else None

    def save_summary(
        self,
        session_id: str,
        content: str,
        message_start_id: str,
        message_end_id: str,
        scope: PrincipalScope,
        *,
        project_id: str,
    ) -> MemorySummary:
        content = _required_text(content, "summary content")
        now = beijing_now_text()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._require_session_row(conn, session_id, scope, project_id)
                bounds = conn.execute(
                    """
                    SELECT
                        (SELECT sequence_no FROM chat_messages WHERE message_id = ? AND session_id = ?) AS start_no,
                        (SELECT sequence_no FROM chat_messages WHERE message_id = ? AND session_id = ?) AS end_no
                    """,
                    (message_start_id, session_id, message_end_id, session_id),
                ).fetchone()
                if bounds["start_no"] is None or bounds["end_no"] is None:
                    raise ValidationError("Summary message range must belong to the session")
                if int(bounds["start_no"]) > int(bounds["end_no"]):
                    raise ValidationError("Summary message range is reversed")
                version = int(
                    conn.execute(
                        "SELECT COALESCE(MAX(version), 0) + 1 FROM memory_summaries WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()[0]
                )
                summary_id = _new_id()
                content_hash = _hash_text(content)
                conn.execute(
                    """
                    INSERT INTO memory_summaries (
                        summary_id, session_id, version, content, content_hash,
                        message_start_id, message_end_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        summary_id,
                        session_id,
                        version,
                        content,
                        content_hash,
                        message_start_id,
                        message_end_id,
                        now,
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return MemorySummary(
            summary_id,
            session_id,
            version,
            content,
            content_hash,
            message_start_id,
            message_end_id,
            now,
        )

    def get_latest_summary(
        self, session_id: str, scope: PrincipalScope, *, project_id: str
    ) -> MemorySummary | None:
        with closing(self._connect()) as conn:
            self._require_session_row(conn, session_id, scope, project_id)
            row = conn.execute(
                """
                SELECT * FROM memory_summaries
                WHERE session_id = ? ORDER BY version DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return _summary_from_row(row) if row else None

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
    ) -> MemoryItem:
        project_id = _required_text(project_id, "project_id")
        canonical_text = _required_text(canonical_text, "canonical_text")
        source_type = _required_text(source_type, "source_type")
        scope = _scope(scope)
        confidence = _confidence(confidence)
        confidentiality = _required_text(confidentiality, "confidentiality")
        now = beijing_now_text()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if session_id is not None:
                    self._require_session_row(conn, session_id, scope, project_id)
                item = self._insert_memory(
                    conn,
                    project_id=project_id,
                    session_id=session_id,
                    scope=scope,
                    source_type=source_type,
                    canonical_text=canonical_text,
                    confidence=confidence,
                    confidentiality=confidentiality,
                    expires_at=expires_at,
                    supersedes_id=None,
                    now=now,
                )
                self._insert_memory_audit(conn, item, "created", None, item.content_hash, now)
                self._insert_outbox(conn, item, "upsert", now)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return item

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
    ) -> MemoryItem:
        canonical_text = _required_text(canonical_text, "canonical_text")
        now = beijing_now_text()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                old_row = self._require_memory_row(
                    conn, memory_id, scope, project_id
                )
                old = _memory_from_row(old_row)
                if old.status != "active":
                    raise ValidationError("Only active memory can be superseded")
                conn.execute(
                    "UPDATE memory_items SET status = 'superseded', updated_at = ? WHERE memory_id = ?",
                    (now, memory_id),
                )
                replacement = self._insert_memory(
                    conn,
                    project_id=old.project_id,
                    session_id=old.session_id,
                    scope=old.scope,
                    source_type=source_type or old.source_type,
                    canonical_text=canonical_text,
                    confidence=old.confidence if confidence is None else _confidence(confidence),
                    confidentiality=confidentiality or old.confidentiality,
                    expires_at=old.expires_at if expires_at is None else expires_at,
                    supersedes_id=old.memory_id,
                    now=now,
                )
                self._insert_memory_audit(
                    conn, old, "superseded", old.content_hash, replacement.content_hash, now
                )
                self._insert_memory_audit(
                    conn, replacement, "created", old.content_hash, replacement.content_hash, now
                )
                self._insert_outbox(conn, old, "delete", now)
                self._insert_outbox(conn, replacement, "upsert", now)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return replacement

    def expire_memory(
        self, memory_id: str, scope: PrincipalScope, *, project_id: str
    ) -> MemoryItem:
        now = beijing_now_text()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._require_memory_row(conn, memory_id, scope, project_id)
                item = _memory_from_row(row)
                if item.status == "superseded":
                    raise ValidationError("Superseded memory cannot be expired")
                conn.execute(
                    "UPDATE memory_items SET status = 'expired', updated_at = ? WHERE memory_id = ?",
                    (now, memory_id),
                )
                expired = MemoryItem(
                    item.memory_id,
                    item.project_id,
                    item.session_id,
                    item.scope,
                    item.source_type,
                    item.canonical_text,
                    item.content_hash,
                    "expired",
                    item.confidence,
                    item.confidentiality,
                    item.expires_at,
                    item.supersedes_id,
                    item.created_at,
                    now,
                )
                self._insert_memory_audit(
                    conn, expired, "expired", item.content_hash, item.content_hash, now
                )
                self._insert_outbox(conn, expired, "delete", now)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return expired

    def get_memory(
        self, memory_id: str, scope: PrincipalScope, *, project_id: str
    ) -> MemoryItem:
        with closing(self._connect()) as conn:
            row = self._require_memory_row(conn, memory_id, scope, project_id)
        return _memory_from_row(row)

    def list_memories(
        self,
        project_id: str,
        scope: PrincipalScope,
        limit: int = 100,
        *,
        now: str | None = None,
    ) -> list[MemoryItem]:
        project_id = _required_text(project_id, "project_id")
        scope = _scope(scope)
        now = now or beijing_now_text()
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM memory_items
                WHERE project_id = ? AND user_id IS ? AND org_id IS ?
                  AND status = 'active'
                  AND (expires_at IS NULL OR expires_at = '' OR expires_at > ?)
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (project_id, scope.user_id, scope.org_id, now, _limit(limit)),
            ).fetchall()
        return [_memory_from_row(row) for row in rows]

    def search_active_memories(
        self,
        project_id: str,
        scope: PrincipalScope,
        query: str,
        limit: int,
    ) -> list[MemoryItem]:
        project_id = _required_text(project_id, "project_id")
        query = _required_text(query, "query")
        scope = _scope(scope)
        now = beijing_now_text()
        terms = _memory_search_terms(query)
        patterns = [f"%{_escape_like(term)}%" for term in terms]
        minimum_hits = _memory_search_minimum_hits(terms)
        score_sql = " + ".join(
            "CASE WHEN canonical_text LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END"
            for _ in patterns
        )
        match_sql = " OR ".join(
            "canonical_text LIKE ? ESCAPE '\\'" for _ in patterns
        )
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT memory_items.*, ({score_sql}) AS match_count
                FROM memory_items
                WHERE project_id = ? AND user_id IS ? AND org_id IS ?
                  AND status = 'active'
                  AND (expires_at IS NULL OR expires_at = '' OR expires_at > ?)
                  AND ({match_sql})
                  AND ({score_sql}) >= ?
                ORDER BY match_count DESC, created_at DESC, memory_items.rowid DESC
                LIMIT ?
                """,
                (
                    *patterns,
                    project_id,
                    scope.user_id,
                    scope.org_id,
                    now,
                    *patterns,
                    *patterns,
                    minimum_hits,
                    _limit(limit),
                ),
            ).fetchall()
        return [_memory_from_row(row) for row in rows]

    def list_pending_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM memory_index_outbox
                WHERE status = 'pending'
                ORDER BY next_attempt_at, created_at, rowid LIMIT ?
                """,
                (_limit(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_outbox(
        self,
        worker_id: str,
        limit: int = 100,
        lease_seconds: int = 60,
        *,
        now: str | None = None,
    ) -> list[dict[str, Any]]:
        worker_id = _required_text(worker_id, "worker_id")[:100]
        now = now or beijing_now_text()
        lease_expires_at = _add_seconds(now, max(1, min(int(lease_seconds), 3600)))
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    """
                    SELECT outbox_id FROM memory_index_outbox
                    WHERE (
                        status = 'pending'
                        AND (next_attempt_at = '' OR next_attempt_at <= ?)
                    ) OR (
                        status = 'processing'
                        AND lease_expires_at IS NOT NULL
                        AND lease_expires_at <= ?
                    )
                    ORDER BY created_at, rowid
                    LIMIT ?
                    """,
                    (now, now, _limit(limit)),
                ).fetchall()
                outbox_ids = [str(row["outbox_id"]) for row in rows]
                if not outbox_ids:
                    conn.commit()
                    return []
                placeholders = ",".join("?" for _ in outbox_ids)
                conn.execute(
                    f"""
                    UPDATE memory_index_outbox
                    SET status = 'processing', claimed_by = ?, lease_expires_at = ?,
                        attempt_count = attempt_count + 1, last_error_type = ''
                    WHERE outbox_id IN ({placeholders})
                    """,
                    (worker_id, lease_expires_at, *outbox_ids),
                )
                claimed_rows = conn.execute(
                    f"SELECT * FROM memory_index_outbox WHERE outbox_id IN ({placeholders})",
                    outbox_ids,
                ).fetchall()
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        by_id = {str(row["outbox_id"]): dict(row) for row in claimed_rows}
        return [by_id[outbox_id] for outbox_id in outbox_ids]

    def mark_outbox_processed(self, outbox_id: str, worker_id: str) -> None:
        outbox_id = _required_text(outbox_id, "outbox_id")
        worker_id = _required_text(worker_id, "worker_id")[:100]
        now = beijing_now_text()
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                UPDATE memory_index_outbox
                SET status = 'processed', processed_at = ?, last_error_type = '',
                    claimed_by = NULL, lease_expires_at = NULL
                WHERE outbox_id = ? AND status = 'processing' AND claimed_by = ?
                """,
                (now, outbox_id, worker_id),
            )
            if cursor.rowcount == 0:
                raise OutboxLeaseLost("Outbox item is not leased by this worker")

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
        outbox_id = _required_text(outbox_id, "outbox_id")
        worker_id = _required_text(worker_id, "worker_id")[:100]
        safe_error_type = _sanitize_error_type(error_type) or "UnknownError"
        now = now or beijing_now_text()
        max_attempts = max(1, min(int(max_attempts), 20))
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM memory_index_outbox WHERE outbox_id = ?",
                    (outbox_id,),
                ).fetchone()
                if row is None:
                    raise ChatNotFound(f"Outbox item not found: {outbox_id}")
                if row["status"] != "processing" or row["claimed_by"] != worker_id:
                    raise OutboxLeaseLost("Outbox item is not leased by this worker")
                attempt_count = int(row["attempt_count"] or 0)
                is_dead = bool(permanent) or attempt_count >= max_attempts
                delay_seconds = min(300, 5 * (2 ** max(0, attempt_count - 1)))
                next_attempt_at = "" if is_dead else _add_seconds(now, delay_seconds)
                conn.execute(
                    """
                    UPDATE memory_index_outbox
                    SET status = ?, next_attempt_at = ?, claimed_by = NULL,
                        lease_expires_at = NULL, last_error_type = ?
                    WHERE outbox_id = ?
                    """,
                    (
                        "dead" if is_dead else "pending",
                        next_attempt_at,
                        safe_error_type,
                        outbox_id,
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _append_message_in_transaction(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        role: str,
        content: str,
        idempotency_key: str,
        operation_hash: str,
        reply_to_message_id: str | None,
        scope: PrincipalScope,
        project_id: str,
    ) -> tuple[ChatMessage, bool]:
        self._require_session_row(conn, session_id, scope, project_id)
        existing = conn.execute(
            """
            SELECT * FROM chat_messages
            WHERE session_id = ? AND idempotency_key = ?
            """,
            (session_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["role"]) != role
                or str(existing["content"]) != content
                or str(existing["operation_hash"] or "") != operation_hash
                or existing["reply_to_message_id"] != reply_to_message_id
            ):
                raise IdempotencyConflict(
                    "Idempotency key was already used with a different message payload"
                )
            return _message_from_row(existing), False
        sequence_no = int(
            conn.execute(
                "SELECT COALESCE(MAX(sequence_no), 0) + 1 FROM chat_messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
        )
        now = beijing_now_text()
        message = ChatMessage(
            _new_id(),
            session_id,
            sequence_no,
            role,
            content,
            _hash_text(content),
            _token_estimate(content),
            now,
            reply_to_message_id,
        )
        conn.execute(
            """
            INSERT INTO chat_messages (
                message_id, session_id, sequence_no, role, content, content_hash,
                token_estimate, idempotency_key, operation_hash,
                reply_to_message_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.message_id,
                session_id,
                sequence_no,
                role,
                content,
                message.content_hash,
                message.token_estimate,
                idempotency_key,
                operation_hash,
                reply_to_message_id,
                now,
            ),
        )
        conn.execute(
            "UPDATE chat_sessions SET updated_at = ? WHERE session_id = ?", (now, session_id)
        )
        return message, True

    def _validate_reply_target(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        reply_to_message_id: str | None,
    ) -> str | None:
        if reply_to_message_id is None:
            return None
        row = conn.execute(
            """
            SELECT session_id, role FROM chat_messages WHERE message_id = ?
            """,
            (reply_to_message_id,),
        ).fetchone()
        if row is None:
            raise ValidationError("reply_to_message_id was not found")
        if str(row["session_id"]) != session_id:
            raise ValidationError("reply_to_message_id must belong to the same session")
        if str(row["role"]) != "user":
            raise ValidationError("Assistant replies must reference a user message")
        return reply_to_message_id

    def _require_session_row(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        scope: PrincipalScope,
        project_id: str,
    ) -> sqlite3.Row:
        session_id = _required_text(session_id, "session_id")
        scope = _scope(scope)
        row = conn.execute(
            "SELECT * FROM chat_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise ChatNotFound(f"Session not found: {session_id}")
        project_id = _required_text(project_id, "project_id")
        if str(row["project_id"]) != project_id:
            raise SessionScopeMismatch("Session does not belong to the requested project")
        if row["user_id"] != scope.user_id or row["org_id"] != scope.org_id:
            raise SessionScopeMismatch("Session principal scope does not match")
        return row

    def _require_memory_row(
        self,
        conn: sqlite3.Connection,
        memory_id: str,
        scope: PrincipalScope,
        project_id: str,
    ) -> sqlite3.Row:
        memory_id = _required_text(memory_id, "memory_id")
        scope = _scope(scope)
        row = conn.execute(
            "SELECT * FROM memory_items WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            raise ChatNotFound(f"Memory not found: {memory_id}")
        project_id = _required_text(project_id, "project_id")
        if str(row["project_id"]) != project_id:
            raise SessionScopeMismatch("Memory does not belong to the requested project")
        if row["user_id"] != scope.user_id or row["org_id"] != scope.org_id:
            raise SessionScopeMismatch("Memory principal scope does not match")
        return row

    def _insert_memory(
        self,
        conn: sqlite3.Connection,
        *,
        project_id: str,
        session_id: str | None,
        scope: PrincipalScope,
        source_type: str,
        canonical_text: str,
        confidence: float,
        confidentiality: str,
        expires_at: str | None,
        supersedes_id: str | None,
        now: str,
    ) -> MemoryItem:
        memory_id = _new_id()
        content_hash = _hash_text(canonical_text)
        conn.execute(
            """
            INSERT INTO memory_items (
                memory_id, project_id, session_id, user_id, org_id, source_type,
                canonical_text, content_hash, status, confidence, confidentiality,
                expires_at, supersedes_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)
            """,
            (
                memory_id,
                project_id,
                session_id,
                scope.user_id,
                scope.org_id,
                source_type,
                canonical_text,
                content_hash,
                confidence,
                confidentiality,
                expires_at,
                supersedes_id,
                now,
                now,
            ),
        )
        return MemoryItem(
            memory_id,
            project_id,
            session_id,
            scope,
            source_type,
            canonical_text,
            content_hash,
            "active",
            confidence,
            confidentiality,
            expires_at,
            supersedes_id,
            now,
            now,
        )

    def _insert_memory_audit(
        self,
        conn: sqlite3.Connection,
        item: MemoryItem,
        action: str,
        previous_hash: str | None,
        new_hash: str | None,
        now: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO memory_audit (
                audit_id, memory_id, project_id, session_id, user_id, org_id,
                action, source_type, previous_content_hash, new_content_hash,
                confidentiality, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _new_id(),
                item.memory_id,
                item.project_id,
                item.session_id,
                item.scope.user_id,
                item.scope.org_id,
                action,
                item.source_type,
                previous_hash,
                new_hash,
                item.confidentiality,
                now,
            ),
        )

    def _insert_outbox(
        self, conn: sqlite3.Connection, item: MemoryItem, action: str, now: str
    ) -> None:
        conn.execute(
            """
            INSERT INTO memory_index_outbox (
                outbox_id, memory_id, project_id, session_id, user_id, org_id,
                action, content_hash, source_type, confidentiality, status,
                attempt_count, next_attempt_at, lease_expires_at, claimed_by,
                last_error_type, created_at, processed_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL, NULL,
                '', ?, NULL
            )
            """,
            (
                _new_id(),
                item.memory_id,
                item.project_id,
                item.session_id,
                item.scope.user_id,
                item.scope.org_id,
                action,
                item.content_hash,
                item.source_type,
                item.confidentiality,
                now,
                now,
            ),
        )

    def _insert_llm_audit(
        self,
        conn: sqlite3.Connection,
        message: ChatMessage,
        project_id: str,
        audit: Mapping[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO chat_llm_audit (
                audit_id, message_id, session_id, project_id, request_hash,
                response_hash, prompt_chars, response_chars, model,
                fallback_used, status, error_type, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _new_id(),
                message.message_id,
                message.session_id,
                project_id,
                str(audit.get("request_hash") or ""),
                str(audit.get("response_hash") or ""),
                max(0, int(audit.get("prompt_chars") or 0)),
                max(0, int(audit.get("response_chars") or len(message.content))),
                str(audit.get("model") or ""),
                int(bool(audit.get("fallback_used", False))),
                str(audit.get("status") or "unknown")[:50],
                str(audit.get("error_type") or "")[:100],
                beijing_now_text(),
            ),
        )

    def _init_db(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    session_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('active', 'archived')),
                    user_id TEXT,
                    org_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chat_messages (
                    message_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES chat_sessions(session_id),
                    sequence_no INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    token_estimate INTEGER NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    operation_hash TEXT NOT NULL DEFAULT '',
                    reply_to_message_id TEXT REFERENCES chat_messages(message_id),
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, sequence_no),
                    UNIQUE(session_id, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS chat_message_citations (
                    citation_id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL REFERENCES chat_messages(message_id) ON DELETE CASCADE,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    locator TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS memory_summaries (
                    summary_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES chat_sessions(session_id),
                    version INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    message_start_id TEXT NOT NULL REFERENCES chat_messages(message_id),
                    message_end_id TEXT NOT NULL REFERENCES chat_messages(message_id),
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, version)
                );

                CREATE TABLE IF NOT EXISTS memory_items (
                    memory_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    session_id TEXT REFERENCES chat_sessions(session_id),
                    user_id TEXT,
                    org_id TEXT,
                    source_type TEXT NOT NULL,
                    canonical_text TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('active', 'superseded', 'expired')),
                    confidence REAL NOT NULL,
                    confidentiality TEXT NOT NULL,
                    expires_at TEXT,
                    supersedes_id TEXT REFERENCES memory_items(memory_id),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_audit (
                    audit_id TEXT PRIMARY KEY,
                    memory_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    session_id TEXT,
                    user_id TEXT,
                    org_id TEXT,
                    action TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    previous_content_hash TEXT,
                    new_content_hash TEXT,
                    confidentiality TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_index_outbox (
                    outbox_id TEXT PRIMARY KEY,
                    memory_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    session_id TEXT,
                    user_id TEXT,
                    org_id TEXT,
                    action TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    confidentiality TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL DEFAULT '',
                    lease_expires_at TEXT,
                    claimed_by TEXT,
                    last_error_type TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    processed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS chat_llm_audit (
                    audit_id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL REFERENCES chat_messages(message_id),
                    session_id TEXT NOT NULL REFERENCES chat_sessions(session_id),
                    project_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL DEFAULT '',
                    response_hash TEXT NOT NULL DEFAULT '',
                    prompt_chars INTEGER NOT NULL DEFAULT 0,
                    response_chars INTEGER NOT NULL DEFAULT 0,
                    model TEXT NOT NULL DEFAULT '',
                    fallback_used INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    error_type TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_chat_sessions_project_scope
                    ON chat_sessions(project_id, user_id, org_id, updated_at);
                CREATE INDEX IF NOT EXISTS idx_chat_messages_session_sequence
                    ON chat_messages(session_id, sequence_no);
                CREATE INDEX IF NOT EXISTS idx_citations_message
                    ON chat_message_citations(message_id);
                CREATE INDEX IF NOT EXISTS idx_summaries_session_version
                    ON memory_summaries(session_id, version);
                CREATE INDEX IF NOT EXISTS idx_memory_active_scope
                    ON memory_items(project_id, user_id, org_id, status, expires_at);
                CREATE INDEX IF NOT EXISTS idx_memory_audit_record
                    ON memory_audit(memory_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_memory_outbox_status
                    ON memory_index_outbox(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_chat_llm_audit_session
                    ON chat_llm_audit(session_id, created_at);
                """
            )
            message_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(chat_messages)")
            }
            if "operation_hash" not in message_columns:
                conn.execute(
                    "ALTER TABLE chat_messages ADD COLUMN operation_hash TEXT NOT NULL DEFAULT ''"
                )
            if "reply_to_message_id" not in message_columns:
                conn.execute(
                    """
                    ALTER TABLE chat_messages
                    ADD COLUMN reply_to_message_id TEXT REFERENCES chat_messages(message_id)
                    """
                )
            legacy_rows = conn.execute(
                """
                SELECT message_id, role, content FROM chat_messages
                WHERE operation_hash = ''
                """
            ).fetchall()
            for row in legacy_rows:
                operation_hash = _operation_hash(
                    {
                        "operation": "append_message",
                        "role": str(row["role"]),
                        "content": str(row["content"]),
                    }
                )
                conn.execute(
                    "UPDATE chat_messages SET operation_hash = ? WHERE message_id = ?",
                    (operation_hash, str(row["message_id"])),
                )
            outbox_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(memory_index_outbox)")
            }
            outbox_migrations = {
                "attempt_count": "INTEGER NOT NULL DEFAULT 0",
                "next_attempt_at": "TEXT NOT NULL DEFAULT ''",
                "lease_expires_at": "TEXT",
                "claimed_by": "TEXT",
            }
            for column_name, definition in outbox_migrations.items():
                if column_name not in outbox_columns:
                    conn.execute(
                        f"ALTER TABLE memory_index_outbox ADD COLUMN {column_name} {definition}"
                    )
            if "attempts" in outbox_columns:
                conn.execute(
                    """
                    UPDATE memory_index_outbox
                    SET attempt_count = attempts
                    WHERE attempt_count = 0 AND attempts > 0
                    """
                )
            conn.execute(
                """
                UPDATE memory_index_outbox
                SET next_attempt_at = created_at
                WHERE next_attempt_at = ''
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_outbox_claim
                ON memory_index_outbox(
                    status, next_attempt_at, lease_expires_at, created_at
                )
                """
            )


def _scope(scope: PrincipalScope) -> PrincipalScope:
    if not isinstance(scope, PrincipalScope):
        raise ValidationError("scope must be a PrincipalScope")
    return PrincipalScope(scope.user_id, scope.org_id)


def _required_text(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValidationError(f"{field} is required")
    return text


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _message_input(role: str, content: str, idempotency_key: str) -> tuple[str, str, str]:
    role = _required_text(role, "role")
    if role not in _MESSAGE_ROLES:
        raise ValidationError(f"Unsupported message role: {role}")
    content = _required_text(content, "content")
    idempotency_key = _required_text(idempotency_key, "idempotency_key")
    return role, content, idempotency_key


def _confidence(value: float) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValidationError("confidence must be a finite number between 0 and 1") from exc
    if not math.isfinite(confidence) or confidence < 0 or confidence > 1:
        raise ValidationError("confidence must be between 0 and 1")
    return confidence


def _limit(value: int) -> int:
    return max(1, min(int(value or 100), 1000))


def _memory_search_terms(query: str, max_terms: int = 32) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        normalized = term.strip().lower()
        if len(normalized) < 2 or normalized in seen or len(terms) >= max_terms:
            return
        seen.add(normalized)
        terms.append(normalized)

    for segment in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9]+", query[:500]):
        if re.fullmatch(r"[\u4e00-\u9fff]+", segment):
            segment = _strip_chinese_query_fillers(segment)
            if not segment:
                continue
            if len(segment) <= 16:
                add(segment)
            for index in range(len(segment) - 1):
                add(segment[index : index + 2])
        else:
            add(segment)
        if len(terms) >= max_terms:
            break
    if not terms:
        add(query[:100])
    return terms


def _strip_chinese_query_fillers(segment: str) -> str:
    cleaned = re.sub(
        r"^(?:(?:请|请帮我|帮我|帮忙)?(?:分析|评估|说明|看看|查看))",
        "",
        segment,
    )
    cleaned = re.sub(r"(?:分析一下|评估一下|说明一下|看一下)$", "", cleaned)
    return cleaned or segment


def _memory_search_minimum_hits(terms: list[str]) -> int:
    chinese_terms = [term for term in terms if re.search(r"[\u4e00-\u9fff]", term)]
    return 2 if len(chinese_terms) >= 3 else 1


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _add_seconds(timestamp: str, seconds: int) -> str:
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise ValidationError("Invalid outbox timestamp") from exc
    return (parsed + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")


def _new_id() -> str:
    return str(uuid4())


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _operation_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return _hash_text(canonical)


_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
_SAFE_ERROR_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,99}$")
_CREDENTIAL_PREFIX_RE = re.compile(
    r"(?i)^(?:sk|pk|rk|ak)[-_][A-Za-z0-9_-]{8,}$"
)
_BASE64URL_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?i)(?:^|[?&;,\s])(?:api[_-]?key|access[_-]?token|password|secret|token)\s*[:=]"
)
_BEARER_CREDENTIAL_RE = re.compile(r"(?i)^bearer(?:[\s._-]+)[A-Za-z0-9._-]{6,}$")
_URL_RE = re.compile(r"(?i)^[a-z][a-z0-9+.-]*://")
_LLM_AUDIT_STATUSES = {
    "success",
    "failed",
    "fallback",
    "degraded",
    "unavailable",
    "unknown",
}


def _normalize_sha256(value: object) -> str:
    text = str(value or "").strip()
    return text.lower() if _SHA256_RE.fullmatch(text) else ""


def _safe_nonnegative_int(value: object, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return max(0, int(default))


def _sanitize_model(value: object) -> str:
    text = str(value or "").strip()
    if not text or _looks_like_credential(text):
        return ""
    return text if _SAFE_MODEL_RE.fullmatch(text) else ""


def _sanitize_error_type(value: object) -> str:
    text = str(value or "").strip()
    if not text or _looks_like_credential(text):
        return ""
    return text if _SAFE_ERROR_TYPE_RE.fullmatch(text) else ""


def _looks_like_credential(value: str) -> bool:
    if (
        _CREDENTIAL_PREFIX_RE.fullmatch(value)
        or _looks_like_jwt(value)
        or _BEARER_CREDENTIAL_RE.fullmatch(value)
        or _URL_RE.search(value)
        or _CREDENTIAL_ASSIGNMENT_RE.search(value)
    ):
        return True
    compact = value.replace("-", "").replace("_", "")
    if len(compact) < 20 or not compact.isascii() or not compact.isalnum():
        return False
    character_classes = sum(
        (
            any(char.islower() for char in compact),
            any(char.isupper() for char in compact),
            any(char.isdigit() for char in compact),
        )
    )
    return character_classes >= 3 and len(set(compact)) / len(compact) >= 0.5


def _looks_like_jwt(value: str) -> bool:
    segments = value.split(".")
    if len(segments) != 3 or not all(
        segment and _BASE64URL_SEGMENT_RE.fullmatch(segment) for segment in segments
    ):
        return False
    try:
        header_bytes = _decode_base64url(segments[0])
        payload_bytes = _decode_base64url(segments[1])
        signature_bytes = _decode_base64url(segments[2])
        header = json.loads(header_bytes.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return False
    return (
        isinstance(header, dict)
        and isinstance(header.get("alg"), str)
        and bool(header["alg"].strip())
        and bool(payload_bytes)
        and bool(signature_bytes)
    )


def _decode_base64url(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.b64decode(segment + padding, altchars=b"-_", validate=True)


def _normalize_llm_audit(
    audit: Mapping[str, Any], response_default_chars: int
) -> dict[str, Any]:
    status = str(audit.get("status") or "unknown").strip().lower()
    if status not in _LLM_AUDIT_STATUSES:
        status = "unknown"
    return {
        "request_hash": _normalize_sha256(audit.get("request_hash")),
        "response_hash": _normalize_sha256(audit.get("response_hash")),
        "prompt_chars": _safe_nonnegative_int(audit.get("prompt_chars")),
        "response_chars": _safe_nonnegative_int(
            audit.get("response_chars"), response_default_chars
        ),
        "model": _sanitize_model(audit.get("model")),
        "fallback_used": _strict_audit_bool(audit.get("fallback_used")),
        "status": status,
        "error_type": _sanitize_error_type(audit.get("error_type")),
    }


def _strict_audit_bool(value: object) -> bool:
    return value if isinstance(value, bool) else False


def _normalize_citations(citations: Sequence[Citation]) -> list[Citation]:
    normalized: list[Citation] = []
    for citation in citations:
        citation_id = _required_text(citation.citation_id, "citation_id")
        try:
            UUID(citation_id)
        except ValueError as exc:
            raise ValidationError("citation_id must be a UUID") from exc
        normalized.append(
            Citation(
                citation_id=citation_id,
                message_id="",
                source_type=_required_text(
                    citation.source_type, "citation source_type"
                ),
                source_id=_required_text(citation.source_id, "citation source_id"),
                label=str(citation.label or "").strip(),
                locator=str(citation.locator or "").strip(),
            )
        )
    return sorted(
        normalized,
        key=lambda item: (
            item.source_type,
            item.source_id,
            item.label,
            item.locator,
            item.citation_id,
        ),
    )


def _token_estimate(value: str) -> int:
    return max(1, (len(value) + 3) // 4)


def _session_from_row(row: sqlite3.Row) -> ChatSession:
    return ChatSession(
        str(row["session_id"]),
        str(row["project_id"]),
        str(row["title"]),
        str(row["status"]),
        PrincipalScope(row["user_id"], row["org_id"]),
        str(row["created_at"]),
        str(row["updated_at"]),
    )


def _message_from_row(row: sqlite3.Row) -> ChatMessage:
    return ChatMessage(
        str(row["message_id"]),
        str(row["session_id"]),
        int(row["sequence_no"]),
        str(row["role"]),
        str(row["content"]),
        str(row["content_hash"]),
        int(row["token_estimate"]),
        str(row["created_at"]),
        str(row["reply_to_message_id"])
        if row["reply_to_message_id"] is not None
        else None,
    )


def _summary_from_row(row: sqlite3.Row) -> MemorySummary:
    return MemorySummary(
        str(row["summary_id"]),
        str(row["session_id"]),
        int(row["version"]),
        str(row["content"]),
        str(row["content_hash"]),
        str(row["message_start_id"]),
        str(row["message_end_id"]),
        str(row["created_at"]),
    )


def _memory_from_row(row: sqlite3.Row) -> MemoryItem:
    return MemoryItem(
        str(row["memory_id"]),
        str(row["project_id"]),
        str(row["session_id"]) if row["session_id"] is not None else None,
        PrincipalScope(row["user_id"], row["org_id"]),
        str(row["source_type"]),
        str(row["canonical_text"]),
        str(row["content_hash"]),
        str(row["status"]),
        float(row["confidence"]),
        str(row["confidentiality"]),
        str(row["expires_at"]) if row["expires_at"] is not None else None,
        str(row["supersedes_id"]) if row["supersedes_id"] is not None else None,
        str(row["created_at"]),
        str(row["updated_at"]),
    )


def _citation_from_row(row: sqlite3.Row) -> Citation:
    return Citation(
        str(row["citation_id"]),
        str(row["message_id"]),
        str(row["source_type"]),
        str(row["source_id"]),
        str(row["label"]),
        str(row["locator"]),
    )
