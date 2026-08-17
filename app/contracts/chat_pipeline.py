"""Select, redact and gate contract clauses before anything leaves the machine.

Pipeline: clause_select -> redact -> verify (fail-closed) -> build_payload

Each stage is a plain function over plain data so it can be tested without an
LLM, a database or a network. The ordering is not arbitrary:

- Selection happens before redaction so that topic matching runs against the
  original wording. Redacting first would blank out terms the topic table is
  looking for.
- Verification happens after redaction, not before, because its job is to
  check the redactor's work rather than the source document.
- The payload is assembled last from an explicit allowlist, so a field added
  to an upstream dataclass cannot silently start being transmitted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .clause_split import Clause
from .text_redaction import contains_sensitive, redact_text

# Selection ceilings. Without them, "select the matching clauses" degrades into
# "send the whole contract", which contradicts the promise that only retrieved
# fragments leave the machine.
MAX_CLAUSES_PER_CONTRACT = 6
MAX_CLAUSE_CHARS = 800
MAX_TOTAL_CLAUSE_CHARS = 6000
MAX_CONTRACTS_PER_REQUEST = 20

SIGNAL_NO_CLAUSES = "no_clause_text_available"
# Distinct from the above on purpose. "We could not read any contract text" and
# "we read the text and nothing in it is about what you asked" are different
# problems with different responses: the first sends someone to check the
# attachments, the second means rephrasing the question. Reporting both as
# 未检索到合同正文 sent an operator looking for documents that were present,
# readable and already split - observed on 2026-08-14 asking three 承诺函 about
# 付款条件, where the splitter had produced clauses and none mentioned payment.
SIGNAL_NO_TOPIC_MATCH = "no_clause_matched_the_question"
SIGNAL_ALL_DROPPED = "all_clauses_dropped_by_redaction_gate"
SIGNAL_SOME_DROPPED = "some_clauses_dropped_by_redaction_gate"
SIGNAL_TRUNCATED = "clause_payload_truncated"
SIGNAL_TOPIC_FALLBACK = "clause_selection_fell_back_to_top_findings"
SIGNAL_CONTRACTS_TRUNCATED = "contract_list_truncated"

# Maps what a user asks about to the wording contracts actually use. Stated
# explicitly because "keyword matching" is otherwise an unfalsifiable claim.
TOPIC_TABLE: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("违约", ("违约", "赔偿", "罚则", "罚款"), ("违约责任", "违约金", "赔偿", "罚息")),
    ("付款", ("付款", "账期", "预付", "支付", "回款"), ("付款", "支付", "预付款", "进度款", "尾款", "质保金")),
    ("争议", ("争议", "仲裁", "诉讼", "管辖", "纠纷"), ("争议解决", "仲裁", "诉讼", "管辖", "适用法律")),
    ("质保", ("质保", "保修", "质量", "验收"), ("质保期", "保修", "质量标准", "验收")),
    ("交付", ("交货", "交付", "工期", "运输", "物流"), ("交货", "交付", "生产周期", "贸易术语", "风险转移")),
    ("不可抗力", ("不可抗力", "疫情", "战争", "天灾"), ("不可抗力",)),
    ("知识产权", ("知识产权", "专利", "著作权", "商标"), ("知识产权", "专利", "著作权")),
    ("保密", ("保密", "泄密", "机密"), ("保密", "机密")),
    ("解除", ("解除", "终止", "退出"), ("解除合同", "终止合同", "合同解除")),
)


@dataclass
class SelectedClause:
    contract_ref: str
    index: int
    heading: str
    text: str
    topic: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "contract_ref": self.contract_ref,
            "clause_index": self.index,
            "heading": self.heading,
            "text": self.text,
        }


@dataclass
class PipelineResult:
    payload: dict[str, Any] = field(default_factory=dict)
    signals: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


def match_topics(question: str) -> list[str]:
    """Which topics the question is asking about."""
    text = str(question or "")
    return [topic for topic, triggers, _ in TOPIC_TABLE if any(t in text for t in triggers)]


def select_clauses(
    question: str,
    clauses_by_contract: dict[str, list[Clause]],
    *,
    top_findings: dict[str, list[str]] | None = None,
) -> tuple[list[SelectedClause], list[str]]:
    """Pick the clauses worth sending. Deterministic: no vector search.

    Deterministic selection matters more than recall here. The same question
    must select the same clauses every time, or a finding cannot be reproduced
    when someone challenges it.
    """
    signals: list[str] = []
    topics = match_topics(question)
    wanted: list[tuple[str, ...]] = [terms for topic, _, terms in TOPIC_TABLE if topic in topics]

    selected: list[SelectedClause] = []
    for contract_ref, clauses in clauses_by_contract.items():
        scored: list[tuple[int, Clause, str]] = []
        for clause in clauses:
            best_topic, hits = "", 0
            for topic, _, terms in TOPIC_TABLE:
                if topic not in topics:
                    continue
                count = sum(clause.text.count(term) for term in terms)
                if count > hits:
                    best_topic, hits = topic, count
            if hits:
                scored.append((hits, clause, best_topic))

        if not scored and wanted == []:
            # No topic matched the question at all. Rather than sending
            # nothing, fall back to the clauses behind this contract's most
            # severe findings - and say that is what happened, so the answer
            # is not read as a targeted search result.
            keywords = (top_findings or {}).get(contract_ref) or []
            for clause in clauses:
                if any(word and word in clause.text for word in keywords):
                    scored.append((1, clause, "fallback"))
            if scored and SIGNAL_TOPIC_FALLBACK not in signals:
                signals.append(SIGNAL_TOPIC_FALLBACK)

        scored.sort(key=lambda row: (-row[0], row[1].index))
        for hits, clause, topic in scored[:MAX_CLAUSES_PER_CONTRACT]:
            selected.append(
                SelectedClause(
                    contract_ref=contract_ref,
                    index=clause.index,
                    heading=clause.heading,
                    text=clause.text,
                    topic=topic,
                )
            )
    return selected, signals


def redact_clauses(clauses: list[SelectedClause]) -> list[SelectedClause]:
    return [
        SelectedClause(
            contract_ref=clause.contract_ref,
            index=clause.index,
            heading=redact_text(clause.heading),
            text=redact_text(clause.text),
            topic=clause.topic,
        )
        for clause in clauses
    ]


def verify_clauses(clauses: list[SelectedClause]) -> tuple[list[SelectedClause], int]:
    """Fail-closed gate: drop anything still matching a sensitive pattern.

    Run AFTER redaction, so this checks the redactor rather than the source.
    A clause that survives redaction while still looking like a bank account
    means a pattern was missed, and the safe response is to not send it.
    """
    kept, dropped = [], 0
    for clause in clauses:
        if contains_sensitive(clause.text) or contains_sensitive(clause.heading):
            dropped += 1
            continue
        kept.append(clause)
    return kept, dropped


def build_payload(
    question: str,
    contract_findings: list[dict[str, Any]],
    clauses: list[SelectedClause],
) -> PipelineResult:
    """Assemble the outbound payload from an explicit allowlist.

    Fields are copied by name. Nothing is passed through wholesale, so adding
    a column upstream cannot start transmitting it by accident.
    """
    result = PipelineResult()
    signals: list[str] = []

    findings = contract_findings[:MAX_CONTRACTS_PER_REQUEST]
    if len(contract_findings) > MAX_CONTRACTS_PER_REQUEST:
        signals.append(SIGNAL_CONTRACTS_TRUNCATED)

    safe_findings = [
        {
            "contract_ref": str(item.get("contract_ref") or ""),
            "risk_level": str(item.get("risk_level") or ""),
            "risk_score": item.get("risk_score"),
            "approval_status": str(item.get("approval_status") or ""),
            "execution_status": str(item.get("execution_status") or ""),
            "total_amount": str(item.get("total_amount") or ""),
            "end_date": str(item.get("end_date") or ""),
            "findings": [
                {
                    "rule": str(finding.get("rule") or ""),
                    "severity": finding.get("severity"),
                    "reason": str(finding.get("reason") or ""),
                    "evidence": str(finding.get("evidence") or ""),
                }
                for finding in (item.get("findings") or [])
            ],
        }
        for item in findings
    ]

    safe_clauses: list[dict[str, Any]] = []
    running = 0
    truncated = False
    for clause in clauses:
        text = clause.text[:MAX_CLAUSE_CHARS]
        if len(clause.text) > MAX_CLAUSE_CHARS:
            truncated = True
        if running + len(text) > MAX_TOTAL_CLAUSE_CHARS:
            truncated = True
            break
        running += len(text)
        entry = clause.to_payload()
        entry["text"] = text
        safe_clauses.append(entry)
    if truncated:
        signals.append(SIGNAL_TRUNCATED)
    if not safe_clauses:
        signals.append(SIGNAL_NO_CLAUSES)

    result.payload = {
        "question": str(question or "")[:2000],
        "contract_findings": safe_findings,
        "clauses": safe_clauses,
    }
    result.signals = signals
    result.stats = {
        "contracts": len(safe_findings),
        "clauses_sent": len(safe_clauses),
        "clause_chars": running,
        "truncated": truncated,
    }
    return result


def run_pipeline(
    question: str,
    contract_findings: list[dict[str, Any]],
    clauses_by_contract: dict[str, list[Clause]],
    *,
    top_findings: dict[str, list[str]] | None = None,
) -> PipelineResult:
    """Full select -> redact -> verify -> build path."""
    selected, signals = select_clauses(
        question, clauses_by_contract, top_findings=top_findings
    )
    redacted = redact_clauses(selected)
    kept, dropped = verify_clauses(redacted)

    if dropped and kept:
        signals.append(SIGNAL_SOME_DROPPED)
    elif dropped and not kept:
        # Never silent. "No mention of liability" would otherwise be read as
        # "there is no liability clause".
        signals.append(SIGNAL_ALL_DROPPED)

    result = build_payload(question, contract_findings, kept)
    result.signals = _dedupe(signals + result.signals)

    # `build_payload` cannot tell the two empty-clause cases apart - it only
    # sees that nothing arrived. Here we still have the input, so correct the
    # signal when text existed and simply did not match. Not when clauses were
    # dropped by the redaction gate: they DID match, and SIGNAL_ALL_DROPPED
    # already states the more specific truth.
    if (
        SIGNAL_NO_CLAUSES in result.signals
        and not dropped
        and any(clauses for clauses in clauses_by_contract.values())
    ):
        result.signals = [
            SIGNAL_NO_TOPIC_MATCH if signal == SIGNAL_NO_CLAUSES else signal
            for signal in result.signals
        ]

    result.stats["selected"] = len(selected)
    result.stats["redacted_dropped"] = dropped
    return result


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered
