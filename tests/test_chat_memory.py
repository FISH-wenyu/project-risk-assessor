from __future__ import annotations

import importlib
import hashlib
import math
import os
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import app.chat.models as chat_models
from app.chat.embedding import (
    DeterministicEmbeddingProvider,
    FastEmbedProvider,
    NullEmbeddingProvider,
    create_embedding_provider,
)
from app.chat.memory import MemoryOrchestrator
from app.chat.models import PrincipalScope
from app.chat.storage import SQLiteAuthorityStore
from app.chat.vector import (
    InMemoryVectorIndex,
    NullVectorIndex,
    QdrantLocalIndex,
    create_vector_index,
)
from app.config import Settings, load_settings


class FailingEmbeddingProvider:
    available = True
    reason = ""

    def embed(self, texts):
        raise RuntimeError("embedding exploded")


class FailingVectorIndex:
    available = True
    reason = ""

    def upsert(self, record_id, vector, payload):
        raise RuntimeError("vector exploded")

    def delete(self, record_id):
        raise RuntimeError("vector exploded")

    def search(self, vector, *, project_id, scope, limit):
        raise RuntimeError("vector exploded")


class FakeEmbeddingModel:
    def __init__(self, rows):
        self.rows = rows

    def embed(self, texts):
        return iter(self.rows)


@dataclass
class FakePoint:
    id: str
    score: float
    payload: dict


class FakeQdrantClient:
    def __init__(self):
        self.upserts = []
        self.query_calls = []
        self.deleted = []
        self.created = []
        self.get_calls = []
        self.exists = False
        self.vector_size = 3
        self.distance = "Cosine"
        self.points = [
            FakePoint(
                "11111111-1111-4111-8111-111111111111",
                0.91,
                {
                    "record_id": "11111111-1111-4111-8111-111111111111",
                    "project_id": "1006",
                    "session_id": "session-1",
                    "user_id": "user-1",
                    "org_id": "org-1",
                    "embedding_model": "model",
                    "embedding_version": "1",
                },
            )
        ]

    def collection_exists(self, collection_name):
        return self.exists

    def create_collection(self, collection_name, vectors_config):
        self.created.append((collection_name, vectors_config))
        self.exists = True
        self.vector_size = vectors_config.size
        self.distance = vectors_config.distance

    def get_collection(self, collection_name):
        self.get_calls.append(collection_name)
        vectors = type(
            "VectorParams",
            (),
            {"size": self.vector_size, "distance": self.distance},
        )()
        params = type("CollectionParams", (), {"vectors": vectors})()
        config = type("CollectionConfig", (), {"params": params})()
        return type("CollectionInfo", (), {"config": config})()

    def upsert(self, collection_name, points, wait=True):
        self.upserts.append((collection_name, points, wait))

    def delete(self, collection_name, points_selector, wait=True):
        self.deleted.append((collection_name, points_selector, wait))

    def query_points(self, **kwargs):
        self.query_calls.append(kwargs)
        return type("QueryResult", (), {"points": self.points})()


def _condition_map(query_filter):
    result = {}
    for condition in query_filter.must:
        key = getattr(condition, "key", None)
        match = getattr(condition, "match", None)
        if match is not None:
            result[key] = getattr(match, "value", None)
            continue
        is_null = getattr(condition, "is_null", None)
        payload = getattr(is_null, "key", is_null)
        result[getattr(payload, "key", payload)] = None
    return result


class ConfigurationTests(unittest.TestCase):
    def test_memory_settings_defaults(self):
        settings = Settings()

        self.assertEqual(settings.memory_recent_messages, 12)
        self.assertEqual(settings.memory_context_chars, 12000)
        self.assertEqual(settings.memory_retrieval_limit, 6)
        self.assertEqual(settings.qdrant_path, Path("data/qdrant"))
        self.assertEqual(settings.qdrant_collection, "risk_agent_memory")
        self.assertEqual(
            settings.embedding_model,
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        )
        self.assertEqual(settings.embedding_version, "1")
        self.assertEqual(settings.embedding_dimension, 384)

    def test_memory_settings_environment_mapping(self):
        environment = {
            "MEMORY_RECENT_MESSAGES": "8",
            "MEMORY_CONTEXT_CHARS": "9000",
            "MEMORY_RETRIEVAL_LIMIT": "4",
            "QDRANT_PATH": "runtime/vectors",
            "QDRANT_COLLECTION": "memory_test",
            "EMBEDDING_MODEL": "local/test-model",
            "EMBEDDING_VERSION": "7",
            "EMBEDDING_DIMENSION": "768",
        }
        with patch.dict(os.environ, environment, clear=True), patch(
            "app.config.load_dotenv"
        ):
            settings = load_settings()

        self.assertEqual(settings.memory_recent_messages, 8)
        self.assertEqual(settings.memory_context_chars, 9000)
        self.assertEqual(settings.memory_retrieval_limit, 4)
        self.assertEqual(settings.qdrant_path, Path("runtime/vectors"))
        self.assertEqual(settings.qdrant_collection, "memory_test")
        self.assertEqual(settings.embedding_model, "local/test-model")
        self.assertEqual(settings.embedding_version, "7")
        self.assertEqual(settings.embedding_dimension, 768)

    def test_memory_settings_reject_invalid_schema_values(self):
        invalid_cases = (
            ({"memory_recent_messages": 0}, "memory_recent_messages"),
            ({"memory_context_chars": -1}, "memory_context_chars"),
            ({"memory_retrieval_limit": 0}, "memory_retrieval_limit"),
            ({"embedding_dimension": 0}, "embedding_dimension"),
            ({"qdrant_collection": "  "}, "qdrant_collection"),
            ({"embedding_model": ""}, "embedding_model"),
            ({"embedding_version": "  "}, "embedding_version"),
        )

        for kwargs, field in invalid_cases:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                Settings(**kwargs)


class EmbeddingProviderTests(unittest.TestCase):
    def test_default_embedding_model_is_registered_when_fastembed_is_installed(self):
        try:
            fastembed = importlib.import_module("fastembed")
        except ModuleNotFoundError:
            self.skipTest("fastembed is not installed")

        supported = {
            item["model"]: item
            for item in fastembed.TextEmbedding.list_supported_models()
        }
        settings = Settings()

        self.assertIn(settings.embedding_model, supported)
        self.assertEqual(
            int(supported[settings.embedding_model]["dim"]),
            settings.embedding_dimension,
        )

    def test_null_and_deterministic_providers(self):
        null = NullEmbeddingProvider("not installed")
        deterministic = DeterministicEmbeddingProvider(dimension=10)

        self.assertFalse(null.available)
        self.assertEqual(null.reason, "not installed")
        first = deterministic.embed(["same text", "different"])
        second = deterministic.embed(["same text"])
        self.assertEqual(len(first[0]), 10)
        self.assertEqual(first[0], second[0])
        self.assertNotEqual(first[0], first[1])

    def test_fastembed_uses_injected_factory_and_validates_results(self):
        captured = []

        def factory(model_name):
            captured.append(model_name)
            return FakeEmbeddingModel([[1, 2, 3], [4, 5, 6]])

        provider = FastEmbedProvider("local/model", model_factory=factory)

        self.assertTrue(provider.available)
        self.assertEqual(provider.embed(["a", "b"]), [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])
        self.assertEqual(captured, ["local/model"])

        wrong_count = FastEmbedProvider(
            "local/model", model_factory=lambda _: FakeEmbeddingModel([[1, 2]])
        )
        with self.assertRaises(ValueError):
            wrong_count.embed(["a", "b"])

        wrong_dimension = FastEmbedProvider(
            "local/model", model_factory=lambda _: FakeEmbeddingModel([[1, 2], [3]])
        )
        with self.assertRaises(ValueError):
            wrong_dimension.embed(["a", "b"])

        schema_mismatch = FastEmbedProvider(
            "local/model",
            expected_dimension=4,
            model_factory=lambda _: FakeEmbeddingModel([[1, 2, 3]]),
        )
        with self.assertRaisesRegex(ValueError, "dimension"):
            schema_mismatch.embed(["a"])

    def test_missing_optional_embedding_package_returns_null_without_import_failure(self):
        real_import_module = importlib.import_module

        def missing(name, package=None):
            if name == "fastembed":
                raise ModuleNotFoundError("fastembed")
            return real_import_module(name, package)

        with patch("importlib.import_module", side_effect=missing):
            provider = create_embedding_provider("local/model")

        self.assertIsInstance(provider, NullEmbeddingProvider)
        self.assertFalse(provider.available)
        self.assertIn("fastembed", provider.reason.lower())


class VectorIndexTests(unittest.TestCase):
    def test_in_memory_cosine_scope_and_expiration_filters(self):
        index = InMemoryVectorIndex(now_factory=lambda: "2026-08-07 13:00:00")
        common = {
            "session_id": None,
            "source_type": "user_confirmed",
            "confidentiality": "sanitized",
            "embedding_model": "test",
            "embedding_version": "1",
            "created_at": "2026-08-07 12:00:00",
            "expires_at": None,
        }
        index.upsert(
            "same",
            [1, 0],
            {
                **common,
                "record_id": "same",
                "project_id": "1006",
                "user_id": "user-1",
                "org_id": "org-1",
                "content_hash": "h1",
                "canonical_text": "must not be retained",
            },
        )
        index.upsert(
            "other-project",
            [1, 0],
            {**common, "record_id": "other-project", "project_id": "2001", "user_id": "user-1", "org_id": "org-1", "content_hash": "h2"},
        )
        index.upsert(
            "other-user",
            [1, 0],
            {**common, "record_id": "other-user", "project_id": "1006", "user_id": "user-2", "org_id": "org-1", "content_hash": "h3"},
        )
        index.upsert(
            "expired",
            [1, 0],
            {**common, "record_id": "expired", "project_id": "1006", "user_id": "user-1", "org_id": "org-1", "content_hash": "h4", "expires_at": "2026-08-07 12:59:59"},
        )

        hits = index.search(
            [1, 0],
            project_id="1006",
            scope=PrincipalScope("user-1", "org-1"),
            limit=10,
        )

        self.assertEqual([hit.memory_id for hit in hits], ["same"])
        self.assertTrue(math.isclose(hits[0].score, 1.0))
        self.assertNotIn("canonical_text", index.get_payload("same"))

    def test_qdrant_builds_server_filter_and_payload_has_no_authority_text(self):
        client = FakeQdrantClient()
        index = QdrantLocalIndex(
            Path("unused"),
            "memory",
            vector_size=3,
            embedding_model="model",
            embedding_version="1",
            client=client,
        )
        payload = {
            "record_id": "11111111-1111-4111-8111-111111111111",
            "project_id": "1006",
            "session_id": "session-1",
            "user_id": "user-1",
            "org_id": "org-1",
            "source_type": "user_confirmed",
            "confidentiality": "sanitized",
            "embedding_model": "model",
            "embedding_version": "1",
            "content_hash": "hash",
            "created_at": "2026-08-07 13:00:00",
            "expires_at": None,
            "canonical_text": "authority text",
        }

        index.upsert(payload["record_id"], [1, 0, 0], payload)
        hits = index.search(
            [1, 0, 0],
            project_id="1006",
            scope=PrincipalScope("user-1", "org-1"),
            limit=6,
        )

        point = client.upserts[0][1][0]
        expected_fields = {
            "record_id", "project_id", "session_id", "user_id", "org_id",
            "source_type", "confidentiality", "embedding_model",
            "embedding_version", "content_hash", "created_at", "expires_at",
        }
        self.assertEqual(set(point.payload), expected_fields)
        self.assertNotIn("canonical_text", point.payload)
        query = client.query_calls[0]
        self.assertEqual(
            _condition_map(query["query_filter"]),
            {
                "project_id": "1006",
                "user_id": "user-1",
                "org_id": "org-1",
                "embedding_model": "model",
                "embedding_version": "1",
            },
        )
        self.assertEqual([hit.memory_id for hit in hits], [payload["record_id"]])

        for field, bad_value in (
            ("embedding_model", "other-model"),
            ("embedding_version", "2"),
        ):
            invalid_payload = dict(payload)
            invalid_payload[field] = bad_value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                index.upsert(payload["record_id"], [1, 0, 0], invalid_payload)

    def test_qdrant_existing_collection_schema_must_match_dimension_and_cosine(self):
        for vector_size, distance, expected in (
            (4, "Cosine", "dimension"),
            (3, "Dot", "COSINE"),
        ):
            client = FakeQdrantClient()
            client.exists = True
            client.vector_size = vector_size
            client.distance = distance

            index = QdrantLocalIndex(
                Path("unused"),
                "memory",
                vector_size=3,
                embedding_model="model",
                embedding_version="1",
                client=client,
            )

            with self.subTest(vector_size=vector_size, distance=distance):
                self.assertFalse(index.available)
                self.assertIn(expected, index.reason)
                self.assertEqual(client.get_calls, ["memory"])

    def test_vector_factory_degrades_when_qdrant_initialization_fails(self):
        class BrokenClient:
            def collection_exists(self, collection_name):
                raise RuntimeError("cannot initialize")

        index = create_vector_index(
            Path("unused"),
            "memory",
            vector_size=3,
            embedding_model="model",
            embedding_version="1",
            client=BrokenClient(),
        )

        self.assertIsInstance(index, NullVectorIndex)
        self.assertIn("RuntimeError", index.reason)

    def test_qdrant_drops_an_out_of_scope_response_after_sending_server_filter(self):
        client = FakeQdrantClient()
        client.points.append(
            FakePoint(
                "22222222-2222-4222-8222-222222222222",
                0.99,
                {
                    "record_id": "22222222-2222-4222-8222-222222222222",
                    "project_id": "2001",
                    "session_id": None,
                    "user_id": "user-1",
                    "org_id": "org-1",
                    "embedding_model": "model",
                    "embedding_version": "1",
                },
            )
        )
        index = QdrantLocalIndex(
            Path("unused"),
            "memory",
            vector_size=3,
            embedding_model="model",
            embedding_version="1",
            client=client,
        )

        hits = index.search(
            [1, 0, 0],
            project_id="1006",
            scope=PrincipalScope("user-1", "org-1"),
            limit=6,
        )

        self.assertEqual(
            _condition_map(client.query_calls[0]["query_filter"])["project_id"],
            "1006",
        )
        self.assertEqual(
            [hit.memory_id for hit in hits],
            ["11111111-1111-4111-8111-111111111111"],
        )


class MemoryOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = SQLiteAuthorityStore(Path(self.temp_dir.name) / "chat.db")
        self.scope = PrincipalScope("user-1", "org-1")
        self.session = self.store.create_session("1006", "Memory", self.scope)
        self.embedding = DeterministicEmbeddingProvider(dimension=12)
        self.vector = InMemoryVectorIndex()

    def tearDown(self):
        self.temp_dir.cleanup()

    def orchestrator(self, **kwargs):
        return MemoryOrchestrator(
            self.store,
            self.embedding,
            self.vector,
            recent_message_limit=kwargs.pop("recent_message_limit", 12),
            context_char_budget=kwargs.pop("context_char_budget", 12000),
            retrieval_limit=kwargs.pop("retrieval_limit", 6),
            embedding_model="test/model",
            embedding_version="1",
            **kwargs,
        )

    def append_messages(self, count, *, width=20):
        start = len(
            self.store.list_messages(
                self.session.session_id, self.scope, project_id="1006"
            )
        )
        messages = []
        for index in range(start, start + count):
            messages.append(
                self.store.append_message(
                    self.session.session_id,
                    "user" if index % 2 == 0 else "assistant",
                    f"message-{index:02d} " + ("x" * width),
                    f"req-{index}",
                    self.scope,
                    project_id="1006",
                ).message
            )
        return messages

    def test_recent_limit_and_context_budget_keep_latest_message(self):
        messages = self.append_messages(20, width=500)
        memory = self.store.save_memory(
            "1006", "important " + ("m" * 2000), "user_confirmed", self.scope
        )
        context = self.orchestrator(context_char_budget=3000).build_context(
            self.session.session_id,
            project_id="1006",
            scope=self.scope,
            query="important",
        )

        self.assertLessEqual(len(context.recent_messages), 12)
        self.assertLessEqual(context.estimated_chars, 3000)
        self.assertEqual(context.recent_messages[-1].message_id, messages[-1].message_id)
        self.assertTrue(all(item.project_id == "1006" for item in context.memories))
        self.assertNotIn(memory.memory_id, [item.memory_id for item in context.memories])

    def test_budget_truncation_recomputes_message_and_summary_metadata(self):
        original = self.store.append_message(
            self.session.session_id,
            "user",
            "M" * 80,
            "budget-message",
            self.scope,
            project_id="1006",
        ).message
        context = self.orchestrator(context_char_budget=10).build_context(
            self.session.session_id,
            project_id="1006",
            scope=self.scope,
            query="no match",
        )
        message = context.recent_messages[-1]

        self.assertNotEqual(message.content, original.content)
        self.assertEqual(
            message.content_hash,
            hashlib.sha256(message.content.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(message.token_estimate, max(1, (len(message.content) + 3) // 4))

        summary_session = self.store.create_session("1006", "Summary budget", self.scope)
        first = self.store.append_message(
            summary_session.session_id,
            "user",
            "a",
            "summary-budget-a",
            self.scope,
            project_id="1006",
        ).message
        second = self.store.append_message(
            summary_session.session_id,
            "assistant",
            "b",
            "summary-budget-b",
            self.scope,
            project_id="1006",
        ).message
        authority_summary = self.store.save_summary(
            summary_session.session_id,
            "S" * 80,
            first.message_id,
            second.message_id,
            self.scope,
            project_id="1006",
        )

        summary_context = self.orchestrator(context_char_budget=20).build_context(
            summary_session.session_id,
            project_id="1006",
            scope=self.scope,
            query="no match",
        )

        self.assertNotEqual(summary_context.summary.content, authority_summary.content)
        self.assertEqual(
            summary_context.summary.content_hash,
            hashlib.sha256(summary_context.summary.content.encode("utf-8")).hexdigest(),
        )

    def test_summary_next_version_preserves_previous_tail_and_new_key_item(self):
        messages = []
        for index, content in enumerate(
            (
                "OLD_HEAD " + ("h" * 70),
                "OLD_LAST_CRITICAL",
                "bridge",
            )
        ):
            messages.append(
                self.store.append_message(
                    self.session.session_id,
                    "user",
                    content,
                    f"summary-tail-{index}",
                    self.scope,
                    project_id="1006",
                ).message
            )
        orchestrator = self.orchestrator(
            summary_threshold=2,
            summary_keep_messages=1,
            summary_char_limit=120,
        )
        self.assertTrue(hasattr(orchestrator, "requested_summary_char_limit"))
        self.assertTrue(hasattr(orchestrator, "effective_summary_char_limit"))
        self.assertTrue(hasattr(orchestrator, "summary_line_char_limit"))
        self.assertEqual(orchestrator.requested_summary_char_limit, 120)
        self.assertEqual(orchestrator.effective_summary_char_limit, 256)
        self.assertEqual(orchestrator.summary_line_char_limit, 80)
        orchestrator.build_context(
            self.session.session_id,
            project_id="1006",
            scope=self.scope,
            query="bridge",
        )
        first = self.store.get_latest_summary(
            self.session.session_id, self.scope, project_id="1006"
        )
        self.assertIn("OLD_LAST_CRITICAL", first.content)

        for index, content in enumerate(("NEW_CRITICAL", "latest"), start=3):
            self.store.append_message(
                self.session.session_id,
                "user",
                content,
                f"summary-tail-{index}",
                self.scope,
                project_id="1006",
            )
        orchestrator.build_context(
            self.session.session_id,
            project_id="1006",
            scope=self.scope,
            query="latest",
        )
        second = self.store.get_latest_summary(
            self.session.session_id, self.scope, project_id="1006"
        )

        self.assertEqual(second.version, 2)
        self.assertIn("OLD_HEAD", second.content)
        self.assertIn("OLD_LAST_CRITICAL", second.content)
        self.assertIn("NEW_CRITICAL", second.content)
        self.assertLessEqual(
            len(second.content), orchestrator.effective_summary_char_limit
        )
        self.assertTrue(
            all(
                len(line) <= orchestrator.summary_line_char_limit
                for line in second.content.splitlines()
            )
        )
        complete_lines = {
            *first.content.splitlines(),
            "user: bridge",
            "user: NEW_CRITICAL",
        }
        self.assertTrue(
            all(line in complete_lines for line in second.content.splitlines())
        )

    def test_rolling_summary_ranges_versions_redacts_and_keeps_originals(self):
        secret = self.store.append_message(
            self.session.session_id,
            "user",
            "api_key=top-secret project fact",
            "secret-message",
            self.scope,
            project_id="1006",
        ).message
        messages = self.append_messages(25, width=5)
        orchestrator = self.orchestrator(summary_char_limit=500)

        first_context = orchestrator.build_context(
            self.session.session_id, project_id="1006", scope=self.scope, query="fact"
        )
        first = self.store.get_latest_summary(
            self.session.session_id, self.scope, project_id="1006"
        )
        repeated_context = orchestrator.build_context(
            self.session.session_id, project_id="1006", scope=self.scope, query="fact"
        )
        repeated = self.store.get_latest_summary(
            self.session.session_id, self.scope, project_id="1006"
        )

        self.assertEqual(first.version, 1)
        self.assertEqual(first.message_start_id, secret.message_id)
        self.assertEqual(first.message_end_id, messages[12].message_id)
        self.assertEqual(repeated.summary_id, first.summary_id)
        self.assertNotIn("top-secret", first.content)
        self.assertLessEqual(len(first.content), 500)
        self.assertEqual(first_context.summary, repeated_context.summary)
        originals = self.store.list_messages(
            self.session.session_id, self.scope, project_id="1006"
        )
        self.assertEqual(len(originals), 26)
        self.assertEqual(originals[0].message_id, secret.message_id)

        later = self.append_messages(13, width=5)
        orchestrator.build_context(
            self.session.session_id, project_id="1006", scope=self.scope, query="later"
        )
        second = self.store.get_latest_summary(
            self.session.session_id, self.scope, project_id="1006"
        )
        self.assertEqual(second.version, 2)
        self.assertEqual(second.message_start_id, secret.message_id)
        self.assertEqual(second.message_end_id, later[0].message_id)
        self.assertIn("message-26", second.content)
        self.assertTrue(hasattr(orchestrator, "requested_summary_char_limit"))
        self.assertTrue(hasattr(orchestrator, "effective_summary_char_limit"))
        self.assertTrue(hasattr(orchestrator, "summary_line_char_limit"))
        self.assertEqual(orchestrator.requested_summary_char_limit, 500)
        self.assertEqual(orchestrator.effective_summary_char_limit, 500)
        self.assertLessEqual(
            len(second.content), orchestrator.effective_summary_char_limit
        )
        self.assertTrue(
            all(
                len(line) <= orchestrator.summary_line_char_limit
                for line in second.content.splitlines()
            )
        )

    def test_promotion_uses_confirmed_sanitized_authority_record(self):
        promoted = self.orchestrator().promote_user_confirmed(
            self.session.session_id,
            project_id="1006",
            scope=self.scope,
            canonical_text="No unsecured credit",
        )

        self.assertEqual(promoted.source_type, "user_confirmed")
        self.assertEqual(promoted.confidentiality, "sanitized")
        self.assertEqual(promoted.session_id, self.session.session_id)

    def test_promotion_marks_token_jwt_and_account_query_url_local_only(self):
        sensitive_texts = (
            "Token: super-secret-token-value",
            "Authorization Token opaque-token-value-123456",
            (
                "JWT eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
                "eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature123456"
            ),
            "Report https://example.test/risk?account=finance-admin&view=full",
            "secret=internal-value",
            "Authorization: Bearer bearer-token-value-123456",
            "Database mysql://root:dbpass@db.local:3306/risk",
            "Database mysql+pymysql://root:dbpass@db.local:3306/risk",
            "Owner alice@example.com",
            "Phone 13812345678",
            "account=finance-admin",
            '{"account": "finance-admin"}',
            "X-API-Key: x-api-secret-value",
            "X-Auth-Token: x-auth-token-value",
            "Authorization: Basic dXNlcjpwYXNzd29yZA==",
            "密码=pass-value",
            "口令=phrase-value",
            "密钥=key-value",
            "身份证号=11010519491231002X",
            "银行卡号=6222021234567890123",
            "Public link https://example.test/risk/report",
        )

        for index, canonical_text in enumerate(sensitive_texts):
            with self.subTest(index=index):
                promoted = self.orchestrator().promote_user_confirmed(
                    self.session.session_id,
                    project_id="1006",
                    scope=self.scope,
                    canonical_text=canonical_text,
                )

                self.assertEqual(promoted.confidentiality, "local_only")
                self.assertEqual(promoted.canonical_text, canonical_text)

        project_number = self.orchestrator().promote_user_confirmed(
            self.session.session_id,
            project_id="1006",
            scope=self.scope,
            canonical_text="项目编号=12012345678",
        )
        self.assertEqual(project_number.confidentiality, "sanitized")

    def test_promotion_supports_fullwidth_assignments_and_business_number_labels(self):
        sensitive_texts = (
            "密码：pass-value",
            "身份证号＝11010519491231002X",
            "银行卡号：6222021234567890123",
            "账号＝finance-admin",
            "联系电话：13812345678",
            "可回拨 13912345678",
        )
        for index, canonical_text in enumerate(sensitive_texts):
            with self.subTest(kind="sensitive", index=index):
                promoted = self.orchestrator().promote_user_confirmed(
                    self.session.session_id,
                    project_id="1006",
                    scope=self.scope,
                    canonical_text=canonical_text,
                )
                self.assertEqual(promoted.confidentiality, "local_only")

        business_numbers = (
            "项目编号：13812345678",
            "合同编号＝13912345678",
            "客户编号: 13712345678",
            "订单编号：13612345678",
        )
        for index, canonical_text in enumerate(business_numbers):
            with self.subTest(kind="business_number", index=index):
                promoted = self.orchestrator().promote_user_confirmed(
                    self.session.session_id,
                    project_id="1006",
                    scope=self.scope,
                    canonical_text=canonical_text,
                )
                self.assertEqual(promoted.confidentiality, "sanitized")

    def test_summary_redacts_fullwidth_assignments_but_keeps_business_numbers(self):
        sensitive = (
            "密码：pass-value；身份证号＝11010519491231002X；"
            "银行卡号：6222021234567890123；账号＝finance-admin；"
            "项目编号：13812345678；合同编号＝13912345678；"
            "客户编号:13712345678；独立手机号 13612345678"
        )
        self.store.append_message(
            self.session.session_id,
            "user",
            sensitive,
            "fullwidth-sanitizer",
            self.scope,
            project_id="1006",
        )
        self.store.append_message(
            self.session.session_id,
            "assistant",
            "Acknowledged",
            "fullwidth-sanitizer-ack",
            self.scope,
            project_id="1006",
        )
        self.store.append_message(
            self.session.session_id,
            "user",
            "Current question",
            "fullwidth-sanitizer-current",
            self.scope,
            project_id="1006",
        )

        summary = self.orchestrator(
            summary_threshold=2,
            summary_keep_messages=1,
            summary_char_limit=1000,
        ).build_context(
            self.session.session_id,
            project_id="1006",
            scope=self.scope,
            query="Current question",
        ).summary.content

        for secret in (
            "pass-value",
            "11010519491231002X",
            "6222021234567890123",
            "finance-admin",
            "13612345678",
        ):
            self.assertNotIn(secret, summary)
        self.assertIn("密码=[REDACTED]", summary)
        self.assertIn("身份证号=[REDACTED_ID]", summary)
        self.assertIn("银行卡号=[REDACTED_BANK_CARD]", summary)
        self.assertIn("账号=[REDACTED]", summary)
        self.assertIn("项目编号：13812345678", summary)
        self.assertIn("合同编号＝13912345678", summary)
        self.assertIn("客户编号:13712345678", summary)
        self.assertIn("[REDACTED_PHONE]", summary)

    def test_rolling_summary_sanitizer_redacts_probes_but_keeps_structure(self):
        sensitive = (
            "Database mysql+pymysql://root:dbpass@db.local:3306/risk?ssl=true; "
            "phone 13812345678; account=finance-admin; "
            "report https://api.example.test/risk?account=alice&token=abc; "
            "owner alice@example.com; Bearer bearer-token-value-123456; "
            "JWT eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "signature123456; secret=internal-value; "
            "X-API-Key: x-api-secret-value; "
            "X-Auth-Token: x-auth-token-value; "
            "Authorization: Basic dXNlcjpwYXNzd29yZA==; "
            "密码=pass-value; 口令=phrase-value; 密钥=key-value; "
            "身份证号=11010519491231002X; "
            "银行卡号=6222021234567890123; "
            "public https://example.test/risk/report; "
            "项目编号=12012345678; "
            "keep Project Alpha repayment review"
        )
        self.store.append_message(
            self.session.session_id,
            "user",
            sensitive,
            "sanitizer-probe",
            self.scope,
            project_id="1006",
        )
        self.store.append_message(
            self.session.session_id,
            "assistant",
            "Acknowledged",
            "sanitizer-ack",
            self.scope,
            project_id="1006",
        )
        self.store.append_message(
            self.session.session_id,
            "user",
            "Current question",
            "sanitizer-current",
            self.scope,
            project_id="1006",
        )

        context = self.orchestrator(
            summary_threshold=2,
            summary_keep_messages=1,
            summary_char_limit=1000,
        ).build_context(
            self.session.session_id,
            project_id="1006",
            scope=self.scope,
            query="Current question",
        )

        summary = context.summary.content
        for secret in (
            "root",
            "dbpass",
            "13812345678",
            "finance-admin",
            "account=alice",
            "token=abc",
            "alice@example.com",
            "bearer-token-value-123456",
            "eyJhbGciOiJIUzI1NiJ9",
            "internal-value",
            "x-api-secret-value",
            "x-auth-token-value",
            "dXNlcjpwYXNzd29yZA==",
            "pass-value",
            "phrase-value",
            "key-value",
            "11010519491231002X",
            "6222021234567890123",
            "https://example.test/risk/report",
        ):
            self.assertNotIn(secret, summary)
        self.assertIn("[REDACTED_DATABASE_URL]", summary)
        self.assertIn("[REDACTED_PHONE]", summary)
        self.assertIn("account=[REDACTED]", summary)
        self.assertIn("[REDACTED_URL]", summary)
        self.assertIn("[REDACTED_EMAIL]", summary)
        self.assertIn("Bearer [REDACTED]", summary)
        self.assertIn("[REDACTED_JWT]", summary)
        self.assertIn("secret=[REDACTED]", summary)
        self.assertIn("X-API-Key=[REDACTED]", summary)
        self.assertIn("X-Auth-Token=[REDACTED]", summary)
        self.assertIn("Authorization: Basic [REDACTED]", summary)
        self.assertIn("密码=[REDACTED]", summary)
        self.assertIn("口令=[REDACTED]", summary)
        self.assertIn("密钥=[REDACTED]", summary)
        self.assertIn("身份证号=[REDACTED_ID]", summary)
        self.assertIn("银行卡号=[REDACTED_BANK_CARD]", summary)
        self.assertIn("项目编号=12012345678", summary)
        self.assertIn("Project Alpha repayment review", summary)

    def test_exact_and_semantic_deduplicate_and_sort_by_authority(self):
        inference = self.store.save_memory(
            "1006", "refund risk inferred", "assistant_inference", self.scope, confidence=1.0
        )
        decision = self.store.save_memory(
            "1006", "refund risk decision", "decision", self.scope, confidence=0.8
        )
        fact = self.store.save_memory(
            "1006", "refund risk fact", "source_fact", self.scope, confidence=0.7
        )
        for item, vector in ((inference, [1, 0]), (decision, [0.9, 0.1]), (fact, [0.8, 0.2])):
            self.vector.upsert(
                item.memory_id,
                vector,
                self.orchestrator().vector_payload(item),
            )

        class FixedEmbedding:
            available = True
            reason = ""

            def embed(self, texts):
                return [[1, 0] for _ in texts]

        context = MemoryOrchestrator(
            self.store,
            FixedEmbedding(),
            self.vector,
            retrieval_limit=6,
        ).build_context(
            self.session.session_id,
            project_id="1006",
            scope=self.scope,
            query="refund risk",
        )

        ids = [item.memory_id for item in context.memories]
        self.assertEqual(ids, [fact.memory_id, decision.memory_id, inference.memory_id])
        self.assertEqual(len(ids), len(set(ids)))

    def test_same_authority_prefers_recency_before_confidence(self):
        with patch(
            "app.chat.storage.beijing_now_text",
            return_value="2026-08-07 12:00:00",
        ):
            older = self.store.save_memory(
                "1006",
                "priority marker older high confidence",
                "user_confirmed",
                self.scope,
                confidence=0.99,
            )
        with patch(
            "app.chat.storage.beijing_now_text",
            return_value="2026-08-07 13:00:00",
        ):
            newer = self.store.save_memory(
                "1006",
                "priority marker newer low confidence",
                "user_confirmed",
                self.scope,
                confidence=0.10,
            )

        context = self.orchestrator().build_context(
            self.session.session_id,
            project_id="1006",
            scope=self.scope,
            query="priority marker",
        )

        self.assertEqual(
            [item.memory_id for item in context.memories[:2]],
            [newer.memory_id, older.memory_id],
        )

    def test_equal_content_hash_keeps_only_the_higher_authority_memory(self):
        inference = self.store.save_memory(
            "1006", "same remembered condition", "assistant_inference", self.scope
        )
        confirmed = self.store.save_memory(
            "1006", "same remembered condition", "user_confirmed", self.scope
        )

        context = self.orchestrator().build_context(
            self.session.session_id,
            project_id="1006",
            scope=self.scope,
            query="remembered condition",
        )

        self.assertEqual([item.memory_id for item in context.memories], [confirmed.memory_id])
        self.assertNotEqual(inference.memory_id, confirmed.memory_id)

    def test_expired_superseded_and_cross_project_semantic_hits_are_excluded(self):
        old = self.store.save_memory("1006", "old risk", "source_fact", self.scope)
        replacement = self.store.supersede_memory(
            old.memory_id, "current risk", self.scope, project_id="1006"
        )
        expired = self.store.save_memory(
            "1006", "expired risk", "source_fact", self.scope,
            expires_at="2020-01-01 00:00:00",
        )
        other = self.store.save_memory("2001", "other risk", "source_fact", self.scope)
        for item in (old, replacement, expired, other):
            self.vector.upsert(item.memory_id, [1, 0], self.orchestrator().vector_payload(item))

        class FixedEmbedding:
            available = True
            reason = ""

            def embed(self, texts):
                return [[1, 0]]

        context = MemoryOrchestrator(
            self.store, FixedEmbedding(), self.vector, retrieval_limit=10
        ).build_context(
            self.session.session_id,
            project_id="1006",
            scope=self.scope,
            query="risk",
        )

        self.assertEqual([item.memory_id for item in context.memories], [replacement.memory_id])

    def test_embedding_and_vector_failures_degrade_without_blocking_exact_memory(self):
        exact = self.store.save_memory(
            "1006", "refund risk confirmed", "user_confirmed", self.scope
        )
        embedding_context = MemoryOrchestrator(
            self.store, FailingEmbeddingProvider(), self.vector
        ).build_context(
            self.session.session_id, project_id="1006", scope=self.scope, query="refund risk"
        )
        vector_context = MemoryOrchestrator(
            self.store, self.embedding, FailingVectorIndex()
        ).build_context(
            self.session.session_id, project_id="1006", scope=self.scope, query="refund risk"
        )

        self.assertTrue(embedding_context.retrieval_degraded)
        self.assertTrue(vector_context.retrieval_degraded)
        self.assertEqual(embedding_context.memories[0].memory_id, exact.memory_id)
        self.assertEqual(vector_context.memories[0].memory_id, exact.memory_id)

    def test_outbox_success_and_failure_preserve_authority(self):
        memory = self.store.save_memory(
            "1006", "confirmed local fact", "user_confirmed", self.scope
        )
        result = self.orchestrator().process_outbox_once("worker-a", limit=10)

        self.assertEqual(result.processed, 1)
        self.assertEqual(result.failed, 0)
        self.assertEqual(self.store.list_pending_outbox(), [])
        self.assertEqual(
            self.store.get_memory(memory.memory_id, self.scope, project_id="1006").canonical_text,
            "confirmed local fact",
        )

        failed_memory = self.store.save_memory(
            "1006", "still authoritative", "user_confirmed", self.scope
        )
        failing = MemoryOrchestrator(
            self.store, self.embedding, FailingVectorIndex()
        )
        failed_result = failing.process_outbox_once("worker-b", limit=10)

        self.assertEqual(failed_result.processed, 0)
        self.assertEqual(failed_result.failed, 1)
        pending = self.store.list_pending_outbox()
        self.assertEqual(pending[0]["memory_id"], failed_memory.memory_id)
        self.assertEqual(
            self.store.get_memory(failed_memory.memory_id, self.scope, project_id="1006").canonical_text,
            "still authoritative",
        )

    def test_outbox_failure_reaches_dead_without_deleting_authority(self):
        memory = self.store.save_memory(
            "1006", "dead index job authority", "user_confirmed", self.scope
        )
        result = MemoryOrchestrator(
            self.store, self.embedding, FailingVectorIndex()
        ).process_outbox_once("worker-dead", max_attempts=1)

        self.assertEqual((result.processed, result.failed), (0, 1))
        self.assertEqual(self.store.list_pending_outbox(), [])
        self.assertEqual(
            self.store.get_memory(memory.memory_id, self.scope, project_id="1006").canonical_text,
            "dead index job authority",
        )

    def test_outbox_lease_lost_does_not_mark_failed_or_stop_later_items(self):
        self.assertTrue(hasattr(chat_models, "OutboxLeaseLost"))
        first = self.store.save_memory(
            "1006", "lease lost first", "user_confirmed", self.scope
        )
        second = self.store.save_memory(
            "1006", "lease retained second", "user_confirmed", self.scope
        )

        class LeaseLosingRepository:
            def __init__(self, delegate):
                self.delegate = delegate
                self.first_outbox_id = None
                self.failed_ids = []

            def __getattr__(self, name):
                return getattr(self.delegate, name)

            def claim_outbox(self, worker_id, limit=100, lease_seconds=60, *, now=None):
                rows = self.delegate.claim_outbox(
                    worker_id, limit=limit, lease_seconds=lease_seconds, now=now
                )
                self.first_outbox_id = rows[0]["outbox_id"]
                return rows

            def mark_outbox_processed(self, outbox_id, worker_id):
                if outbox_id == self.first_outbox_id:
                    raise chat_models.OutboxLeaseLost("lease transferred")
                return self.delegate.mark_outbox_processed(outbox_id, worker_id)

            def mark_outbox_failed(self, outbox_id, worker_id, error_type, **kwargs):
                self.failed_ids.append(outbox_id)
                return self.delegate.mark_outbox_failed(
                    outbox_id, worker_id, error_type, **kwargs
                )

        repository = LeaseLosingRepository(self.store)
        result = MemoryOrchestrator(
            repository, self.embedding, self.vector
        ).process_outbox_once("worker-lease", limit=10)

        self.assertEqual((result.claimed, result.processed, result.failed), (2, 1, 1))
        self.assertNotIn(repository.first_outbox_id, repository.failed_ids)
        self.assertIn(second.memory_id, self.vector._records)
        self.assertIn(first.memory_id, self.vector._records)


class OptionalImportTests(unittest.TestCase):
    def test_memory_modules_import_without_optional_packages(self):
        imported = [
            importlib.import_module("app.chat.embedding"),
            importlib.import_module("app.chat.vector"),
            importlib.import_module("app.chat.memory"),
        ]

        self.assertEqual(len(imported), 3)


if __name__ == "__main__":
    unittest.main()
