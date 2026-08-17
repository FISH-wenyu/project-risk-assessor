from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .models import ContractDiscoveryResult


@dataclass(frozen=True)
class ContractAsset:
    asset_id: str
    project_id: str
    asset_kind: str
    source_ref: str
    display_name: str = ""
    file_ext: str = ""
    file_size: int = 0
    status: str = "discovered"
    risk_signal: str = ""
    sanitized_url_ref: str = ""
    source_table: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_contract_assets(result: ContractDiscoveryResult) -> list[ContractAsset]:
    project_id = str(result.project_id)
    assets: list[ContractAsset] = []
    for contract in result.contracts:
        source_ref = str(contract.contract_id or "").strip()
        if not source_ref:
            continue
        assets.append(
            ContractAsset(
                asset_id=f"contract:{source_ref}",
                project_id=project_id,
                asset_kind="contract_metadata",
                source_ref=source_ref,
                display_name=str(contract.contract_name or contract.contract_code or source_ref),
                status="no_text",
                risk_signal="metadata_only",
                source_table=contract.source_table,
            )
        )
    for attachment in result.attachments:
        source_ref = str(attachment.attach_id or "").strip()
        if not source_ref:
            continue
        assets.append(
            ContractAsset(
                asset_id=f"attachment:{source_ref}",
                project_id=project_id,
                asset_kind="attachment_candidate",
                source_ref=source_ref,
                display_name=str(attachment.file_name or source_ref),
                file_ext=str(attachment.file_ext or ""),
                file_size=max(0, int(attachment.file_size or 0)),
                status="ready_for_extraction",
                risk_signal="attachment_candidate",
                sanitized_url_ref=str(attachment.sanitized_url_ref or ""),
                source_table=attachment.source_table,
            )
        )
    return assets


def contract_asset_counts(assets: list[ContractAsset]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    contract_count = 0
    attachment_count = 0
    signals: set[str] = set()
    for asset in assets:
        by_status[asset.status] = by_status.get(asset.status, 0) + 1
        if asset.asset_kind == "contract_metadata":
            contract_count += 1
        if asset.asset_kind == "attachment_candidate":
            attachment_count += 1
        if asset.risk_signal:
            signals.add(asset.risk_signal)
    if not assets:
        signals.add("missing_contracts")
    return {
        "total": len(assets),
        "contract_metadata": contract_count,
        "attachment_candidate": attachment_count,
        "by_status": by_status,
        "signals": sorted(signals),
    }
