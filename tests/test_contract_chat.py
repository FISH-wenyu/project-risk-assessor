"""Tests for the contract chat pipeline and service.

No LLM, no database, no network: the pipeline is plain functions over plain
data and the service takes all three dependencies by injection.

The outbound-payload tests are the important ones. They assert what must NEVER
leave the machine, and they are the reason this feature could be approved at
all.
"""

from __future__ import annotations

import unittest

from app.contracts.chat_pipeline import (
    MAX_CLAUSE_CHARS,
    MAX_CLAUSES_PER_CONTRACT,
    MAX_TOTAL_CLAUSE_CHARS,
    SIGNAL_ALL_DROPPED,
    SIGNAL_NO_CLAUSES,
    SIGNAL_SOME_DROPPED,
    SIGNAL_TRUNCATED,
    build_payload,
    match_topics,
    run_pipeline,
    select_clauses,
    verify_clauses,
)
from app.contracts.chat_service import (
    SIGNAL_DOC_UNREADABLE,
    SIGNAL_LLM_FAILED,
    SIGNAL_LLM_UNAVAILABLE,
    ContractChatService,
)
from app.contracts.clause_split import Clause

LIABILITY = Clause(1, "违约责任", "违约责任：逾期付款每日按万分之三支付违约金，逾期超过 45 日可解除合同。" * 3)
PAYMENT = Clause(2, "付款方式", "付款：20% 预付款，70% 进度款，10% 质保尾款，均通过银行转账支付。" * 3)
DISPUTE = Clause(3, "争议解决", "争议解决：提交 CIETAC 仲裁，适用中华人民共和国法律。" * 3)

ROW = {
    "contract_ref": "code:ACME-C2026011",
    "contract_name": "绝密合同名称",
    "org_id": "123",
    "risk_level": "严重",
    "risk_score": 85,
    "approval_status": "审核中",
    "execution_status": "进行中",
    "total_amount": "15360000.00",
    "end_date": "2026-03-06",
    "findings": [
        {"rule": "executing_without_approval", "severity": 85,
         "reason": "合同已在执行但审核状态不是审核通过", "evidence": "审核状态=审核中"},
    ],
}


class TopicSelectionTests(unittest.TestCase):
    def test_question_maps_to_topics(self):
        self.assertIn("违约", match_topics("这些合同有哪些违约风险？"))
        self.assertIn("付款", match_topics("付款账期是怎么安排的？"))
        self.assertIn("争议", match_topics("发生纠纷去哪里仲裁？"))

    def test_unrelated_question_matches_nothing(self):
        self.assertEqual(match_topics("今天天气怎么样"), [])

    def test_only_topic_relevant_clauses_are_selected(self):
        selected, _ = select_clauses(
            "违约责任是怎么约定的？",
            {"code:A": [LIABILITY, PAYMENT, DISPUTE]},
        )
        headings = [clause.heading for clause in selected]

        self.assertIn("违约责任", headings)
        self.assertNotIn("争议解决", headings)

    def test_selection_is_deterministic(self):
        # A conclusion nobody can reproduce cannot be defended when challenged.
        args = ("付款条件如何？", {"code:A": [LIABILITY, PAYMENT, DISPUTE]})
        first, _ = select_clauses(*args)
        second, _ = select_clauses(*args)

        self.assertEqual([c.index for c in first], [c.index for c in second])

    def test_per_contract_clause_cap_is_enforced(self):
        many = [Clause(i, "违约责任", "违约金" * 100) for i in range(1, 20)]
        selected, _ = select_clauses("违约金怎么算", {"code:A": many})

        self.assertLessEqual(len(selected), MAX_CLAUSES_PER_CONTRACT)


class RedactionGateTests(unittest.TestCase):
    def test_clause_still_holding_sensitive_data_is_dropped(self):
        # The gate checks the REDACTOR, not the source: anything that survives
        # redaction while still matching a pattern means a pattern was missed.
        leaked = [Clause(1, "账户", "银行账号 6222021234567890123")]
        selected, _ = select_clauses("付款账户", {"code:A": leaked})
        from app.contracts.chat_pipeline import SelectedClause

        kept, dropped = verify_clauses(
            [SelectedClause("code:A", 1, "账户", "银行账号 6222021234567890123")]
        )

        self.assertEqual(kept, [])
        self.assertEqual(dropped, 1)

    def test_clean_clause_survives_the_gate(self):
        from app.contracts.chat_pipeline import SelectedClause

        kept, dropped = verify_clauses([SelectedClause("code:A", 1, "违约", "违约金按万分之三计")])

        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, 0)

    def _with_broken_redactor(self, run):
        """Simulate the redactor missing a pattern.

        With a working redactor the gate correctly has nothing to drop, so the
        only way to exercise it is to make redaction fail - which is precisely
        the situation it exists to catch.
        """
        import app.contracts.chat_pipeline as pipeline

        original = pipeline.redact_text
        pipeline.redact_text = lambda value: value
        try:
            return run()
        finally:
            pipeline.redact_text = original

    def test_dropping_every_clause_is_announced(self):
        dirty = Clause(1, "账户", "付款账户 银行账号 6222021234567890123 " * 10)
        result = self._with_broken_redactor(
            lambda: run_pipeline("付款账户是什么", [ROW], {"code:ACME-C2026011": [dirty]})
        )

        self.assertIn(SIGNAL_ALL_DROPPED, result.signals)
        self.assertEqual(result.stats["redacted_dropped"], 1)
        self.assertEqual(result.payload["clauses"], [])

    def test_partial_drops_are_announced(self):
        dirty = Clause(1, "账户", "付款 银行账号 6222021234567890123 " * 10)
        result = self._with_broken_redactor(
            lambda: run_pipeline("付款怎么约定", [ROW], {"code:ACME-C2026011": [dirty, PAYMENT]})
        )

        self.assertIn(SIGNAL_SOME_DROPPED, result.signals)
        self.assertEqual(result.stats["redacted_dropped"], 1)

    def test_a_working_redactor_leaves_nothing_to_drop(self):
        # The normal path: redaction handles it, so the gate stays quiet.
        dirty = Clause(1, "账户", "付款账户 银行账号 6222021234567890123 " * 10)
        result = run_pipeline("付款账户是什么", [ROW], {"code:ACME-C2026011": [dirty]})

        self.assertEqual(result.stats["redacted_dropped"], 0)
        self.assertNotIn("6222021234567890123", str(result.payload))


class OutboundPayloadTests(unittest.TestCase):
    """What must never leave the machine."""

    def _payload(self):
        return run_pipeline(
            "违约责任如何约定", [ROW], {"code:ACME-C2026011": [LIABILITY]}
        ).payload

    def test_contract_name_never_leaves(self):
        # The name is a table field only. Payloads reach an external provider.
        self.assertNotIn("绝密合同名称", str(self._payload()))

    def test_free_text_columns_never_leave(self):
        blob = str(self._payload())
        for field_name in ("sign_user", "remark", "purpose", "contract_performance", "org_id"):
            self.assertNotIn(field_name, blob)

    def test_payload_has_only_the_three_allowed_sections(self):
        self.assertEqual(set(self._payload()), {"question", "contract_findings", "clauses"})

    def test_personal_data_in_clause_text_is_redacted_before_sending(self):
        clause = Clause(1, "联系", "违约责任 联系 13800138000 邮箱 a@b.com " * 8)
        payload = run_pipeline("违约责任", [ROW], {"code:ACME-C2026011": [clause]}).payload

        self.assertNotIn("13800138000", str(payload))
        self.assertNotIn("a@b.com", str(payload))

    def test_single_clause_is_truncated_and_reported(self):
        long_clause = Clause(1, "违约责任", "违约金" * 2000)
        result = run_pipeline("违约金", [ROW], {"code:ACME-C2026011": [long_clause]})

        for clause in result.payload["clauses"]:
            self.assertLessEqual(len(clause["text"]), MAX_CLAUSE_CHARS)
        self.assertIn(SIGNAL_TRUNCATED, result.signals)

    def test_total_clause_budget_is_enforced(self):
        clauses = [Clause(i, "违约责任", "违约金" * 300) for i in range(1, 40)]
        result = run_pipeline("违约金", [ROW], {"code:ACME-C2026011": clauses})
        total = sum(len(c["text"]) for c in result.payload["clauses"])

        self.assertLessEqual(total, MAX_TOTAL_CLAUSE_CHARS)

    def test_no_clauses_is_signalled(self):
        result = build_payload("问题", [ROW], [])

        self.assertIn(SIGNAL_NO_CLAUSES, result.signals)

    def test_question_is_length_bounded(self):
        payload = build_payload("问" * 5000, [ROW], []).payload

        self.assertLessEqual(len(payload["question"]), 2000)


class _Llm:
    def __init__(self, reply="", boom=False):
        self.reply, self.boom = reply, boom
        self.seen = None

    def complete(self, system, payload):
        if self.boom:
            raise RuntimeError("provider down")
        self.seen = payload
        return self.reply


class ContractChatServiceTests(unittest.TestCase):
    def _service(self, **kwargs):
        return ContractChatService(ledger_loader=lambda: [ROW], **kwargs)

    def test_without_an_llm_it_falls_back_and_says_so(self):
        answer = self._service().ask("违约风险如何", ["code:ACME-C2026011"])

        self.assertTrue(answer.fallback_used)
        self.assertIn(SIGNAL_LLM_UNAVAILABLE, answer.signals)
        self.assertIn("本地摘要", answer.answer)

    def test_provider_failure_does_not_fail_the_request(self):
        answer = self._service(llm_client=_Llm(boom=True)).ask("违约", ["code:ACME-C2026011"])

        self.assertTrue(answer.fallback_used)
        self.assertIn(SIGNAL_LLM_FAILED, answer.signals)
        self.assertTrue(answer.answer)

    def test_a_hallucinated_citation_is_flagged_not_hidden(self):
        llm = _Llm(reply="根据合同 ACME-C2099999 第九十九条，乙方须赔偿。")
        answer = self._service(llm_client=llm).ask("违约", ["code:ACME-C2026011"])

        self.assertIn("无法核实的引用", answer.answer)
        self.assertGreaterEqual(answer.clause_stats["unverified_refs"], 1)

    def test_a_grounded_answer_is_left_alone(self):
        llm = _Llm(reply="合同 ACME-C2026011 审核未通过却已在执行。")
        answer = self._service(llm_client=llm).ask("违约", ["code:ACME-C2026011"])

        self.assertNotIn("无法核实", answer.answer)
        self.assertFalse(answer.fallback_used)

    def test_unreadable_document_is_reported(self):
        service = ContractChatService(
            ledger_loader=lambda: [ROW],
            document_provider=lambda ref: ["no-such-file.pdf"],
        )
        answer = service.ask("违约", ["code:ACME-C2026011"])

        self.assertIn(SIGNAL_DOC_UNREADABLE, answer.signals)

    def test_document_provider_failure_does_not_fail_the_request(self):
        def boom(ref):
            raise RuntimeError("provider exploded")

        service = ContractChatService(ledger_loader=lambda: [ROW], document_provider=boom)
        answer = service.ask("违约", ["code:ACME-C2026011"])

        self.assertIn(SIGNAL_DOC_UNREADABLE, answer.signals)
        self.assertTrue(answer.answer)

    def test_empty_question_is_rejected(self):
        with self.assertRaises(ValueError):
            self._service().ask("   ", ["code:ACME-C2026011"])

    def test_unknown_contract_refs_select_nothing(self):
        answer = self._service().ask("违约", ["code:DOES-NOT-EXIST"])

        self.assertEqual(answer.clause_stats["contracts"], 0)

    def test_citations_are_server_built(self):
        service = ContractChatService(
            ledger_loader=lambda: [ROW], llm_client=_Llm(reply="见条款。")
        )
        answer = service.ask("违约", ["code:ACME-C2026011"])

        for citation in answer.citations:
            self.assertIn("code:ACME-C2026011", citation)

    def test_the_llm_never_sees_the_contract_name(self):
        llm = _Llm(reply="ok")
        self._service(llm_client=llm).ask("违约责任", ["code:ACME-C2026011"])

        self.assertIsNotNone(llm.seen)
        self.assertNotIn("绝密合同名称", str(llm.seen))


class InvariantTests(unittest.TestCase):
    def test_chat_cannot_change_rule_output(self):
        # The whole reason this boundary could be opened: asking a question
        # must not alter what the rule layer said about the contract.
        before = {"level": ROW["risk_level"], "score": ROW["risk_score"],
                  "findings": [dict(f) for f in ROW["findings"]]}

        ContractChatService(
            ledger_loader=lambda: [ROW], llm_client=_Llm(reply="风险等级应为低。")
        ).ask("这个合同风险高吗", ["code:ACME-C2026011"])

        self.assertEqual(ROW["risk_level"], before["level"])
        self.assertEqual(ROW["risk_score"], before["score"])
        self.assertEqual(ROW["findings"], before["findings"])


if __name__ == "__main__":
    unittest.main()


class CitationTests(unittest.TestCase):
    """Citations must identify distinct clauses, not repeat one reference."""

    def _service_with_two_docs(self, tmp):
        import zipfile
        from pathlib import Path

        def make(name, body):
            path = Path(tmp) / name
            doc = (
                '<?xml version="1.0"?><w:document '
                'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                + "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in body)
                + "</w:body></w:document>"
            )
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("word/document.xml", doc)
            return path

        cn = "一二三四五六七八九十"
        body = [f"{cn[i]}、违约责任约定，逾期付款按万分之三支付违约金并可解除合同。" * 3 for i in range(6)]
        paths = [make("a.docx", body), make("b.docx", body)]
        return ContractChatService(
            ledger_loader=lambda: [ROW], document_provider=lambda ref: paths
        )

    def test_clause_indices_do_not_collide_across_documents(self):
        # Two attachments on one contract used to restart numbering at 1, so
        # distinct clauses shared an index and citations pointed at nothing
        # distinguishable.
        #
        # Asserted on the INDICES, not on the citation list: citations are
        # deduplicated, so a length check there passes even when the collision
        # is present and proves nothing.
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            service = self._service_with_two_docs(tmp)
            by_contract, _signals, _strategies = service._load_clauses([ROW])

        indices = [clause.index for clause in by_contract["code:ACME-C2026011"]]

        self.assertGreater(len(indices), 1)
        self.assertEqual(len(indices), len(set(indices)), f"colliding indices: {indices}")

    def test_citations_are_deduplicated(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as tmp:
            answer = self._service_with_two_docs(tmp).ask("违约责任", ["code:ACME-C2026011"])

        self.assertEqual(len(answer.citations), len(set(answer.citations)))


class ChatPresentationParityTests(unittest.TestCase):
    """The contract chat must BE the project chat component, not resemble it."""

    def _js(self):
        from tests.frontend_assets import contracts_js

        return contracts_js()

    def test_it_reuses_the_project_chat_renderer(self):
        # Reuse rather than reimplementation is what stops the two drifting.
        js = self._js()

        self.assertIn("appendChatMessageToTarget", js)
        self.assertIn("typeChatText", js)

    def test_the_user_message_is_echoed_before_the_request(self):
        # The call takes seconds; waiting for the round trip to show what the
        # user typed makes the app feel broken.
        js = self._js()
        send = js[js.index("async function sendContractChat"):]
        send = send[: send.index("function renderChatSignals")]

        echo = send.index('appendContractChatMessage({role: "user"')
        request = send.index('api("/api/contracts/chat"')

        self.assertLess(echo, request, "echo must precede the request")

    def test_a_stale_reply_cannot_overwrite_a_newer_one(self):
        js = self._js()

        self.assertIn("contractChat.requestToken", js)
        self.assertIn("if (requestToken !== contractChat.requestToken) return;", js)

    def test_the_typewriter_can_be_cancelled(self):
        js = self._js()

        self.assertIn("cancelContractChatTypewriter", js)
        self.assertIn("clearTimeout", js)

    def test_double_send_is_prevented(self):
        self.assertIn("contractChat.sending", self._js())


class LedgerChatLauncherTests(unittest.TestCase):
    def _js(self):
        from tests.frontend_assets import contracts_js

        return contracts_js()

    def test_the_ledger_has_a_start_chat_button_like_the_project_page(self):
        from tests.frontend_assets import index_html

        html = index_html()

        self.assertIn('id="ledgerStartChatButton"', html)
        # Same wrapper class as the project page's launcher, so the two look
        # and sit the same rather than merely both existing.
        self.assertEqual(html.count('class="main-chat-launch"'), 2)

    def test_starting_a_chat_carries_the_current_filter(self):
        # Acting on what you are looking at, as the project launcher does.
        js = self._js()
        fn = js[js.index("function startChatFromLedger"):]
        fn = fn[: fn.index("\nfunction ")]

        self.assertIn("filterContractRows()", fn)
        self.assertIn("chatState.selected", fn)

    def test_the_redundant_header_entry_is_gone(self):
        from tests.frontend_assets import index_html

        self.assertNotIn("contractAiButton", index_html())


class VerifierPrecisionTests(unittest.TestCase):
    """The unverified-citation warning is the mechanism that makes an
    unauditable model's output checkable. Its value depends entirely on it
    being quiet when the answer is honest: a warning that fires on correct
    answers teaches the reader to skip the one that matters.

    Measured against the live model on 2026-08-14, where an answer that
    correctly said there was no data was stamped with a fabrication warning.
    """

    EMPTY = {"question": "q", "contract_findings": [], "clauses": []}

    def test_an_acronym_followed_by_chinese_is_not_a_contract_code(self):
        r"""`[A-Z]{2,}[-\w]{4,}` was the bare-code pattern, and `\w` matches
        Chinese, so every acronym this domain uses swallowed the sentence
        after it and was reported as a fabricated reference."""
        from app.contracts.chat_verify import verify_answer

        for answer in (
            "JSON中没有合同风险规则命中或条款片段内容。",
            "该 PDF文件无法解析。",
            "使用 OCR识别后仍无内容。",
            "LLM响应为空。",
            "CSV导出包含全部列。",
            "API返回了错误。",
            "见 UTF-8编码说明。",
        ):
            with self.subTest(answer=answer):
                result = verify_answer(answer, self.EMPTY)
                self.assertEqual([], result.unverified_codes)

    def test_references_the_source_actually_uses_are_still_checked(self):
        """Six of 65 live contracts carry references no "what a contract code
        looks like" pattern would accept - `code:1`, `code:a111`, `code:编号`,
        `code:0920`. Before this they could be fabricated freely, because the
        verifier could not see them at all."""
        from app.contracts.chat_verify import verify_answer

        payload = {
            "question": "q",
            "contract_findings": [{"contract_ref": "code:1725", "reason": "x"}],
            "clauses": [],
        }
        for answer, expected in (
            ("见 code:9999 的约定。", ["9999"]),
            ("见 code:编号 的说明。", ["编号"]),
            ("见 code:a111。", ["a111"]),
            ("见 code:0920。", ["0920"]),
            ("合同 ACME-C2099999 规定…", ["ACME-C2099999"]),
            ("合同 HT20990101999 规定…", ["HT20990101999"]),
            ("见 code:1725。", []),
        ):
            with self.subTest(answer=answer):
                self.assertEqual(expected, verify_answer(answer, payload).unverified_codes)

    def test_punctuation_is_not_swallowed_into_the_reference(self):
        """`code:1725，` must yield `1725`, not `1725，以及其他`, or a correct
        citation is reported as unverifiable because of the comma after it."""
        from app.contracts.chat_verify import verify_answer

        payload = {
            "question": "q",
            "contract_findings": [{"contract_ref": "code:1725"}],
            "clauses": [],
        }
        for answer in ("见 code:1725，以及其他。", "见 code:1725。", "（code:1725）", "见 code:1725、"):
            with self.subTest(answer=answer):
                self.assertEqual([], verify_answer(answer, payload).unverified_codes)


class EmptyClauseSignalTests(unittest.TestCase):
    """"We could not read any contract text" and "we read it and none of it is
    about what you asked" are different problems. Reporting both as
    未检索到合同正文 sent an operator hunting for attachments that were present,
    readable and already split."""

    FINDINGS = [{"contract_ref": "code:A", "risk_level": "高", "findings": []}]

    def test_text_that_matched_nothing_is_not_reported_as_missing_text(self):
        from app.contracts.chat_pipeline import SIGNAL_NO_TOPIC_MATCH

        clauses = {"code:A": [Clause(index=1, heading="一、总则", text="本合同为承诺函。")]}
        result = run_pipeline("付款条件是什么", self.FINDINGS, clauses)

        self.assertIn(SIGNAL_NO_TOPIC_MATCH, result.signals)
        self.assertNotIn(SIGNAL_NO_CLAUSES, result.signals)

    def test_genuinely_absent_text_still_reports_missing_text(self):
        from app.contracts.chat_pipeline import SIGNAL_NO_TOPIC_MATCH

        result = run_pipeline("付款条件是什么", self.FINDINGS, {"code:A": []})

        self.assertIn(SIGNAL_NO_CLAUSES, result.signals)
        self.assertNotIn(SIGNAL_NO_TOPIC_MATCH, result.signals)

    def test_clauses_lost_to_the_redaction_gate_keep_their_own_signal(self):
        """They DID match. Relabelling them as "nothing matched" would hide
        that the gate dropped content, which must never go unsaid.

        Redaction has to be broken to reach the gate at all - with a working
        redactor there is correctly nothing to drop - which is the same device
        `RedactionGateTests` uses.
        """
        import app.contracts.chat_pipeline as pipeline
        from app.contracts.chat_pipeline import SIGNAL_NO_TOPIC_MATCH

        dirty = Clause(1, "账户", "付款账户 银行账号 6222021234567890123 " * 10)
        original = pipeline.redact_text
        pipeline.redact_text = lambda value: value
        try:
            result = run_pipeline("付款条件是什么", self.FINDINGS, {"code:A": [dirty]})
        finally:
            pipeline.redact_text = original

        self.assertIn(SIGNAL_ALL_DROPPED, result.signals)
        self.assertIn(SIGNAL_NO_CLAUSES, result.signals)
        self.assertNotIn(SIGNAL_NO_TOPIC_MATCH, result.signals)

    def test_the_frontend_can_name_the_new_signal(self):
        from tests.frontend_assets import contracts_js

        self.assertIn("no_clause_matched_the_question:", contracts_js())
