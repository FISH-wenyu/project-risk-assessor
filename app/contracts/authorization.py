from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import hashlib
import re
from uuid import uuid4

from app.risk.time_utils import beijing_now_text


ALLOWED_SOURCE_TABLES = {"project_attachment", "contract_attachment"}
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|password|secret|token|cookie)\s*[:=]\s*[^,;\s]+"
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
QUERY_URL_RE = re.compile(r"https?://[^\s?]+[?][^\s]+")
HASH_REF_RE = re.compile(r"^[a-f0-9]{8,128}$")


class AttachmentAuthorizationStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    USED = "used"


ALLOWED_TRANSITIONS = {
    AttachmentAuthorizationStatus.DRAFT: {
        AttachmentAuthorizationStatus.APPROVED,
        AttachmentAuthorizationStatus.REJECTED,
        AttachmentAuthorizationStatus.EXPIRED,
    },
    AttachmentAuthorizationStatus.APPROVED: {
        AttachmentAuthorizationStatus.USED,
        AttachmentAuthorizationStatus.EXPIRED,
    },
    AttachmentAuthorizationStatus.REJECTED: set(),
    AttachmentAuthorizationStatus.EXPIRED: set(),
    AttachmentAuthorizationStatus.USED: set(),
}


def current_time_text() -> str:
    return beijing_now_text()


def default_expires_at(hours: int = 24) -> str:
    return beijing_now_text(datetime.now(UTC) + timedelta(hours=hours))


def normalize_authorization_status(value: str) -> AttachmentAuthorizationStatus:
    return AttachmentAuthorizationStatus(str(value))


def can_transition_authorization(current: str, target: str) -> bool:
    current_status = normalize_authorization_status(current)
    target_status = normalize_authorization_status(target)
    return target_status in ALLOWED_TRANSITIONS[current_status]


def assert_sanitized_url_ref(value: str) -> None:
    if "?" in value or "&" in value:
        raise ValueError("sanitized_url_ref must not contain URL query parameters")
    if value and "[PATH_REDACTED]" not in value and "[URL_WITH_QUERY_REDACTED]" not in value:
        raise ValueError("sanitized_url_ref must be redacted")


def assert_allowed_source_table(value: str) -> None:
    if value not in ALLOWED_SOURCE_TABLES:
        raise ValueError("source_table is not allowed for attachment authorization")


def assert_hash_ref(value: str, *, required: bool) -> None:
    text = str(value or "").strip().lower()
    if not text and not required:
        return
    if not HASH_REF_RE.fullmatch(text):
        raise ValueError("source identifier reference must be hashed")


def sanitize_authorization_text(value: object, limit: int = 300) -> str:
    text = str(value or "").strip()
    text = QUERY_URL_RE.sub("[URL_WITH_QUERY_REDACTED]", text)
    text = BEARER_RE.sub("Bearer [REDACTED]", text)
    text = SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    return text[:limit]


@dataclass(frozen=True)
class AttachmentAuthorization:
    authorization_id: str
    project_id: str
    source_table: str
    source_attach_id_hash: str
    source_biz_id_hash: str
    candidate_bucket: str
    format_group: str
    file_ext: str
    size_bucket: str
    sanitized_url_ref: str
    status: AttachmentAuthorizationStatus
    requested_by: str
    decision_reason: str
    created_at: str
    updated_at: str
    expires_at: str

    @classmethod
    def create_draft(
        cls,
        *,
        project_id: str,
        source_table: str,
        source_attach_id_hash: str,
        source_biz_id_hash: str,
        candidate_bucket: str,
        format_group: str,
        file_ext: str,
        size_bucket: str,
        sanitized_url_ref: str,
        requested_by: str,
        decision_reason: str,
        expires_at: str | None = None,
    ) -> AttachmentAuthorization:
        assert_sanitized_url_ref(sanitized_url_ref)
        assert_allowed_source_table(source_table)
        assert_hash_ref(source_attach_id_hash, required=True)
        assert_hash_ref(source_biz_id_hash, required=False)
        now = current_time_text()
        return cls(
            authorization_id=str(uuid4()),
            project_id=str(project_id),
            source_table=source_table,
            source_attach_id_hash=source_attach_id_hash,
            source_biz_id_hash=source_biz_id_hash,
            candidate_bucket=candidate_bucket,
            format_group=format_group,
            file_ext=file_ext.lower().lstrip(".")[:20],
            size_bucket=size_bucket,
            sanitized_url_ref=sanitized_url_ref,
            status=AttachmentAuthorizationStatus.DRAFT,
            requested_by=sanitize_authorization_text(requested_by, 80),
            decision_reason=sanitize_authorization_text(decision_reason, 300),
            created_at=now,
            updated_at=now,
            expires_at=expires_at or default_expires_at(),
        )

    def public_dict(self) -> dict[str, str]:
        return {
            "authorization_id": self.authorization_id,
            "project_id": self.project_id,
            "source_table": self.source_table,
            "source_attach_id_hash": self.source_attach_id_hash,
            "source_biz_id_hash": self.source_biz_id_hash,
            "candidate_bucket": self.candidate_bucket,
            "format_group": self.format_group,
            "file_ext": self.file_ext,
            "size_bucket": self.size_bucket,
            "sanitized_url_ref": self.sanitized_url_ref,
            "status": self.status.value,
            "requested_by": self.requested_by,
            "decision_reason": self.decision_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class AttachmentAuthorizationAuditEvent:
    event_id: str
    authorization_id: str
    project_id: str
    event_type: str
    event_summary: str
    created_at: str

    @classmethod
    def create(
        cls,
        *,
        authorization_id: str,
        project_id: str,
        event_type: str,
        event_summary: str,
    ) -> AttachmentAuthorizationAuditEvent:
        return cls(
            event_id=str(uuid4()),
            authorization_id=authorization_id,
            project_id=str(project_id),
            event_type=event_type,
            event_summary=sanitize_authorization_text(event_summary, 300),
            created_at=current_time_text(),
        )


class AttachmentAuthorizationService:
    def __init__(self, store):
        self.store = store

    def create_draft(self, **kwargs) -> AttachmentAuthorization:
        authorization = AttachmentAuthorization.create_draft(**kwargs)
        self.store.save_attachment_authorization(authorization)
        self.store.add_attachment_authorization_audit_event(
            authorization_id=authorization.authorization_id,
            project_id=authorization.project_id,
            event_type="created",
            event_summary="authorization draft created from sanitized candidate metadata",
        )
        return authorization

    def create_drafts_from_local_assets(
        self, project_id: str, requested_by: str = "local_agent"
    ) -> list[AttachmentAuthorization]:
        clean_project_id = str(project_id or "").strip()
        if not clean_project_id:
            raise ValueError("project_id is required")
        existing_keys = {
            (item.source_table, item.source_attach_id_hash)
            for item in self.store.list_attachment_authorizations(clean_project_id)
        }
        created: list[AttachmentAuthorization] = []
        for row in self.store.list_contract_assets(clean_project_id):
            draft_kwargs = _draft_kwargs_from_asset_row(
                row, clean_project_id, requested_by
            )
            if draft_kwargs is None:
                continue
            dedupe_key = (
                draft_kwargs["source_table"],
                draft_kwargs["source_attach_id_hash"],
            )
            if dedupe_key in existing_keys:
                continue
            authorization = self.create_draft(**draft_kwargs)
            existing_keys.add(dedupe_key)
            created.append(authorization)
        return created

    def approve(self, authorization_id: str, reason: str) -> AttachmentAuthorization:
        authorization = self.store.update_attachment_authorization_status(
            authorization_id, AttachmentAuthorizationStatus.APPROVED.value, reason
        )
        self.store.add_attachment_authorization_audit_event(
            authorization_id=authorization.authorization_id,
            project_id=authorization.project_id,
            event_type="approved",
            event_summary="authorization approved for future single-file retrieval",
        )
        return authorization

    def reject(self, authorization_id: str, reason: str) -> AttachmentAuthorization:
        authorization = self.store.update_attachment_authorization_status(
            authorization_id, AttachmentAuthorizationStatus.REJECTED.value, reason
        )
        self.store.add_attachment_authorization_audit_event(
            authorization_id=authorization.authorization_id,
            project_id=authorization.project_id,
            event_type="rejected",
            event_summary="authorization rejected",
        )
        return authorization

    def expire(self, authorization_id: str, reason: str) -> AttachmentAuthorization:
        authorization = self.store.update_attachment_authorization_status(
            authorization_id, AttachmentAuthorizationStatus.EXPIRED.value, reason
        )
        self.store.add_attachment_authorization_audit_event(
            authorization_id=authorization.authorization_id,
            project_id=authorization.project_id,
            event_type="expired",
            event_summary="authorization expired",
        )
        return authorization

    def can_retrieve(self, authorization_id: str) -> bool:
        authorization = self.store.get_attachment_authorization(authorization_id)
        if authorization is None:
            return False
        if _is_expired(authorization.expires_at):
            if authorization.status == AttachmentAuthorizationStatus.APPROVED:
                self.expire(
                    authorization.authorization_id,
                    "authorization expired before retrieval",
                )
            return False
        if authorization.status != AttachmentAuthorizationStatus.APPROVED:
            self.store.add_attachment_authorization_audit_event(
                authorization_id=authorization.authorization_id,
                project_id=authorization.project_id,
                event_type="blocked_download_attempt",
                event_summary="retrieval blocked because authorization is not approved",
            )
            return False
        return True

    def preflight_retrieval(self, authorization_id: str) -> dict[str, object]:
        authorization = self.store.get_attachment_authorization(authorization_id)
        if authorization is None:
            raise LookupError("authorization not found")
        if _is_expired(authorization.expires_at):
            if authorization.status == AttachmentAuthorizationStatus.APPROVED:
                authorization = self.expire(
                    authorization.authorization_id,
                    "authorization expired before retrieval preflight",
                )
        authorization_valid = authorization.status == AttachmentAuthorizationStatus.APPROVED
        message = (
            "Attachment retrieval is not enabled in the current phase"
            if authorization_valid
            else "Attachment retrieval blocked because authorization is not approved"
        )
        self.store.add_attachment_authorization_audit_event(
            authorization_id=authorization.authorization_id,
            project_id=authorization.project_id,
            event_type="blocked_download_attempt",
            event_summary=message,
        )
        return {
            "authorization_id": authorization.authorization_id,
            "project_id": authorization.project_id,
            "status": authorization.status.value,
            "authorization_valid": authorization_valid,
            "download_enabled": False,
            "message": message,
        }


def _is_expired(expires_at: str) -> bool:
    text = str(expires_at or "").strip()
    return bool(text and text < current_time_text())


def _draft_kwargs_from_asset_row(
    row: dict[str, object], project_id: str, requested_by: str
) -> dict[str, str] | None:
    if str(row.get("asset_kind") or "") != "attachment_candidate":
        return None
    source_table = str(row.get("source_table") or "").strip()
    if source_table not in ALLOWED_SOURCE_TABLES:
        return None
    source_ref = str(row.get("source_ref") or "").strip()
    if not source_ref:
        return None
    file_ext = str(row.get("file_ext") or "").lower().lstrip(".")[:20]
    return {
        "project_id": project_id,
        "source_table": source_table,
        "source_attach_id_hash": _stable_hash(source_ref),
        "source_biz_id_hash": "",
        "candidate_bucket": _candidate_bucket(row, file_ext),
        "format_group": _format_group(file_ext),
        "file_ext": file_ext,
        "size_bucket": _size_bucket(row.get("file_size")),
        "sanitized_url_ref": str(row.get("sanitized_url_ref") or ""),
        "requested_by": requested_by,
        "decision_reason": "draft generated from local contract asset metadata",
    }


def _stable_hash(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def _candidate_bucket(row: dict[str, object], file_ext: str) -> str:
    risk_signal = str(row.get("risk_signal") or "").strip()
    if risk_signal in {
        "likely_contract_text_candidate",
        "manual_review_text_document_candidate",
        "project_related_business_attachment",
    }:
        return risk_signal
    if file_ext in {"pdf", "doc", "docx", "wps"}:
        return "manual_review_text_document_candidate"
    return "attachment_metadata_review_candidate"


def _format_group(file_ext: str) -> str:
    if file_ext in {"pdf", "doc", "docx", "wps", "txt", "rtf"}:
        return "text_document"
    if file_ext in {"jpg", "jpeg", "png", "bmp", "tif", "tiff"}:
        return "image"
    if file_ext in {"xls", "xlsx", "csv"}:
        return "spreadsheet"
    if file_ext in {"zip", "rar", "7z"}:
        return "archive"
    return "unknown"


def _size_bucket(value: object) -> str:
    try:
        size = max(0, int(value or 0))
    except (TypeError, ValueError):
        size = 0
    if size == 0:
        return "unknown"
    if size < 1024:
        return "<1KB"
    if size < 1024 * 1024:
        return "1KB-1MB"
    if size < 10 * 1024 * 1024:
        return "1MB-10MB"
    return ">=10MB"
