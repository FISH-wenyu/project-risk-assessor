"""Verify that an LLM answer only cites things we actually sent it.

Why this exists: the boundary "no LLM contract analysis" was held because an
LLM is not auditable. Saying "the LLM does not score" is necessary but not
sufficient - it does not stop the model inventing a clause number, or
attributing a term to a contract that was never in the payload.

This module does not make the model auditable. It makes its OUTPUT verifiable,
which is the part that actually reaches a reader.

Severity is deliberately split:

- Contract codes and clause references are checked STRICTLY. They are pure
  lookups; a code we never sent is unambiguously fabricated.
- Amounts are checked SOFTLY. A model asked to compare several contracts may
  legitimately produce a sum or a difference that appears in no single clause,
  so treating a novel number as fabrication would cry wolf constantly.

Findings annotate rather than delete. Cutting a citation out of the middle of
a sentence leaves prose that reads as authoritative but is now missing its
qualifier, which is harder to judge than the original.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

SIGNAL_UNVERIFIED_REFS = "answer_contains_unverified_references"
SIGNAL_UNVERIFIED_AMOUNTS = "answer_contains_unmatched_amounts"

# Contract references, in the two shapes an answer can carry them.
#
# 1. `code:XXXX`, exactly as the payload emits every reference. Anything after
#    the prefix counts, because the source contains references this module has
#    no business second-guessing: `code:1`, `code:a111`, `code:编号`, `code:0920`.
#    Six of 65 live contracts are of a form no pattern of "what a contract code
#    looks like" would accept, and before this they could be fabricated freely -
#    the verifier simply could not see them.
# 2. A bare formal code with no prefix, for the two families actually in use.
#
# The bare-code branch is ASCII-only, and that is the whole point of writing it
# out instead of `\w`. `\w` matches Chinese, so `[A-Z]{2,}[-\w]{4,}` read
# "JSON中没有合同风险规则命中或条款片段内容" as one contract code and stamped an
# honest "there is no data" answer with a fabrication warning. Measured on
# 2026-08-14: JSON, PDF, OCR, LLM, CSV and API all did it - every acronym this
# domain uses, followed by any Chinese. A warning that fires on correct answers
# is worse than no warning, because it teaches the reader to skip the one that
# matters.
_CODE_RE = re.compile(
    r"code:(?P<prefixed>[^\s，。；：、（）()\[\]【】\"'`]+)"
    r"|(?<![A-Za-z0-9_-])(?P<bare>ACME[-A-Za-z0-9_]*|HT\d{6,})(?![A-Za-z0-9_-])"
)
# Clause references in any of the marker families the splitter can produce.
_CLAUSE_RE = re.compile(r"第[一二三四五六七八九十百零〇\d]{1,6}[条章]")
# A money token needs a marker, but the marker may be a leading ¥ OR a
# trailing unit - contracts use both, often in the same document. Requiring the
# trailing unit alone silently matched nothing in `¥45,000,000.00`, so novel
# amounts went unreported.
_AMOUNT_RE = re.compile(
    r"(?:(?P<prefix>[¥￥])\s*)?"
    r"(?P<number>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<unit>万元|亿元|万|亿|元)?"
)


def _is_money(match: re.Match[str]) -> bool:
    """Only count a number as money when it is explicitly marked as such.

    Without this, dates, quantities and clause numbers all parse as amounts -
    the same false-positive class already measured in `text_money`.
    """
    return bool(match.group("prefix") or match.group("unit"))


@dataclass
class VerificationResult:
    unverified_codes: list[str] = field(default_factory=list)
    unverified_clauses: list[str] = field(default_factory=list)
    unmatched_amounts: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)

    @property
    def has_hard_failures(self) -> bool:
        return bool(self.unverified_codes or self.unverified_clauses)

    def to_dict(self) -> dict[str, Any]:
        return {
            "unverified_codes": list(self.unverified_codes),
            "unverified_clauses": list(self.unverified_clauses),
            "unmatched_amounts": list(self.unmatched_amounts),
            "unverified_refs": len(self.unverified_codes) + len(self.unverified_clauses),
            "signals": list(self.signals),
        }


def verify_answer(answer: str, payload: dict[str, Any]) -> VerificationResult:
    """Check every citation in `answer` against the payload actually sent."""
    result = VerificationResult()
    if not answer or not answer.strip():
        return result

    known_codes = _known_codes(payload)
    known_clauses = _known_clauses(payload)
    known_amounts = _known_amounts(payload)

    for match in _CODE_RE.finditer(answer):
        code = (match.group("prefixed") or match.group("bare") or "").strip()
        if not code:
            continue
        if _normalize(code) not in known_codes and code not in result.unverified_codes:
            result.unverified_codes.append(code)

    for match in _CLAUSE_RE.finditer(answer):
        clause = match.group(0)
        if clause not in known_clauses and clause not in result.unverified_clauses:
            result.unverified_clauses.append(clause)

    for match in _AMOUNT_RE.finditer(answer):
        if not _is_money(match):
            continue
        value = _to_yuan(match.group("number"), match.group("unit") or "")
        if value is None:
            continue
        if not any(abs(value - known) <= Decimal("1") for known in known_amounts):
            text = match.group(0).strip()
            if text not in result.unmatched_amounts:
                result.unmatched_amounts.append(text)

    if result.has_hard_failures:
        result.signals.append(SIGNAL_UNVERIFIED_REFS)
    if result.unmatched_amounts:
        result.signals.append(SIGNAL_UNVERIFIED_AMOUNTS)
    return result


def annotate_answer(answer: str, result: VerificationResult) -> str:
    """Append a warning. The answer body is never edited.

    Excising a citation mid-sentence produces text that still reads as
    confident while quietly losing what qualified it.
    """
    if not result.has_hard_failures and not result.unmatched_amounts:
        return answer
    parts = []
    if result.unverified_codes:
        parts.append("合同编号 " + "、".join(result.unverified_codes[:5]))
    if result.unverified_clauses:
        parts.append("条款 " + "、".join(result.unverified_clauses[:5]))
    warning = ""
    if parts:
        warning = (
            f"\n\n⚠️ 本回答含 {len(result.unverified_codes) + len(result.unverified_clauses)} "
            f"处无法核实的引用（{'；'.join(parts)}）。这些内容未出现在提供给模型的数据中，"
            "请人工核对后再使用。"
        )
    if result.unmatched_amounts:
        warning += (
            f"\n\n注：回答中的金额 {'、'.join(result.unmatched_amounts[:5])} "
            "未直接出现在所提供的数据中，可能是模型的合计或换算，请自行核对。"
        )
    return answer + warning


def _known_codes(payload: dict[str, Any]) -> set[str]:
    codes: set[str] = set()
    for finding in payload.get("contract_findings") or []:
        ref = str(finding.get("contract_ref") or "")
        codes.add(_normalize(ref.split(":", 1)[-1]))
    for clause in payload.get("clauses") or []:
        ref = str(clause.get("contract_ref") or "")
        codes.add(_normalize(ref.split(":", 1)[-1]))
    codes.discard("")
    return codes


def _known_clauses(payload: dict[str, Any]) -> set[str]:
    clauses: set[str] = set()
    for clause in payload.get("clauses") or []:
        for field_name in ("heading", "text"):
            for match in _CLAUSE_RE.finditer(str(clause.get(field_name) or "")):
                clauses.add(match.group(0))
    return clauses


def _known_amounts(payload: dict[str, Any]) -> set[Decimal]:
    amounts: set[Decimal] = set()
    blob_parts: list[str] = []
    for finding in payload.get("contract_findings") or []:
        blob_parts.append(str(finding.get("total_amount") or ""))
        blob_parts.append(str(finding.get("evidence") or ""))
    for clause in payload.get("clauses") or []:
        blob_parts.append(str(clause.get("text") or ""))
    for part in blob_parts:
        for match in _AMOUNT_RE.finditer(part):
            if not _is_money(match):
                continue
            value = _to_yuan(match.group("number"), match.group("unit") or "")
            if value is not None:
                amounts.add(value)
        # Bare decimals such as a stored total_amount of "15360000.00".
        for bare in re.finditer(r"^\s*(\d+(?:\.\d+)?)\s*$", part):
            try:
                amounts.add(Decimal(bare.group(1)))
            except InvalidOperation:
                continue
    return amounts


def _to_yuan(number: str, unit: str) -> Decimal | None:
    try:
        value = Decimal(number.replace(",", ""))
    except InvalidOperation:
        return None
    if unit.startswith("万"):
        return value * 10**4
    if unit.startswith("亿"):
        return value * 10**8
    return value


def _normalize(value: str) -> str:
    """Compare identifiers without separators.

    Extraction drops hyphens, so `ACME-C2026011` and `ACMEC2026011` are the
    same contract and must not be reported as a fabricated citation.
    """
    return re.sub(r"[\s\-‐-―_/.]", "", value).upper()
