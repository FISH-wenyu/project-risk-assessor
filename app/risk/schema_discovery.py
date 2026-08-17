from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class ProjectMapping:
    primary_table: str
    fields: dict[str, str]
    related_tables: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "primary_table": self.primary_table,
            "fields": self.fields,
            "related_tables": self.related_tables,
        }


FIELD_SYNONYMS = {
    "project_id": ["项目编号", "项目ID", "project_no", "project_code", "code", "id"],
    "name": ["项目名称", "project_name", "name", "title"],
    "project_type": ["项目类型", "project_type", "type"],
    "project_status": ["项目状态", "project_status", "status"],
    "approval_status": ["审核状态", "审批状态", "audit_status", "approval_status"],
    "budget": ["项目预算", "budget", "budget_amount", "amount"],
    "estimated_profit": ["预估收益", "estimated_profit", "profit", "income_forecast"],
    "partner": ["合作伙伴", "partner", "customer"],
    "funding_source": ["资金来源", "funding_source", "source"],
    "credit_support": ["是否增信", "资源协助", "credit", "support"],
    "start_date": ["开始时间", "start_time", "start_date"],
    "end_date": ["结束时间", "end_time", "end_date"],
    "owner": ["负责人", "owner", "manager", "leader"],
    "location": ["项目所在位置", "location", "address"],
    "investment_highlights": ["投资要点", "投资规模", "highlights"],
    "project_summary": ["项目情况简介", "summary", "description", "intro"],
    "exit_forecast": ["投资退出", "预测回报", "forecast", "return"],
    "risk_control": ["风险控制", "risk_control", "risk"],
}

RELATED_TABLE_HINTS = {
    "plans": ["plan", "计划"],
    "progress": ["progress", "进度"],
    "contracts": ["contract", "合同"],
    "income": ["income", "收入", "receipt"],
    "expense": ["expense", "支出", "cost"],
    "attachments": ["attach", "附件", "file"],
    "approvals": ["approval", "audit", "审批", "审核"],
}


def suggest_project_mapping(schema_rows: Iterable[dict[str, object]]) -> ProjectMapping:
    rows = list(schema_rows)
    table_scores: dict[str, int] = {}
    columns_by_table: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        table = str(row.get("table_name", ""))
        if not table:
            continue
        columns_by_table.setdefault(table, []).append(row)
        haystack = _row_haystack(row)
        score = 0
        if "project" in table.lower() or "项目" in haystack:
            score += 20
        for synonyms in FIELD_SYNONYMS.values():
            if any(_contains(haystack, item) for item in synonyms):
                score += 3
        table_scores[table] = table_scores.get(table, 0) + score

    primary_table = max(table_scores, key=table_scores.get) if table_scores else ""
    fields = _suggest_fields(columns_by_table.get(primary_table, []))
    related_tables = _suggest_related_tables(columns_by_table.keys(), primary_table)
    return ProjectMapping(primary_table=primary_table, fields=fields, related_tables=related_tables)


def _suggest_fields(rows: list[dict[str, object]]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for target, synonyms in FIELD_SYNONYMS.items():
        best_column = ""
        best_score = 0
        for row in rows:
            column = str(row.get("column_name", ""))
            haystack = _row_haystack(row)
            score = sum(1 for item in synonyms if _contains(haystack, item))
            if target == "project_id" and column.lower() in {"id", "project_id", "project_no", "project_code"}:
                score += 2
            if score > best_score:
                best_column = column
                best_score = score
        if best_column and best_score > 0:
            fields[target] = best_column
    return fields


def _suggest_related_tables(table_names: Iterable[str], primary_table: str) -> dict[str, list[str]]:
    related: dict[str, list[str]] = {key: [] for key in RELATED_TABLE_HINTS}
    for table in table_names:
        if table == primary_table:
            continue
        lowered = table.lower()
        for key, hints in RELATED_TABLE_HINTS.items():
            if any(hint.lower() in lowered or hint in table for hint in hints):
                related[key].append(table)
    return related


def _row_haystack(row: dict[str, object]) -> str:
    return " ".join(str(row.get(key, "")) for key in ("table_name", "column_name", "column_comment")).lower()


def _contains(haystack: str, needle: str) -> bool:
    return needle.lower() in haystack
