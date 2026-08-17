"""Compare two stored evaluations of the same project.

The history has always been there and has never been comparable. An operator
can see that a project scored 51 last week and 63 today, and nothing tells them
which dimension moved or which rule started firing — which is the only part
that suggests what to do.

The one thing this module refuses to do quietly is compare across rule
versions. Scores from `v2` and `v4` are produced by different rules; the
arithmetic still works and the answer is meaningless. When the versions differ
the comparison is still returned — refusing outright would be less useful than
saying so — but it is marked, and the marking is not a footnote: the caller
gets `comparable: false` and a signal, so a UI cannot render the delta as if it
meant something without ignoring an explicit flag.
"""

from __future__ import annotations

from typing import Any

DIFF_VERSION = "risk-diff-v1"

SIGNAL_RULE_VERSION_CHANGED = "rule_version_changed_between_evaluations"
SIGNAL_SAME_EVALUATION = "compared_an_evaluation_with_itself"
SIGNAL_MISSING_DIMENSIONS = "dimension_detail_unavailable"

# Direction of travel. Named rather than inferred from the sign at the call
# site, because "higher score" means "worse" here and that is not universal.
DIRECTION_WORSE = "worse"
DIRECTION_BETTER = "better"
DIRECTION_UNCHANGED = "unchanged"


def _direction(delta: int) -> str:
    if delta > 0:
        return DIRECTION_WORSE
    if delta < 0:
        return DIRECTION_BETTER
    return DIRECTION_UNCHANGED


def _dimension_scores(payload: dict[str, Any]) -> dict[str, int]:
    scores: dict[str, int] = {}
    for item in payload.get("dimensions") or []:
        name = str(item.get("name") or "")
        if name:
            scores[name] = int(item.get("score") or 0)
    return scores


def _hits_by_rule(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    hits: dict[str, dict[str, Any]] = {}
    for hit in payload.get("hits") or []:
        # Hits carry a dimension and a reason but no stable rule id, so the
        # reason is the identity. It is generated from a fixed string per rule,
        # so it is stable in practice - and if two rules ever share wording,
        # they are indistinguishable to a reader as well.
        key = str(hit.get("reason") or hit.get("rule") or "")
        if key:
            hits[key] = {
                "dimension": str(hit.get("dimension") or ""),
                "severity": int(hit.get("severity") or 0),
                "reason": key,
                "evidence": str(hit.get("evidence") or ""),
            }
    return hits


def compare_evaluations(older: dict[str, Any], newer: dict[str, Any]) -> dict[str, Any]:
    """Diff two decoded evaluation payloads, oldest first.

    Both arguments are the stored `ProjectRiskResult.to_dict()` shape, with the
    row's `created_at` and `id` folded in by the caller.
    """
    signals: list[str] = []

    older_version = str(older.get("rule_version") or "")
    newer_version = str(newer.get("rule_version") or "")
    comparable = older_version == newer_version
    if not comparable:
        signals.append(SIGNAL_RULE_VERSION_CHANGED)
    if older.get("history_id") and older.get("history_id") == newer.get("history_id"):
        signals.append(SIGNAL_SAME_EVALUATION)

    older_score = int(older.get("score") or 0)
    newer_score = int(newer.get("score") or 0)
    score_delta = newer_score - older_score

    older_dims = _dimension_scores(older)
    newer_dims = _dimension_scores(newer)
    if not older_dims or not newer_dims:
        # A row stored before dimensions were persisted, or a payload that
        # failed to decode. Say so rather than reporting "no dimension moved",
        # which is what an empty list would otherwise be read as.
        signals.append(SIGNAL_MISSING_DIMENSIONS)

    dimensions: list[dict[str, Any]] = []
    for name in sorted(set(older_dims) | set(newer_dims)):
        before = older_dims.get(name)
        after = newer_dims.get(name)
        delta = (after or 0) - (before or 0)
        dimensions.append(
            {
                "name": name,
                "before": before,
                "after": after,
                "delta": delta,
                "direction": _direction(delta),
                # A dimension present in one evaluation and absent from the
                # other is a scoring-shape change, not a movement. The contract
                # dimension is conditional, so this is a real case.
                "appeared": before is None and after is not None,
                "disappeared": before is not None and after is None,
            }
        )

    older_hits = _hits_by_rule(older)
    newer_hits = _hits_by_rule(newer)
    new_keys = [key for key in newer_hits if key not in older_hits]
    resolved_keys = [key for key in older_hits if key not in newer_hits]

    return {
        "diff_version": DIFF_VERSION,
        "comparable": comparable,
        "signals": signals,
        "older": _side(older),
        "newer": _side(newer),
        "score": {
            "before": older_score,
            "after": newer_score,
            "delta": score_delta,
            "direction": _direction(score_delta),
        },
        "level": {
            "before": str(older.get("level") or ""),
            "after": str(newer.get("level") or ""),
            "changed": str(older.get("level") or "") != str(newer.get("level") or ""),
        },
        "dimensions": dimensions,
        "changed_dimensions": [item for item in dimensions if item["delta"] != 0],
        "new_hits": [newer_hits[key] for key in new_keys],
        "resolved_hits": [older_hits[key] for key in resolved_keys],
        "unchanged_hit_count": len([key for key in newer_hits if key in older_hits]),
    }


def _side(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "history_id": payload.get("history_id"),
        "created_at": str(payload.get("created_at") or ""),
        "rule_version": str(payload.get("rule_version") or ""),
        "score": int(payload.get("score") or 0),
        "level": str(payload.get("level") or ""),
    }
