from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from app.chat.memory import MemoryOrchestrator
from app.chat.memory import ContextMemory, MemoryContext
from app.chat.models import MemoryItem, PrincipalScope, ValidationError
from app.chat.storage import SQLiteAuthorityStore
from app.chat.vector import NullVectorIndex
from app.chat.embedding import NullEmbeddingProvider


class FakeRiskContextProvider:
    def __init__(self, payload: dict[str, Any] | None):
        self.payload = payload
        self.calls: list[str] = []

    def get_sanitized_context(self, project_id: str) -> dict[str, Any] | None:
        self.calls.append(project_id)
        return self.payload


class FakeLlmClient:
    def __init__(
        self,
        *,
        response: dict[str, Any] | str | None = None,
        available: bool = True,
        fail: bool = False,
    ):
        self.response = response or {
            "answer": "综合看，当前最大风险是回款不确定性。",
            "citation_ids": [],
        }
        self._available = available
        self.fail = fail
        self.calls = 0
        self.last_messages: list[dict[str, str]] = []
        self.last_payload_json = ""

    @property
    def available(self) -> bool:
        return self._available

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
    ) -> dict[str, Any]:
        self.calls += 1
        self.last_messages = [dict(message) for message in messages]
        self.last_payload_json = messages[-1]["content"]
        if self.fail:
            raise RuntimeError("provider exploded with token=do-not-leak")
        if isinstance(self.response, str):
            return {"answer": self.response}
        return dict(self.response)


class SlowFakeLlmClient(FakeLlmClient):
    def __init__(self):
        super().__init__(
            response={"answer": "并发回答", "citation_ids": ["risk-history:42"]}
        )
        self.barrier = threading.Barrier(2)

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
    ) -> dict[str, Any]:
        self.calls += 1
        self.last_messages = [dict(message) for message in messages]
        self.last_payload_json = messages[-1]["content"]
        try:
            self.barrier.wait(timeout=1)
        except threading.BrokenBarrierError:
            pass
        time.sleep(0.05)
        return dict(self.response)


class ExplodingRiskContextProvider:
    def get_sanitized_context(self, project_id: str) -> dict[str, Any] | None:
        raise RuntimeError("source SQL failed password=leak-me")


class ExplodingMemory:
    def build_context(self, *args: Any, **kwargs: Any):
        raise RuntimeError("vector backend token=leak-me")

    def promote_user_confirmed(self, *args: Any, **kwargs: Any):
        raise RuntimeError("promotion token=leak-me")


class CrossProjectMemory:
    def __init__(self, item: MemoryItem):
        self.item = item

    def build_context(self, *args: Any, **kwargs: Any) -> MemoryContext:
        return MemoryContext(
            summary=None,
            recent_messages=[],
            memories=[ContextMemory(self.item, 1.0, "semantic")],
            estimated_chars=len(self.item.canonical_text),
        )

    def promote_user_confirmed(self, *args: Any, **kwargs: Any):
        return None


class ChatServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.tmp.name) / "risk-chat.db"
        self.store = SQLiteAuthorityStore(self.db_path)
        self.scope = PrincipalScope()
        self.session = self.store.create_session("1006", "首次分析", self.scope)
        self.risk_payload = {
            "evaluated": True,
            "latest": {
                "history_id": 42,
                "score": 73,
                "level": "高",
                "created_at": "2026-08-07 14:00:00",
                "dimensions": [{"name": "回款", "score": 82, "summary": "回款周期偏长"}],
                "hits": [{"rule": "合同回款滞后", "severity": "high"}],
                "suggestions": ["补充回款节点约束"],
                "project_name": "资金来源测试111",
            },
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_service(
        self,
        *,
        llm: FakeLlmClient | None = None,
        risk_payload: dict[str, Any] | None | object = Ellipsis,
    ):
        from app.chat.service import ChatService

        memory = MemoryOrchestrator(
            self.store,
            NullEmbeddingProvider("test"),
            NullVectorIndex("test"),
        )
        payload = self.risk_payload if risk_payload is Ellipsis else risk_payload
        return ChatService(
            repository=self.store,
            memory=memory,
            risk_context_provider=FakeRiskContextProvider(payload),
            llm_client=llm or FakeLlmClient(),
            model="deepseek-v4-flash",
        )

    def make_service_with_ports(
        self,
        *,
        memory: Any,
        risk_context_provider: Any,
        llm: FakeLlmClient | None = None,
    ):
        from app.chat.service import ChatService

        return ChatService(
            repository=self.store,
            memory=memory,
            risk_context_provider=risk_context_provider,
            llm_client=llm or FakeLlmClient(),
            model="deepseek-v4-flash",
        )

    def read_llm_audit_rows(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute("SELECT * FROM chat_llm_audit")]

    def test_send_message_sanitizes_payload_persists_answer_citations_and_memory(self):
        memory = self.store.save_memory(
            "1006",
            "该项目不得接受无担保赊销",
            "user_confirmed",
            self.scope,
            session_id=self.session.session_id,
            confidence=0.9,
        )
        llm = FakeLlmClient(
            response={
                "answer": "建议优先锁定回款节点，并避免无担保赊销。",
                "citation_ids": ["risk-history:42", f"memory:{memory.memory_id}"],
            }
        )
        service = self.make_service(llm=llm)

        result = service.send_message(
            session_id=self.session.session_id,
            content="请分析无担保赊销风险，token=secret-value",
            idempotency_key="request-1",
            remember=True,
            scope=self.scope,
        )

        self.assertEqual(result.user_message.sequence_no, 1)
        self.assertEqual(result.assistant_message.sequence_no, 2)
        self.assertNotIn("secret-value", llm.last_payload_json)
        self.assertIn("[REDACTED", llm.last_payload_json)
        self.assertTrue(result.citations)
        self.assertEqual(
            {citation.source_id for citation in result.citations},
            {"risk-history:42", f"memory:{memory.memory_id}"},
        )
        remembered = self.store.list_memories("1006", self.scope)
        self.assertTrue(
            any(item.source_type == "user_confirmed" for item in remembered),
            remembered,
        )

    def test_prompt_payload_places_latest_risk_before_memory_snippets(self):
        memory = self.store.save_memory(
            "1006",
            "长期记忆：禁止接受无担保赊销",
            "user_confirmed",
            self.scope,
            session_id=self.session.session_id,
        )
        llm = FakeLlmClient(
            response={
                "answer": "结合最新评分和长期记忆给出建议。",
                "citation_ids": ["risk-history:42", f"memory:{memory.memory_id}"],
            }
        )

        self.make_service(llm=llm).send_message(
            session_id=self.session.session_id,
            content="无担保赊销风险怎么处理？",
            idempotency_key="request-2",
            remember=False,
            scope=self.scope,
        )

        payload = json.loads(llm.last_payload_json)
        self.assertLess(
            llm.last_payload_json.index('"risk_summary"'),
            llm.last_payload_json.index('"memories"'),
        )
        self.assertEqual(payload["risk_summary"]["history_id"], 42)
        self.assertEqual(payload["memories"][0]["citation_id"], f"memory:{memory.memory_id}")

    def test_disallowed_citations_are_rejected_before_persisting(self):
        other_project = self.store.create_session("2001", "其他项目", self.scope)
        other_memory = self.store.save_memory(
            "2001",
            "其他项目记忆不能泄漏",
            "user_confirmed",
            self.scope,
            session_id=other_project.session_id,
        )
        llm = FakeLlmClient(
            response={
                "answer": "只保留允许引用。",
                "citation_ids": [
                    "risk-history:42",
                    "risk-history:999",
                    f"memory:{other_memory.memory_id}",
                    "memory:missing",
                ],
            }
        )

        result = self.make_service(llm=llm).send_message(
            session_id=self.session.session_id,
            content="请给出引用。",
            idempotency_key="request-3",
            remember=False,
            scope=self.scope,
        )

        self.assertEqual([citation.source_id for citation in result.citations], ["risk-history:42"])
        stored = self.store.list_citations(
            result.assistant_message.message_id,
            self.scope,
            project_id="1006",
        )
        self.assertEqual([citation.source_id for citation in stored], ["risk-history:42"])

    def test_duplicate_idempotency_key_replays_without_calling_llm_twice(self):
        llm = FakeLlmClient(response={"answer": "第一次回答", "citation_ids": ["risk-history:42"]})
        service = self.make_service(llm=llm)

        first = service.send_message(
            session_id=self.session.session_id,
            content="请分析回款风险。",
            idempotency_key="same-request",
            remember=False,
            scope=self.scope,
        )
        second = service.send_message(
            session_id=self.session.session_id,
            content="请分析回款风险。",
            idempotency_key="same-request",
            remember=False,
            scope=self.scope,
        )

        self.assertEqual(llm.calls, 1)
        self.assertEqual(first.user_message.message_id, second.user_message.message_id)
        self.assertEqual(
            first.assistant_message.message_id,
            second.assistant_message.message_id,
        )

    def test_duplicate_fallback_replay_preserves_fallback_status(self):
        service = self.make_service(llm=FakeLlmClient(available=False))

        first = service.send_message(
            session_id=self.session.session_id,
            content="第一次 fallback。",
            idempotency_key="fallback-replay",
            remember=False,
            scope=self.scope,
        )
        second = service.send_message(
            session_id=self.session.session_id,
            content="第一次 fallback。",
            idempotency_key="fallback-replay",
            remember=False,
            scope=self.scope,
        )

        self.assertTrue(first.fallback_used)
        self.assertTrue(second.fallback_used)

    def test_unconfigured_or_failed_llm_stores_local_fallback_answer(self):
        for index, llm in enumerate(
            (
                FakeLlmClient(available=False),
                FakeLlmClient(available=True, fail=True),
            ),
            start=1,
        ):
            with self.subTest(index=index):
                result = self.make_service(llm=llm).send_message(
                    session_id=self.session.session_id,
                    content=f"LLM 不可用时也要回答 {index}",
                    idempotency_key=f"fallback-{index}",
                    remember=False,
                    scope=self.scope,
                )
                self.assertIn("高", result.assistant_message.content)
                self.assertTrue(result.fallback_used)

        audits = self.read_llm_audit_rows()
        self.assertEqual([bool(row["fallback_used"]) for row in audits], [True, True])

    def test_fallback_answer_uses_structured_allowlist_not_raw_risk_objects(self):
        risk_payload = {
            "latest": {
                "history_id": 42,
                "score": 71,
                "level": "高",
                "hits": [
                    {
                        "rule": "回款延期",
                        "severity": "high",
                        "summary": "周期偏长",
                        "source_sql": "SELECT password FROM secret",
                        "contract_text": "合同原文不得公开",
                        "attachments": ["合同附件.pdf"],
                        "browser_state": {"cookie": "sid=secret"},
                    }
                ],
                "suggestions": [
                    "补充付款节点",
                    {"contract_text": "合同原文不得公开", "raw": "secret"},
                ],
            }
        }

        result = self.make_service(
            llm=FakeLlmClient(available=False),
            risk_payload=risk_payload,
        ).send_message(
            session_id=self.session.session_id,
            content="fallback 也要安全。",
            idempotency_key="fallback-allowlist",
            remember=False,
            scope=self.scope,
        )

        text = result.assistant_message.content
        self.assertIn("回款延期", text)
        for marker in (
            "source_sql",
            "contract_text",
            "attachments",
            "browser_state",
            "password",
            "合同原文不得公开",
            "合同附件.pdf",
            "sid=secret",
        ):
            self.assertNotIn(marker, text)

    def test_plain_text_llm_response_is_accepted(self):
        llm = FakeLlmClient(response="这是普通文本回答。")

        result = self.make_service(llm=llm).send_message(
            session_id=self.session.session_id,
            content="请用普通文本回答。",
            idempotency_key="plain-text",
            remember=False,
            scope=self.scope,
        )

        self.assertEqual(result.assistant_message.content, "这是普通文本回答。")

    def test_rejects_non_principal_scope_without_falling_back_to_anonymous(self):
        with self.assertRaises(ValidationError):
            self.make_service().send_message(
                session_id=self.session.session_id,
                content="非法 scope 不能访问匿名会话。",
                idempotency_key="bad-scope",
                remember=False,
                scope={},  # type: ignore[arg-type]
            )

    def test_prompt_allowlist_drops_forbidden_nested_risk_fields_and_caps_total(self):
        from app.chat.llm import build_prompt

        prompt = build_prompt(
            question="q" * 3000 + " token=secret-value",
            risk_context={
                "latest": {
                    "history_id": 42,
                    "score": 88,
                    "level": "高",
                    "dimensions": [
                        {
                            "name": "合同",
                            "score": 90,
                            "summary": "含敏感 cookie: sid=secret; auth=still-secret",
                            "source_sql": "SELECT password FROM user",
                            "contract_text": "合同原文不得外发",
                            "attachments": ["附件不得外发"],
                            "browser_state": {"cookie": "x"},
                            "sqlite_rows": [{"raw": "row"}],
                        }
                    ],
                    "hits": [
                        {
                            "rule": "命中规则",
                            "severity": "high",
                            "evidence": "mysql://root:pass@db.local/risk",
                            "contract_text": "合同原文不得外发",
                        }
                    ],
                    "suggestions": ["联系 13800138000，邮箱 a@example.com"],
                    "source_sql": "SELECT * FROM secret",
                }
            },
            conversation_summary="summary " * 500,
            recent_messages=[{"role": "user", "content": "m" * 1000}],
            memories=[
                {
                    "citation_id": "memory:1",
                    "memory_type": "user_confirmed",
                    "canonical_text": "memory " * 1000,
                    "confidence": 1,
                }
            ],
            total_char_limit=1000,
        )

        self.assertLessEqual(len(prompt.payload_json), 1000)
        self.assertEqual(
            set(prompt.payload),
            {
                "question",
                "risk_summary",
                "conversation_summary",
                "recent_messages",
                "memories",
            },
        )
        forbidden = repr(prompt.payload)
        for marker in (
            "source_sql",
            "contract_text",
            "attachments",
            "browser_state",
            "sqlite_rows",
            "sid=secret",
            "still-secret",
            "root:pass",
            "13800138000",
            "a@example.com",
            "secret-value",
        ):
            self.assertNotIn(marker, forbidden)

    def test_prompt_sanitizes_history_and_rejects_malformed_memory_citation_ids(self):
        from app.chat.llm import build_prompt

        prompt = build_prompt(
            question="请分析",
            risk_context={
                "latest": {
                    "history_id": "42 token=secret-value",
                    "score": 80,
                    "level": "高",
                }
            },
            conversation_summary="",
            recent_messages=[],
            memories=[
                {
                    "citation_id": "memory:account=secret123",
                    "memory_type": "user_confirmed",
                    "canonical_text": "secret-value should not ride in citation id",
                    "confidence": 1,
                }
            ],
            total_char_limit=12000,
        )

        serialized = prompt.payload_json
        self.assertNotIn("secret-value", serialized)
        self.assertNotIn("account=secret123", serialized)
        self.assertEqual(prompt.payload["memories"], [])
        self.assertEqual(prompt.allowed_citations, {})

    def test_cross_project_memory_context_is_not_allowed_as_citation(self):
        other_session = self.store.create_session("2001", "其他项目", self.scope)
        other_memory = self.store.save_memory(
            "2001",
            "跨项目记忆不能引用",
            "user_confirmed",
            self.scope,
            session_id=other_session.session_id,
        )
        llm = FakeLlmClient(
            response={
                "answer": "不要保存跨项目引用。",
                "citation_ids": [f"memory:{other_memory.memory_id}"],
            }
        )
        service = self.make_service_with_ports(
            memory=CrossProjectMemory(other_memory),
            risk_context_provider=FakeRiskContextProvider(None),
            llm=llm,
        )

        result = service.send_message(
            session_id=self.session.session_id,
            content="请分析跨项目引用。",
            idempotency_key="cross-project-memory",
            remember=False,
            scope=self.scope,
        )

        self.assertEqual(result.citations, [])
        self.assertNotIn(f"memory:{other_memory.memory_id}", llm.last_payload_json)

    def test_context_failures_degrade_to_fallback_without_leaking_error_details(self):
        service = self.make_service_with_ports(
            memory=ExplodingMemory(),
            risk_context_provider=ExplodingRiskContextProvider(),
            llm=FakeLlmClient(),
        )

        result = service.send_message(
            session_id=self.session.session_id,
            content="上下文失败也要可恢复。",
            idempotency_key="context-failure",
            remember=False,
            scope=self.scope,
        )

        self.assertTrue(result.fallback_used)
        self.assertIn("unknown", result.assistant_message.content)
        public = repr(result)
        self.assertNotIn("leak-me", public)
        self.assertNotIn("source SQL", public)

    def test_remember_is_recoverable_when_first_attempt_falls_back_after_user_message(self):
        service = self.make_service_with_ports(
            memory=ExplodingMemory(),
            risk_context_provider=FakeRiskContextProvider(self.risk_payload),
            llm=FakeLlmClient(),
        )
        service.send_message(
            session_id=self.session.session_id,
            content="记住：不得接受无担保赊销",
            idempotency_key="recover-remember",
            remember=True,
            scope=self.scope,
        )
        self.assertEqual(self.store.list_memories("1006", self.scope), [])

        healthy = self.make_service()
        healthy.send_message(
            session_id=self.session.session_id,
            content="记住：不得接受无担保赊销",
            idempotency_key="recover-remember",
            remember=True,
            scope=self.scope,
        )

        self.assertTrue(
            any(
                item.source_type == "user_confirmed"
                for item in self.store.list_memories("1006", self.scope)
            )
        )

    def test_concurrent_duplicate_idempotency_calls_llm_once(self):
        llm = SlowFakeLlmClient()
        service = self.make_service(llm=llm)
        results = []
        errors = []

        def worker() -> None:
            try:
                results.append(
                    service.send_message(
                        session_id=self.session.session_id,
                        content="并发请求只应调用一次 LLM。",
                        idempotency_key="concurrent-same",
                        remember=False,
                        scope=self.scope,
                    )
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(llm.calls, 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(
            {result.assistant_message.message_id for result in results},
            {results[0].assistant_message.message_id},
        )
        self.assertEqual(service.idempotency_lock_count(), 0)

    def test_llm_audit_keeps_hashes_counts_model_and_never_payload_or_provider_secret(self):
        secret = "secret-value-123"
        llm = FakeLlmClient(
            response={
                "answer": f"回答不能把 {secret} 写入审计。",
                "citation_ids": ["risk-history:42"],
            }
        )

        self.make_service(llm=llm).send_message(
            session_id=self.session.session_id,
            content=f"请分析，password={secret}",
            idempotency_key="audit-request",
            remember=False,
            scope=self.scope,
        )

        audits = self.read_llm_audit_rows()
        self.assertEqual(len(audits), 1)
        audit = audits[0]
        self.assertEqual(len(audit["request_hash"]), 64)
        self.assertEqual(len(audit["response_hash"]), 64)
        self.assertGreater(audit["prompt_chars"], 0)
        self.assertGreater(audit["response_chars"], 0)
        self.assertEqual(audit["model"], "deepseek-v4-flash")
        self.assertEqual(audit["status"], "success")
        self.assertNotIn(secret, repr(audit))


if __name__ == "__main__":
    unittest.main()
