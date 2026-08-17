from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from .analysis import ContractAnalysisService
from .annotations import AnnotationError, apply_annotations
from .authorization import AttachmentAuthorizationService
from .assets import build_contract_assets, contract_asset_counts
from .chat_service import ContractChatService
from .ledger import build_contract_ledger, ledger_to_csv
from .portfolio import build_contract_portfolio


MAX_PROJECT_ID_CHARS = 128
MAX_CONTRACT_JOB_ID_CHARS = 160
MAX_AUTHORIZATION_ID_CHARS = 160
MAX_CONTRACT_REF_CHARS = 120


def build_contract_router(
    discovery_provider: Callable[[], Any],
    registry_provider: Callable[[], Any],
    auth_dependency: Any,
    project_budget_loader: Callable[[str], Any] | None = None,
    document_provider: Callable[[str], Any] | None = None,
    chat_llm_client: Any | None = None,
    annotation_store: Any | None = None,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/contracts",
        tags=["contracts"],
        dependencies=[auth_dependency] if auth_dependency is not None else [],
    )

    @router.post("/discover")
    def discover(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            project_id = _required_project_id(
                payload.get("project_id") if isinstance(payload, dict) else ""
            )
            result = discovery_provider().discover_project(project_id)
            registry_provider().upsert_discovery_result(result)
            return result.to_dict()
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid contract discovery request"
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail="Contract discovery failed"
            ) from exc

    @router.get("")
    def list_registered(project_id: str = "") -> dict[str, Any]:
        try:
            clean_project_id = _required_project_id(project_id)
            registry = registry_provider()
            return {
                "project_id": clean_project_id,
                "contracts": registry.list_contracts(clean_project_id),
                "attachments": registry.list_attachments(clean_project_id),
            }
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid contract registry request"
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail="Contract registry failed"
            ) from exc

    @router.post("/assets/refresh")
    async def refresh_assets(request: Request) -> dict[str, Any]:
        try:
            payload = await _required_json_object(request)
            project_id = _required_project_id(
                payload.get("project_id")
            )
            result = discovery_provider().discover_project(project_id)
            registry = registry_provider()
            registry.upsert_discovery_result(result)
            assets = build_contract_assets(result)
            registry.upsert_contract_assets(project_id, assets)
            return {
                "project_id": project_id,
                "asset_counts": contract_asset_counts(assets),
                "assets": [asset.to_dict() for asset in assets],
                "warnings": list(result.warnings),
                "latest_job": registry.latest_contract_analysis_job(project_id),
                "latest_summary": registry.get_latest_contract_risk_summary(project_id),
            }
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid contract asset request"
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail="Contract asset refresh failed"
            ) from exc

    @router.get("/assets")
    def list_assets(project_id: str = "") -> dict[str, Any]:
        try:
            clean_project_id = _required_project_id(project_id)
            registry = registry_provider()
            assets = registry.list_contract_assets(clean_project_id)
            return {
                "project_id": clean_project_id,
                "asset_counts": contract_asset_counts(
                    [_asset_row_to_contract_asset(row) for row in assets]
                ),
                "assets": assets,
                "latest_job": registry.latest_contract_analysis_job(clean_project_id),
                "latest_summary": registry.get_latest_contract_risk_summary(
                    clean_project_id
                ),
            }
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid contract asset request"
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail="Contract asset refresh failed"
            ) from exc

    @router.get("/portfolio")
    def contract_portfolio(limit: int = 500) -> dict[str, Any]:
        """Read-only risk view over every active contract.

        The project-keyed endpoints only reach contracts whose project still
        exists, so this covers standalone and orphaned contracts too. It
        computes on demand and writes nothing, which keeps it free of the
        stale-registry problem the project path had.
        """
        try:
            contracts = discovery_provider().discover_portfolio(limit=limit)
            return build_contract_portfolio(contracts)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid contract portfolio request"
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail="Contract portfolio failed"
            ) from exc

    @router.get("/ledger")
    def contract_ledger(limit: int = 500) -> dict[str, Any]:
        """Actionable view over the portfolio: tiers, owners, worklist.

        Same read as `/portfolio`, one query, computed on demand and persisted
        nowhere. Organisations are reported as IDs; no organisation or contract
        names are emitted.
        """
        try:
            contracts = discovery_provider().discover_portfolio(limit=limit)
            portfolio = build_contract_portfolio(contracts)
            ledger = build_contract_ledger(
                portfolio, contracts_by_ref=_org_by_contract_ref(contracts)
            )
            _attach_annotations(ledger)
            return ledger
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid contract ledger request"
            ) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Contract ledger failed") from exc

    def _attach_annotations(ledger: dict[str, Any]) -> None:
        """Fold local acknowledgement and ownership into the ledger.

        Annotations are LOCAL facts about a source row - the source database is
        read-only and has no field for either - so they arrive under their own
        keys and never overwrite a source column. A store failure degrades to
        "everything is open" with a signal rather than failing the ledger: the
        risk data is the point, the worklist state is an overlay.
        """
        rows = ledger.get("rows") or []
        if annotation_store is None:
            ledger["annotation_summary"] = None
            ledger.setdefault("signals", []).append("annotations_unavailable")
            return
        try:
            summary = apply_annotations(rows, annotation_store.all_annotations())
        except Exception:
            ledger["annotation_summary"] = None
            ledger.setdefault("signals", []).append("annotations_unavailable")
            return
        ledger["annotation_summary"] = summary

    @router.get("/annotations/{contract_ref:path}")
    def get_contract_annotation(contract_ref: str) -> dict[str, Any]:
        """Current annotation plus the full decision history for one contract."""
        if annotation_store is None:
            raise HTTPException(status_code=503, detail="Annotations are not available")
        try:
            ref = _required_text(contract_ref, MAX_CONTRACT_REF_CHARS)
            return {
                "contract_ref": ref,
                "annotations": [item.to_dict() for item in annotation_store.get_annotations(ref)],
                "history": annotation_store.history(ref),
            }
        except (ValueError, AnnotationError) as exc:
            raise HTTPException(status_code=400, detail="Invalid annotation request") from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Annotation lookup failed") from exc

    @router.post("/annotations")
    async def set_contract_annotation(request: Request) -> dict[str, Any]:
        """Acknowledge a finding, or assign an owner, or both.

        Writes only to the local database. `state`, `owner` and `note` are all
        optional and independent: omitting one leaves it untouched, so setting
        an owner does not silently re-open an acknowledged row.
        """
        if annotation_store is None:
            raise HTTPException(status_code=503, detail="Annotations are not available")
        try:
            payload = await _required_json_object(request)
            ref = _required_text(payload.get("contract_ref"), MAX_CONTRACT_REF_CHARS)
            annotation = annotation_store.set_annotation(
                ref,
                rule=str(payload.get("rule") or ""),
                state=payload.get("state"),
                owner=payload.get("owner"),
                note=payload.get("note"),
                current_score=payload.get("risk_score"),
            )
            return {"contract_ref": ref, "annotation": annotation.to_dict()}
        except (ValueError, AnnotationError) as exc:
            raise HTTPException(status_code=400, detail="Invalid annotation request") from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Annotation update failed") from exc

    @router.get("/ledger.csv")
    def contract_ledger_csv(limit: int = 500) -> Response:
        """The same ledger as CSV, for offline circulation.

        A UTF-8 BOM is prepended deliberately: Excel on Windows is the
        realistic destination and renders Chinese as mojibake without it.
        """
        try:
            contracts = discovery_provider().discover_portfolio(limit=limit)
            portfolio = build_contract_portfolio(contracts)
            ledger = build_contract_ledger(
                portfolio, contracts_by_ref=_org_by_contract_ref(contracts)
            )
            # The export carries the acknowledgement state, so a circulated
            # sheet says what has already been dealt with. It carries neither
            # the owner nor the note: an owner is a person's name and a note is
            # free operator text, and this project keeps identity and unredacted
            # free text out of anything that leaves the machine.
            _attach_annotations(ledger)
            body = "﻿" + ledger_to_csv(ledger)
            return Response(
                content=body.encode("utf-8"),
                media_type="text/csv; charset=utf-8",
                headers={
                    "Content-Disposition": 'attachment; filename="contract-ledger.csv"'
                },
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid contract ledger request"
            ) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Contract ledger failed") from exc

    @router.post("/chat")
    async def contract_chat(request: Request) -> dict[str, Any]:
        """Answer a question about selected contracts.

        The LLM sits on top of the rule layer and cannot change it: this route
        reads ledger output and never writes a score. Every answer is verified
        against the payload actually sent before it is returned.
        """
        try:
            payload = await _required_json_object(request)
            question = _required_text(payload.get("question"), 2000)
            refs = payload.get("contract_refs") or []
            if not isinstance(refs, list):
                raise ValueError("contract_refs must be a list")
            clean_refs = [_required_text(ref, 80) for ref in refs[:20]]
            if not clean_refs:
                # Rejected here rather than answered. With no contracts there
                # is nothing to ground an answer in, so the call reaches the
                # provider, spends several seconds and real tokens, and comes
                # back with a paraphrase of "you gave me no data" - which the
                # verifier then flags, because the model quotes the field names
                # from the empty payload. A missing question is already a 400;
                # an empty selection is the same kind of incomplete request.
                raise ValueError("contract_refs must not be empty")

            def load_rows() -> list[dict[str, Any]]:
                contracts = discovery_provider().discover_portfolio()
                portfolio = build_contract_portfolio(contracts)
                return build_contract_ledger(
                    portfolio, contracts_by_ref=_org_by_contract_ref(contracts)
                )["rows"]

            service = ContractChatService(
                ledger_loader=load_rows,
                document_provider=document_provider,
                llm_client=chat_llm_client,
            )
            return service.ask(question, clean_refs).to_dict()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid contract chat request") from exc
        except Exception as exc:
            # Never surface provider or SQL detail to the caller.
            raise HTTPException(status_code=500, detail="Contract chat failed") from exc

    @router.post("/analysis/jobs")
    async def create_analysis_job(request: Request) -> dict[str, Any]:
        try:
            payload = await _required_json_object(request)
            project_id = _required_project_id(
                payload.get("project_id")
            )
            return ContractAnalysisService(
                registry_provider(), project_budget_loader, document_provider
            ).create_analysis_job(project_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid contract analysis request"
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail="Contract analysis job failed"
            ) from exc

    @router.get("/analysis/jobs/{job_id}")
    def get_analysis_job(job_id: str) -> dict[str, Any]:
        try:
            clean_job_id = str(job_id or "").strip()
            if not clean_job_id or len(clean_job_id) > MAX_CONTRACT_JOB_ID_CHARS:
                raise ValueError("invalid job_id")
            registry = registry_provider()
            job = registry.get_contract_analysis_job(clean_job_id)
            if not job:
                raise HTTPException(
                    status_code=404, detail="Contract analysis job not found"
                )
            summary = (
                registry.get_contract_risk_summary(job.get("summary_id"))
                if job.get("summary_id")
                else None
            )
            return {"job": job, "summary": summary}
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid contract analysis request"
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail="Contract analysis job failed"
            ) from exc

    @router.post("/attachments/authorizations")
    async def create_attachment_authorization(request: Request) -> dict[str, Any]:
        try:
            payload = await _required_json_object(request)
            service = AttachmentAuthorizationService(registry_provider())
            authorization = service.create_draft(
                project_id=_required_project_id(payload.get("project_id")),
                source_table=_required_text(payload.get("source_table"), 80),
                source_attach_id_hash=_required_text(
                    payload.get("source_attach_id_hash"), 128
                ),
                source_biz_id_hash=_optional_text(
                    payload.get("source_biz_id_hash"), 128
                ),
                candidate_bucket=_required_text(payload.get("candidate_bucket"), 80),
                format_group=_required_text(payload.get("format_group"), 80),
                file_ext=_optional_text(payload.get("file_ext"), 20),
                size_bucket=_required_text(payload.get("size_bucket"), 40),
                sanitized_url_ref=_optional_text(
                    payload.get("sanitized_url_ref"), 240
                ),
                requested_by=_optional_text(
                    payload.get("requested_by"), 80
                ) or "local_agent",
                decision_reason=_optional_text(
                    payload.get("decision_reason"), 300
                ),
            )
            return authorization.public_dict()
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid contract authorization request"
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail="Contract authorization failed"
            ) from exc

    @router.get("/attachments/authorizations")
    def list_attachment_authorizations(project_id: str = "") -> dict[str, Any]:
        try:
            clean_project_id = _required_project_id(project_id)
            authorizations = registry_provider().list_attachment_authorizations(
                clean_project_id
            )
            return {
                "project_id": clean_project_id,
                "authorizations": [
                    authorization.public_dict() for authorization in authorizations
                ],
            }
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid contract authorization request"
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail="Contract authorization failed"
            ) from exc

    @router.post("/attachments/authorizations/drafts/from-assets")
    async def create_attachment_authorization_drafts_from_assets(
        request: Request,
    ) -> dict[str, Any]:
        try:
            payload = await _required_json_object(request)
            project_id = _required_project_id(payload.get("project_id"))
            requested_by = (
                _optional_text(payload.get("requested_by"), 80) or "local_agent"
            )
            service = AttachmentAuthorizationService(registry_provider())
            created = service.create_drafts_from_local_assets(
                project_id, requested_by=requested_by
            )
            authorizations = registry_provider().list_attachment_authorizations(
                project_id
            )
            return {
                "project_id": project_id,
                "created_count": len(created),
                "total_count": len(authorizations),
                "created_authorizations": [
                    authorization.public_dict() for authorization in created
                ],
                "authorizations": [
                    authorization.public_dict() for authorization in authorizations
                ],
            }
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid contract authorization request"
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail="Contract authorization failed"
            ) from exc

    @router.post("/attachments/authorizations/{authorization_id}/approve")
    async def approve_attachment_authorization(
        authorization_id: str, request: Request
    ) -> dict[str, Any]:
        return await _transition_attachment_authorization(
            authorization_id, request, registry_provider, "approve"
        )

    @router.post("/attachments/authorizations/{authorization_id}/reject")
    async def reject_attachment_authorization(
        authorization_id: str, request: Request
    ) -> dict[str, Any]:
        return await _transition_attachment_authorization(
            authorization_id, request, registry_provider, "reject"
        )

    @router.post("/attachments/authorizations/{authorization_id}/expire")
    async def expire_attachment_authorization(
        authorization_id: str, request: Request
    ) -> dict[str, Any]:
        return await _transition_attachment_authorization(
            authorization_id, request, registry_provider, "expire"
        )

    @router.post("/attachments/authorizations/{authorization_id}/retrieval-preflight")
    def attachment_authorization_retrieval_preflight(
        authorization_id: str,
    ) -> dict[str, Any]:
        try:
            clean_authorization_id = _required_authorization_id(authorization_id)
            service = AttachmentAuthorizationService(registry_provider())
            return service.preflight_retrieval(clean_authorization_id)
        except LookupError as exc:
            raise HTTPException(
                status_code=404, detail="Attachment authorization not found"
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid contract authorization request"
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail="Contract authorization failed"
            ) from exc

    return router


def _org_by_contract_ref(contracts: list[Any]) -> dict[str, str]:
    """Map each contract reference to its org_id for ledger aggregation.

    Built here rather than inside the portfolio payload so the portfolio
    response shape stays unchanged. Uses the same ref rule as the portfolio:
    contract code first, contract id as fallback, never the name.
    """
    mapping: dict[str, str] = {}
    for contract, _link_status in contracts:
        code = str(getattr(contract, "contract_code", "") or "").strip()
        ref = (
            f"code:{code[:40]}"
            if code
            else f"id:{str(getattr(contract, 'contract_id', '') or '').strip()[:40]}"
        )
        mapping[ref] = str(getattr(contract, "org_id", "") or "").strip()
    return mapping


def _required_project_id(value: object) -> str:
    text = str(value or "").strip()
    if not text or len(text) > MAX_PROJECT_ID_CHARS:
        raise ValueError("invalid project_id")
    return text


def _required_authorization_id(value: object) -> str:
    text = str(value or "").strip()
    if not text or len(text) > MAX_AUTHORIZATION_ID_CHARS:
        raise ValueError("invalid authorization_id")
    return text


def _required_text(value: object, max_chars: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > max_chars:
        raise ValueError("invalid text field")
    return text


def _optional_text(value: object, max_chars: int) -> str:
    text = str(value or "").strip()
    if len(text) > max_chars:
        raise ValueError("invalid text field")
    return text


async def _required_json_object(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise ValueError("invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid JSON body")
    return payload


async def _transition_attachment_authorization(
    authorization_id: str,
    request: Request,
    registry_provider: Callable[[], Any],
    action: str,
) -> dict[str, Any]:
    try:
        payload = await _optional_json_object(request)
        reason = _optional_text(payload.get("decision_reason"), 300)
        service = AttachmentAuthorizationService(registry_provider())
        clean_authorization_id = _required_authorization_id(authorization_id)
        if action == "approve":
            authorization = service.approve(clean_authorization_id, reason)
        elif action == "reject":
            authorization = service.reject(clean_authorization_id, reason)
        elif action == "expire":
            authorization = service.expire(clean_authorization_id, reason)
        else:
            raise ValueError("invalid authorization action")
        return authorization.public_dict()
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="Invalid contract authorization request"
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail="Contract authorization failed"
        ) from exc


async def _optional_json_object(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        return {}
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("invalid JSON body")
    return payload


def _asset_row_to_contract_asset(row: dict[str, Any]) -> Any:
    from .assets import ContractAsset

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
