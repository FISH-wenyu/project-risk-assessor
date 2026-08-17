from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .assets import ContractAsset, contract_asset_counts
from .models import ContractMetadata
from .rules import (
    ContractRiskAssessment,
    build_contract_summary_text,
    evaluate_contract_metadata,
)
from .text_extraction import extract_document
from .text_rules import analyze_contract_text

# Emitted when no contract body was read, so a metadata-only score can never be
# mistaken for a contract-text review.
#
# It must NOT be emitted once text really was analysed, or the signal becomes a
# lie in the other direction.
TEXT_EXTRACTION_SIGNAL = "contract_text_not_analyzed"
TEXT_ANALYZED_SIGNAL = "contract_text_analyzed"
# A document was supplied but could not be read (no text layer, bad format,
# oversized). Distinct from "no document at all", and never silence.
TEXT_UNREADABLE_SIGNAL = "contract_document_unreadable"
# The text/metadata amount cross-check needs an unambiguous pairing. With
# several contracts on one project there is no way to know which document
# belongs to which contract, so the check is skipped and said out loud.
TEXT_AMOUNT_PAIRING_SIGNAL = "text_amount_check_needs_single_contract"
COVERAGE_SIGNAL = "project_linked_contracts_only"
# Emitted when the budget could not be read, so a missing over-budget finding
# is not misread as "no overrun".
BUDGET_UNAVAILABLE_SIGNAL = "budget_check_unavailable"


class ContractAnalysisService:
    def __init__(self, store, project_budget_loader=None, document_provider=None):
        self.store = store
        # Optional so the service stays local-only and testable without a
        # source-system gateway. A loader failure degrades to "no budget"
        # rather than failing the whole analysis.
        self.project_budget_loader = project_budget_loader
        # Returns local file paths already on disk for a project. Optional, and
        # absent by default: this service never downloads or fetches anything,
        # so with no provider the text layer simply does not run and the
        # metadata-only signal stays truthful.
        self.document_provider = document_provider

    def _analyze_documents(
        self, project_id: str, contracts: list[ContractMetadata]
    ) -> tuple[list[dict[str, Any]], list[str], int]:
        """Run text rules over any local documents for this project.

        Returns `(findings, signals, analyzed_count)`. Every failure path adds a
        signal instead of returning quietly, because a document that could not
        be read must never look like a document with no problems.
        """
        if self.document_provider is None:
            return [], [TEXT_EXTRACTION_SIGNAL], 0
        try:
            paths = list(self.document_provider(project_id) or [])
        except Exception:
            return [], [TEXT_EXTRACTION_SIGNAL, TEXT_UNREADABLE_SIGNAL], 0
        if not paths:
            return [], [TEXT_EXTRACTION_SIGNAL], 0

        # Only cross-check the registered amount when exactly one contract is
        # on the project; otherwise the document/contract pairing is a guess.
        metadata_total: Decimal | None = None
        signals: list[str] = []
        if len(contracts) == 1:
            metadata_total = _decimal_or_none(contracts[0].total_amount)
        elif len(contracts) > 1:
            signals.append(TEXT_AMOUNT_PAIRING_SIGNAL)

        findings: list[dict[str, Any]] = []
        analyzed = 0
        for path in paths:
            extracted = extract_document(path)
            if not extracted.usable:
                signals.append(TEXT_UNREADABLE_SIGNAL)
                signals.extend(extracted.signals)
                continue
            result = analyze_contract_text(
                extracted.text,
                metadata_total_yuan=metadata_total,
                text_usable=True,
            )
            analyzed += 1
            signals.extend(result.signals)
            for finding in result.findings:
                enriched = finding.to_dict()
                # Reference the document, never its contents, and never a path
                # that could expose a directory layout.
                enriched["contract_ref"] = f"doc:{extracted.source_name[:60]}"
                findings.append(enriched)

        signals.append(TEXT_ANALYZED_SIGNAL if analyzed else TEXT_EXTRACTION_SIGNAL)
        return findings, signals, analyzed

    def _load_project_budget(self, project_id: str) -> tuple[Any, bool]:
        """Return `(budget, unavailable)`.

        A loader failure must stay visible: without the flag, a skipped budget
        comparison is indistinguishable from "no overrun found", which would
        silently understate risk exactly when the source system is unreachable.
        """
        if self.project_budget_loader is None:
            return None, False
        try:
            return self.project_budget_loader(project_id), False
        except Exception:
            return None, True

    def create_analysis_job(self, project_id: str) -> dict[str, Any]:
        clean_project_id = str(project_id or "").strip()
        if not clean_project_id:
            raise ValueError("project_id is required")

        contracts = [
            _contract_from_row(row)
            for row in self.store.list_contracts(clean_project_id)
        ]
        assets = [
            _asset_from_row(row)
            for row in self.store.list_contract_assets(clean_project_id)
        ]
        counts = contract_asset_counts(assets)

        if not contracts and counts["total"] == 0:
            return self._finish(
                project_id=clean_project_id,
                status="skipped",
                stage="asset_check",
                message="no_contract_assets",
                asset_count=0,
                risk_level="not_scored",
                risk_score=None,
                summary="本项目没有已登记的合同或附件候选，未产出合同风险发现。",
                signals=counts["signals"],
                asset_counts=counts,
                findings=[],
            )

        if not contracts:
            # Attachment candidates exist but no contract rows are linked to the
            # project, so metadata rules have nothing to score.
            return self._finish(
                project_id=clean_project_id,
                status="skipped",
                stage="metadata_rules",
                message="no_project_linked_contracts",
                asset_count=counts["total"],
                risk_level="not_scored",
                risk_score=None,
                summary=(
                    "本项目存在附件候选，但没有关联到本项目的合同记录，"
                    "因此未产出合同元数据风险发现。"
                ),
                signals=_with_phase_signals(counts["signals"]),
                asset_counts=counts,
                findings=[],
            )

        project_budget, budget_unavailable = self._load_project_budget(clean_project_id)
        assessment = evaluate_contract_metadata(
            clean_project_id, contracts, project_budget=project_budget
        )
        text_findings, text_signals, analyzed_docs = self._analyze_documents(
            clean_project_id, contracts
        )

        signals = counts["signals"] + assessment.signals + text_signals
        if budget_unavailable:
            signals = signals + [BUDGET_UNAVAILABLE_SIGNAL]

        findings = [item.to_dict() for item in assessment.findings] + text_findings
        findings.sort(key=lambda item: -int(item.get("severity") or 0))
        # The headline score must account for text findings too, or a severe
        # text problem would be hidden behind a mild metadata score.
        score = max(
            [assessment.risk_score]
            + [int(item.get("severity") or 0) for item in text_findings]
        )
        stage = "metadata_and_text_rules" if analyzed_docs else "metadata_rules"
        return self._finish(
            project_id=clean_project_id,
            status="succeeded",
            stage=stage,
            message="metadata_analysis_complete",
            asset_count=counts["total"],
            risk_level=_risk_level(score),
            risk_score=score,
            summary=build_contract_summary_text(assessment),
            signals=_with_phase_signals(signals, text_analyzed=bool(analyzed_docs)),
            asset_counts=_counts_with_coverage(
                counts, assessment, analyzed_docs, len(text_findings)
            ),
            findings=findings,
        )

    def _finish(
        self,
        *,
        project_id: str,
        status: str,
        stage: str,
        message: str,
        asset_count: int,
        risk_level: str,
        risk_score: int | None,
        summary: str,
        signals: list[str],
        asset_counts: dict[str, Any],
        findings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        job = self.store.create_contract_analysis_job(
            project_id=project_id,
            status=status,
            stage=stage,
            message=message,
            asset_count=asset_count,
        )
        saved_summary = self.store.save_contract_risk_summary(
            project_id=project_id,
            job_id=job["job_id"],
            risk_level=risk_level,
            risk_score=risk_score,
            summary=summary,
            signals=signals,
            asset_counts=asset_counts,
            findings=findings,
        )
        self.store.update_contract_analysis_job(
            job["job_id"],
            status=status,
            stage=stage,
            message=message,
            summary_id=saved_summary["summary_id"],
        )
        job = self.store.get_contract_analysis_job(job["job_id"]) or job
        return {"job": job, "summary": saved_summary}


def _with_phase_signals(signals: list[str], *, text_analyzed: bool = False) -> list[str]:
    combined = list(signals)
    # Only claim "text not analysed" when that is actually true. Emitting it
    # unconditionally, as this did while the phase had no text layer at all,
    # would now understate what was checked.
    if not text_analyzed:
        combined.append(TEXT_EXTRACTION_SIGNAL)
    combined.append(COVERAGE_SIGNAL)
    seen: set[str] = set()
    ordered: list[str] = []
    for signal in combined:
        if signal and signal not in seen:
            seen.add(signal)
            ordered.append(signal)
    # A contradictory pair would make the result unreadable.
    if text_analyzed and TEXT_EXTRACTION_SIGNAL in ordered:
        ordered.remove(TEXT_EXTRACTION_SIGNAL)
    return ordered


def _decimal_or_none(value: object) -> Decimal | None:
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed > 0 else None


def _risk_level(score: int) -> str:
    if score >= 76:
        return "严重"
    if score >= 51:
        return "高"
    if score >= 26:
        return "中"
    return "低"


def _counts_with_coverage(
    counts: dict[str, Any],
    assessment: ContractRiskAssessment,
    analyzed_docs: int = 0,
    text_finding_count: int = 0,
) -> dict[str, Any]:
    enriched = dict(counts)
    enriched["contracts_evaluated"] = assessment.evaluated_count
    enriched["findings_total"] = len(assessment.findings) + text_finding_count
    enriched["metadata_findings"] = len(assessment.findings)
    enriched["text_findings"] = text_finding_count
    enriched["documents_analyzed"] = analyzed_docs
    enriched["rule_version"] = assessment.rule_version
    # Standalone contracts carry no project_id and are out of scope for the
    # project-keyed entry point; recorded so a reader does not read this as
    # full contract coverage.
    enriched["coverage_scope"] = "project_linked_contracts_only"
    return enriched


def _contract_from_row(row: dict[str, Any]) -> ContractMetadata:
    return ContractMetadata(
        contract_id=str(row.get("contract_id") or ""),
        project_id=str(row.get("project_id") or ""),
        contract_code=str(row.get("contract_code") or ""),
        contract_name=str(row.get("contract_name") or ""),
        contract_type=str(row.get("contract_type") or ""),
        total_amount=str(row.get("total_amount") or ""),
        status=str(row.get("status") or ""),
        contract_status=str(row.get("contract_status") or ""),
        sign_date=str(row.get("sign_date") or ""),
        start_date=str(row.get("start_date") or ""),
        end_date=str(row.get("end_date") or ""),
        has_project_link=bool(row.get("has_project_link")),
        source_table=str(row.get("source_table") or "contract_record"),
    )


def _asset_from_row(row: dict[str, Any]) -> ContractAsset:
    return ContractAsset(
        asset_id=str(row.get("asset_id") or ""),
        project_id=str(row.get("project_id") or ""),
        asset_kind=str(row.get("asset_kind") or ""),
        source_ref=str(row.get("source_ref") or ""),
        display_name=str(row.get("display_name") or ""),
        file_ext=str(row.get("file_ext") or ""),
        file_size=int(row.get("file_size") or 0),
        status=str(row.get("status") or "discovered"),
        risk_signal=str(row.get("risk_signal") or ""),
        sanitized_url_ref=str(row.get("sanitized_url_ref") or ""),
        source_table=str(row.get("source_table") or ""),
    )
