import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.risk.llm import build_sanitized_prompt_payload, explain_with_fallback, normalize_model_name
from app.risk.models import DimensionScore, ProjectRiskResult, RiskHit
from app.risk.storage import RiskHistoryStore


class StorageAndLlmTests(unittest.TestCase):
    def test_history_store_persists_and_reads_latest_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "risk.db"
            store = RiskHistoryStore(db_path)
            result = ProjectRiskResult(
                project_id="PROJ-1",
                project_name="测试项目",
                score=68,
                level="高",
                dimensions=[DimensionScore(name="进度履约", score=80, weight=1, summary="延期")],
                hits=[RiskHit(dimension="进度履约", severity=80, reason="结束时间已早于当前日期", evidence="end_date=2026-08-01")],
                suggestions=["补充项目计划"],
                explanation="规则解释",
                rule_version="v1",
            )

            store.save_result(result)
            latest = store.get_latest("PROJ-1")
            history = store.list_history("PROJ-1")

            self.assertIsNotNone(latest)
            self.assertEqual(latest["level"], "高")
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["project_id"], "PROJ-1")

    def test_history_store_returns_latest_by_project_and_dashboard_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "risk.db"
            store = RiskHistoryStore(db_path)
            for project_id, project_name, score, level in [
                ("PROJ-1", "项目一", 20, "低"),
                ("PROJ-2", "项目二", 66, "高"),
                ("PROJ-1", "项目一", 83, "严重"),
            ]:
                store.save_result(
                    ProjectRiskResult(
                        project_id=project_id,
                        project_name=project_name,
                        score=score,
                        level=level,
                        dimensions=[DimensionScore(name="进度履约", score=score, weight=1, summary="测试")],
                        hits=[RiskHit(dimension="进度履约", severity=score, reason="测试命中", evidence="x=1")],
                        suggestions=["测试建议"],
                        explanation="测试解释",
                        rule_version="v1",
                    )
                )

            latest = store.latest_by_project(["PROJ-1", "PROJ-2", "PROJ-404"])
            summary = store.dashboard_summary()

            self.assertEqual(latest["PROJ-1"]["level"], "严重")
            self.assertEqual(latest["PROJ-2"]["score"], 66)
            self.assertNotIn("PROJ-404", latest)
            self.assertEqual(summary["total_evaluations"], 3)
            self.assertEqual(summary["level_counts"]["严重"], 1)
            self.assertEqual(summary["level_counts"]["高"], 1)
            self.assertEqual(summary["latest_project_count"], 2)
            self.assertEqual(summary["latest_level_counts"]["严重"], 1)

    def test_llm_payload_is_sanitized_and_fallback_explains_without_api_key(self):
        result = ProjectRiskResult(
            project_id="PROJ-1",
            project_name="包含敏感伙伴名的项目",
            score=42,
            level="中",
            dimensions=[DimensionScore(name="资料完整性", score=50, weight=1, summary="附件缺失")],
            hits=[RiskHit(dimension="资料完整性", severity=50, reason="暂无附件数据", evidence="attachment_count=0")],
            suggestions=["补充附件"],
            explanation="",
            rule_version="v1",
        )

        payload = build_sanitized_prompt_payload(result)
        explanation = explain_with_fallback(result, settings={"api_key": "", "base_url": "", "model": ""})

        self.assertNotIn("包含敏感伙伴名", str(payload))
        self.assertIn("资料完整性", str(payload))
        self.assertIn("中风险", explanation)

    def test_llm_audit_metadata_uses_hash_and_dimension_summary(self):
        result = ProjectRiskResult(
            project_id="PROJ-1",
            project_name="敏感项目名称",
            score=42,
            level="中",
            dimensions=[DimensionScore(name="资料完整性", score=50, weight=1, summary="附件缺失")],
            hits=[RiskHit(dimension="资料完整性", severity=50, reason="暂无附件数据", evidence="attachment_count=0")],
            suggestions=["补充附件"],
            explanation="",
            rule_version="v1",
        )

        from app.risk.llm import build_sanitized_prompt_metadata

        metadata = build_sanitized_prompt_metadata(result)

        self.assertEqual(len(metadata["sanitized_payload_hash"]), 64)
        self.assertGreater(metadata["sanitized_payload_chars"], 0)
        self.assertEqual(metadata["sanitized_dimension_summary"][0]["name"], "资料完整性")
        self.assertEqual(metadata["sanitized_dimension_summary"][0]["hit_count"], 1)
        self.assertNotIn("敏感项目名称", str(metadata))

    def test_explain_with_audit_marks_unconfigured_fallback(self):
        result = ProjectRiskResult(
            project_id="PROJ-1",
            project_name="测试项目",
            score=42,
            level="中",
            dimensions=[DimensionScore(name="资料完整性", score=50, weight=1, summary="附件缺失")],
            hits=[RiskHit(dimension="资料完整性", severity=50, reason="暂无附件数据", evidence="attachment_count=0")],
            suggestions=["补充附件"],
            explanation="",
            rule_version="v1",
        )

        from app.risk.llm import explain_with_audit

        explanation = explain_with_audit(result, settings={"api_key": "", "base_url": "", "model": ""})

        self.assertFalse(explanation.configured)
        self.assertFalse(explanation.attempted)
        self.assertTrue(explanation.fallback_used)
        self.assertIn("中风险", explanation.content)

    def test_explain_with_audit_falls_back_for_configured_provider_response_shape_errors(self):
        result = ProjectRiskResult(
            project_id="PROJ-1",
            project_name="Test Project",
            score=42,
            level="medium",
            dimensions=[DimensionScore(name="data completeness", score=50, weight=1, summary="missing attachment")],
            hits=[RiskHit(dimension="data completeness", severity=50, reason="no attachment data", evidence="attachment_count=0")],
            suggestions=["add attachment"],
            explanation="",
            rule_version="v1",
        )

        from app.risk.llm import explain_with_audit

        response_shape_errors = (
            IndexError("missing choice"),
            TypeError("message is not subscriptable"),
            AttributeError("content has no strip"),
            ValueError("malformed provider payload"),
        )
        settings = {
            "api_key": "configured-test-key",
            "base_url": "https://llm.invalid",
            "model": "deepseek",
        }

        for error in response_shape_errors:
            with self.subTest(error=type(error).__name__):
                with patch("app.risk.llm._call_openai_compatible", side_effect=error):
                    explanation = explain_with_audit(result, settings=settings)

                self.assertTrue(explanation.configured)
                self.assertTrue(explanation.attempted)
                self.assertTrue(explanation.fallback_used)
                self.assertEqual(explanation.model, "deepseek-v4-flash")
                self.assertIn("medium", explanation.content)

    def test_llm_normalizes_deepseek_short_model_name(self):
        self.assertEqual(normalize_model_name("deepseek"), "deepseek-v4-flash")
        self.assertEqual(normalize_model_name("deepseek-v4-pro"), "deepseek-v4-pro")


if __name__ == "__main__":
    unittest.main()
