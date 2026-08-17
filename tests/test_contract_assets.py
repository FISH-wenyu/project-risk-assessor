from __future__ import annotations

import unittest

from app.contracts.assets import build_contract_assets, contract_asset_counts
from app.contracts.models import (
    AttachmentCandidate,
    ContractDiscoveryResult,
    ContractMetadata,
)


class ContractAssetTests(unittest.TestCase):
    def test_empty_discovery_result_has_missing_contract_signal(self):
        result = ContractDiscoveryResult(
            project_id="1008",
            contracts=[],
            attachments=[],
            warnings=["no_active_contracts_found"],
        )

        assets = build_contract_assets(result)

        self.assertEqual(assets, [])
        self.assertEqual(
            contract_asset_counts(assets),
            {
                "total": 0,
                "contract_metadata": 0,
                "attachment_candidate": 0,
                "by_status": {},
                "signals": ["missing_contracts"],
            },
        )

    def test_non_empty_discovery_result_builds_deterministic_assets(self):
        result = ContractDiscoveryResult(
            project_id="189",
            contracts=[
                ContractMetadata(
                    contract_id="123",
                    project_id="189",
                    contract_code="HT-001",
                    contract_name="Local Contract Name",
                    contract_type="purchase",
                    total_amount="1000.00",
                    has_project_link=True,
                )
            ],
            attachments=[
                AttachmentCandidate(
                    attach_id="900",
                    project_id="189",
                    biz_type="PROJECT",
                    biz_id="189",
                    file_name="contract.pdf",
                    file_ext="pdf",
                    file_size=2048,
                    sanitized_url_ref="[URL_WITH_QUERY_REDACTED]",
                )
            ],
            warnings=[],
        )

        assets = build_contract_assets(result)

        self.assertEqual([item.asset_id for item in assets], ["contract:123", "attachment:900"])
        self.assertEqual(assets[0].asset_kind, "contract_metadata")
        self.assertEqual(assets[0].source_ref, "123")
        self.assertEqual(assets[0].status, "no_text")
        self.assertEqual(assets[0].risk_signal, "metadata_only")
        self.assertEqual(assets[1].asset_kind, "attachment_candidate")
        self.assertEqual(assets[1].status, "ready_for_extraction")
        self.assertEqual(assets[1].risk_signal, "attachment_candidate")
        self.assertEqual(assets[1].sanitized_url_ref, "[URL_WITH_QUERY_REDACTED]")
        self.assertNotIn("token=", str([item.to_dict() for item in assets]))
        self.assertEqual(
            contract_asset_counts(assets),
            {
                "total": 2,
                "contract_metadata": 1,
                "attachment_candidate": 1,
                "by_status": {"no_text": 1, "ready_for_extraction": 1},
                "signals": ["attachment_candidate", "metadata_only"],
            },
        )


if __name__ == "__main__":
    unittest.main()


class SpreadsheetExtractionTests(unittest.TestCase):
    """`.xlsx` is a zip of XML, like `.docx`, so it needs no dependency.

    Two things a spreadsheet needs that a document does not: shared strings
    (cell text usually lives in `xl/sharedStrings.xml`, and the sheet holds
    only an index) and row structure (rules that look for a term near an
    amount need the two to stay on one line).
    """

    def _workbook(self, tmp, rows, shared=None):
        import zipfile
        from pathlib import Path

        path = Path(tmp) / "book.xlsx"
        sheet_rows = "".join(
            "<row>" + "".join(cells) + "</row>" for cells in rows
        )
        with zipfile.ZipFile(path, "w") as archive:
            if shared is not None:
                items = "".join(f"<si><t>{value}</t></si>" for value in shared)
                archive.writestr("xl/sharedStrings.xml", f"<sst>{items}</sst>")
            archive.writestr(
                "xl/worksheets/sheet1.xml", f"<worksheet><sheetData>{sheet_rows}</sheetData></worksheet>"
            )
        return path

    def test_shared_strings_are_resolved_not_printed_as_indices(self):
        """Reading only the sheet yields a page of integers."""
        import tempfile

        from app.contracts.text_extraction import extract_document

        with tempfile.TemporaryDirectory() as tmp:
            path = self._workbook(
                tmp,
                [['<c t="s"><v>0</v></c>', '<c t="s"><v>1</v></c>']],
                shared=["履约保证金", "壹拾万元整"],
            )
            result = extract_document(path)

        self.assertIn("履约保证金", result.text)
        self.assertIn("壹拾万元整", result.text)
        self.assertNotIn("<v>", result.text)

    def test_rows_stay_on_one_line(self):
        import tempfile

        from app.contracts.text_extraction import extract_document

        with tempfile.TemporaryDirectory() as tmp:
            path = self._workbook(
                tmp,
                [
                    ['<c t="s"><v>0</v></c>', "<c><v>100</v></c>"],
                    ['<c t="s"><v>1</v></c>', "<c><v>200</v></c>"],
                ],
                shared=["甲方", "乙方"],
            )
            result = extract_document(path)

        lines = [line for line in result.text.splitlines() if line.strip()]
        self.assertEqual(2, len(lines))
        self.assertIn("甲方", lines[0])
        self.assertIn("100", lines[0])

    def test_inline_strings_and_plain_numbers_are_read(self):
        import tempfile

        from app.contracts.text_extraction import extract_document

        with tempfile.TemporaryDirectory() as tmp:
            path = self._workbook(
                tmp, [['<c t="inlineStr"><is><t>直接文本</t></is></c>', "<c><v>42</v></c>"]]
            )
            result = extract_document(path)

        self.assertIn("直接文本", result.text)
        self.assertIn("42", result.text)

    def test_xml_entities_are_decoded_once(self):
        """`&amp;lt;` must survive as `&lt;`, not decode twice into `<`."""
        from app.contracts.text_extraction import _unescape_xml

        self.assertEqual("甲方 & 乙方", _unescape_xml("甲方 &amp; 乙方"))
        self.assertEqual("&lt;", _unescape_xml("&amp;lt;"))

    def test_the_fetcher_and_the_extractor_agree_on_formats(self):
        """Fetching a format the extractor cannot read downloads a file and
        produces an empty analysis; refusing one it CAN read wastes the
        capability silently."""
        from app.contracts.attachment_fetch import FETCHABLE_SUFFIXES
        from app.contracts.text_extraction import SUPPORTED_SUFFIXES

        self.assertEqual(set(SUPPORTED_SUFFIXES), set(FETCHABLE_SUFFIXES))


class LegacyDocExtractionTests(unittest.TestCase):
    """A half-parsed `.doc` returns text that LOOKS like prose but has runs
    missing, and that text then feeds contract rules which report findings
    with confident-sounding evidence. Silence is safer than plausible garbage,
    so the reader judges its own output and discards what fails."""

    def test_incoherent_output_is_discarded_rather_than_returned(self):
        from app.contracts.text_extraction import _doc_text_is_coherent

        # What a mis-decoded WordDocument stream actually looks like. Measured
        # on the one .doc in the source data: 55 characters, 20% printable.
        self.assertFalse(_doc_text_is_coherent("؝\x00$\x00ε\x00ɨ\x00\u038b\x00*\x00ƀ\n洈ै猄ै\x04Āࠀ"))
        self.assertFalse(_doc_text_is_coherent("°‚. °ÆA!°"))

    def test_real_contract_text_passes_the_check(self):
        from app.contracts.text_extraction import _doc_text_is_coherent

        self.assertTrue(
            _doc_text_is_coherent(
                "第一条 合同金额为人民币壹佰万元整（¥1,000,000.00）。\n"
                "第二条 乙方应于验收合格后 30 日内付款，逾期按日万分之五计违约金。"
            )
        )

    def test_too_short_is_not_accepted_on_length_alone(self):
        from app.contracts.text_extraction import _doc_text_is_coherent

        self.assertFalse(_doc_text_is_coherent("合同"))

    def test_a_missing_reader_is_reported_not_silently_skipped(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from app.contracts.text_extraction import (
            SIGNAL_DOC_READER_MISSING,
            extract_document,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.doc"
            path.write_bytes(b"\xd0\xcf\x11\xe0" + b"\x00" * 100)
            with patch.dict("sys.modules", {"olefile": None}):
                result = extract_document(path)

        self.assertIn(SIGNAL_DOC_READER_MISSING, result.signals)
        self.assertFalse(result.usable)
        self.assertIn("convert", result.error.lower())

    def test_a_file_that_is_not_ole_is_reported(self):
        import tempfile
        from pathlib import Path

        from app.contracts.text_extraction import extract_document

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fake.doc"
            path.write_bytes(b"this is not an OLE compound file at all")
            result = extract_document(path)

        self.assertFalse(result.usable)
        self.assertTrue(result.error)


class SpreadsheetPersonalDataTests(unittest.TestCase):
    """Enabling `.xlsx` raised a real exposure: a spreadsheet is far more
    likely than a contract to be a roster of people. The one in the source data
    is exactly that - names, mobile numbers, job titles.

    Pattern redaction cannot touch personal NAMES: a Chinese name has no
    distinguishing shape, and a regex broad enough to catch 张伟 also catches
    half the vocabulary in a contract. The grid is the only honest signal.
    """

    def test_a_row_carrying_contact_data_is_redacted_whole(self):
        """The rule that makes this safe by default. The real file has NO
        header row, so header-based column detection alone did nothing to it."""
        from app.contracts.text_redaction import redact_table

        rows, signals = redact_table(
            [["李时珍", "13521919547", "董事长"], ["张仲景", "17370854125", "董事"]]
        )

        flat = " ".join(cell for row in rows for cell in row)
        self.assertNotIn("李时珍", flat)
        self.assertNotIn("13521919547", flat)
        self.assertNotIn("董事长", flat)
        self.assertIn("spreadsheet_looks_like_a_personal_roster", signals)

    def test_a_contract_artefact_passes_through_untouched(self):
        """A payment schedule has no phone numbers in its rows, so nothing
        about it triggers the rule. Redacting it would destroy the amounts the
        text rules exist to read."""
        from app.contracts.text_redaction import redact_table

        rows, signals = redact_table(
            [
                ["付款节点", "金额", "日期"],
                ["预付款", "1500000", "2026-01-01"],
                ["进度款", "3000000", "2026-06-01"],
            ]
        )

        self.assertEqual(
            [["付款节点", "金额", "日期"], ["预付款", "1500000", "2026-01-01"],
             ["进度款", "3000000", "2026-06-01"]],
            rows,
        )
        self.assertEqual([], signals)

    def test_a_named_column_is_redacted_even_without_contact_data(self):
        """A 姓名 column holds names whatever they look like."""
        from app.contracts.text_redaction import redact_table

        rows, _signals = redact_table(
            [["姓名", "部门"], ["王先进", "财务"], ["钱多多", "人事"]]
        )

        self.assertEqual(["姓名", "部门"], rows[0])          # header kept
        self.assertNotIn("王先进", " ".join(rows[1]))
        self.assertEqual("财务", rows[1][1])                 # other column intact

    def test_redaction_happens_before_the_text_is_flattened(self):
        """Ordering bug found on the first attempt: `_normalize` collapses tabs
        into spaces, so redacting after flattening saw no columns at all and
        silently did nothing to the real roster."""
        import tempfile
        import zipfile
        from pathlib import Path

        from app.contracts.text_extraction import extract_document
        from app.contracts.text_redaction import contains_sensitive

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "roster.xlsx"
            rows = "".join(
                f'<row><c t="s"><v>{i * 2}</v></c><c><v>1352191954{i}</v></c>'
                f'<c t="s"><v>{i * 2 + 1}</v></c></row>'
                for i in range(2)
            )
            shared = "".join(
                f"<si><t>{name}</t></si>" for name in ("李时珍", "董事长", "张仲景", "董事")
            )
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("xl/sharedStrings.xml", f"<sst>{shared}</sst>")
                archive.writestr(
                    "xl/worksheets/sheet1.xml", f"<worksheet><sheetData>{rows}</sheetData></worksheet>"
                )
            result = extract_document(path)

        self.assertNotIn("李时珍", result.text)
        self.assertFalse(contains_sensitive(result.text))
        self.assertIn("spreadsheet_looks_like_a_personal_roster", result.signals)

    def test_an_amount_alone_is_not_treated_as_personal(self):
        """Contract amounts have many digits too. Treating them as contact
        data would redact the numbers the rules exist to compare."""
        from app.contracts.text_redaction import _holds_contact_data

        self.assertFalse(_holds_contact_data("1500000"))
        self.assertFalse(_holds_contact_data("¥45,000,000.00"))
        self.assertTrue(_holds_contact_data("13521919547"))
