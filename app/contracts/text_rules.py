"""Deterministic risk rules over extracted contract text.

Design constraints, all of them load-bearing:

1. NO LLM. Every rule here is a deterministic condition with a citable reason.
   An LLM cannot be regression-tested and cannot be audited after the fact.
   LLM assistance, if ever added, belongs strictly on top of this layer and
   must never replace it.

2. A rule that cannot be evaluated emits a SIGNAL, never silence. "Not checked"
   and "checked and clean" must stay distinguishable. This project has twice
   shipped a silent gap that read as an all-clear.

3. Findings quote only SHORT, BOUNDED evidence spans, never whole clauses, and
   never party names, addresses or contact details.

4. Identifier comparison is normalised before comparing. The 2026-08-12 samples
   proved extraction drops hyphens: `TB/T 2817-2020` came out `TB/T 28172020`
   and `BRI-LW-20260812` came out `BRILW20260812`. Strict equality would report
   a mismatch on two documents that actually agree.

Thresholds that encode a judgement rather than a fact are named constants and
named constants rather than inline literals, so they can be argued
with and tuned, rather than being buried in a comparison.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .text_money import (
    find_amounts_in_yuan,
    find_chinese_amounts_in_yuan,
    find_percentages,
)
from .text_redaction import redact_text

TEXT_RULE_VERSION = "contract-text-v1"

# --- Tunable thresholds (judgement, not fact) -------------------------------
# Advance payment above this share of the total puts the buyer's cash at risk
# before anything is delivered. 30% is a common commercial review trigger; the
# 2026-08-12 sample uses 20%, which is comfortably normal.
HIGH_ADVANCE_RATIO = Decimal("0.30")
# Amounts are compared with a tolerance because contracts round. One yuan is
# tight enough to catch a real discrepancy and loose enough to ignore rounding.
AMOUNT_TOLERANCE_YUAN = Decimal("1")
# Percentage splits should sum to 100. Allow a small rounding band.
PERCENT_TOLERANCE = Decimal("0.5")
# Characters scanned either side of a `%` to decide what that percentage means.
# Asymmetric on purpose, from a measured failure: with one wide window, the
# `10% 到货验收质保尾款` tranche sat close enough to a following `税种：13%` to be
# excluded as a tax rate. The disqualifying noun is always adjacent to its own
# number ("税率 13%", "8% 违约金"), so exclusion uses a tight window while the
# tranche noun may sit slightly further out.
PERCENT_EXCLUDE_CHARS = 6
PERCENT_CONTEXT_CHARS = 16
# Evidence spans are capped so a finding can never become a content leak.
MAX_EVIDENCE_CHARS = 60

# --- Signals ----------------------------------------------------------------
SIGNAL_TEXT_UNUSABLE = "contract_text_unusable"
SIGNAL_NO_TOTAL_FOUND = "contract_total_not_found_in_text"
SIGNAL_NO_METADATA_TOTAL = "metadata_total_unavailable"
SIGNAL_DETERMINISTIC_ONLY = "deterministic_rules_only_no_llm"
SIGNAL_CLAUSE_SCAN_KEYWORD_BASED = "clause_presence_is_keyword_based"
SIGNAL_NOT_A_CONTRACT_BODY = "document_is_not_a_contract_body"

# --- Clause presence checks -------------------------------------------------
# Keyword presence is a WEAK signal: it proves a heading exists, not that the
# clause is adequate. Absence is the stronger direction, which is why these are
# only ever used to report a MISSING clause.
REQUIRED_CLAUSES: tuple[tuple[str, str, tuple[str, ...], int], ...] = (
    ("missing_liability_clause", "缺少违约责任条款", ("违约责任", "违约金", "赔偿责任"), 70),
    ("missing_dispute_clause", "缺少争议解决条款", ("争议解决", "仲裁", "诉讼", "管辖法院"), 70),
    ("missing_governing_law", "缺少法律适用条款", ("适用法律", "适用中华人民共和国法律", "准据法"), 60),
    ("missing_force_majeure", "缺少不可抗力条款", ("不可抗力",), 45),
    ("missing_warranty_clause", "缺少质量保证条款", ("质保", "保修", "质量保证"), 50),
    ("missing_payment_terms", "缺少付款条款", ("付款", "支付"), 75),
    ("missing_delivery_terms", "缺少交付条款", ("交货", "交付", "供货"), 65),
    ("missing_confidentiality", "缺少保密条款", ("保密",), 35),
    ("missing_ip_clause", "缺少知识产权条款", ("知识产权", "专利", "著作权"), 40),
    ("missing_termination_clause", "缺少解除/终止条款", ("解除合同", "终止合同", "合同解除"), 55),
)

# Blank-line fills used by unsigned Chinese contracts: ___, ＿＿, ……, or spaces
# between a label and the line end.
_BLANK_FILL_RE = re.compile(r"[_＿]{3,}|[…]{3,}")

# Signature block labels. If these exist but are followed by blanks, the
# contract is unexecuted.
_SIGNATURE_LABELS = ("盖章", "签字", "签署", "授权代表")
# Clause that makes signing a precondition of effectiveness.
_EFFECTIVE_ON_SIGNATURE = ("签字", "盖章", "公章")
_EFFECTIVENESS_MARKERS = ("生效", "效力")

# Words that mark a percentage as a payment tranche, and words that rule it
# out. The exclusion list is not optional: penalty rates, tax rates and
# interest rates all appear as percentages in the same document.
_TRANCHE_TOKENS = (
    "预付", "首付", "进度款", "进度", "尾款", "验收", "质保金", "留质",
    "期款", "到货", "分期", "付款", "支付",
)
_NON_TRANCHE_TOKENS = ("违约", "税", "利率", "罚", "滞纳", "汇率", "折扣")
_ADVANCE_TOKENS = ("预付", "首付")

# Contract-shape markers. Clause-presence rules only make sense on something
# that is actually a contract: the 2026-08-12 PDF is a project description, and
# reporting "缺少违约责任条款" against it is a category error, not a finding.
#
# Each group must be evidence that the document IS a contract, not that it
# MENTIONS one. "双方签署采购合同原件" appears in the 2026-08-12 project file as a
# deliverable and wrongly qualified it, so self-referential phrasing only.
_CONTRACT_SHAPE_MARKERS = (
    ("甲方（", "乙方（", "甲方:", "甲方：", "乙方:", "乙方："),
    ("第一条", "第二条", "第三条"),
    ("本合同", "本协议"),
)
# How many of the marker groups must appear for the document to be treated as
# a contract body.
MIN_CONTRACT_SHAPE_GROUPS = 2

# Party identity fields that must not be blank on a real contract.
_PARTY_IDENTITY_LABELS = (
    "统一社会信用代码",
    "法定代表人",
    "注册地址",
    "开户银行",
    "银行账号",
    "纳税人识别号",
)


@dataclass
class TextFinding:
    rule: str
    reason: str
    severity: int
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "reason": self.reason,
            "severity": self.severity,
            "evidence": self.evidence,
        }


@dataclass
class TextAnalysisResult:
    findings: list[TextFinding]
    signals: list[str]
    rule_version: str = TEXT_RULE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_version": self.rule_version,
            "findings": [finding.to_dict() for finding in self.findings],
            "signals": list(self.signals),
            "finding_count": len(self.findings),
        }


def normalize_identifier(value: str) -> str:
    """Strip separators so extraction artefacts do not read as real mismatches.

    `TB/T 2817-2020` and `TB/T 28172020` must compare equal; the difference is
    a lost hyphen from the DOCX reader, not a discrepancy between documents.
    """
    return re.sub(r"[\s\-‐-―_/.、,，]", "", value).upper()


def looks_like_contract(text: str) -> bool:
    """Cheap structural check that this is a contract body, not a project file.

    Deliberately structural rather than semantic: a title can say anything, but
    a contract almost always names 甲方/乙方 and numbers its clauses.
    """
    matched = sum(
        1
        for group in _CONTRACT_SHAPE_MARKERS
        if any(marker in text for marker in group)
    )
    return matched >= MIN_CONTRACT_SHAPE_GROUPS


def analyze_contract_text(
    text: str,
    *,
    metadata_total_yuan: Decimal | None = None,
    text_usable: bool = True,
) -> TextAnalysisResult:
    """Run every deterministic text rule and report what could not be checked."""
    signals: list[str] = [SIGNAL_DETERMINISTIC_ONLY]

    if not text_usable or not text.strip():
        # Refuse to score an unreadable document. Returning zero findings here
        # would be indistinguishable from a clean contract.
        signals.append(SIGNAL_TEXT_UNUSABLE)
        return TextAnalysisResult(findings=[], signals=signals)

    findings: list[TextFinding] = []
    findings.extend(_check_signature_block(text))
    findings.extend(_check_party_identity(text))
    if looks_like_contract(text):
        findings.extend(_check_missing_clauses(text, signals))
    else:
        # Amount and payment checks still apply to a project file, but "this
        # document lacks a liability clause" is meaningless for one.
        signals.append(SIGNAL_NOT_A_CONTRACT_BODY)
    findings.extend(_check_amount_consistency(text, signals))
    findings.extend(_check_payment_schedule(text))
    findings.extend(_check_metadata_agreement(text, metadata_total_yuan, signals))

    findings = [_sanitize_finding(finding) for finding in findings]
    findings.sort(key=lambda finding: (-finding.severity, finding.rule))
    return TextAnalysisResult(findings=findings, signals=signals)


def _sanitize_finding(finding: TextFinding) -> TextFinding:
    """Redact, then bound. Both, in that order.

    Applied centrally to every finding rather than inside each rule: a rule
    added later would otherwise have to remember to redact, and the one that
    forgets is the one that leaks. Redaction runs before clipping because
    truncating mid-way through a bank account leaves a partial number the
    patterns no longer match but a reader could still piece together.
    """
    evidence = " ".join(redact_text(finding.evidence).split())
    if len(evidence) > MAX_EVIDENCE_CHARS:
        evidence = evidence[:MAX_EVIDENCE_CHARS] + "…"
    return TextFinding(
        rule=finding.rule,
        reason=redact_text(finding.reason),
        severity=finding.severity,
        evidence=evidence,
    )


def _check_signature_block(text: str) -> list[TextFinding]:
    """An unexecuted contract that the system treats as live is a top risk."""
    findings: list[TextFinding] = []
    signature_lines = [
        line
        for line in text.splitlines()
        if any(label in line for label in _SIGNATURE_LABELS)
    ]
    blank_signature_lines = [
        line for line in signature_lines if _BLANK_FILL_RE.search(line)
    ]
    if not blank_signature_lines:
        return findings

    # Does the contract itself make signature a condition of effectiveness?
    conditioned = any(
        any(marker in line for marker in _EFFECTIVENESS_MARKERS)
        and any(token in line for token in _EFFECTIVE_ON_SIGNATURE)
        for line in text.splitlines()
    )
    findings.append(
        TextFinding(
            rule="contract_not_executed",
            reason=(
                "签署栏为空且合同约定签字盖章后生效，合同很可能尚未生效"
                if conditioned
                else "签署栏为空，合同可能尚未签署"
            ),
            severity=90 if conditioned else 70,
            evidence=f"空签署栏 {len(blank_signature_lines)} 处",
        )
    )
    return findings


def _check_party_identity(text: str) -> list[TextFinding]:
    """Blank statutory identity fields make a counterparty unverifiable."""
    blank_labels = []
    for line in text.splitlines():
        for label in _PARTY_IDENTITY_LABELS:
            if label in line and _BLANK_FILL_RE.search(line) and label not in blank_labels:
                blank_labels.append(label)
    if not blank_labels:
        return []
    return [
        TextFinding(
            rule="party_identity_incomplete",
            reason="合同主体关键标识字段留空，无法核验交易对手",
            severity=65,
            # Labels only. Never the surrounding line, which holds party names.
            evidence="留空字段：" + "、".join(blank_labels[:5]),
        )
    ]


def _check_missing_clauses(text: str, signals: list[str]) -> list[TextFinding]:
    signals.append(SIGNAL_CLAUSE_SCAN_KEYWORD_BASED)
    findings = []
    for rule, reason, keywords, severity in REQUIRED_CLAUSES:
        if not any(keyword in text for keyword in keywords):
            findings.append(
                TextFinding(rule=rule, reason=reason, severity=severity, evidence="正文未出现相关表述")
            )
    return findings


def _check_amount_consistency(text: str, signals: list[str]) -> list[TextFinding]:
    """Capital-numeral vs Arabic disagreement is the classic altered-amount tell."""
    arabic = find_amounts_in_yuan(text)
    chinese = find_chinese_amounts_in_yuan(text)
    if not arabic:
        signals.append(SIGNAL_NO_TOTAL_FOUND)
        return []
    if not chinese:
        return []

    largest_arabic = max(arabic)
    findings = []
    # Every capital-numeral amount should have an Arabic twin somewhere.
    for capital in chinese:
        if not any(abs(capital - value) <= AMOUNT_TOLERANCE_YUAN for value in arabic):
            findings.append(
                TextFinding(
                    rule="capital_amount_mismatch",
                    reason="中文大写金额与阿拉伯数字金额不一致，需人工核对",
                    severity=85,
                    evidence=f"大写 {capital:,.2f} 元 无对应小写金额（最大小写 {largest_arabic:,.2f} 元）",
                )
            )
    return findings


def _payment_tranches(text: str) -> list[tuple[Decimal, str]]:
    """Percentages that are genuinely payment tranches, with their context.

    Classification is by PROXIMITY, not by line. Two measured failures forced
    this on the 2026-08-12 samples:

    - Line granularity swept `8% 违约金` from the penalty clause into the
      payment schedule, producing a bogus 116% total.
    - The PDF wraps one payment sentence across lines, so a line-based scan saw
      only 2 of 3 tranches and reported 90%.

    A window around each `%` is both tighter and immune to line breaks.
    """
    flat = " ".join(text.split())
    tranches: list[tuple[Decimal, str]] = []
    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*%", flat):
        tight = flat[
            max(0, match.start() - PERCENT_EXCLUDE_CHARS) : match.end() + PERCENT_EXCLUDE_CHARS
        ]
        if any(token in tight for token in _NON_TRANCHE_TOKENS):
            continue
        window = flat[
            max(0, match.start() - PERCENT_CONTEXT_CHARS) : match.end() + PERCENT_CONTEXT_CHARS
        ]
        if not any(token in window for token in _TRANCHE_TOKENS):
            continue
        try:
            tranches.append((Decimal(match.group(1)), window))
        except Exception:  # noqa: BLE001 - a malformed number is simply not a tranche
            continue
    return tranches


def _check_payment_schedule(text: str) -> list[TextFinding]:
    """Payment split sanity: shares should total 100%, advance should be sane."""
    findings: list[TextFinding] = []
    tranches = _payment_tranches(text)
    if not tranches:
        return findings

    shares = [share for share, _ in tranches]
    total_share = sum(shares)
    if len(shares) >= 2 and abs(total_share - Decimal(100)) > PERCENT_TOLERANCE:
        findings.append(
            TextFinding(
                rule="payment_share_not_100",
                reason="分期付款比例合计不等于 100%，付款安排可能不完整",
                severity=70,
                evidence=f"比例合计 {total_share}%（{len(shares)} 期）",
            )
        )

    advance = next(
        (share for share, window in tranches if any(t in window for t in _ADVANCE_TOKENS)),
        None,
    )
    if advance is not None and advance > HIGH_ADVANCE_RATIO * 100:
        findings.append(
            TextFinding(
                rule="high_advance_payment",
                reason="预付款比例偏高，交付前资金敞口较大",
                severity=55,
                evidence=f"预付 {advance}%，阈值 {HIGH_ADVANCE_RATIO * 100:.0f}%",
            )
        )
    return findings


def _check_metadata_agreement(
    text: str, metadata_total_yuan: Decimal | None, signals: list[str]
) -> list[TextFinding]:
    """Cross-check the registered amount against the amount in the document.

    This is the highest-value text rule: it is the only check that can catch a
    contract whose database record does not match the signed paper.
    """
    if metadata_total_yuan is None or metadata_total_yuan <= 0:
        signals.append(SIGNAL_NO_METADATA_TOTAL)
        return []
    amounts = find_amounts_in_yuan(text)
    if not amounts:
        signals.append(SIGNAL_NO_TOTAL_FOUND)
        return []
    if any(abs(value - metadata_total_yuan) <= AMOUNT_TOLERANCE_YUAN for value in amounts):
        return []
    return [
        TextFinding(
            rule="text_total_differs_from_metadata",
            reason="系统登记的合同金额在正文中找不到对应金额，登记值可能有误",
            severity=80,
            evidence=f"登记 {metadata_total_yuan:,.2f} 元，正文最大金额 {max(amounts):,.2f} 元",
        )
    ]
