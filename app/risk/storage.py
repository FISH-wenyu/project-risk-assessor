from __future__ import annotations

import json
import math
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from ..sqlite_support import SQLiteStoreMixin
from .models import ProjectRiskResult
from .time_utils import beijing_now_text

RISK_LEVELS = ("低", "中", "高", "严重")


class RiskHistoryStore(SQLiteStoreMixin):
    def __init__(self, db_path: str | Path):
        self._prepare_database(db_path)
        self._init_db()

    def save_result(self, result: ProjectRiskResult) -> int:
        payload = json.dumps(result.to_dict(), ensure_ascii=False)
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                INSERT INTO risk_history
                    (project_id, project_name, score, level, rule_version, explanation, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.project_id,
                    result.project_name,
                    result.score,
                    result.level,
                    result.rule_version,
                    result.explanation,
                    payload,
                    beijing_now_text(),
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def get_latest(self, project_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM risk_history WHERE project_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        return dict(row) if row else None

    def latest_by_project(self, project_ids: list[str]) -> dict[str, dict[str, Any]]:
        clean_ids = [str(project_id) for project_id in project_ids if str(project_id)]
        if not clean_ids:
            return {}
        placeholders = ",".join("?" for _ in clean_ids)
        sql = f"""
            SELECT *
            FROM risk_history
            WHERE project_id IN ({placeholders})
            ORDER BY project_id, created_at DESC, id DESC
        """
        latest: dict[str, dict[str, Any]] = {}
        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, clean_ids).fetchall()
        for row in rows:
            item = dict(row)
            latest.setdefault(str(item["project_id"]), item)
        return latest

    def list_history(self, project_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM risk_history"
        params: list[Any] = []
        if project_id:
            sql += " WHERE project_id = ?"
            params.append(project_id)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def latest_rule_versions(self) -> list[dict[str, Any]]:
        """The newest evaluation per project, with the rules that produced it.

        Grouped in SQL rather than by pulling every row and reducing in Python:
        the table grows without bound and this is asked for on page load. The
        `id` tiebreak matters because several evaluations of one project can
        share a `created_at` second, and MAX(created_at) alone would then pick
        an arbitrary one of them.
        """
        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT h.project_id, h.project_name, h.rule_version, h.score,
                       h.level, h.created_at, h.id
                  FROM risk_history h
                  JOIN (
                        SELECT project_id, MAX(id) AS newest
                          FROM risk_history
                         GROUP BY project_id
                       ) latest
                    ON latest.project_id = h.project_id AND latest.newest = h.id
                 ORDER BY h.project_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def evaluation_payload(self, history_id: int) -> dict[str, Any] | None:
        """One stored evaluation, decoded, with its row identity folded in.

        The diff needs the full payload (dimensions and hits), not the compact
        row the list view returns. Returns None when the id does not exist so
        the caller can answer 404 rather than diffing against an empty dict,
        which would report every rule as newly resolved.
        """
        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM risk_history WHERE id = ?", (int(history_id),)
            ).fetchone()
        if row is None:
            return None
        record = dict(row)
        payload = _decode_payload(record) or _compact_history_row(record)
        payload["history_id"] = record.get("id")
        payload["created_at"] = record.get("created_at")
        payload["project_id"] = str(record.get("project_id") or "")
        payload.setdefault("score", record.get("score"))
        payload.setdefault("level", record.get("level"))
        payload.setdefault("rule_version", record.get("rule_version"))
        return payload

    def risk_summary(self, project_id: str, limit: int = 20) -> dict[str, Any]:
        history = self.list_history(project_id, limit=max(1, min(int(limit or 20), 50)))
        if not history:
            return {
                "project_id": str(project_id),
                "evaluated": False,
                "latest": None,
                "history": [],
                "dimension_chart": [],
                "hit_distribution": {},
                "history_chart": {"points": [], "min_score": 0, "max_score": 0},
            }

        latest_row = history[0]
        latest_payload = _decode_payload(latest_row)
        latest = latest_payload or _compact_history_row(latest_row)
        latest.setdefault("history_id", latest_row.get("id"))
        latest.setdefault("created_at", latest_row.get("created_at"))
        latest.setdefault("score", latest_row.get("score"))
        latest.setdefault("level", latest_row.get("level"))
        latest.setdefault("project_id", latest_row.get("project_id"))
        latest.setdefault("project_name", latest_row.get("project_name"))

        dimensions = latest.get("dimensions") or []
        hits = latest.get("hits") or []
        ascending_history = sorted(
            history,
            key=lambda row: (str(row.get("created_at") or ""), int(row.get("id") or 0)),
        )
        history_points = [
            {
                "history_id": row.get("id"),
                "score": _chart_score(row.get("score")),
                "level": row.get("level"),
                "created_at": row.get("created_at"),
                "label": _compact_datetime_label(row.get("created_at")),
            }
            for row in ascending_history
        ]
        history_scores = [point["score"] for point in history_points]
        return {
            "project_id": str(project_id),
            "evaluated": True,
            "latest": latest,
            "history": [_compact_history_row(row) for row in history],
            "dimension_chart": [
                {
                    "name": str(item.get("name") or ""),
                    "score": _chart_score(item.get("score")),
                    "summary": str(item.get("summary") or ""),
                }
                for item in dimensions
            ],
            "hit_distribution": _hit_distribution(hits),
            "history_chart": {
                "points": history_points,
                "min_score": min(history_scores) if history_scores else 0,
                "max_score": max(history_scores) if history_scores else 0,
            },
        }

    def dashboard_summary(
        self, recent_limit: int = 10, *, rule_version: str = ""
    ) -> dict[str, Any]:
        """Headline numbers for the dashboard.

        `rule_version` scopes every aggregate. It matters because a score only
        means something within the rules that produced it: on 2026-08-14 this
        method was averaging 148 v1 scores together with 25 v4 ones and showing
        the result as a single number. v5 changes the aggregation itself, so
        mixing versions now compares a floored score with an unfloored one.

        Passing "" keeps the old whole-table behaviour, which is still what the
        raw history view wants; the dashboard passes the current version.
        """
        scope = "WHERE rule_version = ?" if rule_version else ""
        params: tuple[Any, ...] = (rule_version,) if rule_version else ()
        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            total = conn.execute(
                f"SELECT COUNT(*) AS c FROM risk_history {scope}", params
            ).fetchone()["c"]
            average_row = conn.execute(
                f"SELECT AVG(score) AS avg_score FROM risk_history {scope}", params
            ).fetchone()
            level_rows = conn.execute(
                f"SELECT level, COUNT(*) AS c FROM risk_history {scope} GROUP BY level",
                params,
            ).fetchall()
            recent_rows = conn.execute(
                """
                SELECT *
                FROM risk_history
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, min(recent_limit, 50)),),
            ).fetchall()
            # "Latest per project" is computed over the whole table and then
            # scoped, not scoped first: taking the newest row that happens to
            # match a version would report a superseded evaluation as current.
            latest_rows = [
                row
                for row in conn.execute(
                    """
                    SELECT h.*
                    FROM risk_history h
                    JOIN (
                        SELECT project_id, MAX(id) AS latest_id
                        FROM risk_history
                        GROUP BY project_id
                    ) latest ON latest.latest_id = h.id
                    """
                ).fetchall()
                if not rule_version or str(row["rule_version"]) == rule_version
            ]
            stale_rows = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM risk_history h
                JOIN (
                    SELECT project_id, MAX(id) AS latest_id
                    FROM risk_history
                    GROUP BY project_id
                ) latest ON latest.latest_id = h.id
                WHERE h.rule_version <> ?
                """,
                (rule_version,),
            ).fetchone() if rule_version else None

        level_counts = {level: 0 for level in RISK_LEVELS}
        for row in level_rows:
            level_counts[str(row["level"])] = int(row["c"])

        latest_level_counts = {level: 0 for level in RISK_LEVELS}
        for row in latest_rows:
            level = str(row["level"])
            latest_level_counts[level] = latest_level_counts.get(level, 0) + 1

        avg_score = average_row["avg_score"] if average_row else None
        return {
            "total_evaluations": int(total or 0),
            "average_score": round(float(avg_score), 2) if avg_score is not None else None,
            "level_counts": level_counts,
            "latest_project_count": len(latest_rows),
            "latest_level_counts": latest_level_counts,
            "recent": [dict(row) for row in recent_rows],
            # Named so the reader knows what the numbers above cover, and how
            # many projects they silently exclude. A dashboard that quietly
            # silently drops most projects is worse than one that mixes them.
            "scoped_rule_version": rule_version,
            "stale_project_count": int(stale_rows["c"]) if stale_rows else 0,
        }

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS risk_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    project_name TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    level TEXT NOT NULL,
                    rule_version TEXT NOT NULL,
                    explanation TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_risk_history_project ON risk_history(project_id, created_at)")
            conn.commit()


def _decode_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload") or ""
    if not payload:
        return {}
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _compact_history_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "project_id": row.get("project_id"),
        "project_name": row.get("project_name"),
        "score": row.get("score"),
        "level": row.get("level"),
        "rule_version": row.get("rule_version"),
        "explanation": row.get("explanation"),
        "created_at": row.get("created_at"),
    }


def _compact_datetime_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return text
    return parsed.strftime("%m-%d %H:%M")


def _chart_score(value: Any) -> int:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(score):
        return 0
    return max(0, min(int(score), 100))


def _hit_distribution(hits: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for hit in hits:
        dimension = str(hit.get("dimension") or "未分类")
        counts[dimension] = counts.get(dimension, 0) + 1
    return counts
