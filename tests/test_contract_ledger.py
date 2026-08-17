"""Tests for the contract risk ledger, expiry look-ahead, and text wiring."""

from __future__ import annotations

import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from app.contracts.analysis import (
    TEXT_AMOUNT_PAIRING_SIGNAL,
    TEXT_ANALYZED_SIGNAL,
    TEXT_EXTRACTION_SIGNAL,
    TEXT_UNREADABLE_SIGNAL,
    ContractAnalysisService,
)
from app.contracts.ledger import (
    CSV_COLUMNS,
    LEDGER_VERSION,
    build_contract_ledger,
    ledger_to_csv,
    tier_for_score,
)
from app.contracts.models import ContractMetadata
from app.contracts.portfolio import build_contract_portfolio
from app.contracts.rules import (
    EXPIRY_LOOKAHEAD_TIERS,
    evaluate_contract_metadata,
)

TODAY = date(2026, 8, 12)


def _contract(**overrides) -> ContractMetadata:
    base = {
        "contract_id": "1",
        "project_id": "100",
        "contract_code": "ACME-2026-001",
        "contract_name": "不应出现的合同名称",
        "total_amount": "15360000",
        "status": "2",
        "contract_status": "1",
        "sign_date": "2026-01-01",
        "start_date": "2026-01-01",
        "end_date": "2027-01-01",
        "org_id": "org-1",
    }
    base.update(overrides)
    return ContractMetadata(**base)


class ExpiryLookaheadTests(unittest.TestCase):
    def _rules(self, end_date, execution="1"):
        contract = _contract(end_date=end_date.isoformat(), contract_status=execution)
        assessment = evaluate_contract_metadata("100", [contract], today=TODAY)
        return {f.rule: f for f in assessment.findings}

    def test_contract_expiring_within_30_days_is_flagged(self):
        fired = self._rules(TODAY + timedelta(days=10))

        self.assertIn("contract_expiring_soon", fired)
        self.assertEqual(fired["contract_expiring_soon"].severity, 65)
        self.assertIn("剩余 10 天", fired["contract_expiring_soon"].evidence)

    def test_severity_decreases_with_distance(self):
        near = self._rules(TODAY + timedelta(days=10))["contract_expiring_soon"]
        mid = self._rules(TODAY + timedelta(days=45))["contract_expiring_soon"]
        far = self._rules(TODAY + timedelta(days=80))["contract_expiring_soon"]

        self.assertGreater(near.severity, mid.severity)
        self.assertGreater(mid.severity, far.severity)

    def test_beyond_the_last_window_is_not_flagged(self):
        longest = max(days for days, _, _ in EXPIRY_LOOKAHEAD_TIERS)
        fired = self._rules(TODAY + timedelta(days=longest + 5))

        self.assertNotIn("contract_expiring_soon", fired)

    def test_already_expired_does_not_also_report_expiring_soon(self):
        # The two rules describe the same problem; emitting both double-counts.
        fired = self._rules(TODAY - timedelta(days=5))

        self.assertIn("contract_expired_not_closed", fired)
        self.assertNotIn("contract_expiring_soon", fired)

    def test_closed_contracts_are_not_chased_for_renewal(self):
        for closed in ("8", "9"):
            fired = self._rules(TODAY + timedelta(days=10), execution=closed)
            self.assertNotIn("contract_expiring_soon", fired, closed)

    def test_boundary_day_is_inclusive(self):
        exactly_30 = self._rules(TODAY + timedelta(days=30))
        self.assertEqual(exactly_30["contract_expiring_soon"].severity, 65)

    def test_expiring_today_is_flagged_not_treated_as_expired(self):
        fired = self._rules(TODAY)

        self.assertIn("contract_expiring_soon", fired)
        self.assertNotIn("contract_expired_not_closed", fired)


class LedgerTierTests(unittest.TestCase):
    def test_tiers_follow_the_shared_risk_thresholds(self):
        self.assertEqual(tier_for_score(90)[0], "act_now")
        self.assertEqual(tier_for_score(76)[0], "act_now")
        self.assertEqual(tier_for_score(60)[0], "plan")
        self.assertEqual(tier_for_score(51)[0], "plan")
        self.assertEqual(tier_for_score(30)[0], "monitor")
        self.assertEqual(tier_for_score(10)[0], "record")

    def test_contract_without_findings_is_record_only(self):
        portfolio = {
            "items": [
                {
                    "contract_ref": "code:A",
                    "link_status": "standalone",
                    "risk_score": 80,
                    "findings": [],
                }
            ]
        }
        ledger = build_contract_ledger(portfolio)

        self.assertEqual(ledger["by_tier"]["act_now"], 0)
        self.assertEqual(ledger["by_tier"]["record"], 1)


class LedgerAggregationTests(unittest.TestCase):
    def _portfolio(self):
        contracts = [
            (_contract(contract_id="1", contract_code="A", contract_status="1", status="0"), "standalone"),
            (_contract(contract_id="2", contract_code="B", total_amount="", org_id="org-2"), "orphaned"),
            (_contract(contract_id="3", contract_code="C", org_id="org-2"), "project_linked"),
        ]
        portfolio = build_contract_portfolio(contracts, today=TODAY)
        org_map = {
            "code:A": "org-1",
            "code:B": "org-2",
            "code:C": "org-2",
        }
        return portfolio, org_map

    def test_ledger_groups_by_organisation(self):
        portfolio, org_map = self._portfolio()
        ledger = build_contract_ledger(portfolio, contracts_by_ref=org_map)

        orgs = {bucket["org_id"]: bucket for bucket in ledger["by_organisation"]}
        self.assertEqual(ledger["organisation_count"], 2)
        self.assertEqual(orgs["org-2"]["contract_count"], 2)
        self.assertEqual(orgs["org-1"]["contract_count"], 1)

    def test_unreachable_contracts_are_counted_per_organisation(self):
        portfolio, org_map = self._portfolio()
        ledger = build_contract_ledger(portfolio, contracts_by_ref=org_map)
        orgs = {bucket["org_id"]: bucket for bucket in ledger["by_organisation"]}

        # A is standalone, B is orphaned; both are unreachable by project entry.
        self.assertEqual(orgs["org-1"]["unreachable_count"], 1)
        self.assertEqual(orgs["org-2"]["unreachable_count"], 1)

    def test_organisations_sort_most_urgent_first(self):
        portfolio, org_map = self._portfolio()
        ledger = build_contract_ledger(portfolio, contracts_by_ref=org_map)
        act_now = [bucket["act_now"] for bucket in ledger["by_organisation"]]

        self.assertEqual(act_now, sorted(act_now, reverse=True))

    def test_contract_with_no_org_anywhere_degrades_to_unknown(self):
        # The genuine fallback: neither the override map nor the item carries
        # an org_id. It must bucket rather than fail.
        portfolio = build_contract_portfolio(
            [(_contract(contract_code="Z", org_id=""), "standalone")], today=TODAY
        )
        ledger = build_contract_ledger(portfolio)

        self.assertEqual(ledger["organisation_count"], 1)
        self.assertEqual(ledger["by_organisation"][0]["org_id"], "unknown")

    def test_coverage_caveat_is_carried_through(self):
        portfolio, org_map = self._portfolio()
        ledger = build_contract_ledger(portfolio, contracts_by_ref=org_map)

        self.assertIn("project_entry_coverage", ledger)
        self.assertEqual(ledger["project_entry_coverage"]["total"], 3)
        self.assertEqual(ledger["ledger_version"], LEDGER_VERSION)

    def test_rows_sort_by_descending_risk(self):
        portfolio, org_map = self._portfolio()
        ledger = build_contract_ledger(portfolio, contracts_by_ref=org_map)
        scores = [row["risk_score"] for row in ledger["rows"]]

        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_ledger_carries_contract_name_for_display(self):
        # Unlocked 2026-08-12 by user decision: the table needs a readable
        # name. Findings still reference code:/id: only, pinned by
        # tests/test_contract_rules.py::SanitizationTests.
        portfolio, org_map = self._portfolio()
        ledger = build_contract_ledger(portfolio, contracts_by_ref=org_map)

        self.assertTrue(all("contract_name" in row for row in ledger["rows"]))
        self.assertIn("不应出现的合同名称", str(ledger["rows"]))

    def test_ledger_still_omits_counterparty_and_free_text(self):
        # Only the name was unlocked. sign_user, remark, purpose and
        # contract_performance are not read at all.
        portfolio, org_map = self._portfolio()
        ledger = build_contract_ledger(portfolio, contracts_by_ref=org_map)
        blob = str(ledger)

        for field in ("sign_user", "remark", "purpose", "contract_performance"):
            self.assertNotIn(field, blob)

    def test_findings_inside_the_ledger_never_carry_the_name(self):
        portfolio, org_map = self._portfolio()
        ledger = build_contract_ledger(portfolio, contracts_by_ref=org_map)
        finding_text = str([row.get("top_reason") for row in ledger["rows"]])

        self.assertNotIn("不应出现的合同名称", finding_text)

    def test_org_id_comes_from_the_item_when_no_map_is_supplied(self):
        portfolio, _ = self._portfolio()
        ledger = build_contract_ledger(portfolio)

        self.assertNotIn("unknown", {b["org_id"] for b in ledger["by_organisation"]})


class LedgerCsvTests(unittest.TestCase):
    def test_csv_has_a_header_and_one_row_per_contract(self):
        portfolio = build_contract_portfolio(
            [(_contract(contract_code="A"), "standalone")], today=TODAY
        )
        csv_text = ledger_to_csv(build_contract_ledger(portfolio))
        lines = [line for line in csv_text.splitlines() if line]

        self.assertEqual(lines[0], ",".join(CSV_COLUMNS))
        self.assertEqual(len(lines), 2)

    def test_csv_uses_crlf_for_excel(self):
        portfolio = build_contract_portfolio(
            [(_contract(contract_code="A"), "standalone")], today=TODAY
        )
        self.assertIn("\r\n", ledger_to_csv(build_contract_ledger(portfolio)))

    def test_csv_excludes_columns_outside_the_allowlist(self):
        portfolio = build_contract_portfolio(
            [(_contract(contract_code="A"), "standalone")], today=TODAY
        )
        ledger = build_contract_ledger(portfolio)
        ledger["rows"][0]["secret_field"] = "should not appear"

        self.assertNotIn("should not appear", ledger_to_csv(ledger))

    def test_empty_ledger_still_produces_a_header(self):
        csv_text = ledger_to_csv({"rows": []})

        self.assertEqual(csv_text.strip(), ",".join(CSV_COLUMNS))

    def test_every_csv_column_exists_on_a_row(self):
        """A column named here but absent from the row dict exports as blank.

        `DictWriter` is built with `extrasaction="ignore"`, which silently
        tolerates extra row keys - and would let a typo'd column name ship a
        column of empty cells with nobody noticing.
        """
        portfolio = build_contract_portfolio(
            [(_contract(contract_code="A"), "standalone")], today=TODAY
        )
        row = build_contract_ledger(portfolio)["rows"][0]

        self.assertEqual((), tuple(c for c in CSV_COLUMNS if c not in row))

    def test_ledger_publishes_its_column_order_for_the_browser(self):
        """The browser's filtered export reads these columns instead of
        keeping its own list. The two lists had already drifted by two columns,
        so the same button produced a different file shape depending on which
        export you pressed."""
        portfolio = build_contract_portfolio(
            [(_contract(contract_code="A"), "standalone")], today=TODAY
        )

        self.assertEqual(
            list(CSV_COLUMNS), build_contract_ledger(portfolio)["csv_columns"]
        )

    def test_the_browser_export_keeps_no_column_list_of_its_own(self):
        from tests.frontend_assets import contracts_js

        script = contracts_js()

        self.assertIn("contractState.meta?.csv_columns", script)
        for column in ("contract_ref", "top_reason"):
            self.assertNotIn(f'"{column}", "', script)


class _FakeStore:
    def __init__(self, contracts):
        self._contracts = contracts
        self.saved = {}

    def list_contracts(self, project_id):
        return self._contracts

    def list_contract_assets(self, project_id):
        return []

    def create_contract_analysis_job(self, **kwargs):
        return {"job_id": "job-1", **kwargs}

    def save_contract_risk_summary(self, **kwargs):
        self.saved = kwargs
        return {"summary_id": "sum-1", **kwargs}

    def update_contract_analysis_job(self, job_id, **kwargs):
        return None

    def get_contract_analysis_job(self, job_id):
        return {"job_id": job_id}


def _write_docx(directory: Path, name: str, paragraphs: list[str]) -> Path:
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    document = (
        '<?xml version="1.0"?><w:document '
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    path = directory / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)
    return path


CONTRACT_PARAGRAPHS = [
    "铁路火车轮采购合同",
    "甲方（买方）：某某公司 统一社会信用代码：91110000AAAAAAAA",
    "第一条 合同总价款：人民币壹仟伍佰叁拾陆万元整（¥15,360,000.00）。",
    "第二条 付款方式：20% 预付款、70% 进度款、10% 质保尾款。",
    "第七条 违约责任：逾期按万分之三支付违约金。",
    "第八条 不可抗力：地震、战争属于不可抗力。",
    "第九条 争议解决：适用中华人民共和国法律，提交仲裁。",
    "第十条 知识产权与保密：双方负有保密义务。",
    "第十一条 交货与质保：质保 24 个月，一方违约守约方有权解除合同。",
    "甲方（盖章）：某某公司 授权代表签字：张某 日期：2026-08-12",
]


class AnalysisTextWiringTests(unittest.TestCase):
    def _row(self, **overrides):
        row = {
            "contract_id": "1",
            "project_id": "100",
            "contract_code": "ACME-2026-001",
            "total_amount": "15360000",
            "status": "2",
            "contract_status": "1",
            "sign_date": "2026-01-01",
            "start_date": "2026-01-01",
            "end_date": "2027-06-01",
        }
        row.update(overrides)
        return row

    def test_without_a_document_provider_the_metadata_only_signal_stays(self):
        service = ContractAnalysisService(_FakeStore([self._row()]))

        result = service.create_analysis_job("100")

        self.assertIn(TEXT_EXTRACTION_SIGNAL, result["summary"]["signals"])
        self.assertNotIn(TEXT_ANALYZED_SIGNAL, result["summary"]["signals"])

    def test_analyzing_text_removes_the_not_analyzed_claim(self):
        with TemporaryDirectory() as tmp:
            path = _write_docx(Path(tmp), "c.docx", CONTRACT_PARAGRAPHS)
            service = ContractAnalysisService(
                _FakeStore([self._row()]), document_provider=lambda pid: [path]
            )
            result = service.create_analysis_job("100")

        signals = result["summary"]["signals"]
        self.assertIn(TEXT_ANALYZED_SIGNAL, signals)
        # Claiming both would make the result self-contradictory.
        self.assertNotIn(TEXT_EXTRACTION_SIGNAL, signals)
        self.assertEqual(result["job"]["job_id"], "job-1")

    def test_unreadable_document_is_reported_not_silently_skipped(self):
        with TemporaryDirectory() as tmp:
            bad = Path(tmp) / "scan.pdf"
            bad.write_bytes(b"not really a pdf")
            service = ContractAnalysisService(
                _FakeStore([self._row()]), document_provider=lambda pid: [bad]
            )
            result = service.create_analysis_job("100")

        self.assertIn(TEXT_UNREADABLE_SIGNAL, result["summary"]["signals"])
        self.assertIn(TEXT_EXTRACTION_SIGNAL, result["summary"]["signals"])

    def test_provider_failure_does_not_fail_the_whole_analysis(self):
        def boom(project_id):
            raise RuntimeError("provider exploded")

        service = ContractAnalysisService(
            _FakeStore([self._row()]), document_provider=boom
        )
        result = service.create_analysis_job("100")

        self.assertEqual(result["summary"]["risk_level"], "低")
        self.assertIn(TEXT_UNREADABLE_SIGNAL, result["summary"]["signals"])

    def test_amount_cross_check_is_skipped_when_pairing_is_ambiguous(self):
        with TemporaryDirectory() as tmp:
            path = _write_docx(Path(tmp), "c.docx", CONTRACT_PARAGRAPHS)
            store = _FakeStore([self._row(), self._row(contract_id="2", contract_code="B")])
            service = ContractAnalysisService(
                store, document_provider=lambda pid: [path]
            )
            result = service.create_analysis_job("100")

        self.assertIn(TEXT_AMOUNT_PAIRING_SIGNAL, result["summary"]["signals"])

    def test_text_findings_raise_the_headline_score(self):
        unsigned = [
            line.replace("授权代表签字：张某", "授权代表签字：________________")
            for line in CONTRACT_PARAGRAPHS
        ] + ["11.3 合同生效：双方授权代表签字加盖公章之日生效。"]
        with TemporaryDirectory() as tmp:
            path = _write_docx(Path(tmp), "c.docx", unsigned)
            # Metadata alone is clean, so any elevation comes from the text.
            service = ContractAnalysisService(
                _FakeStore([self._row()]), document_provider=lambda pid: [path]
            )
            result = service.create_analysis_job("100")

        self.assertGreaterEqual(result["summary"]["risk_score"], 90)
        self.assertEqual(result["summary"]["risk_level"], "严重")

    def test_text_findings_reference_the_document_not_its_path(self):
        with TemporaryDirectory() as tmp:
            unsigned = CONTRACT_PARAGRAPHS[:-1] + [
                "甲方（盖章）：________________ 授权代表签字：________________"
            ]
            path = _write_docx(Path(tmp), "contract.docx", unsigned)
            service = ContractAnalysisService(
                _FakeStore([self._row()]), document_provider=lambda pid: [path]
            )
            result = service.create_analysis_job("100")

        text_findings = [
            f for f in result["summary"]["findings"] if str(f["contract_ref"]).startswith("doc:")
        ]
        self.assertTrue(text_findings)
        self.assertEqual(text_findings[0]["contract_ref"], "doc:contract.docx")
        self.assertNotIn(tmp, str(result["summary"]["findings"]))

    def test_counts_separate_metadata_and_text_findings(self):
        with TemporaryDirectory() as tmp:
            path = _write_docx(Path(tmp), "c.docx", CONTRACT_PARAGRAPHS)
            service = ContractAnalysisService(
                _FakeStore([self._row()]), document_provider=lambda pid: [path]
            )
            result = service.create_analysis_job("100")

        counts = result["summary"]["asset_counts"]
        self.assertEqual(counts["documents_analyzed"], 1)
        self.assertIn("metadata_findings", counts)
        self.assertIn("text_findings", counts)


if __name__ == "__main__":
    unittest.main()
