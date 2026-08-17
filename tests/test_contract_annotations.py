"""Tests for local acknowledgement and ownership of contract findings.

The feature exists because a ledger that cannot record "we looked at this and
accepted it" reprints the same 18 立即处理 rows on every load until nobody reads
it. The tests that matter here are the ones about what an acknowledgement
MEANS: it is a statement about a specific level of risk, not a permanent mute.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.contracts.annotations import (
    MAX_NOTE_CHARS,
    STATE_ACCEPTED,
    STATE_ACKNOWLEDGED,
    STATE_OPEN,
    AnnotationError,
    ContractAnnotationStore,
    apply_annotations,
)


class AnnotationStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = ContractAnnotationStore(Path(self._tmp.name) / "risk.db")

    def test_acknowledging_records_the_score_it_was_acknowledged_at(self):
        annotation = self.store.set_annotation(
            "code:1", state=STATE_ACKNOWLEDGED, current_score=40
        )

        self.assertEqual(40, annotation.acknowledged_score)

    def test_an_acknowledgement_does_not_cover_a_higher_score(self):
        """The whole point. Accepting a finding at 40 must not silence the same
        contract at 80 - what was accepted is not what is there now."""
        annotation = self.store.set_annotation(
            "code:1", state=STATE_ACKNOWLEDGED, current_score=40
        )

        self.assertFalse(annotation.is_stale_for(40))
        self.assertFalse(annotation.is_stale_for(30))
        self.assertTrue(annotation.is_stale_for(80))

    def test_an_open_row_is_never_stale(self):
        annotation = self.store.set_annotation("code:1", state=STATE_OPEN, current_score=40)

        self.assertIsNone(annotation.acknowledged_score)
        self.assertFalse(annotation.is_stale_for(99))

    def test_setting_an_owner_does_not_disturb_the_acknowledgement(self):
        """Owner and state are independent. Assigning someone must not quietly
        re-open a row that was already accepted."""
        self.store.set_annotation("code:1", state=STATE_ACKNOWLEDGED, current_score=40)
        self.store.set_annotation("code:1", owner="张三")

        annotation = self.store.get_annotations("code:1")[0]

        self.assertEqual(STATE_ACKNOWLEDGED, annotation.state)
        self.assertEqual(40, annotation.acknowledged_score)
        self.assertEqual("张三", annotation.owner)

    def test_none_leaves_a_field_alone_but_empty_string_clears_it(self):
        self.store.set_annotation("code:1", owner="张三", note="先放着")
        self.store.set_annotation("code:1", owner="")

        annotation = self.store.get_annotations("code:1")[0]

        self.assertEqual("", annotation.owner)
        self.assertEqual("先放着", annotation.note)

    def test_reopening_clears_the_acknowledged_score(self):
        self.store.set_annotation("code:1", state=STATE_ACKNOWLEDGED, current_score=40)
        self.store.set_annotation("code:1", state=STATE_OPEN)

        annotation = self.store.get_annotations("code:1")[0]

        self.assertIsNone(annotation.acknowledged_score)
        self.assertFalse(annotation.is_stale_for(99))

    def test_a_whole_contract_annotation_is_separate_from_a_rule_one(self):
        """Dismissing one finding must not dismiss a different rule that fires
        later for another reason."""
        self.store.set_annotation("code:1", rule="still_draft", state=STATE_ACKNOWLEDGED)

        annotations = {a.rule: a for a in self.store.get_annotations("code:1")}

        self.assertIn("still_draft", annotations)
        self.assertNotIn("", annotations)

    def test_nothing_is_deleted_so_the_decision_trail_survives(self):
        self.store.set_annotation("code:1", state=STATE_ACKNOWLEDGED, note="业务确认", current_score=40)
        self.store.set_annotation("code:1", state=STATE_OPEN, note="重新打开")
        self.store.set_annotation("code:1", state=STATE_ACCEPTED, note="接受风险", current_score=40)

        history = self.store.history("code:1")

        self.assertEqual(3, len(history))
        self.assertEqual([STATE_ACCEPTED, STATE_OPEN, STATE_ACKNOWLEDGED],
                         [entry["state"] for entry in history])

    def test_input_limits_are_enforced(self):
        with self.assertRaises(AnnotationError):
            self.store.set_annotation("code:1", note="x" * (MAX_NOTE_CHARS + 1))
        with self.assertRaises(AnnotationError):
            self.store.set_annotation("", state=STATE_ACKNOWLEDGED)
        with self.assertRaises(AnnotationError):
            self.store.set_annotation("code:1", state="something_else")

    def test_control_characters_are_stripped_from_operator_text(self):
        annotation = self.store.set_annotation("code:1", owner="张\x00三\x1f", note="a\x07b")

        self.assertEqual("张三", annotation.owner)
        self.assertEqual("ab", annotation.note)

    def test_one_read_serves_the_whole_ledger(self):
        self.store.set_annotation("code:1", owner="张三")
        self.store.set_annotation("code:2", state=STATE_ACKNOWLEDGED)

        grouped = self.store.all_annotations()

        self.assertEqual({"code:1", "code:2"}, set(grouped))


class ApplyAnnotationsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = ContractAnnotationStore(Path(self._tmp.name) / "risk.db")

    def _rows(self):
        return [
            {"contract_ref": "code:1", "risk_score": 80},
            {"contract_ref": "code:2", "risk_score": 30},
        ]

    def test_rows_without_an_annotation_are_open_and_need_attention(self):
        rows = self._rows()
        summary = apply_annotations(rows, {})

        self.assertEqual(2, summary["open_count"])
        self.assertEqual(2, summary["unassigned_count"])
        self.assertTrue(all(row["needs_attention"] for row in rows))

    def test_an_acknowledged_row_stops_needing_attention(self):
        self.store.set_annotation("code:1", state=STATE_ACKNOWLEDGED, current_score=80)
        rows = self._rows()

        summary = apply_annotations(rows, self.store.all_annotations())

        self.assertEqual(1, summary["acknowledged_count"])
        self.assertFalse(rows[0]["needs_attention"])

    def test_a_stale_acknowledgement_is_presented_as_open(self):
        """It has to come back to the worklist, or a risk that has since grown
        stays silently dismissed by a decision that no longer covers it."""
        self.store.set_annotation("code:1", state=STATE_ACKNOWLEDGED, current_score=40)
        rows = self._rows()

        summary = apply_annotations(rows, self.store.all_annotations())

        self.assertEqual(STATE_OPEN, rows[0]["annotation_state"])
        self.assertTrue(rows[0]["annotation_stale"])
        self.assertTrue(rows[0]["needs_attention"])
        self.assertEqual(1, summary["stale_count"])

    def test_owners_are_counted_for_the_worklist(self):
        self.store.set_annotation("code:1", owner="张三")
        self.store.set_annotation("code:2", owner="张三")
        rows = self._rows()

        summary = apply_annotations(rows, self.store.all_annotations())

        self.assertEqual({"张三": 2}, summary["owner_counts"])
        self.assertEqual(0, summary["unassigned_count"])

    def test_annotations_never_overwrite_a_source_column(self):
        """They are LOCAL facts about a source row. Writing them over source
        fields would make them indistinguishable from the source system's own
        data, which is read-only and has neither field."""
        self.store.set_annotation("code:1", state=STATE_ACKNOWLEDGED, owner="张三", current_score=80)
        rows = [{"contract_ref": "code:1", "risk_score": 80, "approval_status": "审核通过"}]

        apply_annotations(rows, self.store.all_annotations())

        self.assertEqual("审核通过", rows[0]["approval_status"])
        self.assertEqual(80, rows[0]["risk_score"])
        self.assertIn("annotation", rows[0])


class AnnotationExportBoundaryTests(unittest.TestCase):
    """The CSV leaves the machine. An owner is a person's name and a note is
    unredacted operator free text; neither belongs in a circulated file. The
    state does, because that is what makes the export useful."""

    def test_csv_columns_carry_state_but_not_owner_or_note(self):
        from app.contracts.ledger import CSV_COLUMNS

        self.assertIn("annotation_state", CSV_COLUMNS)
        self.assertNotIn("owner", CSV_COLUMNS)
        self.assertNotIn("note", CSV_COLUMNS)

    def test_every_row_carries_a_state_even_with_no_annotation_store(self):
        """The column must never export blank cells that read as "unknown"."""
        from app.contracts.ledger import build_contract_ledger
        from app.contracts.portfolio import build_contract_portfolio
        from tests.test_contract_ledger import TODAY, _contract

        portfolio = build_contract_portfolio(
            [(_contract(contract_code="A"), "standalone")], today=TODAY
        )
        row = build_contract_ledger(portfolio)["rows"][0]

        self.assertEqual("open", row["annotation_state"])


if __name__ == "__main__":
    unittest.main()
