from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .models import ContractMetadata

CONTRACT_RULE_VERSION = "contract-metadata-v2"

# Expiry look-ahead windows, in days, most urgent first. The existing
# `contract_expired_not_closed` rule only fires once a contract is ALREADY
# overdue, which is too late to act on: renewal, settlement and termination all
# need lead time. These tiers turn expiry into a warning rather than a report.
#
# The tiers are a judgement call, not a measured fact. 30/60/90 matches the
# usual commercial review cadence, and is a named constant so it can be
# argued with rather than being buried in a comparison.
EXPIRY_LOOKAHEAD_TIERS: tuple[tuple[int, int, str], ...] = (
    (30, 65, "30 天内"),
    (60, 50, "60 天内"),
    (90, 35, "90 天内"),
)
EXPIRY_LOOKAHEAD_MAX_DAYS = max(days for days, _, _ in EXPIRY_LOOKAHEAD_TIERS)

# `contract_record.status` is the approval dimension and `contract_record.contract_status`
# is the execution dimension. They are different axes, not synonyms.
APPROVAL_STATUS_LABELS = {
    "0": "待审核",
    "1": "审核中",
    "2": "审核通过",
    "3": "审核驳回",
}
EXECUTION_STATUS_LABELS = {
    "0": "未开始",
    "1": "进行中",
    "2": "暂停",
    "4": "草稿",
    "8": "已完成",
    "9": "终止",
}

APPROVAL_PASSED = "2"
APPROVAL_REJECTED = "3"
EXECUTION_IN_PROGRESS = "1"
EXECUTION_DRAFT = "4"
EXECUTION_FINISHED = "8"
EXECUTION_TERMINATED = "9"
EXECUTION_OPEN_STATES = {"0", "1", "2"}
EXECUTION_CLOSED_STATES = {EXECUTION_FINISHED, EXECUTION_TERMINATED}


def approval_status_label(value: object) -> str:
    code = _status_code(value)
    return APPROVAL_STATUS_LABELS.get(code, code or "")


def execution_status_label(value: object) -> str:
    code = _status_code(value)
    return EXECUTION_STATUS_LABELS.get(code, code or "")


@dataclass(frozen=True)
class ContractFinding:
    contract_ref: str
    rule: str
    severity: int
    reason: str
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ContractRiskAssessment:
    project_id: str
    risk_level: str
    risk_score: int
    findings: list[ContractFinding] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    contract_count: int = 0
    evaluated_count: int = 0
    rule_version: str = CONTRACT_RULE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "risk_level": self.risk_level,
            "risk_score": self.risk_score,
            "findings": [item.to_dict() for item in self.findings],
            "signals": list(self.signals),
            "contract_count": self.contract_count,
            "evaluated_count": self.evaluated_count,
            "rule_version": self.rule_version,
        }


def evaluate_contract_metadata(
    project_id: str,
    contracts: list[ContractMetadata],
    *,
    today: str | date | None = None,
    project_budget: object = None,
) -> ContractRiskAssessment:
    """Score contracts from registered metadata only.

    Reads nothing from the source system and touches no attachment file. Only
    the columns already registered by contract discovery are used.

    `project_budget` is optional. When supplied it is compared against the sum
    of contract amounts; both sides are yuan, confirmed against the column
    definitions rather than assumed.
    """
    today_date = _coerce_date(today) or date.today()
    findings: list[ContractFinding] = []
    for contract in contracts:
        findings.extend(_evaluate_contract(contract, today_date))
    findings.extend(_evaluate_budget_coverage(project_id, contracts, project_budget))

    score = max((item.severity for item in findings), default=0)
    return ContractRiskAssessment(
        project_id=str(project_id),
        risk_level=_risk_level(score),
        risk_score=score,
        findings=findings,
        signals=sorted({item.rule for item in findings}),
        contract_count=len(contracts),
        evaluated_count=len(contracts),
    )


def _evaluate_contract(contract: ContractMetadata, today_date: date) -> list[ContractFinding]:
    ref = _contract_ref(contract)
    approval = _status_code(contract.status)
    execution = _status_code(contract.contract_status)
    start = _coerce_date(contract.start_date)
    end = _coerce_date(contract.end_date)
    amount = _parse_amount(contract.total_amount)
    findings: list[ContractFinding] = []

    if end and end < today_date and execution in EXECUTION_OPEN_STATES:
        findings.append(
            ContractFinding(
                ref,
                "contract_expired_not_closed",
                80,
                "合同已过到期日期但执行状态仍未结束",
                f"end_date={contract.end_date}, 执行状态={execution_status_label(execution)}",
            )
        )

    # Look-ahead. Deliberately mutually exclusive with the overdue rule above:
    # a contract cannot be both already expired and expiring soon, and emitting
    # both would double-count the same problem.
    if end and end >= today_date and execution in EXECUTION_OPEN_STATES:
        remaining = (end - today_date).days
        tier = next(
            ((severity, label) for days, severity, label in EXPIRY_LOOKAHEAD_TIERS if remaining <= days),
            None,
        )
        if tier is not None:
            severity, label = tier
            findings.append(
                ContractFinding(
                    ref,
                    "contract_expiring_soon",
                    severity,
                    f"合同将在{label}到期，需提前处理续签或结算",
                    (
                        f"end_date={contract.end_date}, 剩余 {remaining} 天, "
                        f"执行状态={execution_status_label(execution)}"
                    ),
                )
            )

    if execution == EXECUTION_IN_PROGRESS and approval and approval != APPROVAL_PASSED:
        findings.append(
            ContractFinding(
                ref,
                "executing_without_approval",
                85,
                "合同已在执行但审核状态不是审核通过",
                f"审核状态={approval_status_label(approval)}, 执行状态={execution_status_label(execution)}",
            )
        )

    if approval == APPROVAL_REJECTED and execution and execution not in EXECUTION_CLOSED_STATES:
        findings.append(
            ContractFinding(
                ref,
                "rejected_but_not_closed",
                75,
                "合同审核已驳回但执行状态未终止或完成",
                f"审核状态={approval_status_label(approval)}, 执行状态={execution_status_label(execution)}",
            )
        )

    if start and end and end < start:
        findings.append(
            ContractFinding(
                ref,
                "date_range_inverted",
                70,
                "合同到期日期早于生效日期",
                f"start_date={contract.start_date}, end_date={contract.end_date}",
            )
        )

    if amount is None or amount <= 0:
        findings.append(
            ContractFinding(
                ref,
                "missing_total_amount",
                50,
                "缺少合同总金额",
                f"total_amount={_blank_marker(contract.total_amount)}",
            )
        )

    if not _has_text(contract.sign_date):
        findings.append(
            ContractFinding(
                ref,
                "missing_sign_date",
                45,
                "缺少合同签订日期",
                "sign_date=blank",
            )
        )

    if not _has_text(contract.end_date):
        findings.append(
            ContractFinding(
                ref,
                "missing_end_date",
                40,
                "缺少合同到期日期",
                "end_date=blank",
            )
        )

    if execution == EXECUTION_DRAFT:
        findings.append(
            ContractFinding(
                ref,
                "still_draft",
                35,
                "合同仍处于草稿状态",
                f"执行状态={execution_status_label(execution)}",
            )
        )

    return findings


def _evaluate_budget_coverage(
    project_id: str, contracts: list[ContractMetadata], project_budget: object
) -> list[ContractFinding]:
    """Compare the sum of contract amounts against the project budget.

    `project_record.budget` is a NOT NULL decimal where 0 means "not filled in",
    so a non-positive budget is treated as unknown and the comparison is
    skipped rather than reported as an overrun. Contracts with a missing amount
    are excluded, which can only understate the total, so this rule errs toward
    false negatives instead of false alarms. The evidence records how many
    contracts actually carried an amount.
    """
    budget = _parse_amount(project_budget)
    if budget is None or budget <= 0:
        return []
    known = [
        amount
        for amount in (_parse_amount(item.total_amount) for item in contracts)
        if amount is not None and amount > 0
    ]
    if not known:
        return []
    total = sum(known)
    if total <= budget:
        return []
    return [
        ContractFinding(
            f"project:{project_id}",
            "contract_total_exceeds_budget",
            80,
            "合同总额已超过项目预算",
            (
                f"contract_total={_amount_text(total)}, budget={_amount_text(budget)}, "
                f"counted={len(known)}/{len(contracts)}"
            ),
        )
    ]


def _amount_text(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f")


def build_contract_summary_text(assessment: ContractRiskAssessment) -> str:
    """Sanitized one-paragraph summary. Never includes contract names."""
    if assessment.contract_count == 0:
        return "本项目没有已登记的合同元数据，未产出合同风险发现。"
    if not assessment.findings:
        return (
            f"已评估 {assessment.evaluated_count} 份项目合同元数据，"
            "未命中当前合同元数据风险规则。"
        )
    top = "；".join(
        f"{item.reason}" for item in _top_findings(assessment.findings, limit=3)
    )
    return (
        f"已评估 {assessment.evaluated_count} 份项目合同元数据，"
        f"命中 {len(assessment.findings)} 条风险规则，"
        f"合同风险等级为{assessment.risk_level}（{assessment.risk_score} 分）。"
        f"主要问题：{top}。"
    )


def _top_findings(findings: list[ContractFinding], limit: int) -> list[ContractFinding]:
    seen: set[str] = set()
    ordered: list[ContractFinding] = []
    for item in sorted(findings, key=lambda f: f.severity, reverse=True):
        if item.rule in seen:
            continue
        seen.add(item.rule)
        ordered.append(item)
        if len(ordered) >= limit:
            break
    return ordered


def _contract_ref(contract: ContractMetadata) -> str:
    """Stable non-identifying reference. Contract names are never exposed."""
    code = str(contract.contract_code or "").strip()
    if code:
        return f"code:{code[:40]}"
    return f"id:{str(contract.contract_id or '').strip()[:40]}"


def _status_code(value: object) -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        return ""
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


def _parse_amount(value: object) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _has_text(value: object) -> bool:
    text = str(value if value is not None else "").strip()
    return bool(text) and text.lower() not in {"none", "null", "-"}


def _blank_marker(value: object) -> str:
    return str(value).strip() if _has_text(value) else "blank"


def _coerce_date(value: str | date | None) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "-"}:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).date()
        except ValueError:
            continue
    return None


def _risk_level(score: int) -> str:
    # Same thresholds as the project risk engine so the two read consistently.
    if score >= 76:
        return "严重"
    if score >= 51:
        return "高"
    if score >= 26:
        return "中"
    return "低"
