from __future__ import annotations

import sqlite3
import tempfile
import unittest
import inspect
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from app.chat.models import (
    ChatMessage,
    ChatSession,
    Citation,
    IdempotencyConflict,
    MemoryItem,
    MemorySummary,
    MessageWriteResult,
    PrincipalScope,
    RetrievalHit,
    RetrievalStatus,
    SessionScopeMismatch,
    ValidationError,
)
from app.chat.storage import SQLiteAuthorityStore
from app.chat.ports import ChatRepository


class SQLiteAuthorityStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "chat.db"
        self.store = SQLiteAuthorityStore(self.db_path)
        self.scope = PrincipalScope()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_schema_uses_uuid_text_keys_beijing_time_and_connection_pragmas(self):
        with patch(
            "app.chat.storage.beijing_now_text",
            return_value="2026-08-07 13:30:00",
        ):
            session = self.store.create_session("1006", "Initial analysis", self.scope)
            write = self.store.append_message(
                session.session_id,
                "user",
                "What is the largest risk?",
                "req-1",
                self.scope,
                project_id="1006",
            )
            message = write.message

        UUID(session.session_id)
        UUID(message.message_id)
        self.assertEqual(session.created_at, "2026-08-07 13:30:00")
        self.assertEqual(session.updated_at, "2026-08-07 13:30:00")
        self.assertEqual(message.created_at, "2026-08-07 13:30:00")

        with closing(self.store._connect()) as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        self.assertEqual(journal_mode, "wal")
        self.assertEqual(self.store.journal_mode, "wal")
        self.assertIsNone(self.store.wal_degraded_reason)

    def test_messages_are_ordered_and_idempotent_for_the_same_payload(self):
        session = self.store.create_session("1006", "Analysis", self.scope)
        first_write = self.store.append_message(
            session.session_id, "user", "First", "req-1", self.scope,
            project_id="1006",
        )
        same_write = self.store.append_message(
            session.session_id, "user", "First", "req-1", self.scope,
            project_id="1006",
        )
        second_write = self.store.append_message(
            session.session_id, "assistant", "Second", "req-2", self.scope,
            project_id="1006",
        )

        first = first_write.message
        same = same_write.message
        second = second_write.message
        self.assertTrue(first_write.created)
        self.assertFalse(same_write.created)
        self.assertEqual(first.message_id, same.message_id)
        self.assertEqual(first.sequence_no, 1)
        self.assertEqual(second.sequence_no, 2)
        self.assertEqual(
            [
                message.sequence_no
                for message in self.store.list_messages(
                    session.session_id, self.scope, project_id="1006"
                )
            ],
            [1, 2],
        )

    def test_reusing_idempotency_key_with_different_payload_is_rejected(self):
        session = self.store.create_session("1006", "Analysis", self.scope)
        self.store.append_message(
            session.session_id, "user", "First", "req-1", self.scope,
            project_id="1006",
        )

        with self.assertRaises(IdempotencyConflict):
            self.store.append_message(
                session.session_id, "user", "Changed", "req-1", self.scope,
                project_id="1006",
            )
        with self.assertRaises(IdempotencyConflict):
            self.store.append_message(
                session.session_id, "assistant", "First", "req-1", self.scope,
                project_id="1006",
            )

    def test_session_listing_is_project_filtered_and_scope_is_strict(self):
        unbound = PrincipalScope(user_id="", org_id=None)
        first = self.store.create_session("1006", "First", unbound)
        self.store.create_session("2001", "Other project", PrincipalScope())
        scoped = self.store.create_session(
            "1006", "Scoped", PrincipalScope(user_id="user-1", org_id="org-1")
        )

        sessions = self.store.list_sessions("1006", PrincipalScope())
        self.assertEqual([item.session_id for item in sessions], [first.session_id])
        self.assertEqual(
            self.store.get_session(
                first.session_id, PrincipalScope(), project_id="1006"
            ).scope,
            self.scope,
        )

        for wrong_scope in (
            PrincipalScope(user_id="other", org_id="org-1"),
            PrincipalScope(user_id="user-1", org_id="other"),
            PrincipalScope(),
        ):
            with self.subTest(scope=wrong_scope):
                with self.assertRaises(SessionScopeMismatch):
                    self.store.get_session(
                        scoped.session_id, wrong_scope, project_id="1006"
                    )

        with self.assertRaises(SessionScopeMismatch):
            self.store.get_session(
                first.session_id,
                PrincipalScope(user_id="other"),
                project_id="1006",
            )

    def test_session_updates_and_archive_keep_scope_enforcement(self):
        scope = PrincipalScope(user_id="user-1", org_id="org-1")
        session = self.store.create_session("1006", "Old", scope)

        updated = self.store.update_session(
            session.session_id,
            scope,
            project_id="1006",
            title="New",
            status="active",
        )
        archived = self.store.archive_session(
            session.session_id, scope, project_id="1006"
        )

        self.assertEqual(updated.title, "New")
        self.assertEqual(archived.status, "archived")
        with self.assertRaises(SessionScopeMismatch):
            self.store.update_session(
                session.session_id,
                PrincipalScope(),
                project_id="1006",
                title="Denied",
            )

    def test_assistant_message_citations_and_llm_audit_commit_atomically(self):
        session = self.store.create_session("1006", "Analysis", self.scope)
        citations = [
            Citation(
                citation_id="11111111-1111-4111-8111-111111111111",
                message_id="",
                source_type="risk_result",
                source_id="history-7",
                label="Latest risk result",
                locator="score",
            )
        ]
        write = self.store.save_assistant_message_with_citations(
            session.session_id,
            "Sanitized answer",
            "assistant-1",
            citations,
            self.scope,
            project_id="1006",
            audit={
                "request_hash": "a" * 64,
                "response_hash": "b" * 64,
                "prompt_chars": 120,
                "response_chars": 16,
                "model": "test-model",
                "fallback_used": False,
                "status": "success",
            },
        )
        message = write.message
        self.assertTrue(write.created)

        saved_citations = self.store.list_citations(
            message.message_id, self.scope, project_id="1006"
        )
        self.assertEqual(len(saved_citations), 1)
        self.assertEqual(saved_citations[0].message_id, message.message_id)
        with closing(sqlite3.connect(self.db_path)) as conn:
            audit_count = conn.execute("SELECT COUNT(*) FROM chat_llm_audit").fetchone()[0]
        self.assertEqual(audit_count, 1)

        duplicate_id = "22222222-2222-4222-8222-222222222222"
        invalid_citations = [
            Citation(duplicate_id, "", "memory", "m-1", "One", ""),
            Citation(duplicate_id, "", "memory", "m-2", "Two", ""),
        ]
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save_assistant_message_with_citations(
                session.session_id,
                "Must roll back",
                "assistant-2",
                invalid_citations,
                self.scope,
                project_id="1006",
                audit={"status": "success"},
            )

        self.assertEqual(
            len(
                self.store.list_messages(
                    session.session_id, self.scope, project_id="1006"
                )
            ),
            1,
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM chat_llm_audit").fetchone()[0], 1)

    def test_summary_versions_increment_and_preserve_source_messages(self):
        session = self.store.create_session("1006", "Analysis", self.scope)
        first = self.store.append_message(
            session.session_id, "user", "One", "req-1", self.scope,
            project_id="1006",
        ).message
        second = self.store.append_message(
            session.session_id, "assistant", "Two", "req-2", self.scope,
            project_id="1006",
        ).message

        summary1 = self.store.save_summary(
            session.session_id, "Summary one", first.message_id, second.message_id, self.scope,
            project_id="1006",
        )
        summary2 = self.store.save_summary(
            session.session_id, "Summary two", first.message_id, second.message_id, self.scope,
            project_id="1006",
        )

        self.assertEqual((summary1.version, summary2.version), (1, 2))
        self.assertEqual(summary2.message_start_id, first.message_id)
        self.assertEqual(summary2.message_end_id, second.message_id)
        self.assertEqual(
            self.store.get_latest_summary(
                session.session_id, self.scope, project_id="1006"
            ),
            summary2,
        )
        self.assertEqual(
            len(
                self.store.list_messages(
                    session.session_id, self.scope, project_id="1006"
                )
            ),
            2,
        )

    def test_memory_confidence_rejects_non_finite_values(self):
        scope = PrincipalScope(user_id="user-1", org_id="org-1")
        values = (float("nan"), float("inf"), float("-inf"))
        for index, confidence in enumerate(values):
            with self.subTest(operation="save", confidence=confidence):
                with self.assertRaises(ValidationError):
                    self.store.save_memory(
                        project_id="1006",
                        canonical_text=f"Invalid confidence {index}",
                        source_type="user_confirmed",
                        scope=scope,
                        confidence=confidence,
                    )

        old = self.store.save_memory(
            project_id="1006",
            canonical_text="Valid confidence",
            source_type="user_confirmed",
            scope=scope,
        )
        for index, confidence in enumerate(values):
            with self.subTest(operation="supersede", confidence=confidence):
                with self.assertRaises(ValidationError):
                    self.store.supersede_memory(
                        old.memory_id,
                        canonical_text=f"Invalid replacement {index}",
                        scope=scope,
                        project_id="1006",
                        confidence=confidence,
                    )

    def test_memory_supersede_expiration_and_scope_filters(self):
        scope = PrincipalScope(user_id="user-1", org_id="org-1")
        old = self.store.save_memory(
            project_id="1006",
            canonical_text="Original condition",
            source_type="user_confirmed",
            scope=scope,
            confidence=1.0,
        )
        replacement = self.store.supersede_memory(
            old.memory_id,
            canonical_text="Updated condition",
            scope=scope,
            project_id="1006",
        )
        expired_by_time = self.store.save_memory(
            project_id="1006",
            canonical_text="Expired by date",
            source_type="user_confirmed",
            scope=scope,
            expires_at="2026-01-01 00:00:00",
        )
        explicitly_expired = self.store.save_memory(
            project_id="1006",
            canonical_text="Explicitly expired",
            source_type="user_confirmed",
            scope=scope,
        )
        self.store.expire_memory(
            explicitly_expired.memory_id, scope, project_id="1006"
        )
        self.store.save_memory(
            project_id="2001",
            canonical_text="Other project",
            source_type="user_confirmed",
            scope=scope,
        )
        self.store.save_memory(
            project_id="1006",
            canonical_text="Other principal",
            source_type="user_confirmed",
            scope=PrincipalScope(user_id="other", org_id="org-1"),
        )

        active = self.store.list_memories("1006", scope, now="2026-08-07 13:30:00")

        self.assertEqual([item.memory_id for item in active], [replacement.memory_id])
        self.assertEqual(
            self.store.get_memory(old.memory_id, scope, project_id="1006").status,
            "superseded",
        )
        self.assertEqual(
            self.store.get_memory(
                expired_by_time.memory_id, scope, project_id="1006"
            ).status,
            "active",
        )
        self.assertEqual(
            self.store.get_memory(
                explicitly_expired.memory_id, scope, project_id="1006"
            ).status,
            "expired",
        )
        with self.assertRaises(SessionScopeMismatch):
            self.store.get_memory(
                old.memory_id,
                PrincipalScope(user_id="other", org_id="org-1"),
                project_id="1006",
            )

    def test_memory_audit_and_outbox_store_metadata_without_secret_text(self):
        secret = "api_key=super-secret-value"
        memory = self.store.save_memory(
            project_id="1006",
            canonical_text=secret,
            source_type="user_confirmed",
            scope=self.scope,
        )
        replacement = self.store.supersede_memory(
            memory.memory_id,
            canonical_text="token=another-secret",
            scope=self.scope,
            project_id="1006",
        )

        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            memory_audit = [dict(row) for row in conn.execute("SELECT * FROM memory_audit")]
            outbox = [dict(row) for row in conn.execute("SELECT * FROM memory_index_outbox")]
            llm_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(chat_llm_audit)")
            }
        serialized_metadata = repr(memory_audit + outbox)

        self.assertGreaterEqual(len(memory_audit), 3)
        self.assertGreaterEqual(len(outbox), 3)
        self.assertNotIn("super-secret-value", serialized_metadata)
        self.assertNotIn("another-secret", serialized_metadata)
        self.assertIn(replacement.memory_id, serialized_metadata)
        self.assertFalse(
            {"prompt", "prompt_text", "api_key", "token", "raw_request"} & llm_columns
        )

    def test_content_operations_reject_wrong_project_with_same_principal(self):
        session = self.store.create_session("1006", "Analysis", self.scope)
        message = self.store.append_message(
            session.session_id,
            "user",
            "Project-scoped content",
            "req-project",
            self.scope,
            project_id="1006",
        ).message
        assistant = self.store.save_assistant_message_with_citations(
            session.session_id,
            "Answer",
            "assistant-project",
            [Citation(str(UUID("33333333-3333-4333-8333-333333333333")), "", "risk", "7", "Risk")],
            self.scope,
            project_id="1006",
            audit={"status": "success"},
        ).message
        self.store.save_summary(
            session.session_id,
            "Summary",
            message.message_id,
            assistant.message_id,
            self.scope,
            project_id="1006",
        )
        memory = self.store.save_memory(
            "1006", "Remembered", "user_confirmed", self.scope
        )

        operations = (
            lambda: self.store.get_session(
                session.session_id, self.scope, project_id="2001"
            ),
            lambda: self.store.update_session(
                session.session_id, self.scope, project_id="2001", title="Denied"
            ),
            lambda: self.store.archive_session(
                session.session_id, self.scope, project_id="2001"
            ),
            lambda: self.store.append_message(
                session.session_id,
                "user",
                "Denied",
                "wrong-project",
                self.scope,
                project_id="2001",
            ),
            lambda: self.store.list_messages(
                session.session_id, self.scope, project_id="2001"
            ),
            lambda: self.store.save_assistant_message_with_citations(
                session.session_id,
                "Denied",
                "wrong-assistant",
                [],
                self.scope,
                project_id="2001",
                audit={"status": "success"},
            ),
            lambda: self.store.list_citations(
                assistant.message_id, self.scope, project_id="2001"
            ),
            lambda: self.store.save_summary(
                session.session_id,
                "Denied",
                message.message_id,
                assistant.message_id,
                self.scope,
                project_id="2001",
            ),
            lambda: self.store.get_latest_summary(
                session.session_id, self.scope, project_id="2001"
            ),
            lambda: self.store.get_memory(
                memory.memory_id, self.scope, project_id="2001"
            ),
            lambda: self.store.supersede_memory(
                memory.memory_id,
                "Denied",
                self.scope,
                project_id="2001",
            ),
            lambda: self.store.expire_memory(
                memory.memory_id, self.scope, project_id="2001"
            ),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaises(SessionScopeMismatch):
                    operation()

    def test_assistant_bundle_idempotency_covers_citations_and_safe_audit(self):
        session = self.store.create_session("1006", "Analysis", self.scope)
        citations = [
            Citation("44444444-4444-4444-8444-444444444444", "", "risk", "7", "Risk", "score"),
            Citation("55555555-5555-4555-8555-555555555555", "", "memory", "8", "Memory", ""),
        ]
        audit = {
            "request_hash": "a" * 64,
            "response_hash": "b" * 64,
            "prompt_chars": 20,
            "response_chars": 6,
            "model": "provider/model-1",
            "fallback_used": False,
            "status": "success",
            "error_type": "",
        }
        first = self.store.save_assistant_message_with_citations(
            session.session_id,
            "Answer",
            "bundle-1",
            citations,
            self.scope,
            project_id="1006",
            audit=audit,
        )
        same = self.store.save_assistant_message_with_citations(
            session.session_id,
            "Answer",
            "bundle-1",
            list(reversed(citations)),
            self.scope,
            project_id="1006",
            audit=dict(audit),
        )
        self.assertTrue(first.created)
        self.assertFalse(same.created)
        self.assertEqual(first.message.message_id, same.message.message_id)
        self.assertEqual(
            len(
                self.store.list_citations(
                    first.message.message_id, self.scope, project_id="1006"
                )
            ),
            2,
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM chat_llm_audit").fetchone()[0], 1)

        changed_bundles = (
            ([Citation("66666666-6666-4666-8666-666666666666", "", "risk", "9", "Changed")], audit),
            (citations, {**audit, "model": "other-model"}),
        )
        for changed_citations, changed_audit in changed_bundles:
            with self.subTest(audit=changed_audit):
                with self.assertRaises(IdempotencyConflict):
                    self.store.save_assistant_message_with_citations(
                        session.session_id,
                        "Answer",
                        "bundle-1",
                        changed_citations,
                        self.scope,
                        project_id="1006",
                        audit=changed_audit,
                    )

        self.store.append_message(
            session.session_id,
            "assistant",
            "Plain first",
            "plain-then-bundle",
            self.scope,
            project_id="1006",
        )
        with self.assertRaises(IdempotencyConflict):
            self.store.save_assistant_message_with_citations(
                session.session_id,
                "Plain first",
                "plain-then-bundle",
                [],
                self.scope,
                project_id="1006",
                audit={"status": "success"},
            )

    def test_llm_audit_rejects_or_cleans_secret_bearing_values(self):
        session = self.store.create_session("1006", "Analysis", self.scope)
        secret_marker = "PRIVATE-MARKER-7821"
        self.store.save_assistant_message_with_citations(
            session.session_id,
            "Answer",
            "secret-audit",
            [],
            self.scope,
            project_id="1006",
            audit={
                "request_hash": f"token={secret_marker}",
                "response_hash": f"Bearer {secret_marker}",
                "model": f"model?api_key={secret_marker}",
                "status": f"token={secret_marker}",
                "error_type": f"https://example.test/?token={secret_marker}",
                "prompt_chars": 10,
                "response_chars": 6,
            },
        )

        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            audit = dict(conn.execute("SELECT * FROM chat_llm_audit").fetchone())
        self.assertNotIn(secret_marker, repr(audit))
        self.assertEqual(audit["request_hash"], "")
        self.assertEqual(audit["response_hash"], "")
        self.assertEqual(audit["model"], "")
        self.assertEqual(audit["status"], "unknown")
        self.assertEqual(audit["error_type"], "")

    def test_llm_audit_drops_opaque_credentials_without_harming_normal_tokens(self):
        session = self.store.create_session("1006", "Analysis", self.scope)
        cases = (
            ("sk-TESTOPAQUE7821", "ProviderTimeout"),
            ("deepseek-v4-flash", "pk-TESTOPAQUE7821"),
            ("deepseek-v4-flash", "Bearer-TESTOPAQUE7821"),
            ("deepseek-v4-flash", "A9fK2mQ7xL4pR8vN6cT3zY1w"),
            ("deepseek-v4-flash", "ProviderTimeout"),
        )
        for index, (model, error_type) in enumerate(cases):
            self.store.save_assistant_message_with_citations(
                session.session_id,
                f"Answer {index}",
                f"opaque-audit-{index}",
                [],
                self.scope,
                project_id="1006",
                audit={
                    "model": model,
                    "status": "failed",
                    "error_type": error_type,
                },
            )

        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT model, error_type FROM chat_llm_audit ORDER BY rowid"
            ).fetchall()

        self.assertEqual(rows[0], ("", "ProviderTimeout"))
        self.assertEqual(rows[1], ("deepseek-v4-flash", ""))
        self.assertEqual(rows[2], ("deepseek-v4-flash", ""))
        self.assertEqual(rows[3], ("deepseek-v4-flash", ""))
        self.assertEqual(rows[4], ("deepseek-v4-flash", "ProviderTimeout"))

    def test_supersede_memory_rolls_back_every_write_on_mid_transaction_failure(self):
        memory = self.store.save_memory(
            "1006", "Original", "user_confirmed", self.scope
        )
        with patch.object(
            self.store, "_insert_outbox", side_effect=sqlite3.OperationalError("injected")
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.supersede_memory(
                    memory.memory_id,
                    "Replacement",
                    self.scope,
                    project_id="1006",
                )

        self.assertEqual(
            self.store.get_memory(
                memory.memory_id, self.scope, project_id="1006"
            ).status,
            "active",
        )
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_audit").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memory_index_outbox").fetchone()[0], 1)

    def test_domain_records_are_immutable(self):
        scope = PrincipalScope()
        records = (
            scope,
            ChatSession("s", "p", "t", "active", scope, "c", "u"),
            ChatMessage("m", "s", 1, "user", "c", "h", 1, "t"),
            MessageWriteResult(
                ChatMessage("m", "s", 1, "user", "c", "h", 1, "t"), True
            ),
            MemorySummary("x", "s", 1, "c", "h", "m1", "m2", "t"),
            MemoryItem("i", "p", None, scope, "user_confirmed", "c", "h", "active", 1.0, "sanitized", None, None, "t", "t"),
            Citation("c", "m", "risk", "r", "label"),
            RetrievalHit(
                memory_id="i",
                score=1.0,
                match_type="exact",
                project_id="p",
                scope=scope,
                session_id=None,
            ),
            RetrievalStatus(True),
        )
        for record in records:
            with self.subTest(record=type(record).__name__):
                with self.assertRaises(FrozenInstanceError):
                    record.created_at = "changed"

    def test_schema_contains_all_tables_uuid_keys_and_critical_constraints(self):
        expected_tables = {
            "chat_sessions",
            "chat_messages",
            "chat_message_citations",
            "memory_summaries",
            "memory_items",
            "memory_audit",
            "memory_index_outbox",
            "chat_llm_audit",
        }
        primary_keys = {
            "chat_sessions": "session_id",
            "chat_messages": "message_id",
            "chat_message_citations": "citation_id",
            "memory_summaries": "summary_id",
            "memory_items": "memory_id",
            "memory_audit": "audit_id",
            "memory_index_outbox": "outbox_id",
            "chat_llm_audit": "audit_id",
        }
        with closing(sqlite3.connect(self.db_path)) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            for table, key in primary_keys.items():
                columns = {row[1]: row for row in conn.execute(f"PRAGMA table_info({table})")}
                self.assertEqual(columns[key][2].upper(), "TEXT")
                self.assertEqual(columns[key][5], 1)
            message_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(chat_messages)")
            }
            unique_indexes = []
            for index in conn.execute("PRAGMA index_list(chat_messages)"):
                if index[2]:
                    unique_indexes.append(
                        tuple(
                            row[2]
                            for row in conn.execute(
                                f"PRAGMA index_info('{index[1]}')"
                            )
                        )
                    )

        self.assertTrue(expected_tables.issubset(tables))
        self.assertIn("operation_hash", message_columns)
        self.assertIn(("session_id", "sequence_no"), unique_indexes)
        self.assertIn(("session_id", "idempotency_key"), unique_indexes)

    def test_legacy_message_schema_gains_safe_operation_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.db"
            with closing(sqlite3.connect(db_path)) as conn:
                conn.executescript(
                    """
                    CREATE TABLE chat_sessions (
                        session_id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        status TEXT NOT NULL,
                        user_id TEXT,
                        org_id TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE chat_messages (
                        message_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES chat_sessions(session_id),
                        sequence_no INTEGER NOT NULL,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        token_estimate INTEGER NOT NULL,
                        idempotency_key TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(session_id, sequence_no),
                        UNIQUE(session_id, idempotency_key)
                    );
                    INSERT INTO chat_sessions VALUES (
                        'legacy-session', '1006', 'Legacy', 'active', NULL, NULL,
                        '2026-08-07 13:00:00', '2026-08-07 13:00:00'
                    );
                    INSERT INTO chat_messages VALUES (
                        'legacy-message', 'legacy-session', 1, 'user', 'Legacy text',
                        'content-hash', 3, 'legacy-key', '2026-08-07 13:00:00'
                    );
                    """
                )

            SQLiteAuthorityStore(db_path)
            with closing(sqlite3.connect(db_path)) as conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(chat_messages)")}
                operation_hash = conn.execute(
                    "SELECT operation_hash FROM chat_messages WHERE message_id = 'legacy-message'"
                ).fetchone()[0]

        self.assertIn("operation_hash", columns)
        self.assertIn("reply_to_message_id", columns)
        self.assertEqual(len(operation_hash), 64)

    def test_message_write_result_replay_lookup_and_reply_link_avoid_repeat_work(self):
        session = self.store.create_session("1006", "Analysis", self.scope)
        request = self.store.append_message(
            session.session_id,
            "user",
            "Analyze repayment risk",
            "request-link-1",
            self.scope,
            project_id="1006",
        )
        replayed_request = self.store.append_message(
            session.session_id,
            "user",
            "Analyze repayment risk",
            "request-link-1",
            self.scope,
            project_id="1006",
        )
        answer = self.store.save_assistant_message_with_citations(
            session.session_id,
            "Repayment risk is high",
            "answer-link-1",
            [],
            self.scope,
            project_id="1006",
            reply_to_message_id=request.message.message_id,
            audit={"status": "success"},
        )
        replayed_answer = self.store.save_assistant_message_with_citations(
            session.session_id,
            "Repayment risk is high",
            "answer-link-1",
            [],
            self.scope,
            project_id="1006",
            reply_to_message_id=request.message.message_id,
            audit={"status": "success"},
        )

        self.assertIsInstance(request, MessageWriteResult)
        self.assertTrue(request.created)
        self.assertFalse(replayed_request.created)
        self.assertTrue(answer.created)
        self.assertFalse(replayed_answer.created)
        self.assertEqual(answer.message.reply_to_message_id, request.message.message_id)
        self.assertEqual(answer.message, replayed_answer.message)
        self.assertEqual(
            self.store.get_message_by_idempotency_key(
                session.session_id,
                "request-link-1",
                self.scope,
                project_id="1006",
            ),
            request.message,
        )

        other_session = self.store.create_session("1006", "Other", self.scope)
        other_request = self.store.append_message(
            other_session.session_id,
            "user",
            "Other request",
            "other-request",
            self.scope,
            project_id="1006",
        )
        with self.assertRaises(ValidationError):
            self.store.save_assistant_message_with_citations(
                session.session_id,
                "Invalid link",
                "invalid-reply-link",
                [],
                self.scope,
                project_id="1006",
                reply_to_message_id=other_request.message.message_id,
                audit={"status": "success"},
            )

    def test_concurrent_message_writes_have_contiguous_sequence_numbers(self):
        session = self.store.create_session("1006", "Concurrent", self.scope)

        def append(index: int):
            return self.store.append_message(
                session.session_id,
                "user",
                f"Message {index}",
                f"concurrent-{index}",
                self.scope,
                project_id="1006",
            )

        with ThreadPoolExecutor(max_workers=12) as executor:
            writes = list(executor.map(append, range(12)))

        self.assertTrue(all(write.created for write in writes))
        messages = self.store.list_messages(
            session.session_id, self.scope, project_id="1006"
        )
        self.assertEqual([message.sequence_no for message in messages], list(range(1, 13)))

    def test_concurrent_replay_creates_one_message(self):
        session = self.store.create_session("1006", "Replay", self.scope)

        def append(_: int):
            return self.store.append_message(
                session.session_id,
                "user",
                "Same payload",
                "same-concurrent-key",
                self.scope,
                project_id="1006",
            )

        with ThreadPoolExecutor(max_workers=12) as executor:
            writes = list(executor.map(append, range(12)))

        self.assertEqual(sum(write.created for write in writes), 1)
        self.assertEqual(len({write.message.message_id for write in writes}), 1)

    def test_concurrent_rename_and_archive_do_not_overwrite_each_other(self):
        for index in range(12):
            session = self.store.create_session("1006", f"Old {index}", self.scope)
            with ThreadPoolExecutor(max_workers=2) as executor:
                rename = executor.submit(
                    self.store.update_session,
                    session.session_id,
                    self.scope,
                    project_id="1006",
                    title=f"New {index}",
                )
                archive = executor.submit(
                    self.store.archive_session,
                    session.session_id,
                    self.scope,
                    project_id="1006",
                )
                rename.result()
                archive.result()
            final = self.store.get_session(
                session.session_id, self.scope, project_id="1006"
            )
            self.assertEqual(final.title, f"New {index}")
            self.assertEqual(final.status, "archived")

    def test_chinese_keyword_fallback_uses_bigrams_and_keeps_project_scope(self):
        expected = self.store.save_memory(
            "1006", "回款风险较高", "user_confirmed", self.scope
        )
        self.store.save_memory(
            "2001", "回款风险非常高", "user_confirmed", self.scope
        )
        self.store.save_memory(
            "1006", "审批资料完整", "user_confirmed", self.scope
        )
        self.store.save_memory(
            "1006", "请分析审批资料", "user_confirmed", self.scope
        )
        self.store.save_memory(
            "1006", "风险控制一般", "user_confirmed", self.scope
        )

        hits = self.store.search_active_memories(
            "1006", self.scope, "请分析回款风险", 10
        )

        self.assertEqual([item.memory_id for item in hits], [expected.memory_id])
        self.assertTrue(all(item.project_id == "1006" for item in hits))

    def test_llm_audit_rejects_jwt_and_url_but_keeps_legitimate_token_words(self):
        session = self.store.create_session("1006", "Audit", self.scope)
        jwt = (
            "eyJhbGciOiJIUzI1NiJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        short_payload_jwt = "eyJhbGciOiJIUzI1NiJ9.e30.c2lnbmF0dXJl"
        audits = (
            {"model": jwt, "error_type": "ProviderTimeout", "status": "failed"},
            {"model": "deepseek-v4-flash", "error_type": jwt, "status": "failed"},
            {
                "model": "https://example.test/model?key=value",
                "error_type": "ProviderTimeout",
                "status": "failed",
            },
            {
                "model": "tokenizer-v2",
                "error_type": "TokenBudgetExceeded",
                "status": "failed",
            },
            {
                "model": short_payload_jwt,
                "error_type": "ProviderTimeout",
                "status": "failed",
            },
            {
                "model": "provider.model.v2",
                "error_type": "Parser.Error.v2",
                "status": "failed",
            },
        )
        for index, audit in enumerate(audits):
            self.store.save_assistant_message_with_citations(
                session.session_id,
                f"Audit answer {index}",
                f"jwt-audit-{index}",
                [],
                self.scope,
                project_id="1006",
                audit=audit,
            )

        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT model, error_type, status FROM chat_llm_audit ORDER BY rowid"
            ).fetchall()

        self.assertEqual(rows[0], ("", "ProviderTimeout", "failed"))
        self.assertEqual(rows[1], ("deepseek-v4-flash", "", "failed"))
        self.assertEqual(rows[2], ("", "ProviderTimeout", "failed"))
        self.assertEqual(rows[3], ("tokenizer-v2", "TokenBudgetExceeded", "failed"))
        self.assertEqual(rows[4], ("", "ProviderTimeout", "failed"))
        self.assertEqual(rows[5], ("provider.model.v2", "Parser.Error.v2", "failed"))

    def test_llm_audit_fallback_used_accepts_only_real_booleans(self):
        session = self.store.create_session("1006", "Audit", self.scope)
        for index, fallback_used in enumerate(("false", False, True)):
            self.store.save_assistant_message_with_citations(
                session.session_id,
                f"Fallback answer {index}",
                f"fallback-bool-{index}",
                [],
                self.scope,
                project_id="1006",
                audit={"status": "success", "fallback_used": fallback_used},
            )

        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT fallback_used FROM chat_llm_audit ORDER BY rowid"
            ).fetchall()

        self.assertEqual(rows, [(0,), (0,), (1,)])

    def test_retrieval_hit_is_lightweight_and_contains_scope_metadata(self):
        hit = RetrievalHit(
            memory_id="memory-1",
            score=0.75,
            match_type="semantic",
            project_id="1006",
            scope=PrincipalScope(user_id="user-1", org_id="org-1"),
            session_id="session-1",
        )

        self.assertEqual(hit.memory_id, "memory-1")
        self.assertEqual(hit.project_id, "1006")
        self.assertEqual(hit.scope.user_id, "user-1")
        self.assertFalse(hasattr(hit, "memory"))
        self.assertFalse(hasattr(hit, "canonical_text"))

    def test_outbox_claim_is_atomic_across_workers_and_lease_can_expire(self):
        with patch(
            "app.chat.storage.beijing_now_text",
            return_value="2026-08-07 12:59:00",
        ):
            self.store.save_memory("1006", "Claim me", "user_confirmed", self.scope)
        other_store = SQLiteAuthorityStore(self.db_path)

        def claim(store_and_worker):
            store, worker = store_and_worker
            return store.claim_outbox(
                worker,
                limit=1,
                lease_seconds=60,
                now="2026-08-07 13:00:00",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(
                executor.map(
                    claim,
                    ((self.store, "worker-a"), (other_store, "worker-b")),
                )
            )

        claimed = [item for batch in claims for item in batch]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["status"], "processing")
        self.assertEqual(claimed[0]["attempt_count"], 1)
        first_worker = claimed[0]["claimed_by"]
        waiting_worker = "worker-b" if first_worker == "worker-a" else "worker-a"
        self.assertEqual(
            other_store.claim_outbox(
                waiting_worker,
                limit=1,
                lease_seconds=60,
                now="2026-08-07 13:00:30",
            ),
            [],
        )
        reclaimed = other_store.claim_outbox(
            waiting_worker,
            limit=1,
            lease_seconds=60,
            now="2026-08-07 13:01:01",
        )
        self.assertEqual(len(reclaimed), 1)
        self.assertEqual(reclaimed[0]["outbox_id"], claimed[0]["outbox_id"])
        self.assertEqual(reclaimed[0]["attempt_count"], 2)

    def test_outbox_retry_backoff_dead_letter_and_progress_past_dead_item(self):
        with patch(
            "app.chat.storage.beijing_now_text",
            return_value="2026-08-07 12:59:00",
        ):
            first = self.store.save_memory(
                "1006", "First outbox", "user_confirmed", self.scope
            )
        claimed = self.store.claim_outbox(
            "worker-a", limit=1, lease_seconds=60, now="2026-08-07 13:00:00"
        )[0]
        self.store.mark_outbox_failed(
            claimed["outbox_id"],
            "worker-a",
            "TransientError",
            now="2026-08-07 13:00:10",
            max_attempts=3,
        )
        self.assertEqual(
            self.store.claim_outbox(
                "worker-a", limit=1, now="2026-08-07 13:00:10"
            ),
            [],
        )
        retry = self.store.claim_outbox(
            "worker-a", limit=1, now="2026-08-07 13:01:00"
        )[0]
        self.store.mark_outbox_failed(
            retry["outbox_id"],
            "worker-a",
            "PermanentError",
            now="2026-08-07 13:01:01",
            permanent=True,
            max_attempts=3,
        )
        with patch(
            "app.chat.storage.beijing_now_text",
            return_value="2026-08-07 13:01:30",
        ):
            second = self.store.save_memory(
                "1006", "Second outbox", "user_confirmed", self.scope
            )
        next_claim = self.store.claim_outbox(
            "worker-b", limit=100, now="2026-08-07 13:02:00"
        )

        self.assertEqual([item["memory_id"] for item in next_claim], [second.memory_id])
        with closing(sqlite3.connect(self.db_path)) as conn:
            dead = conn.execute(
                "SELECT status, attempt_count FROM memory_index_outbox WHERE memory_id = ?",
                (first.memory_id,),
            ).fetchone()
        self.assertEqual(dead, ("dead", 2))

    def test_outbox_ownership_mismatch_raises_specific_domain_error(self):
        self.store.save_memory(
            "1006", "Lease ownership", "user_confirmed", self.scope
        )
        claimed = self.store.claim_outbox("worker-a", limit=1)[0]

        with self.assertRaises(ValidationError) as processed_error:
            self.store.mark_outbox_processed(claimed["outbox_id"], "worker-b")
        self.assertEqual(
            type(processed_error.exception).__name__, "OutboxLeaseLost"
        )

        with self.assertRaises(ValidationError) as failed_error:
            self.store.mark_outbox_failed(
                claimed["outbox_id"], "worker-b", "VectorFailure"
            )
        self.assertEqual(type(failed_error.exception).__name__, "OutboxLeaseLost")
        self.assertIn(
            "OutboxLeaseLost",
            ChatRepository.mark_outbox_processed.__doc__ or "",
        )
        self.assertIn(
            "OutboxLeaseLost",
            ChatRepository.mark_outbox_failed.__doc__ or "",
        )

    def test_legacy_outbox_schema_migrates_claim_and_retry_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy-outbox.db"
            with closing(sqlite3.connect(db_path)) as conn:
                conn.executescript(
                    """
                    CREATE TABLE memory_index_outbox (
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
                        attempts INTEGER NOT NULL DEFAULT 0,
                        last_error_type TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        processed_at TEXT
                    );
                    INSERT INTO memory_index_outbox VALUES (
                        'legacy-outbox', 'memory-1', '1006', NULL, NULL, NULL,
                        'upsert', 'hash', 'user_confirmed', 'sanitized',
                        'pending', 2, '', '2026-08-07 13:00:00', NULL
                    );
                    """
                )

            store = SQLiteAuthorityStore(db_path)
            with closing(sqlite3.connect(db_path)) as conn:
                columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(memory_index_outbox)")
                }
                row = conn.execute(
                    """
                    SELECT attempt_count, next_attempt_at, lease_expires_at, claimed_by
                    FROM memory_index_outbox WHERE outbox_id = 'legacy-outbox'
                    """
                ).fetchone()

        self.assertTrue(
            {"attempt_count", "next_attempt_at", "lease_expires_at", "claimed_by"}
            <= columns
        )
        self.assertEqual(row, (2, "2026-08-07 13:00:00", None, None))
        self.assertEqual(store.journal_mode, "wal")

    def test_wal_negotiates_once_and_records_safe_degradation(self):
        class DegradedStore(SQLiteAuthorityStore):
            negotiation_calls = 0

            def _request_wal_mode(self, conn):
                self.negotiation_calls += 1
                return "delete"

        with tempfile.TemporaryDirectory() as tmp:
            store = DegradedStore(Path(tmp) / "degraded.db")
            store.create_session("1006", "One", self.scope)
            store.list_sessions("1006", self.scope)

        self.assertEqual(store.negotiation_calls, 1)
        self.assertEqual(store.journal_mode, "delete")
        self.assertEqual(store.wal_degraded_reason, "wal_not_enabled:delete")

    def test_repository_memory_protocol_has_explicit_typed_parameters(self):
        expected_save = {
            "self",
            "project_id",
            "canonical_text",
            "source_type",
            "scope",
            "session_id",
            "confidence",
            "confidentiality",
            "expires_at",
        }
        expected_supersede = {
            "self",
            "memory_id",
            "canonical_text",
            "scope",
            "project_id",
            "source_type",
            "confidence",
            "confidentiality",
            "expires_at",
        }
        save_parameters = inspect.signature(ChatRepository.save_memory).parameters
        supersede_parameters = inspect.signature(
            ChatRepository.supersede_memory
        ).parameters

        self.assertEqual(set(save_parameters), expected_save)
        self.assertEqual(set(supersede_parameters), expected_supersede)
        self.assertTrue(
            all(
                parameter.kind is not inspect.Parameter.VAR_KEYWORD
                for parameter in (*save_parameters.values(), *supersede_parameters.values())
            )
        )


if __name__ == "__main__":
    unittest.main()
