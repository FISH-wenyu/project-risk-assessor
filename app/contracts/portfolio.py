from __future__ import annotations

from datetime import date
from typing import Any

from .models import ContractMetadata
from .rules import (
    CONTRACT_RULE_VERSION,
    evaluate_contract_metadata,
    execution_status_label,
    approval_status_label,
)

LINK_STATUSES = ("project_linked", "orphaned", "standalone", "link_unverified")
PORTFOLIO_SIGNAL = "contract_text_not_analyzed"


def build_contract_portfolio(
    contracts: list[tuple[ContractMetadata, str]],
    *,
    today: str | date | None = None,
    max_items: int = 200,
) -> dict[str, Any]:
    """Score every contract, whatever its project linkage.

    Each contract is evaluated on its own so that a contract with no project,
    or one pointing at a deleted project, still produces findings. Contract
    names are never emitted; findings reference the contract code.
    """
    items: list[dict[str, Any]] = []
    by_link: dict[str, int] = {status: 0 for status in LINK_STATUSES}
    by_rule: dict[str, int] = {}
    unreachable_with_findings = 0

    for contract, link_status in contracts:
        status = link_status if link_status in by_link else "link_unverified"
        by_link[status] = by_link.get(status, 0) + 1
        assessment = evaluate_contract_metadata(
            contract.project_id or "-", [contract], today=today
        )
        for finding in assessment.findings:
            by_rule[finding.rule] = by_rule.get(finding.rule, 0) + 1
        if assessment.findings and status in {"orphaned", "standalone"}:
            unreachable_with_findings += 1
        items.append(
            {
                "contract_ref": _contract_ref(contract),
                # Display name, unlocked 2026-08-12 by user decision so the
                # ledger table is usable. Deliberately a ROW field: findings
                # still reference `code:`/`id:` only, because finding text
                # flows into audit records and, later, LLM payloads. Other free
                # text (sign_user, remark, purpose, contract_performance) stays
                # unread.
                "contract_name": str(contract.contract_name or "").strip()[:120],
                "org_id": str(contract.org_id or "").strip(),
                "total_amount": str(contract.total_amount or "").strip(),
                "sign_date": str(contract.sign_date or "").strip(),
                "end_date": str(contract.end_date or "").strip(),
                "project_id": str(contract.project_id or "").strip(),
                "link_status": status,
                "approval_status": approval_status_label(contract.status),
                "execution_status": execution_status_label(contract.contract_status),
                "risk_level": assessment.risk_level,
                "risk_score": assessment.risk_score,
                "finding_count": len(assessment.findings),
                "findings": [item.to_dict() for item in assessment.findings],
            }
        )

    items.sort(key=lambda item: (-int(item["risk_score"]), item["contract_ref"]))
    total = len(items)
    reachable = by_link.get("project_linked", 0)
    return {
        "contract_total": total,
        "by_link_status": by_link,
        "by_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])),
        "findings_total": sum(by_rule.values()),
        "contracts_with_findings": sum(1 for item in items if item["finding_count"]),
        # The share the project-keyed entry point can actually reach.
        "project_entry_coverage": {
            "reachable": reachable,
            "total": total,
            "percent": round(reachable / total * 100, 1) if total else 0.0,
            "unreachable_with_findings": unreachable_with_findings,
        },
        "rule_version": CONTRACT_RULE_VERSION,
        "signals": [PORTFOLIO_SIGNAL],
        "truncated": total > max_items,
        "items": items[:max_items],
    }


def _contract_ref(contract: ContractMetadata) -> str:
    code = str(contract.contract_code or "").strip()
    if code:
        return f"code:{code[:40]}"
    return f"id:{str(contract.contract_id or '').strip()[:40]}"
