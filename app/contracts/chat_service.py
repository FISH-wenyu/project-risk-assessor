"""Contract chat: rules first, LLM on top, output verified.

The invariant this service exists to protect:

    The LLM sits ON TOP of the rule layer and never replaces it.

The same contract must produce the same risk level and the same findings
whether or not anyone opens a chat. This service reads rule output; it never
writes it, and nothing here can change a score.

A separate service from `ChatService` on purpose. That one is keyed on
`project_id` all the way through storage, memory and citations, works, and is
covered by tests. Contracts are a different subject with different sanitisation
duties, so reshaping a working path to fit them would risk the project chat for
no gain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .chat_pipeline import PipelineResult, run_pipeline
from .chat_verify import annotate_answer, verify_answer
from .clause_split import Clause, split_clauses
from .text_extraction import SIGNAL_SPARSE_TEXT_LAYER, extract_document

SIGNAL_LLM_UNAVAILABLE = "llm_not_configured"
SIGNAL_LLM_FAILED = "llm_call_failed"
SIGNAL_DOC_UNREADABLE = "contract_document_unreadable"

MAX_QUESTION_CHARS = 2000

SYSTEM_PROMPT = (
    "你是合同风险分析助手。严格依据提供的数据回答，不得编造合同编号、条款或金额。"
    "引用时使用给定的合同编号与条款号。"
    "你不得给出或修改风险等级与分数——风险判定由规则引擎负责，你只做解释、归纳与比较。"
    "如果数据不足以回答，请直接说明数据不足。"
)


@dataclass
class ContractChatAnswer:
    answer: str = ""
    citations: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    clause_stats: dict[str, Any] = field(default_factory=dict)
    fallback_used: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "citations": list(self.citations),
            "signals": list(self.signals),
            "clause_stats": dict(self.clause_stats),
            "fallback_used": self.fallback_used,
        }


class ContractChatService:
    def __init__(self, ledger_loader, document_provider=None, llm_client=None):
        # All three are injected so the service is testable with no database,
        # no filesystem and no network.
        self.ledger_loader = ledger_loader
        self.document_provider = document_provider
        self.llm_client = llm_client

    def ask(self, question: str, contract_refs: list[str]) -> ContractChatAnswer:
        clean_question = str(question or "").strip()[:MAX_QUESTION_CHARS]
        if not clean_question:
            raise ValueError("question is required")

        wanted = [str(ref).strip() for ref in (contract_refs or []) if str(ref).strip()]
        rows = [row for row in self.ledger_loader() if row.get("contract_ref") in wanted]

        clauses_by_contract, doc_signals, split_strategies = self._load_clauses(rows)
        top_findings = {
            row["contract_ref"]: [
                str(f.get("reason") or "")[:8] for f in (row.get("findings") or [])
            ]
            for row in rows
        }

        pipeline = run_pipeline(
            clean_question, rows, clauses_by_contract, top_findings=top_findings
        )
        answer = self._answer(clean_question, pipeline)

        # Verify AFTER generation, against the payload we actually sent.
        verification = verify_answer(answer.answer, pipeline.payload)
        answer.answer = annotate_answer(answer.answer, verification)
        answer.signals = _dedupe(answer.signals + pipeline.signals + doc_signals + verification.signals)
        answer.clause_stats = {
            **pipeline.stats,
            **verification.to_dict(),
            "split_strategies": split_strategies,
        }
        # Server-built, and deduplicated: a citation list that repeats the same
        # reference tells the reader nothing about how much was consulted.
        answer.citations = _dedupe(
            [
                f"{clause['contract_ref']} 第{clause['clause_index']}段"
                for clause in pipeline.payload.get("clauses", [])
            ]
        )
        return answer

    def _load_clauses(
        self, rows: list[dict[str, Any]]
    ) -> tuple[dict[str, list[Clause]], list[str], list[str]]:
        """Extract and split whatever local documents exist for these contracts."""
        by_contract: dict[str, list[Clause]] = {}
        signals: list[str] = []
        strategies: list[str] = []
        if self.document_provider is None:
            return by_contract, signals, strategies

        for row in rows:
            ref = row.get("contract_ref")
            try:
                paths = list(self.document_provider(ref) or [])
            except Exception:
                signals.append(SIGNAL_DOC_UNREADABLE)
                continue
            for path in paths:
                extracted = extract_document(path)
                if not extracted.usable:
                    # Never silent: an unread document is not a clean one.
                    signals.append(SIGNAL_DOC_UNREADABLE)
                    signals.extend(extracted.signals)
                    continue
                if SIGNAL_SPARSE_TEXT_LAYER in extracted.signals:
                    signals.append(SIGNAL_SPARSE_TEXT_LAYER)
                split = split_clauses(extracted.text)
                strategies.append(split.strategy)
                signals.extend(split.signals)
                # Renumber across documents. The splitter numbers from 1 per
                # document, so a contract with several attachments produced
                # repeated indices and therefore citations like "第3段" three
                # times, which point at nothing distinguishable.
                existing = by_contract.setdefault(ref, [])
                offset = len(existing)
                existing.extend(
                    Clause(offset + position, clause.heading, clause.text)
                    for position, clause in enumerate(split.clauses, start=1)
                )
        # Returned separately rather than stuffed into by_contract under a
        # magic key: that dict is iterated as contract -> clauses, so a list of
        # strings in it would blow up clause selection.
        return by_contract, _dedupe(signals), _dedupe(strategies)

    def _answer(self, question: str, pipeline: PipelineResult) -> ContractChatAnswer:
        if self.llm_client is None:
            return ContractChatAnswer(
                answer=_local_summary(question, pipeline.payload),
                signals=[SIGNAL_LLM_UNAVAILABLE],
                fallback_used=True,
            )
        try:
            text = self.llm_client.complete(SYSTEM_PROMPT, pipeline.payload)
        except Exception:
            # A provider outage must not fail the request; it degrades to the
            # deterministic summary, which is still useful.
            return ContractChatAnswer(
                answer=_local_summary(question, pipeline.payload),
                signals=[SIGNAL_LLM_FAILED],
                fallback_used=True,
            )
        return ContractChatAnswer(answer=str(text or "").strip(), fallback_used=False)


def _local_summary(question: str, payload: dict[str, Any]) -> str:
    """Deterministic fallback built only from rule output.

    Deliberately plain. It must never look like a model answer, or a reader
    cannot tell that the LLM was unavailable.
    """
    findings = payload.get("contract_findings") or []
    if not findings:
        return "未选择任何合同，或所选合同没有可用的规则评估结果。"
    lines = [f"（本地摘要，未调用大模型）共 {len(findings)} 份合同："]
    for item in findings[:10]:
        hits = item.get("findings") or []
        reasons = "；".join(str(f.get("reason") or "") for f in hits[:3]) or "未命中规则"
        lines.append(
            f"- {item.get('contract_ref')} {item.get('risk_level')}"
            f"({item.get('risk_score')})：{reasons}"
        )
    clauses = payload.get("clauses") or []
    lines.append(f"已检索到 {len(clauses)} 条相关条款正文。" if clauses else "未检索到条款正文。")
    return "\n".join(lines)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered
