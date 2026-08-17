"""Tests for adaptive clause splitting and grounded-citation verification.

The splitter tests exist because the first design was wrong in a way tests
would not have caught: it hardcoded `第X条`, which appears zero times in real
documents. Each marker family therefore gets an explicit test, and the
fallback must announce itself.
"""

from __future__ import annotations

import unittest

from app.contracts.chat_verify import (
    SIGNAL_UNVERIFIED_AMOUNTS,
    SIGNAL_UNVERIFIED_REFS,
    annotate_answer,
    verify_answer,
)
from app.contracts.clause_split import (
    MIN_CHUNK_CHARS,
    SIGNAL_FALLBACK,
    STRATEGY_PARAGRAPH,
    choose_strategy,
    count_markers,
    split_clauses,
)

FILLER = "本条约定双方权利义务，包括交付、验收、付款与违约处理等具体安排。" * 4


def _document(marker_fn, count=6):
    return "\n\n".join(f"{marker_fn(i)} {FILLER}" for i in range(1, count + 1))


class MarkerDetectionTests(unittest.TestCase):
    def test_article_markers_are_detected(self):
        cn = "一二三四五六七八九十"
        text = _document(lambda i: f"第{cn[i - 1]}条")
        self.assertEqual(split_clauses(text).strategy, "article")

    def test_chinese_ordinal_markers_are_detected(self):
        # The family that actually dominates real documents: 495 occurrences in
        # the largest attachment, and zero `第X条` anywhere.
        cn = "一二三四五六七八九十"
        text = _document(lambda i: f"{cn[i - 1]}、")
        self.assertEqual(split_clauses(text).strategy, "cn_ordinal")

    def test_decimal_markers_are_detected(self):
        text = _document(lambda i: f"1.{i}")
        self.assertEqual(split_clauses(text).strategy, "decimal")

    def test_article_wins_even_when_another_family_is_more_common(self):
        # `第X条` is unambiguous; a bare `1.` is often just a list item.
        cn = "一二三四五六七八九十"
        text = _document(lambda i: f"第{cn[i - 1]}条", count=3)
        text += "\n\n" + _document(lambda i: f"{i}. ", count=9)
        self.assertEqual(split_clauses(text).strategy, "article")

    def test_two_occurrences_are_not_enough_to_believe_a_family(self):
        counts = {"article": 2, "cn_ordinal": 2, "decimal": 0, "digit": 0, "chapter": 0}
        self.assertEqual(choose_strategy(counts), STRATEGY_PARAGRAPH)

    def test_unmarked_text_falls_back_and_says_so(self):
        text = "\n\n".join(FILLER for _ in range(4))
        result = split_clauses(text)

        self.assertEqual(result.strategy, STRATEGY_PARAGRAPH)
        self.assertIn(SIGNAL_FALLBACK, result.signals)

    def test_marker_counts_are_reported_for_inspection(self):
        cn = "一二三四五六七八九十"
        counts = count_markers(_document(lambda i: f"{cn[i - 1]}、"))

        self.assertGreaterEqual(counts["cn_ordinal"], 6)
        self.assertEqual(counts["article"], 0)


class ChunkQualityTests(unittest.TestCase):
    def test_fragments_are_merged_rather_than_emitted(self):
        # The real 763KB document has 495 markers across 935 lines. Splitting
        # naively yields 495 fragments; merging is what makes them usable.
        cn = "一二三四五六七八九十"
        text = "\n".join(f"{cn[i % 10]}、短句{i}" for i in range(400))
        result = split_clauses(text)

        self.assertLess(len(result.clauses), 400)
        for clause in result.clauses[:-1]:
            self.assertGreaterEqual(len(clause.text), MIN_CHUNK_CHARS)

    def test_trailing_fragment_is_appended_not_dropped(self):
        cn = "一二三四五六七八九十"
        text = _document(lambda i: f"{cn[i - 1]}、", count=4) + "\n\n尾部短句"
        result = split_clauses(text)

        self.assertIn("尾部短句", result.clauses[-1].text)

    def test_preamble_before_the_first_marker_is_kept(self):
        # The title and party block sit before clause one; dropping them loses
        # the contract's own identification.
        cn = "一二三四五六七八九十"
        text = "铁路采购合同 合同编号 ACME-C2026011\n\n" + _document(lambda i: f"{cn[i - 1]}、", count=4)
        result = split_clauses(text)

        self.assertIn("ACME-C2026011", result.clauses[0].text)

    def test_oversized_block_is_capped(self):
        from app.contracts.clause_split import MAX_CHUNK_CHARS

        result = split_clauses("单块超长内容。" * 3000)

        for clause in result.clauses:
            self.assertLessEqual(len(clause.text), MAX_CHUNK_CHARS)

    def test_empty_input_is_safe(self):
        for text in ("", "   ", "\n\n"):
            self.assertEqual(split_clauses(text).clauses, [])

    def test_metadata_does_not_serialise_clause_bodies(self):
        cn = "一二三四五六七八九十"
        payload = split_clauses(_document(lambda i: f"{cn[i - 1]}、")).to_dict()

        self.assertNotIn("本条约定双方权利义务", str(payload))
        self.assertIn("clause_count", payload)


PAYLOAD = {
    "contract_findings": [
        {"contract_ref": "code:ACME-C2026011", "total_amount": "15360000.00",
         "evidence": "end_date=2026-03-06"},
    ],
    "clauses": [
        {"contract_ref": "code:ACME-C2026011", "heading": "第七条 违约责任",
         "text": "第七条 逾期付款每日按万分之三支付违约金，合同总额 ¥15,360,000.00。"},
    ],
}


class GroundedVerificationTests(unittest.TestCase):
    def test_a_clean_answer_produces_no_findings(self):
        result = verify_answer(
            "合同 ACME-C2026011 的第七条约定了违约金，总额 ¥15,360,000.00。", PAYLOAD
        )

        self.assertFalse(result.has_hard_failures)
        self.assertEqual(result.unmatched_amounts, [])

    def test_an_invented_contract_code_is_a_hard_failure(self):
        result = verify_answer("合同 ACME-C2099999 存在违约风险。", PAYLOAD)

        self.assertIn("ACME-C2099999", result.unverified_codes)
        self.assertTrue(result.has_hard_failures)
        self.assertIn(SIGNAL_UNVERIFIED_REFS, result.signals)

    def test_an_invented_clause_number_is_a_hard_failure(self):
        result = verify_answer("根据第二十九条，乙方须赔偿。", PAYLOAD)

        self.assertIn("第二十九条", result.unverified_clauses)
        self.assertTrue(result.has_hard_failures)

    def test_hyphen_loss_is_not_treated_as_fabrication(self):
        # Extraction drops hyphens, so the same contract must not be reported
        # as invented merely because it is written without them.
        result = verify_answer("合同 ACMEC2026011 的条款…", PAYLOAD)

        self.assertEqual(result.unverified_codes, [])

    def test_a_novel_amount_is_only_a_soft_finding(self):
        # A cross-contract sum is legitimate; flagging it hard would cry wolf.
        result = verify_answer("三份合同合计 ¥45,000,000.00。", PAYLOAD)

        self.assertFalse(result.has_hard_failures)
        self.assertTrue(result.unmatched_amounts)
        self.assertIn(SIGNAL_UNVERIFIED_AMOUNTS, result.signals)

    def test_amount_matching_tolerates_unit_notation(self):
        result = verify_answer("合同总额 1536 万元。", PAYLOAD)

        self.assertEqual(result.unmatched_amounts, [])

    def test_annotation_preserves_the_answer_body(self):
        answer = "合同 ACME-C2099999 存在风险。"
        annotated = annotate_answer(answer, verify_answer(answer, PAYLOAD))

        self.assertTrue(annotated.startswith(answer))
        self.assertIn("无法核实的引用", annotated)
        self.assertIn("ACME-C2099999", annotated)

    def test_a_clean_answer_is_returned_unchanged(self):
        answer = "合同 ACME-C2026011 的第七条约定了违约金。"
        self.assertEqual(annotate_answer(answer, verify_answer(answer, PAYLOAD)), answer)

    def test_empty_answer_is_safe(self):
        self.assertFalse(verify_answer("", PAYLOAD).has_hard_failures)

    def test_verification_counts_reach_the_stats_payload(self):
        stats = verify_answer("合同 ACME-C2099999 第二十九条。", PAYLOAD).to_dict()

        self.assertEqual(stats["unverified_refs"], 2)


class ScanDensityTests(unittest.TestCase):
    def test_sparse_pdf_text_is_flagged(self):
        # The real 4.68MB attachment: 42 pages, 3,393 chars, ~80 per page. The
        # 50-char floor passes it, so density is what catches it.
        from app.contracts.text_extraction import (
            MIN_CHARS_PER_PAGE,
            SIGNAL_SPARSE_TEXT_LAYER,
            ExtractedDocument,
        )

        doc = ExtractedDocument(source_name="scan.pdf", suffix=".pdf")
        doc.text = "x" * 3393
        doc.char_count = 3393
        doc.page_count = 42

        self.assertTrue(doc.usable)
        self.assertLess(doc.chars_per_page, MIN_CHARS_PER_PAGE)
        self.assertTrue(SIGNAL_SPARSE_TEXT_LAYER)

    def test_dense_pdf_is_not_flagged(self):
        from app.contracts.text_extraction import MIN_CHARS_PER_PAGE, ExtractedDocument

        doc = ExtractedDocument(source_name="real.pdf", suffix=".pdf")
        doc.char_count = 20000
        doc.page_count = 10

        self.assertGreater(doc.chars_per_page, MIN_CHARS_PER_PAGE)

    def test_docx_has_no_page_density(self):
        from app.contracts.text_extraction import ExtractedDocument

        doc = ExtractedDocument(source_name="a.docx", suffix=".docx")
        doc.char_count = 5000

        self.assertEqual(doc.chars_per_page, 0.0)


if __name__ == "__main__":
    unittest.main()
