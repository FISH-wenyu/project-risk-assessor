from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ProjectSnapshot:
    project_id: str
    name: str = ""
    project_type: str = ""
    category_ids: list[int] = field(default_factory=list)
    category_names: list[str] = field(default_factory=list)
    category_parent_names: list[str] = field(default_factory=list)
    category_paths: list[str] = field(default_factory=list)
    project_status: str = ""
    approval_status: str = ""
    budget: Any = None
    estimated_profit: Any = None
    income_total: Any = None
    expense_total: Any = None
    partner: str = ""
    funding_source: str = ""
    credit_support: str = ""
    start_date: str = ""
    end_date: str = ""
    owner: str = ""
    location: str = ""
    investment_highlights: str = ""
    project_summary: str = ""
    exit_forecast: str = ""
    risk_control: str = ""
    plan_count: int = 0
    progress_count: int = 0
    contract_count: int = 0
    income_count: int = 0
    expense_count: int = 0
    attachment_count: int = 0
    approval_pending_count: int = 0
    approval_instance_count: int = 0
    approval_record_count: int = 0
    approval_reject_count: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class RiskHit:
    dimension: str
    severity: int
    reason: str
    evidence: str
    # What kind of problem this is. Classified where the hit is created, not
    # inferred later from its wording: the wording is display text and will be
    # edited, and a scoring rule that depends on a substring match against
    # display text breaks silently when someone rewrites a sentence.
    #
    # KIND_STATE - the project is in a bad state (over budget, past its end
    #   date, rejected in approval). Actionable on its own.
    # KIND_DATA  - a field was not filled in. Real risk, because a missing
    #   budget means nobody can check whether it was exceeded, but never
    #   actionable ON ITS OWN and never sufficient to make a project urgent.
    #
    # The distinction drives both the score floor and the tier. Measured on
    # 2026-08-14: 73% of all findings are KIND_DATA, and without the split they
    # decide the worst dimension for 73% of projects.
    kind: str = "state"


@dataclass
class DimensionScore:
    name: str
    score: int
    weight: float
    summary: str


@dataclass
class ProjectRiskResult:
    project_id: str
    project_name: str
    score: int
    level: str
    dimensions: list[DimensionScore]
    hits: list[RiskHit]
    suggestions: list[str]
    explanation: str
    rule_version: str = "v1"
    # Action tier derived from the shape of the findings rather than from the
    # score. The score compresses - seven rules fire on more than 60% of
    # projects - so it cannot carry triage on its own. Same four tiers as the
    # contract ledger, so one vocabulary covers both halves of the product.
    tier: str = "record"
    tier_label: str = "仅登记"
    # Non-fatal notes about how the score was produced, e.g. that contract risk
    # was not folded in. Absence of a dimension must never be read as a clean
    # dimension, so the reason is carried with the result.
    signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EvaluationCancelled(RuntimeError):
    """The operator stopped this evaluation.

    A domain outcome, not a job-runner detail, which is why it lives here: the
    evaluation service has to tell it apart from a real fault when it records
    the audit event, and importing the job runner to do that would point the
    dependency the wrong way. `app.risk.jobs` re-exports it, so existing
    callers are unaffected.
    """
