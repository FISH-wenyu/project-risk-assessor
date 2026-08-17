from __future__ import annotations

import unittest

from app.contracts.models import ContractMetadata
from app.contracts.rules import (
    approval_status_label,
    build_contract_summary_text,
    evaluate_contract_metadata,
    execution_status_label,
)

TODAY = "2026-08-11"


def contract(**overrides) -> ContractMetadata:
    base = dict(
        contract_id="1",
        project_id="189",
        contract_code="C-001",
        contract_name="不应出现的合同名",
        total_amount="100000.00",
        status="2",
        contract_status="1",
        sign_date="2026-01-01",
        start_date="2026-01-01",
        end_date="2026-12-31",
    )
    base.update(overrides)
    return ContractMetadata(**base)


def rules_for(*contracts: ContractMetadata) -> set[str]:
    assessment = evaluate_contract_metadata("189", list(contracts), today=TODAY)
    return {finding.rule for finding in assessment.findings}


class StatusLabelTests(unittest.TestCase):
    """`status` is the approval axis, `contract_status` the execution axis."""

    def test_approval_labels(self):
        self.assertEqual(approval_status_label("0"), "待审核")
        self.assertEqual(approval_status_label("2"), "审核通过")
        self.assertEqual(approval_status_label("3"), "审核驳回")

    def test_execution_labels(self):
        self.assertEqual(execution_status_label("0"), "未开始")
        self.assertEqual(execution_status_label("1"), "进行中")
        self.assertEqual(execution_status_label("4"), "草稿")
        self.assertEqual(execution_status_label("9"), "终止")

    def test_unknown_code_falls_back_to_raw_value(self):
        self.assertEqual(approval_status_label("77"), "77")
        self.assertEqual(execution_status_label(""), "")

    def test_integer_like_values_from_sqlite_are_normalized(self):
        self.assertEqual(execution_status_label(1), "进行中")
        self.assertEqual(execution_status_label("1.0"), "进行中")


class ContractRuleTests(unittest.TestCase):
    def test_clean_contract_produces_no_findings(self):
        self.assertEqual(rules_for(contract()), set())

    def test_expired_contract_still_open_is_flagged(self):
        self.assertIn(
            "contract_expired_not_closed",
            rules_for(contract(end_date="2026-01-31", contract_status="1")),
        )

    def test_expired_contract_already_finished_is_not_flagged(self):
        self.assertNotIn(
            "contract_expired_not_closed",
            rules_for(contract(end_date="2026-01-31", contract_status="8")),
        )

    def test_executing_without_approval_is_flagged(self):
        self.assertIn(
            "executing_without_approval",
            rules_for(contract(status="1", contract_status="1")),
        )

    def test_executing_with_approval_is_not_flagged(self):
        self.assertNotIn(
            "executing_without_approval",
            rules_for(contract(status="2", contract_status="1")),
        )

    def test_rejected_but_not_closed_is_flagged(self):
        self.assertIn(
            "rejected_but_not_closed",
            rules_for(contract(status="3", contract_status="0")),
        )

    def test_rejected_and_terminated_is_not_flagged(self):
        self.assertNotIn(
            "rejected_but_not_closed",
            rules_for(contract(status="3", contract_status="9")),
        )

    def test_inverted_date_range_is_flagged(self):
        self.assertIn(
            "date_range_inverted",
            rules_for(contract(start_date="2026-06-01", end_date="2026-03-01")),
        )

    def test_missing_amount_is_flagged_for_blank_and_zero(self):
        self.assertIn("missing_total_amount", rules_for(contract(total_amount="")))
        self.assertIn("missing_total_amount", rules_for(contract(total_amount="0.00")))

    def test_present_amount_is_not_flagged(self):
        self.assertNotIn(
            "missing_total_amount", rules_for(contract(total_amount="12.34"))
        )

    def test_missing_sign_and_end_dates_are_flagged(self):
        found = rules_for(contract(sign_date="", end_date=""))
        self.assertIn("missing_sign_date", found)
        self.assertIn("missing_end_date", found)

    def test_draft_contract_is_flagged(self):
        self.assertIn("still_draft", rules_for(contract(contract_status="4")))


class AssessmentAggregationTests(unittest.TestCase):
    def test_score_is_the_highest_severity_and_maps_to_a_level(self):
        assessment = evaluate_contract_metadata(
            "189", [contract(status="1", contract_status="1")], today=TODAY
        )
        self.assertEqual(assessment.risk_score, 85)
        self.assertEqual(assessment.risk_level, "严重")

    def test_no_findings_scores_zero_and_low(self):
        assessment = evaluate_contract_metadata("189", [contract()], today=TODAY)
        self.assertEqual(assessment.risk_score, 0)
        self.assertEqual(assessment.risk_level, "低")

    def test_empty_contract_list_is_reported_as_no_contracts(self):
        assessment = evaluate_contract_metadata("189", [], today=TODAY)
        self.assertEqual(assessment.contract_count, 0)
        self.assertEqual(assessment.findings, [])
        self.assertIn("没有已登记的合同", build_contract_summary_text(assessment))

    def test_counts_track_every_contract(self):
        assessment = evaluate_contract_metadata(
            "189", [contract(contract_id="1"), contract(contract_id="2")], today=TODAY
        )
        self.assertEqual(assessment.contract_count, 2)
        self.assertEqual(assessment.evaluated_count, 2)


class BudgetCoverageRuleTests(unittest.TestCase):
    """Contract amounts and project_record.budget are both yuan (verified), so
    they are directly comparable. A zero budget means "not filled in"."""

    def _assess(self, contracts, budget):
        return evaluate_contract_metadata(
            "189", contracts, today=TODAY, project_budget=budget
        )

    def test_contract_total_over_budget_is_flagged(self):
        assessment = self._assess(
            [contract(total_amount="600000"), contract(total_amount="600000")],
            budget="1000000",
        )
        rules = {f.rule for f in assessment.findings}
        self.assertIn("contract_total_exceeds_budget", rules)

    def test_contract_total_within_budget_is_not_flagged(self):
        assessment = self._assess([contract(total_amount="400000")], budget="1000000")
        self.assertNotIn(
            "contract_total_exceeds_budget", {f.rule for f in assessment.findings}
        )

    def test_total_exactly_equal_to_budget_is_not_an_overrun(self):
        assessment = self._assess([contract(total_amount="1000000")], budget="1000000")
        self.assertNotIn(
            "contract_total_exceeds_budget", {f.rule for f in assessment.findings}
        )

    def test_zero_budget_is_unknown_and_skips_the_comparison(self):
        assessment = self._assess([contract(total_amount="999999")], budget="0")
        self.assertNotIn(
            "contract_total_exceeds_budget", {f.rule for f in assessment.findings}
        )

    def test_absent_budget_skips_the_comparison(self):
        for budget in (None, "", "not-a-number"):
            with self.subTest(budget=budget):
                assessment = self._assess([contract(total_amount="999999")], budget=budget)
                self.assertNotIn(
                    "contract_total_exceeds_budget",
                    {f.rule for f in assessment.findings},
                )

    def test_contracts_without_amounts_are_excluded_from_the_total(self):
        # Only the known amount counts, so the rule cannot fire on unknowns.
        assessment = self._assess(
            [contract(total_amount=""), contract(total_amount="10")], budget="1000"
        )
        self.assertNotIn(
            "contract_total_exceeds_budget", {f.rule for f in assessment.findings}
        )

    def test_evidence_discloses_how_many_contracts_carried_an_amount(self):
        assessment = self._assess(
            [contract(total_amount="2000"), contract(total_amount="")], budget="1000"
        )
        finding = next(
            f for f in assessment.findings if f.rule == "contract_total_exceeds_budget"
        )
        self.assertIn("counted=1/2", finding.evidence)
        self.assertIn("contract_total=2000", finding.evidence)
        self.assertIn("budget=1000", finding.evidence)

    def test_finding_is_project_scoped_not_contract_scoped(self):
        assessment = self._assess([contract(total_amount="2000")], budget="1000")
        finding = next(
            f for f in assessment.findings if f.rule == "contract_total_exceeds_budget"
        )
        self.assertEqual(finding.contract_ref, "project:189")

    def test_no_contracts_means_no_budget_finding(self):
        assessment = self._assess([], budget="1000")
        self.assertEqual(assessment.findings, [])

    def test_budget_rule_is_optional_and_off_by_default(self):
        assessment = evaluate_contract_metadata(
            "189", [contract(total_amount="999999999")], today=TODAY
        )
        self.assertNotIn(
            "contract_total_exceeds_budget", {f.rule for f in assessment.findings}
        )


class SanitizationTests(unittest.TestCase):
    def test_contract_name_never_appears_in_findings_or_summary(self):
        assessment = evaluate_contract_metadata(
            "189",
            [contract(contract_name="绝密合同名", total_amount="", sign_date="")],
            today=TODAY,
        )
        rendered = str(assessment.to_dict()) + build_contract_summary_text(assessment)
        self.assertNotIn("绝密合同名", rendered)

    def test_finding_reference_prefers_contract_code(self):
        assessment = evaluate_contract_metadata(
            "189", [contract(contract_code="C-9", total_amount="")], today=TODAY
        )
        self.assertTrue(
            all(item.contract_ref == "code:C-9" for item in assessment.findings)
        )

    def test_finding_reference_falls_back_to_id_without_code(self):
        assessment = evaluate_contract_metadata(
            "189",
            [contract(contract_code="", contract_id="777", total_amount="")],
            today=TODAY,
        )
        self.assertTrue(
            all(item.contract_ref == "id:777" for item in assessment.findings)
        )


if __name__ == "__main__":
    unittest.main()
