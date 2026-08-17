from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.chat.models import (
    ChatMessage,
    ChatNotFound,
    ChatSession,
    Citation,
    IdempotencyConflict,
    MemoryItem,
    MessageWriteResult,
    PrincipalScope,
    ValidationError,
)


def auth_dependency(x_agent_password: str = "") -> None:
    if x_agent_password == "deny":
        raise HTTPException(status_code=401, detail="denied")


@dataclass
class FakeSendResult:
    session: ChatSession
    user_message: ChatMessage
    assistant_message: ChatMessage
    citations: list[Citation]
    fallback_used: bool = False
    retrieval_degraded: bool = False
    retrieval_reason: str = ""


class FakeRepository:
    def __init__(self):
        self.scope = PrincipalScope()
        self.session = ChatSession(
            "session-1",
            "1006",
            "首次分析",
            "active",
            self.scope,
            "2026-08-10 09:00:00",
            "2026-08-10 09:00:00",
        )
        self.user = ChatMessage(
            "message-user",
            "session-1",
            1,
            "user",
            "请分析风险",
            "hash-u",
            4,
            "2026-08-10 09:01:00",
        )
        self.assistant = ChatMessage(
            "message-assistant",
            "session-1",
            2,
            "assistant",
            "建议关注回款。",
            "hash-a",
            6,
            "2026-08-10 09:02:00",
            reply_to_message_id="message-user",
        )
        self.created_sessions: list[dict[str, Any]] = []

    def create_session(
        self, project_id: str, title: str, scope: PrincipalScope
    ) -> ChatSession:
        self.created_sessions.append({"project_id": project_id, "title": title, "scope": scope})
        return ChatSession(
            "created-session",
            project_id,
            title,
            "active",
            scope,
            "2026-08-10 10:00:00",
            "2026-08-10 10:00:00",
        )

    def list_sessions(
        self, project_id: str, scope: PrincipalScope, limit: int = 100
    ) -> list[ChatSession]:
        return [self.session] if project_id == "1006" else []

    def get_session_by_id(
        self, session_id: str, scope: PrincipalScope
    ) -> ChatSession:
        if session_id != self.session.session_id:
            raise ChatNotFound("not found")
        return self.session

    def list_messages(
        self,
        session_id: str,
        scope: PrincipalScope,
        *,
        project_id: str,
        limit: int | None = None,
    ) -> list[ChatMessage]:
        if session_id != self.session.session_id:
            raise ChatNotFound("not found")
        messages = [self.user, self.assistant]
        return messages[-limit:] if limit else messages

    def list_citations(
        self, message_id: str, scope: PrincipalScope, *, project_id: str
    ) -> list[Citation]:
        if message_id == self.assistant.message_id:
            return [
                Citation(
                    "citation-1",
                    message_id,
                    "risk_history",
                    "risk-history:42",
                    "最新风险评估 #42",
                )
            ]
        return []

    def list_memories(
        self, project_id: str, scope: PrincipalScope, limit: int = 100, *, now: str | None = None
    ) -> list[MemoryItem]:
        return [
            MemoryItem(
                "memory-1",
                project_id,
                "session-1",
                scope,
                "user_confirmed",
                "不得接受无担保赊销",
                "hash-m",
                "active",
                0.9,
                "sanitized",
                None,
                None,
                "2026-08-10 09:03:00",
                "2026-08-10 09:03:00",
            ),
            MemoryItem(
                "memory-secret",
                project_id,
                "session-1",
                scope,
                "user_confirmed",
                "Token: super-secret-token-value",
                "hash-secret",
                "active",
                1.0,
                "local_only",
                None,
                None,
                "2026-08-10 09:04:00",
                "2026-08-10 09:04:00",
            )
        ][:limit]


class FakeChatService:
    def __init__(self, repository: FakeRepository):
        self.repository = repository
        self.sent: list[dict[str, Any]] = []

    def send_message(
        self,
        *,
        session_id: str,
        content: str,
        idempotency_key: str,
        remember: bool,
        scope: PrincipalScope,
    ) -> FakeSendResult:
        if content == "conflict":
            raise IdempotencyConflict("provider SQL password=secret")
        if content == "bad":
            raise ValidationError("bad SQL password=secret")
        session = self.repository.get_session_by_id(session_id, scope)
        self.sent.append(
            {
                "session_id": session_id,
                "content": content,
                "idempotency_key": idempotency_key,
                "remember": remember,
                "scope": scope,
            }
        )
        return FakeSendResult(
            session=session,
            user_message=self.repository.user,
            assistant_message=self.repository.assistant,
            citations=self.repository.list_citations(
                self.repository.assistant.message_id,
                scope,
                project_id=session.project_id,
            ),
            fallback_used=False,
            retrieval_degraded=True,
            retrieval_reason="vector unavailable",
        )


class ChatApiTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.chat.api import build_chat_router

        self.repository = FakeRepository()
        self.service = FakeChatService(self.repository)
        app = FastAPI()
        app.include_router(
            build_chat_router(lambda: self.service, Depends(auth_dependency))
        )
        self.client = TestClient(app)

    def test_session_lifecycle_and_message_contracts(self):
        created = self.client.post(
            "/api/chat/sessions",
            json={"project_id": "1006", "title": "新会话", "user_id": "bad"},
        )
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["session"]["session_id"], "created-session")
        self.assertNotIn("user_id", created.text)

        listed = self.client.get("/api/chat/sessions?project_id=1006&limit=999")
        self.assertEqual(listed.status_code, 200)
        self.assertLessEqual(listed.json()["limit"], 100)
        self.assertEqual(listed.json()["sessions"][0]["project_id"], "1006")

        session = self.client.get("/api/chat/sessions/session-1")
        self.assertEqual(session.status_code, 200)
        self.assertEqual(session.json()["session"]["title"], "首次分析")

        messages = self.client.get("/api/chat/sessions/session-1/messages?limit=1000")
        self.assertEqual(messages.status_code, 200)
        self.assertEqual(messages.json()["messages"][1]["citations"][0]["source_id"], "risk-history:42")
        self.assertLessEqual(messages.json()["limit"], 200)

        sent = self.client.post(
            "/api/chat/sessions/session-1/messages",
            json={
                "content": "请分析风险",
                "idempotency_key": "req-1",
                "remember": True,
                "role": "user",
                "citation_ids": ["memory:bad"],
            },
        )
        self.assertEqual(sent.status_code, 200)
        body = sent.json()
        self.assertEqual(body["assistant_message"]["role"], "assistant")
        self.assertTrue(body["retrieval_degraded"])
        self.assertEqual(self.service.sent[0]["idempotency_key"], "req-1")

        memories = self.client.get("/api/chat/sessions/session-1/memories")
        self.assertEqual(memories.status_code, 200)
        self.assertEqual(memories.json()["memories"][0]["source_type"], "user_confirmed")

    def test_memory_api_does_not_expose_local_only_or_canonical_text(self):
        memories = self.client.get("/api/chat/sessions/session-1/memories")
        self.assertEqual(memories.status_code, 200)
        self.assertNotIn("canonical_text", memories.text)
        self.assertNotIn("super-secret-token-value", memories.text)
        self.assertIn("local_only", memories.text)

    def test_public_errors_are_bounded_and_sanitized(self):
        blank = self.client.post("/api/chat/sessions", json={"project_id": "", "title": ""})
        self.assertEqual(blank.status_code, 400)

        unknown = self.client.get("/api/chat/sessions/missing")
        self.assertEqual(unknown.status_code, 404)

        conflict = self.client.post(
            "/api/chat/sessions/session-1/messages",
            json={"content": "conflict", "idempotency_key": "req-c"},
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertNotIn("SQL", conflict.text)
        self.assertNotIn("secret", conflict.text)

        bad = self.client.post(
            "/api/chat/sessions/session-1/messages",
            json={"content": "bad", "idempotency_key": "req-b"},
        )
        self.assertEqual(bad.status_code, 400)
        self.assertNotIn("password", bad.text)

    def test_readonly_endpoints_can_use_repository_without_initializing_chat_service(self):
        from app.chat.api import build_chat_router

        repository = FakeRepository()

        def forbidden_service():
            raise AssertionError("chat service should not initialize")

        app = FastAPI()
        app.include_router(
            build_chat_router(
                forbidden_service,
                Depends(auth_dependency),
                repository_provider=lambda: repository,
            )
        )
        client = TestClient(app)

        listed = client.get("/api/chat/sessions?project_id=1006")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["sessions"][0]["session_id"], "session-1")


if __name__ == "__main__":
    unittest.main()
