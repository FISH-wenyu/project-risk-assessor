"""Tests for the contract attachment fetcher and sensitive-value redaction.

No test here makes a real network request. Host resolution is injected and the
download step is stubbed, so the guards are exercised without egress.

The SSRF cases are the point of this file. Attachment URLs come out of the
source database, which is untrusted input: a URL edited in the source system
must not be able to make this process fetch an internal address.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.contracts.attachment_fetch import (
    REASON_BLANK_URL,
    REASON_FETCH_FAILED,
    REASON_HOST_BLOCKED,
    REASON_NO_ALLOWLIST,
    REASON_OK,
    REASON_PRIVATE_ADDRESS,
    REASON_REDIRECTED,
    REASON_SCHEME_BLOCKED,
    REASON_TOO_LARGE,
    REASON_UNRESOLVABLE,
    REASON_UNSUPPORTED_TYPE,
    AttachmentFetcher,
    build_document_provider,
)
from app.contracts.text_redaction import (
    contains_sensitive,
    redact_mapping,
    redact_text,
)
from app.contracts.text_rules import analyze_contract_text

ALLOWED = ("oss.example.com", "files.example.net")
GOOD_URL = "https://oss.example.com/contract/2026/abc.pdf"


def _fetcher(tmp, *, hosts=ALLOWED, addresses=("93.184.216.34",), payload=b"%PDF-1.7 body"):
    fetcher = AttachmentFetcher(
        allowed_hosts=hosts,
        destination=Path(tmp),
        _resolver=lambda host: list(addresses),
    )
    # Stub the network step; every guard before it still runs for real.
    fetcher._download = lambda url, outcome: payload  # type: ignore[method-assign]
    return fetcher


class SsrfGuardTests(unittest.TestCase):
    def test_empty_allowlist_fetches_nothing(self):
        with TemporaryDirectory() as tmp:
            outcome = _fetcher(tmp, hosts=()).fetch(GOOD_URL, attach_ref="a1")

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, REASON_NO_ALLOWLIST)

    def test_http_is_refused(self):
        with TemporaryDirectory() as tmp:
            outcome = _fetcher(tmp).fetch(
                "http://oss.example.com/c/a.pdf", attach_ref="a1"
            )

        self.assertEqual(outcome.reason, REASON_SCHEME_BLOCKED)

    def test_file_scheme_is_refused(self):
        with TemporaryDirectory() as tmp:
            outcome = _fetcher(tmp).fetch("file:///etc/passwd", attach_ref="a1")

        self.assertFalse(outcome.ok)
        self.assertNotEqual(outcome.reason, REASON_OK)

    def test_host_outside_the_allowlist_is_refused(self):
        with TemporaryDirectory() as tmp:
            outcome = _fetcher(tmp).fetch(
                "https://attacker.example/c/a.pdf", attach_ref="a1"
            )

        self.assertEqual(outcome.reason, REASON_HOST_BLOCKED)

    def test_suffix_lookalike_host_is_refused(self):
        # Matching by suffix would let this through. It must be exact.
        with TemporaryDirectory() as tmp:
            outcome = _fetcher(tmp).fetch(
                "https://oss.example.com.attacker.example/c/a.pdf", attach_ref="a1"
            )

        self.assertEqual(outcome.reason, REASON_HOST_BLOCKED)

    def test_allowlisted_host_resolving_to_loopback_is_refused(self):
        with TemporaryDirectory() as tmp:
            outcome = _fetcher(tmp, addresses=("127.0.0.1",)).fetch(
                GOOD_URL, attach_ref="a1"
            )

        self.assertEqual(outcome.reason, REASON_PRIVATE_ADDRESS)

    def test_allowlisted_host_resolving_to_private_range_is_refused(self):
        for private in ("10.0.0.5", "192.168.1.10", "172.16.0.9"):
            with TemporaryDirectory() as tmp:
                outcome = _fetcher(tmp, addresses=(private,)).fetch(
                    GOOD_URL, attach_ref="a1"
                )
            self.assertEqual(outcome.reason, REASON_PRIVATE_ADDRESS, private)

    def test_cloud_metadata_address_is_refused(self):
        # 169.254.169.254 is the classic SSRF target for credential theft.
        with TemporaryDirectory() as tmp:
            outcome = _fetcher(tmp, addresses=("169.254.169.254",)).fetch(
                GOOD_URL, attach_ref="a1"
            )

        self.assertEqual(outcome.reason, REASON_PRIVATE_ADDRESS)

    def test_one_private_address_among_several_still_refuses(self):
        with TemporaryDirectory() as tmp:
            outcome = _fetcher(tmp, addresses=("93.184.216.34", "127.0.0.1")).fetch(
                GOOD_URL, attach_ref="a1"
            )

        self.assertEqual(outcome.reason, REASON_PRIVATE_ADDRESS)

    def test_address_check_can_be_skipped_but_never_silently(self):
        # Needed on machines where a proxy does DNS interception: the proxy
        # dials the host, so the locally resolved address is not what is
        # actually contacted. Measured here: both OSS hosts resolve to
        # 198.18.0.x, the RFC 2544 range, behind a local proxy.
        with TemporaryDirectory() as tmp:
            fetcher = _fetcher(tmp, addresses=("198.18.0.154",))
            fetcher.allow_private_addresses = True
            outcome = fetcher.fetch(GOOD_URL, attach_ref="a1")

        self.assertTrue(outcome.ok)
        self.assertIn("address_check_skipped_by_config", outcome.signals)
        self.assertIn("address_check_skipped_by_config", str(outcome.to_dict()))

    def test_skipping_the_address_check_does_not_bypass_the_host_allowlist(self):
        # The allowlist is what actually stops a rewritten database URL, so it
        # must still hold when the address check is off.
        with TemporaryDirectory() as tmp:
            fetcher = _fetcher(tmp, addresses=("127.0.0.1",))
            fetcher.allow_private_addresses = True
            outcome = fetcher.fetch(
                "https://attacker.example/c/a.pdf", attach_ref="a1"
            )

        self.assertEqual(outcome.reason, REASON_HOST_BLOCKED)

    def test_address_check_is_on_by_default(self):
        with TemporaryDirectory() as tmp:
            self.assertFalse(_fetcher(tmp).allow_private_addresses)

    def test_unresolvable_host_is_refused(self):
        with TemporaryDirectory() as tmp:
            fetcher = _fetcher(tmp)
            fetcher._resolver = lambda host: []
            outcome = fetcher.fetch(GOOD_URL, attach_ref="a1")

        self.assertEqual(outcome.reason, REASON_UNRESOLVABLE)

    def test_resolver_failure_is_refused_not_crashed(self):
        def boom(host):
            raise OSError("dns down")

        with TemporaryDirectory() as tmp:
            fetcher = _fetcher(tmp)
            fetcher._resolver = boom
            outcome = fetcher.fetch(GOOD_URL, attach_ref="a1")

        self.assertEqual(outcome.reason, REASON_UNRESOLVABLE)


class FetchPolicyTests(unittest.TestCase):
    def test_blank_url_is_reported(self):
        with TemporaryDirectory() as tmp:
            self.assertEqual(
                _fetcher(tmp).fetch("   ", attach_ref="a1").reason, REASON_BLANK_URL
            )

    def test_unsupported_types_are_reported_not_silently_skipped(self):
        # `.doc` and `.xlsx` became fetchable on 2026-08-14, so the examples
        # here are formats the extractor genuinely cannot read. The invariant
        # is unchanged: refusal is reported with a reason, never silent.
        for url in (
            "https://oss.example.com/c/archive.zip",
            "https://oss.example.com/c/scan.jpg",
            "https://oss.example.com/c/tool.exe",
            "https://oss.example.com/c/notes.txt",
        ):
            with TemporaryDirectory() as tmp:
                outcome = _fetcher(tmp).fetch(url, attach_ref="a1")
            self.assertEqual(outcome.reason, REASON_UNSUPPORTED_TYPE, url)

    def test_the_formats_that_became_readable_are_now_fetched(self):
        """Refusing a format the extractor CAN read wastes the capability
        silently. `.xlsx` reads through the same zip-of-XML path as `.docx`;
        `.doc` is fetched and then judged by the extractor's coherence check."""
        for url, body in (
            ("https://oss.example.com/c/quote.xlsx", b"PK\x03\x04 body"),
            ("https://oss.example.com/c/old.doc", b"\xd0\xcf\x11\xe0 body"),
        ):
            with TemporaryDirectory() as tmp:
                outcome = _fetcher(tmp, payload=body).fetch(url, attach_ref="a1")
            self.assertTrue(outcome.ok, url)

    def test_supported_types_are_written_locally(self):
        for url, body in (
            (GOOD_URL, b"%PDF-1.7 body"),
            ("https://oss.example.com/c/a.docx", b"PK\x03\x04 body"),
        ):
            with TemporaryDirectory() as tmp:
                outcome = _fetcher(tmp, payload=body).fetch(url, attach_ref="a1")
                self.assertTrue(outcome.ok, url)
                self.assertEqual(outcome.reason, REASON_OK)
                self.assertTrue(outcome.path.exists())
                self.assertEqual(outcome.path.read_bytes(), body)

    def test_local_name_is_content_addressed_not_the_remote_filename(self):
        # The remote name is attacker-influenced and may carry a contract name.
        with TemporaryDirectory() as tmp:
            outcome = _fetcher(tmp).fetch(
                "https://oss.example.com/c/%E6%8A%80%E6%9C%AF%E6%9C%8D%E5%8A%A1%E5%90%88%E5%90%8C.pdf",
                attach_ref="a1",
            )

        self.assertTrue(outcome.ok)
        self.assertNotIn("技术服务合同", outcome.path.name)
        self.assertTrue(outcome.path.name.endswith(".pdf"))

    def test_path_traversal_in_the_url_cannot_escape_the_destination(self):
        with TemporaryDirectory() as tmp:
            outcome = _fetcher(tmp).fetch(
                "https://oss.example.com/../../../../etc/evil.pdf", attach_ref="a1"
            )

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.path.parent.resolve(), Path(tmp).resolve())

    def test_second_fetch_reuses_the_local_copy(self):
        with TemporaryDirectory() as tmp:
            fetcher = _fetcher(tmp)
            first = fetcher.fetch(GOOD_URL, attach_ref="a1")
            second = fetcher.fetch(GOOD_URL, attach_ref="a1")

        self.assertEqual(first.reason, REASON_OK)
        self.assertEqual(second.reason, "already_local")

    def test_outcome_dict_leaks_neither_url_nor_local_path(self):
        with TemporaryDirectory() as tmp:
            payload = _fetcher(tmp).fetch(GOOD_URL, attach_ref="a1").to_dict()

        self.assertNotIn("contract/2026/abc.pdf", str(payload))
        self.assertNotIn(tmp, str(payload))
        self.assertEqual(payload["host"], "oss.example.com")


class DownloadGuardTests(unittest.TestCase):
    """Exercise the real _download against a stubbed HTTP response."""

    def _response(self, *, body, url=GOOD_URL, declared=None):
        class _Response:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def geturl(self_inner):
                return url

            @property
            def headers(self_inner):
                return {"Content-Length": declared} if declared else {}

            def read(self_inner, size):
                return body[:size]

        return _Response()

    def _run(self, tmp, response, *, max_bytes=1024):
        import app.contracts.attachment_fetch as module

        fetcher = AttachmentFetcher(
            allowed_hosts=ALLOWED,
            destination=Path(tmp),
            max_bytes=max_bytes,
            _resolver=lambda host: ["93.184.216.34"],
        )
        original = module.urlopen
        module.urlopen = lambda *a, **k: response
        try:
            return fetcher.fetch(GOOD_URL, attach_ref="a1")
        finally:
            module.urlopen = original

    def test_redirect_to_another_host_is_refused(self):
        with TemporaryDirectory() as tmp:
            outcome = self._run(
                tmp,
                self._response(body=b"x" * 10, url="https://attacker.example/c/a.pdf"),
            )

        self.assertEqual(outcome.reason, REASON_REDIRECTED)

    def test_declared_length_over_the_ceiling_is_refused(self):
        with TemporaryDirectory() as tmp:
            outcome = self._run(
                tmp, self._response(body=b"x" * 10, declared="999999"), max_bytes=1024
            )

        self.assertEqual(outcome.reason, REASON_TOO_LARGE)

    def test_a_lying_content_length_is_still_caught_by_the_read_cap(self):
        # Declares 10 bytes, actually returns far more. Trusting the header
        # alone would let an unbounded body through.
        with TemporaryDirectory() as tmp:
            outcome = self._run(
                tmp,
                self._response(body=b"x" * 5000, declared="10"),
                max_bytes=1024,
            )

        self.assertEqual(outcome.reason, REASON_TOO_LARGE)

    def test_transport_error_is_reported_without_echoing_the_url(self):
        import app.contracts.attachment_fetch as module

        with TemporaryDirectory() as tmp:
            fetcher = AttachmentFetcher(
                allowed_hosts=ALLOWED,
                destination=Path(tmp),
                _resolver=lambda host: ["93.184.216.34"],
            )

            def boom(*a, **k):
                raise OSError(f"failed fetching {GOOD_URL}")

            original = module.urlopen
            module.urlopen = boom
            try:
                outcome = fetcher.fetch(GOOD_URL, attach_ref="a1")
            finally:
                module.urlopen = original

        self.assertEqual(outcome.reason, REASON_FETCH_FAILED)
        self.assertNotIn("abc.pdf", str(outcome.to_dict()))


class DocumentProviderTests(unittest.TestCase):
    def test_only_successfully_fetched_documents_are_returned(self):
        class _Discovery:
            def read_contract_attachments(self, project_id):
                return [
                    {"attach_id": "1", "attach_url": GOOD_URL},
                    {"attach_id": "2", "attach_url": "https://attacker.example/a.pdf"},
                    # An unreadable format, and a blocked host. Neither may
                    # reach the caller as if it had been fetched.
                    {"attach_id": "3", "attach_url": "https://oss.example.com/a.zip"},
                ]

        with TemporaryDirectory() as tmp:
            paths = build_document_provider(lambda: _Discovery(), _fetcher(tmp))("100")

        self.assertEqual(len(paths), 1)


class RedactionTests(unittest.TestCase):
    def test_unified_social_credit_code_is_redacted(self):
        self.assertNotIn(
            "91110000MA01ABCDXY", redact_text("统一社会信用代码：91110000MA01ABCDXY")
        )

    def test_bank_account_is_redacted(self):
        text = redact_text("银行账号 6222021234567890123")
        self.assertNotIn("6222021234567890123", text)
        self.assertIn("[已脱敏", text)

    def test_national_id_is_redacted(self):
        self.assertNotIn("11010119900307461X", redact_text("身份证 11010119900307461X"))

    def test_phone_and_email_are_redacted(self):
        text = redact_text("联系 13800138000 邮箱 zhang@example.com")
        self.assertNotIn("13800138000", text)
        self.assertNotIn("zhang@example.com", text)

    def test_named_representative_value_is_redacted_but_label_kept(self):
        text = redact_text("法定代表人：张三")
        self.assertNotIn("张三", text)
        self.assertIn("法定代表人", text)

    def test_redaction_marks_rather_than_deletes(self):
        # A finding that silently lost its evidence is worse than one that
        # shows it was redacted.
        self.assertIn("[已脱敏", redact_text("开户银行：某某支行"))

    def test_amounts_survive_redaction(self):
        # Amounts are the whole point of several rules; they must not be eaten.
        text = redact_text("合同总价款：¥15,360,000.00")
        self.assertIn("15,360,000.00", text)

    def test_contains_sensitive_is_a_working_tripwire(self):
        self.assertTrue(contains_sensitive("账号 6222021234567890123"))
        self.assertFalse(contains_sensitive(redact_text("账号 6222021234567890123")))
        self.assertFalse(contains_sensitive("合同总价款 ¥15,360,000.00"))

    def test_redact_mapping_only_touches_named_keys(self):
        payload = {"reason": "联系 13800138000", "rule": "keep13800138000"}
        cleaned = redact_mapping(payload, keys=("reason",))

        self.assertNotIn("13800138000", cleaned["reason"])
        self.assertEqual(cleaned["rule"], "keep13800138000")


class FindingRedactionTests(unittest.TestCase):
    SENSITIVE_CONTRACT = """
    采购合同
    甲方（买方）：某某公司 统一社会信用代码：91110000MA01ABCDXY
    法定代表人：张三 联系电话：13800138000 邮箱：zhang@example.com
    开户银行：某某银行某某支行 银行账号：6222021234567890123
    经办人身份证：11010119900307461X
    第一条 合同总价款：人民币贰仟万元整（¥15,360,000.00）。
    第二条 付款方式：60% 预付款。
    甲方（盖章）：________________ 授权代表签字：________________
    11.3 合同生效：双方授权代表签字加盖公章之日生效。
    """

    def test_no_finding_carries_a_sensitive_value(self):
        result = analyze_contract_text(self.SENSITIVE_CONTRACT)

        self.assertTrue(result.findings, "expected this contract to trigger rules")
        for finding in result.findings:
            self.assertFalse(
                contains_sensitive(finding.evidence),
                f"{finding.rule} evidence leaked: {finding.evidence}",
            )
            self.assertFalse(
                contains_sensitive(finding.reason),
                f"{finding.rule} reason leaked: {finding.reason}",
            )

    def test_no_finding_carries_a_named_individual(self):
        result = analyze_contract_text(self.SENSITIVE_CONTRACT)
        blob = str([finding.to_dict() for finding in result.findings])

        for secret in ("张三", "6222021234567890123", "91110000MA01ABCDXY"):
            self.assertNotIn(secret, blob)

    def test_rules_still_fire_on_the_sensitive_contract(self):
        # Redaction must not neuter detection.
        fired = {f.rule for f in analyze_contract_text(self.SENSITIVE_CONTRACT).findings}

        self.assertIn("contract_not_executed", fired)
        self.assertIn("high_advance_payment", fired)


if __name__ == "__main__":
    unittest.main()


class UrlEncodingTests(unittest.TestCase):
    """Two real attachments failed with InvalidURL because their stored paths
    hold raw non-ASCII and full-width brackets. urllib refuses to send those."""

    def test_non_ascii_path_is_percent_encoded(self):
        from app.contracts.attachment_fetch import _encode_url

        encoded = _encode_url("https://oss.example.com/test/【R276】(中文翻译)a.pdf")

        self.assertNotIn("【", encoded)
        self.assertNotIn("中文", encoded)
        self.assertTrue(encoded.startswith("https://oss.example.com/"))

    def test_already_encoded_urls_are_not_double_encoded(self):
        from app.contracts.attachment_fetch import _encode_url

        encoded = _encode_url("https://oss.example.com/a/%E4%B8%80.pdf")

        self.assertIn("%E4%B8%80", encoded)
        self.assertNotIn("%25E4", encoded)

    def test_plain_ascii_url_is_unchanged(self):
        from app.contracts.attachment_fetch import _encode_url

        url = "https://oss.example.com/a/plain.pdf"
        self.assertEqual(_encode_url(url), url)

    def test_encoding_cannot_move_the_request_to_another_host(self):
        # Encoding touches path and query only; the host is what the allowlist
        # was checked against and must survive untouched.
        from app.contracts.attachment_fetch import _encode_url

        encoded = _encode_url("https://oss.example.com/【x】.pdf")

        self.assertTrue(encoded.startswith("https://oss.example.com/"))

    def test_spaces_are_encoded(self):
        from app.contracts.attachment_fetch import _encode_url

        self.assertIn("%20", _encode_url("https://oss.example.com/a b.pdf"))


class EncryptedPdfTests(unittest.TestCase):
    def test_encrypted_pdfs_report_a_distinct_signal(self):
        # "text_extraction_failed" sent the operator looking for a corrupt
        # file when the real cause was AES encryption and a missing backend.
        from app.contracts.text_extraction import SIGNAL_PDF_ENCRYPTED

        self.assertEqual(SIGNAL_PDF_ENCRYPTED, "pdf_encrypted")

    def test_crypto_backend_is_available(self):
        # pypdf needs this to open AES-encrypted PDFs; two real attachments do.
        import importlib.util

        self.assertIsNotNone(importlib.util.find_spec("cryptography"))
