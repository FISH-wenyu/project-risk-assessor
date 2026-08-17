"""Tests for local contract text extraction, money parsing and text rules.

Self-contained: every fixture is synthetic text or a docx built in-memory. No
test reads the sample contracts under `data/`, which are git-ignored, and no
test touches MySQL or the network.

The regression tests in `PaymentScheduleRegressionTests` pin defects that were
found by running the rules against two real 2026-08-12 sample documents. They
are the most valuable tests here: each one failed before its fix.
"""

from __future__ import annotations

import io
import unittest
import zipfile
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from app.contracts.text_extraction import (
    MIN_USABLE_CHARS,
    SIGNAL_EXTRACTION_FAILED,
    SIGNAL_FILE_TOO_LARGE,
    SIGNAL_NO_TEXT_LAYER,
    SIGNAL_UNSUPPORTED_FORMAT,
    extract_document,
)
from app.contracts.text_money import (
    find_amounts_in_yuan,
    find_chinese_amounts_in_yuan,
    find_percentages,
    parse_amount_to_yuan,
    parse_chinese_amount,
)
from app.contracts.text_rules import (
    SIGNAL_NOT_A_CONTRACT_BODY,
    SIGNAL_TEXT_UNUSABLE,
    TEXT_RULE_VERSION,
    _payment_tranches,
    analyze_contract_text,
    looks_like_contract,
    normalize_identifier,
)

# A well-formed contract: signed, identified, complete clauses, consistent
# amounts. Rules must stay quiet on this one.
CLEAN_CONTRACT = """
铁路火车轮采购合同
合同编号：BRILW20260812 签订日期：20260812
甲方（买方）：某某国铁路建设发展有限公司 统一社会信用代码：91110000AAAAAAAA
乙方（卖方）：某某轨道交通车轮制造有限公司 统一社会信用代码：91120000BBBBBBBB
第一条 采购标的、数量、金额
1.3 合同总价款（含税）：人民币壹仟伍佰叁拾陆万元整（¥15,360,000.00）；税率 13% 增值税。
第二条 付款方式
2.1 甲方支付合同总金额 20% 预付款：¥3,072,000.00。
2.2 货物集港后，甲方支付合同总金额 70% 进度款：¥10,752,000.00。
2.3 验收合格后支付剩余 10% 质保尾款：¥1,536,000.00。
第三条 交货与风险转移
3.1 生产周期 120 日历天；贸易术语 FOB 天津港。
第四条 质量标准与质保期
4.2 质保期 24 个月。
第五条 保密
5.1 双方对本合同内容负有保密义务。
第七条 违约责任
7.1 逾期付款每日按万分之三支付违约金。
第八条 不可抗力
8.1 战争、地震、港口罢工属于不可抗力。
第九条 法律适用及争议解决
9.1 本合同适用中华人民共和国法律。
9.2 争议提交 CIETAC 仲裁。
第十条 知识产权
10.1 乙方保证不侵犯第三方知识产权。
第十一条 其他
11.1 一方严重违约，守约方有权解除合同。
甲方（盖章）：北京某某有限公司 授权代表签字：张某 日期：2026-08-12
乙方（盖章）：天津某某有限公司 授权代表签字：李某 日期：2026-08-12
"""


class ChineseMoneyTests(unittest.TestCase):
    def test_capital_numerals_convert_to_yuan(self):
        self.assertEqual(parse_chinese_amount("壹仟伍佰叁拾陆万"), Decimal(15360000))
        self.assertEqual(parse_chinese_amount("叁佰零柒万贰仟"), Decimal(3072000))
        self.assertEqual(parse_chinese_amount("壹亿贰仟万"), Decimal(120000000))
        self.assertEqual(parse_chinese_amount("拾贰"), Decimal(12))

    def test_wan_and_yi_suffixes_scale_to_yuan(self):
        # The 2026-08-12 project file writes `人民币 1536 万元整` while the
        # database column is plain yuan. Missing this is a 10,000x error.
        self.assertEqual(parse_amount_to_yuan("1536万元"), Decimal(15360000))
        self.assertEqual(parse_amount_to_yuan("人民币 1536 万元整"), Decimal(15360000))
        self.assertEqual(parse_amount_to_yuan("1.5亿元"), Decimal(150000000))
        self.assertEqual(parse_amount_to_yuan("¥15,360,000.00"), Decimal("15360000.00"))
        self.assertEqual(parse_amount_to_yuan("12800 元"), Decimal(12800))

    def test_garbage_returns_none_instead_of_guessing(self):
        for raw in ("", "abc", "第一条", "TB/T"):
            self.assertIsNone(parse_chinese_amount(raw), raw)

    def test_currency_guard_rejects_dates_codes_and_quantities(self):
        # Measured false positives from the real sample: a standard number, a
        # signing date and a quantity all parse as numbers but are not money.
        text = "执行标准 TB/T 28172020，签订日期 20260812，数量 1200 件，总价 ¥15,360,000.00"
        amounts = find_amounts_in_yuan(text)

        self.assertEqual(amounts, [Decimal("15360000.00")])
        self.assertIn(Decimal(28172020), find_amounts_in_yuan(text, require_currency=False))

    def test_clause_numbers_are_not_amounts(self):
        text = "7.1 甲方逾期付款 7.2 乙方逾期交货 11.3 合同生效"
        self.assertEqual(find_amounts_in_yuan(text), [])

    def test_finds_capital_amounts_and_percentages(self):
        self.assertEqual(
            find_chinese_amounts_in_yuan("总价款：人民币壹仟伍佰叁拾陆万元整"),
            [Decimal(15360000)],
        )
        self.assertEqual(find_percentages("20% 预付，70% 进度"), [Decimal(20), Decimal(70)])


class TextExtractionTests(unittest.TestCase):
    def _write_docx(self, directory: Path, paragraphs: list[str]) -> Path:
        body = "".join(
            f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs
        )
        document = (
            '<?xml version="1.0"?><w:document '
            'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{body}</w:body></w:document>"
        )
        path = directory / "sample.docx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", document)
        return path

    def test_docx_is_read_with_the_standard_library(self):
        with TemporaryDirectory() as tmp:
            path = self._write_docx(
                Path(tmp),
                [
                    "第一条 采购标的、数量、金额",
                    "1.3 合同总价款（含税）：人民币壹仟伍佰叁拾陆万元整（¥15,360,000.00）。",
                    "第二条 付款方式：甲方支付合同总金额 20% 预付款。",
                ],
            )
            result = extract_document(path)

        self.assertTrue(result.usable)
        self.assertIn("第一条 采购标的", result.text)
        # Paragraph breaks must survive, or clause-boundary rules misfire.
        self.assertIn("\n", result.text)
        self.assertEqual(result.signals, [])

    def test_unsupported_format_is_reported_not_silently_empty(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "contract.txt"
            path.write_text("hello", encoding="utf-8")
            result = extract_document(path)

        self.assertFalse(result.usable)
        self.assertIn(SIGNAL_UNSUPPORTED_FORMAT, result.signals)
        self.assertTrue(result.error)

    def test_missing_file_is_reported(self):
        result = extract_document(Path("no-such-directory") / "missing.docx")

        self.assertFalse(result.usable)
        self.assertIn(SIGNAL_EXTRACTION_FAILED, result.signals)

    def test_oversized_file_is_refused(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.docx"
            path.write_bytes(b"x")
            import app.contracts.text_extraction as extraction

            original = extraction.MAX_FILE_BYTES
            extraction.MAX_FILE_BYTES = 0
            try:
                result = extract_document(path)
            finally:
                extraction.MAX_FILE_BYTES = original

        self.assertIn(SIGNAL_FILE_TOO_LARGE, result.signals)

    def test_empty_docx_is_reported_as_empty_not_as_needing_ocr(self):
        # Measured on real source data: four contract attachments are the same
        # 10,127-byte .docx holding one character. OCR would not help, so
        # saying "needs OCR" would send the operator down a pointless path.
        from app.contracts.text_extraction import SIGNAL_DOCUMENT_EMPTY

        with TemporaryDirectory() as tmp:
            path = self._write_docx(Path(tmp), ["q"])
            result = extract_document(path)

        self.assertLess(result.char_count, MIN_USABLE_CHARS)
        self.assertFalse(result.usable)
        self.assertIn(SIGNAL_DOCUMENT_EMPTY, result.signals)
        self.assertNotIn(SIGNAL_NO_TEXT_LAYER, result.signals)

    def test_serialised_result_never_carries_body_text(self):
        with TemporaryDirectory() as tmp:
            path = self._write_docx(
                Path(tmp), ["甲方：机密公司名称", "1.3 合同总价款：¥15,360,000.00"]
            )
            payload = extract_document(path).to_dict()

        self.assertNotIn("text", payload)
        self.assertNotIn("机密公司名称", str(payload))


class ContractShapeTests(unittest.TestCase):
    def test_a_contract_body_is_recognised(self):
        self.assertTrue(looks_like_contract(CLEAN_CONTRACT))

    def test_a_project_file_mentioning_contracts_is_not_a_contract(self):
        # Regression: the real project file says "双方签署采购合同原件" as a
        # deliverable, which wrongly qualified it as a contract body.
        project_file = """
        一带一路跨境铁路-火车轮供应项目文件
        1. 项目概况
        9. 项目交付物清单
        1）双方签署采购合同原件； 2）供货明细、技术规格附件；
        """
        self.assertFalse(looks_like_contract(project_file))

    def test_clause_rules_are_skipped_for_non_contracts(self):
        result = analyze_contract_text("项目概况：本项目为铁路供应项目。付款节点：20% 预付款。")

        self.assertIn(SIGNAL_NOT_A_CONTRACT_BODY, result.signals)
        self.assertEqual(
            [f for f in result.findings if f.rule.startswith("missing_")], []
        )


class PaymentScheduleRegressionTests(unittest.TestCase):
    """Each test here pins a false positive measured on a real sample."""

    def test_penalty_percentages_are_not_payment_tranches(self):
        # Before the fix, `8% 违约金` twice was added to 20+70+10, giving 116%.
        text = (
            "2.1 甲方支付合同总金额 20% 预付款。2.2 甲方支付合同总金额 70% 进度款。"
            "2.3 支付剩余 10% 质保尾款。"
            "7.1 逾期超过 45 日，甲方承担合同总金额 8% 违约金。"
            "7.2 逾期超过 45 日，乙方赔付合同总金额 8% 违约金。"
        )
        total = sum(share for share, _ in _payment_tranches(text))

        self.assertEqual(total, Decimal(100))

    def test_advance_is_the_advance_tranche_not_the_largest(self):
        # Before the fix, max() over the line returned the 70% progress payment
        # and reported a 20% advance as dangerously high.
        text = (
            "2.1 甲方支付合同总金额 20% 预付款：¥3,072,000.00；"
            "2.2 甲方支付合同总金额 70% 进度款：¥10,752,000.00。"
        )
        result = analyze_contract_text(text)

        self.assertNotIn("high_advance_payment", {f.rule for f in result.findings})

    def test_a_tax_rate_next_to_a_tranche_does_not_drop_the_tranche(self):
        # Before the fix the exclusion window was wide enough that `税种：13%`
        # disqualified the neighbouring `10% 到货验收质保尾款`, giving 90%.
        text = "付款节点：20% 预付款、70% 集港进度款、10% 到货验收质保尾款 税种：13% 国内增值税"
        shares = [share for share, _ in _payment_tranches(text)]

        self.assertEqual(sorted(shares), [Decimal(10), Decimal(20), Decimal(70)])

    def test_payment_split_across_lines_is_still_read(self):
        # The PDF wraps one payment sentence across lines.
        text = "付款节点：20% 预付款、70% 集港进度\n款、10% 到货验收质保尾款"
        total = sum(share for share, _ in _payment_tranches(text))

        self.assertEqual(total, Decimal(100))


class ContractTextRuleTests(unittest.TestCase):
    def _rules(self, text, **kwargs):
        return {f.rule for f in analyze_contract_text(text, **kwargs).findings}

    def test_clean_contract_raises_no_structural_findings(self):
        fired = self._rules(CLEAN_CONTRACT, metadata_total_yuan=Decimal(15360000))

        self.assertNotIn("contract_not_executed", fired)
        self.assertNotIn("party_identity_incomplete", fired)
        self.assertNotIn("capital_amount_mismatch", fired)
        self.assertNotIn("payment_share_not_100", fired)
        self.assertNotIn("high_advance_payment", fired)
        self.assertNotIn("text_total_differs_from_metadata", fired)
        self.assertEqual([f for f in fired if f.startswith("missing_")], [])

    def test_blank_signature_block_with_effectiveness_clause_is_severe(self):
        text = CLEAN_CONTRACT.replace(
            "甲方（盖章）：北京某某有限公司 授权代表签字：张某 日期：2026-08-12",
            "甲方（盖章）：________________ 授权代表签字：________________",
        ).replace(
            "第十一条 其他",
            "第十一条 其他\n11.3 合同生效：双方授权代表签字加盖公章之日生效。",
        )
        result = analyze_contract_text(text)
        finding = next(f for f in result.findings if f.rule == "contract_not_executed")

        self.assertEqual(finding.severity, 90)

    def test_blank_party_identity_is_flagged_without_leaking_the_line(self):
        text = CLEAN_CONTRACT.replace(
            "统一社会信用代码：91110000AAAAAAAA", "统一社会信用代码：________________"
        )
        result = analyze_contract_text(text)
        finding = next(
            f for f in result.findings if f.rule == "party_identity_incomplete"
        )

        self.assertIn("统一社会信用代码", finding.evidence)
        # Field labels only; the surrounding line holds the party name.
        self.assertNotIn("某某国铁路建设发展有限公司", finding.evidence)

    def test_capital_and_arabic_amount_disagreement_is_caught(self):
        text = CLEAN_CONTRACT.replace("壹仟伍佰叁拾陆万元整", "贰仟万元整")
        fired = self._rules(text)

        self.assertIn("capital_amount_mismatch", fired)

    def test_metadata_total_absent_from_text_is_caught(self):
        fired = self._rules(CLEAN_CONTRACT, metadata_total_yuan=Decimal(9999999))

        self.assertIn("text_total_differs_from_metadata", fired)

    def test_missing_clauses_are_detected_individually(self):
        stripped = CLEAN_CONTRACT.replace("第八条 不可抗力", "").replace(
            "8.1 战争、地震、港口罢工属于不可抗力。", ""
        )
        fired = self._rules(stripped)

        self.assertIn("missing_force_majeure", fired)
        self.assertNotIn("missing_liability_clause", fired)

    def test_high_advance_payment_is_flagged(self):
        text = CLEAN_CONTRACT.replace("20% 预付款", "60% 预付款")
        fired = self._rules(text)

        self.assertIn("high_advance_payment", fired)

    def test_unusable_text_reports_a_signal_and_scores_nothing(self):
        result = analyze_contract_text("", metadata_total_yuan=Decimal(100), text_usable=False)

        self.assertEqual(result.findings, [])
        self.assertIn(SIGNAL_TEXT_UNUSABLE, result.signals)

    def test_findings_are_sorted_by_descending_severity(self):
        text = CLEAN_CONTRACT.replace("统一社会信用代码：91110000AAAAAAAA", "统一社会信用代码：____________")
        text = text.replace("第五条 保密", "").replace("5.1 双方对本合同内容负有保密义务。", "")
        severities = [f.severity for f in analyze_contract_text(text).findings]

        self.assertEqual(severities, sorted(severities, reverse=True))

    def test_result_carries_a_rule_version(self):
        self.assertEqual(analyze_contract_text(CLEAN_CONTRACT).rule_version, TEXT_RULE_VERSION)

    def test_evidence_is_bounded(self):
        from app.contracts.text_rules import MAX_EVIDENCE_CHARS

        for finding in analyze_contract_text(CLEAN_CONTRACT).findings:
            self.assertLessEqual(len(finding.evidence), MAX_EVIDENCE_CHARS + 1)


class IdentifierNormalisationTests(unittest.TestCase):
    def test_extraction_artefacts_do_not_read_as_mismatches(self):
        # DOCX extraction dropped the hyphen in both of these on 2026-08-12.
        pairs = (
            ("TB/T 2817-2020", "TB/T 28172020"),
            ("BRI-LW-20260812", "BRILW20260812"),
            ("ACME-2026-001", "ACME 2026 001"),
        )
        for left, right in pairs:
            self.assertEqual(
                normalize_identifier(left), normalize_identifier(right), f"{left} vs {right}"
            )

    def test_genuinely_different_identifiers_stay_different(self):
        self.assertNotEqual(
            normalize_identifier("BRILW20260812"), normalize_identifier("BRILW20260813")
        )


if __name__ == "__main__":
    unittest.main()
