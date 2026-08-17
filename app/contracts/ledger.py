"""Contract risk ledger: tiering, organisation aggregation and export.

The portfolio view answers "what is the state of every contract". The ledger
answers the operational question on top of it: **who has to do what, and in
what order**. It adds three things:

1. **Tiering** - findings grouped into act-now / plan / monitor / record bands,
   so a 164-finding list becomes a short worklist instead of a wall.
2. **Organisation aggregation** - `org_id` is the only stable owner dimension
   for standalone contracts, which have no project at all.
3. **Export** - CSV for offline review, because that is how this actually gets
   circulated.

Boundaries carried over unchanged:

- Contract names appear as a ROW field only (unlocked 2026-08-12), never inside
  findings, whose text reaches audit records and later LLM payloads.
- `sign_user`, `remark`, `purpose` and `contract_performance` are not selected
  at all, so no counterparty identity or free text can reach here.
- Organisations appear as IDs, never names.
- The ledger computes on demand from a portfolio result and persists nothing,
  so it cannot develop the stale-registry problem the project path had.
"""

from __future__ import annotations

import csv
import io
from typing import Any

LEDGER_VERSION = "contract-ledger-v1"

# Action tiers. The boundaries align with the shared risk thresholds
# (>=76 严重, >=51 高, >=26 中) so the ledger cannot disagree with the score
# shown elsewhere for the same contract.
TIERS: tuple[tuple[str, str, int, str], ...] = (
    ("act_now", "立即处理", 76, "已在执行却缺乏合规基础，或已超预算"),
    ("plan", "限期计划", 51, "需要排期处理，通常有时间窗口"),
    ("monitor", "持续监控", 26, "数据缺失或状态停滞，先补齐再判断"),
    ("record", "仅登记", 0, "低风险，记录备查即可"),
)

# The one definition of "the ledger as a spreadsheet". It is published in the
# `/ledger` payload as `csv_columns` so the browser's filtered export uses this
# list rather than its own copy: two hand-maintained column lists in two
# languages had already drifted apart by two columns, which meant the same
# button produced a differently-shaped file depending on which one you pressed.
CSV_COLUMNS = (
    "contract_ref",
    "contract_name",
    "org_id",
    "link_status",
    "tier",
    "risk_level",
    "risk_score",
    "finding_count",
    "approval_status",
    "execution_status",
    "sign_date",
    "end_date",
    "annotation_state",
    "top_rule",
    "top_reason",
)


def tier_for_score(score: int) -> tuple[str, str]:
    for key, label, floor, _ in TIERS:
        if score >= floor:
            return key, label
    return TIERS[-1][0], TIERS[-1][1]


def build_contract_ledger(
    portfolio: dict[str, Any],
    *,
    contracts_by_ref: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Turn a portfolio result into an actionable ledger.

    `contracts_by_ref` maps a contract reference to its `org_id`. It is passed
    separately because the portfolio payload deliberately carries no org field;
    keeping it optional means the ledger degrades to "unknown org" rather than
    failing when the caller cannot supply it.
    """
    org_lookup = contracts_by_ref or {}
    items = list(portfolio.get("items") or [])

    tier_counts: dict[str, int] = {key: 0 for key, _, _, _ in TIERS}
    org_rollup: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []

    for item in items:
        score = int(item.get("risk_score") or 0)
        findings = list(item.get("findings") or [])
        tier_key, tier_label = tier_for_score(score)
        # A contract with no findings at all is a record-only row whatever its
        # score arithmetic says, so an empty contract never lands in act_now.
        if not findings:
            tier_key, tier_label = "record", "仅登记"
        tier_counts[tier_key] = tier_counts.get(tier_key, 0) + 1

        ref = str(item.get("contract_ref") or "")
        # The portfolio now carries org_id on the item. The explicit map is
        # kept as an override for callers that resolve it themselves.
        org_id = (
            str(org_lookup.get(ref) or "").strip()
            or str(item.get("org_id") or "").strip()
            or "unknown"
        )
        top = max(findings, key=lambda f: int(f.get("severity") or 0), default=None)

        rows.append(
            {
                "contract_ref": ref,
                "contract_name": item.get("contract_name", ""),
                "org_id": org_id,
                "total_amount": item.get("total_amount", ""),
                "sign_date": item.get("sign_date", ""),
                "end_date": item.get("end_date", ""),
                "project_id": item.get("project_id", ""),
                "link_status": item.get("link_status", ""),
                "tier": tier_key,
                "tier_label": tier_label,
                "risk_level": item.get("risk_level", ""),
                "risk_score": score,
                "finding_count": len(findings),
                "approval_status": item.get("approval_status", ""),
                "execution_status": item.get("execution_status", ""),
                "top_rule": (top or {}).get("rule", ""),
                "top_reason": (top or {}).get("reason", ""),
                # Default state, overwritten by `apply_annotations` when a
                # local annotation exists. Present unconditionally so the CSV
                # column always has a value rather than a blank that reads as
                # "unknown" - and so the export never depends on whether the
                # annotation store happened to load.
                "annotation_state": "open",
                # Full list, not just the headline. The detail view has to
                # answer "which rules fired", and a single top reason left
                # the reader unable to see the rest.
                "findings": findings,
            }
        )

        bucket = org_rollup.setdefault(
            org_id,
            {
                "org_id": org_id,
                "contract_count": 0,
                "finding_count": 0,
                "act_now": 0,
                "plan": 0,
                "max_risk_score": 0,
                "unreachable_count": 0,
            },
        )
        bucket["contract_count"] += 1
        bucket["finding_count"] += len(findings)
        if tier_key in ("act_now", "plan"):
            bucket[tier_key] += 1
        bucket["max_risk_score"] = max(bucket["max_risk_score"], score)
        if item.get("link_status") in ("orphaned", "standalone"):
            bucket["unreachable_count"] += 1

    rows.sort(key=lambda row: (-int(row["risk_score"]), row["contract_ref"]))
    organisations = sorted(
        org_rollup.values(),
        key=lambda bucket: (-bucket["act_now"], -bucket["max_risk_score"], bucket["org_id"]),
    )

    return {
        "ledger_version": LEDGER_VERSION,
        "rule_version": portfolio.get("rule_version", ""),
        "contract_total": portfolio.get("contract_total", len(items)),
        "findings_total": portfolio.get("findings_total", 0),
        "by_tier": tier_counts,
        "tier_definitions": [
            {"tier": key, "label": label, "min_score": floor, "meaning": meaning}
            for key, label, floor, meaning in TIERS
        ],
        "by_organisation": organisations,
        "organisation_count": len(organisations),
        # Carried through so a ledger consumer sees the same coverage caveat.
        "project_entry_coverage": portfolio.get("project_entry_coverage", {}),
        "signals": list(portfolio.get("signals") or []),
        "truncated": bool(portfolio.get("truncated")),
        # Published so the browser's filtered export and the server's full
        # export produce the same columns in the same order.
        "csv_columns": list(CSV_COLUMNS),
        "rows": rows,
    }


def ledger_to_csv(ledger: dict[str, Any]) -> str:
    """Render the ledger rows as CSV.

    Uses CRLF and a UTF-8 BOM at the caller's layer: Excel on Windows is the
    realistic destination, and without a BOM it renders Chinese as mojibake.
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=list(CSV_COLUMNS), extrasaction="ignore", lineterminator="\r\n"
    )
    writer.writeheader()
    for row in ledger.get("rows") or []:
        writer.writerow(row)
    return buffer.getvalue()
