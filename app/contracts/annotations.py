"""Local annotations on contract findings: acknowledgement and ownership.

Why this exists. The ledger is a report, and a report cannot tell "nobody has
looked at this" apart from "we looked, and we accept it". Eighteen 立即处理
rows reappear identically on every load, so the list never gets shorter and
stops being read. Acknowledgement is the one addition that turns it into a
worklist. Ownership answers the question the ledger raises and cannot answer:
`org_id` says which organisation, never which person.

Where the data lives, and why it has to live here. The source MySQL is
read-only for this project, and the source system has no field for either of
these. So both are **local** facts about a source row, joined on
`contract_ref`, and they must never be mistaken for source data — the API
labels them under `annotation`, not alongside the source columns.

Design decisions worth keeping:

- **Keyed by (contract_ref, rule), with rule = '' meaning the whole contract.**
  Acknowledging "this contract is fine" and acknowledging "this specific
  finding is fine" are different statements, and collapsing them would let one
  dismissal silence a rule that fires later for a different reason.
- **Acknowledgement is scoped to a risk score.** A finding accepted at score 40
  must come back when the same contract reaches 80: the thing that was accepted
  is not the thing that is there now. `acknowledged_score` records what was
  accepted; `is_stale_for` compares.
- **Nothing is deleted.** Un-acknowledging writes a new state with a reason,
  so the record of who accepted what, and when, survives. This is the audit
  trail for a decision to *not* act.
- **A note is free text written by an operator.** It is stored, returned to
  this UI, and never enters a finding, an LLM payload or the CSV export -
  those all flow outward, and this text is not covered by any redaction the
  contract boundary relies on.
"""

from __future__ import annotations

import re
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..sqlite_support import SQLiteStoreMixin
from ..risk.time_utils import beijing_now_text

ANNOTATION_VERSION = "contract-annotation-v1"

STATE_OPEN = "open"
STATE_ACKNOWLEDGED = "acknowledged"
STATE_ACCEPTED = "accepted"
STATES = (STATE_OPEN, STATE_ACKNOWLEDGED, STATE_ACCEPTED)

STATE_LABELS = {
    STATE_OPEN: "待处理",
    STATE_ACKNOWLEDGED: "已确认",
    STATE_ACCEPTED: "已接受风险",
}

# Whole-contract annotations use this in place of a rule name.
WHOLE_CONTRACT = ""

MAX_NOTE_CHARS = 500
MAX_OWNER_CHARS = 60
MAX_REF_CHARS = 120
MAX_RULE_CHARS = 80

# An owner is a name or handle typed by an operator. Control characters are
# stripped because this string is rendered; nothing else is - guessing at a
# "valid name" shape is how systems end up rejecting real people's names.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class AnnotationError(ValueError):
    """The caller asked for something this store will not record."""


@dataclass(frozen=True)
class Annotation:
    contract_ref: str
    rule: str
    state: str
    owner: str
    note: str
    acknowledged_score: int | None
    updated_by: str
    created_at: str
    updated_at: str

    @property
    def whole_contract(self) -> bool:
        return self.rule == WHOLE_CONTRACT

    def is_stale_for(self, current_score: int | None) -> bool:
        """True when the risk has grown past what was acknowledged.

        An acknowledgement is a statement about a specific level of risk. If
        the score has since risen, the statement no longer covers what is
        there, and the row has to come back to the worklist rather than stay
        silently dismissed.
        """
        if self.state == STATE_OPEN or self.acknowledged_score is None:
            return False
        if current_score is None:
            return False
        return int(current_score) > int(self.acknowledged_score)

    def to_dict(self, *, current_score: int | None = None) -> dict[str, Any]:
        payload = asdict(self)
        payload["state_label"] = STATE_LABELS.get(self.state, self.state)
        payload["whole_contract"] = self.whole_contract
        payload["stale"] = self.is_stale_for(current_score)
        return payload


def _clean_text(value: object, limit: int, field: str) -> str:
    text = _CONTROL_CHARS.sub("", str(value or "")).strip()
    if len(text) > limit:
        raise AnnotationError(f"{field} exceeds {limit} characters")
    return text


class ContractAnnotationStore(SQLiteStoreMixin):
    """Local acknowledgement and ownership for contract findings."""

    def __init__(self, db_path: str | Path):
        self._prepare_database(db_path)
        self._init_db()

    def set_annotation(
        self,
        contract_ref: str,
        *,
        rule: str = WHOLE_CONTRACT,
        state: str | None = None,
        owner: str | None = None,
        note: str | None = None,
        current_score: int | None = None,
        updated_by: str = "local_operator",
    ) -> Annotation:
        """Create or update one annotation.

        `None` means "leave as it is", which is what lets the UI set an owner
        without touching the acknowledgement and vice versa. Passing an empty
        string clears the field - a distinct instruction from `None`.
        """
        ref = _clean_text(contract_ref, MAX_REF_CHARS, "contract_ref")
        if not ref:
            raise AnnotationError("contract_ref is required")
        rule_key = _clean_text(rule, MAX_RULE_CHARS, "rule")
        if state is not None and state not in STATES:
            raise AnnotationError(f"state must be one of {STATES}")
        clean_owner = None if owner is None else _clean_text(owner, MAX_OWNER_CHARS, "owner")
        clean_note = None if note is None else _clean_text(note, MAX_NOTE_CHARS, "note")

        now = beijing_now_text()
        with closing(self._connect(row_factory=True)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM contract_annotations WHERE contract_ref = ? AND rule = ?",
                (ref, rule_key),
            ).fetchone()

            if existing is None:
                new_state = state or STATE_OPEN
                row = {
                    "contract_ref": ref,
                    "rule": rule_key,
                    "state": new_state,
                    "owner": clean_owner or "",
                    "note": clean_note or "",
                    # Only an acknowledgement pins a score. Recording one for an
                    # open row would make the first later re-open look stale.
                    "acknowledged_score": (
                        None if new_state == STATE_OPEN else _as_score(current_score)
                    ),
                    "updated_by": _clean_text(updated_by, MAX_OWNER_CHARS, "updated_by"),
                    "created_at": now,
                    "updated_at": now,
                }
                conn.execute(
                    """
                    INSERT INTO contract_annotations
                        (contract_ref, rule, state, owner, note, acknowledged_score,
                         updated_by, created_at, updated_at)
                    VALUES (:contract_ref, :rule, :state, :owner, :note, :acknowledged_score,
                            :updated_by, :created_at, :updated_at)
                    """,
                    row,
                )
            else:
                row = dict(existing)
                new_state = state if state is not None else row["state"]
                row["state"] = new_state
                if clean_owner is not None:
                    row["owner"] = clean_owner
                if clean_note is not None:
                    row["note"] = clean_note
                if state is not None:
                    # Re-pin on every state change, including back to open,
                    # which clears the pin so the row is simply live again.
                    row["acknowledged_score"] = (
                        None if new_state == STATE_OPEN else _as_score(current_score)
                    )
                row["updated_by"] = _clean_text(updated_by, MAX_OWNER_CHARS, "updated_by")
                row["updated_at"] = now
                conn.execute(
                    """
                    UPDATE contract_annotations
                       SET state = :state, owner = :owner, note = :note,
                           acknowledged_score = :acknowledged_score,
                           updated_by = :updated_by, updated_at = :updated_at
                     WHERE contract_ref = :contract_ref AND rule = :rule
                    """,
                    row,
                )
            # Both branches record the resulting state, so the history is the
            # full sequence of decisions rather than only the changes.
            self._append_history(conn, row, now)
            conn.commit()
        return _to_annotation(row)

    def get_annotations(self, contract_ref: str) -> list[Annotation]:
        ref = _clean_text(contract_ref, MAX_REF_CHARS, "contract_ref")
        with closing(self._connect(row_factory=True)) as conn:
            rows = conn.execute(
                "SELECT * FROM contract_annotations WHERE contract_ref = ? ORDER BY rule",
                (ref,),
            ).fetchall()
        return [_to_annotation(dict(row)) for row in rows]

    def all_annotations(self) -> dict[str, list[Annotation]]:
        """Every annotation, grouped by contract reference.

        One read for the whole ledger. Per-row lookups would be 65 queries
        against a store whose entire contents comfortably fit in memory.
        """
        with closing(self._connect(row_factory=True)) as conn:
            rows = conn.execute(
                "SELECT * FROM contract_annotations ORDER BY contract_ref, rule"
            ).fetchall()
        grouped: dict[str, list[Annotation]] = {}
        for row in rows:
            annotation = _to_annotation(dict(row))
            grouped.setdefault(annotation.contract_ref, []).append(annotation)
        return grouped

    def history(self, contract_ref: str, limit: int = 50) -> list[dict[str, Any]]:
        """Who decided what, and when. Never pruned by `set_annotation`."""
        ref = _clean_text(contract_ref, MAX_REF_CHARS, "contract_ref")
        clean_limit = max(1, min(int(limit or 50), 200))
        with closing(self._connect(row_factory=True)) as conn:
            rows = conn.execute(
                """
                SELECT rule, state, owner, note, acknowledged_score, updated_by, recorded_at
                  FROM contract_annotation_history
                 WHERE contract_ref = ?
                 ORDER BY id DESC
                 LIMIT ?
                """,
                (ref, clean_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def _append_history(self, conn: Any, row: dict[str, Any], now: str) -> None:
        conn.execute(
            """
            INSERT INTO contract_annotation_history
                (contract_ref, rule, state, owner, note, acknowledged_score,
                 updated_by, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["contract_ref"],
                row["rule"],
                row["state"],
                row["owner"],
                row["note"],
                row["acknowledged_score"],
                row["updated_by"],
                now,
            ),
        )

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS contract_annotations (
                    contract_ref TEXT NOT NULL,
                    rule TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'open',
                    owner TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    acknowledged_score INTEGER,
                    updated_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (contract_ref, rule)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS contract_annotation_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contract_ref TEXT NOT NULL,
                    rule TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'open',
                    owner TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    acknowledged_score INTEGER,
                    updated_by TEXT NOT NULL DEFAULT '',
                    recorded_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_annotation_history_ref "
                "ON contract_annotation_history (contract_ref, id DESC)"
            )
            conn.commit()


def _as_score(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_annotation(row: dict[str, Any]) -> Annotation:
    return Annotation(
        contract_ref=str(row["contract_ref"]),
        rule=str(row.get("rule") or ""),
        state=str(row.get("state") or STATE_OPEN),
        owner=str(row.get("owner") or ""),
        note=str(row.get("note") or ""),
        acknowledged_score=_as_score(row.get("acknowledged_score")),
        updated_by=str(row.get("updated_by") or ""),
        created_at=str(row.get("created_at") or ""),
        updated_at=str(row.get("updated_at") or ""),
    )


def apply_annotations(
    rows: list[dict[str, Any]], annotations: dict[str, list[Annotation]]
) -> dict[str, Any]:
    """Attach annotations to ledger rows and summarise the worklist.

    Row-level state is the whole-contract annotation when there is one. A row
    counts as still needing attention when it is open, or when what was
    acknowledged no longer covers the score it now carries.
    """
    open_count = 0
    acknowledged_count = 0
    stale_count = 0
    owners: dict[str, int] = {}

    for row in rows:
        ref = str(row.get("contract_ref") or "")
        score = row.get("risk_score")
        entries = annotations.get(ref) or []
        whole = next((item for item in entries if item.whole_contract), None)
        per_rule = [item for item in entries if not item.whole_contract]

        row["annotation"] = whole.to_dict(current_score=score) if whole else None
        row["rule_annotations"] = [item.to_dict(current_score=score) for item in per_rule]

        state = whole.state if whole else STATE_OPEN
        stale = bool(whole and whole.is_stale_for(score))
        # A stale acknowledgement is presented as open, because that is what it
        # is: the decision on record does not cover the current risk.
        row["annotation_state"] = STATE_OPEN if (state != STATE_OPEN and stale) else state
        row["annotation_stale"] = stale
        row["owner"] = whole.owner if whole else ""
        row["needs_attention"] = row["annotation_state"] == STATE_OPEN

        if row["needs_attention"]:
            open_count += 1
        else:
            acknowledged_count += 1
        if stale:
            stale_count += 1
        if row["owner"]:
            owners[row["owner"]] = owners.get(row["owner"], 0) + 1

    return {
        "annotation_version": ANNOTATION_VERSION,
        "open_count": open_count,
        "acknowledged_count": acknowledged_count,
        "stale_count": stale_count,
        "owner_counts": dict(sorted(owners.items(), key=lambda item: (-item[1], item[0]))),
        "unassigned_count": sum(1 for row in rows if not row.get("owner")),
    }
