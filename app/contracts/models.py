from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ContractMetadata:
    contract_id: str
    project_id: str
    contract_code: str = ""
    contract_name: str = ""
    contract_type: str = ""
    total_amount: str = ""
    status: str = ""
    contract_status: str = ""
    sign_date: str = ""
    start_date: str = ""
    end_date: str = ""
    has_project_link: bool = False
    # Owning organisation. For standalone contracts this is the only stable
    # grouping dimension there is, since they belong to no project. Measured
    # before relying on it: populated on every row. An ID, never a name.
    org_id: str = ""
    source_table: str = "contract_record"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AttachmentCandidate:
    attach_id: str
    project_id: str
    biz_type: str
    biz_id: str
    file_name: str
    file_ext: str
    file_size: int
    sanitized_url_ref: str
    source_table: str = "project_attachment"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ContractDiscoveryResult:
    project_id: str
    contracts: list[ContractMetadata]
    attachments: list[AttachmentCandidate]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "contracts": [item.to_dict() for item in self.contracts],
            "attachments": [item.to_dict() for item in self.attachments],
            "warnings": list(self.warnings),
        }
