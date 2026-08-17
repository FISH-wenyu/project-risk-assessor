from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_database: str = ""
    mysql_user: str = ""
    mysql_password: str = ""
    mysql_allowed_tables: tuple[str, ...] = ("project_category",)
    agent_password: str = ""
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    sqlite_path: Path = Path("data/risk_agent.db")
    mapping_path: Path = Path("config/project_mapping.json")
    memory_recent_messages: int = 12
    memory_context_chars: int = 12000
    memory_retrieval_limit: int = 6
    qdrant_path: Path = Path("data/qdrant")
    qdrant_collection: str = "risk_agent_memory"
    embedding_model: str = (
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )
    embedding_version: str = "1"
    embedding_dimension: int = 384
    # Hosts whose contract attachments may be fetched. Empty means FETCH
    # NOTHING (fail-closed), matching contract discovery's allowlist semantics
    # rather than app/db.py's fail-open one. Attachment URLs come out of the
    # database, which is untrusted input: without an allowlist, a URL edited in
    # the source system would turn this into a server-side request forgery.
    contract_attachment_hosts: tuple[str, ...] = ()
    contract_attachment_dir: Path = Path("data/contract-attachments")
    # Skip the resolved-address SSRF check. Only for machines where an HTTP
    # proxy does DNS interception, which makes local resolution meaningless
    # because the proxy, not this process, dials the host. The host allowlist
    # still applies and remains the real control.
    contract_attachment_allow_private: bool = False

    # Field names whose values must never be rendered. The dataclass default
    # `__repr__` prints every field, so ANY traceback, log line or exception
    # message that happens to contain a Settings object publishes the source
    # database password and the LLM API key in clear text.
    #
    # This is not hypothetical. On 2026-08-14 a wrong argument to
    # `create_embedding_provider` produced a `ValueError` whose message
    # embedded `repr(settings)`, and the credentials went into the traceback.
    # The audit sanitizer did not catch them either: it matched `password=`
    # but not `mysql_password=`, because there is no word boundary between
    # `_` and `password`. Both ends are fixed; this is the one that matters,
    # because it stops the value from ever being formatted.
    _SECRET_FIELDS = ("mysql_password", "agent_password", "llm_api_key")

    def __repr__(self) -> str:
        parts = []
        for field_info in fields(self):
            value = getattr(self, field_info.name)
            if field_info.name in self._SECRET_FIELDS:
                # Presence is useful for debugging, the value never is.
                value = "***set***" if value else "***unset***"
                parts.append(f"{field_info.name}={value}")
            else:
                parts.append(f"{field_info.name}={value!r}")
        return f"Settings({', '.join(parts)})"

    __str__ = __repr__

    def __post_init__(self) -> None:
        for field_name in (
            "memory_recent_messages",
            "memory_context_chars",
            "memory_retrieval_limit",
            "embedding_dimension",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        for field_name in (
            "qdrant_collection",
            "embedding_model",
            "embedding_version",
        ):
            if not str(getattr(self, field_name) or "").strip():
                raise ValueError(f"{field_name} must not be empty")

    @property
    def mysql_configured(self) -> bool:
        return bool(self.mysql_database and self.mysql_user)

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)


def table_allowed(table_name: str, allowed_tables: tuple[str, ...]) -> bool:
    """Whether a source table may be read. **Fail-closed.**

    The single definition. There were four copies of this rule and they did
    not agree: `app.db` and two audit helpers read an empty allowlist as
    "everything is allowed", while contract discovery read it as "nothing is
    allowed". So a deployment with a missing or unparseable
    `MYSQL_ALLOWED_TABLES` got full read access to the source database from
    one half of the product and none from the other, and the audit record
    reported whichever answer its own copy happened to give.

    Fail-closed wins because the failure mode of the other direction points
    the wrong way: forgetting the variable should not grant MORE access to
    somebody else's production database. `Settings` defaults the field to
    `("project_category",)`, so it is only ever empty when set that way
    deliberately.

    Lives in `config` rather than `db` because everything already imports
    config, and putting it in `db` would have `app.risk.audit` importing
    `app.db`, which imports `app.risk.models` - a loop waiting to happen.
    """
    return table_name in (allowed_tables or ())


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    # `utf-8-sig`, not `utf-8`. This file gets edited on Windows, where the
    # obvious tools write a BOM: `Set-Content -Encoding utf8` in Windows
    # PowerShell 5.1 does, and so does Notepad's "UTF-8" until recently.
    #
    # Read as plain utf-8, a BOM stays in the string and lands on the FIRST
    # key, so `MYSQL_HOST=...` silently becomes `﻿MYSQL_HOST=...`. Python's
    # `str.strip()` does not remove `﻿` - it is not whitespace - so the
    # variable is set under a name nothing reads, `MYSQL_HOST` falls back to
    # its 127.0.0.1 default, and the connection failure points nowhere near
    # the real cause. `utf-8-sig` reads files with and without a BOM.
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        # Belt and braces: a BOM in the middle of a file (concatenated
        # fragments) would not be caught by the decoder above.
        key = key.strip().lstrip("﻿")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load_settings() -> Settings:
    load_dotenv()
    return Settings(
        mysql_host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        mysql_port=int(os.getenv("MYSQL_PORT", "3306")),
        mysql_database=os.getenv("MYSQL_DATABASE", ""),
        mysql_user=os.getenv("MYSQL_USER", ""),
        mysql_password=os.getenv("MYSQL_PASSWORD", ""),
        mysql_allowed_tables=tuple(
            item.strip()
            for item in os.getenv("MYSQL_ALLOWED_TABLES", "project_category").split(",")
            if item.strip()
        ),
        agent_password=os.getenv("AGENT_PASSWORD", ""),
        llm_base_url=os.getenv("LLM_BASE_URL", ""),
        llm_api_key=os.getenv("LLM_API_KEY", ""),
        llm_model=os.getenv("LLM_MODEL", ""),
        sqlite_path=Path(os.getenv("SQLITE_PATH", "data/risk_agent.db")),
        mapping_path=Path(os.getenv("MAPPING_PATH", "config/project_mapping.json")),
        memory_recent_messages=int(os.getenv("MEMORY_RECENT_MESSAGES", "12")),
        memory_context_chars=int(os.getenv("MEMORY_CONTEXT_CHARS", "12000")),
        memory_retrieval_limit=int(os.getenv("MEMORY_RETRIEVAL_LIMIT", "6")),
        qdrant_path=Path(os.getenv("QDRANT_PATH", "data/qdrant")),
        qdrant_collection=os.getenv("QDRANT_COLLECTION", "risk_agent_memory"),
        embedding_model=os.getenv(
            "EMBEDDING_MODEL",
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        ),
        embedding_version=os.getenv("EMBEDDING_VERSION", "1"),
        embedding_dimension=int(os.getenv("EMBEDDING_DIMENSION", "384")),
        # Default empty: fetching is off until a host is named explicitly.
        contract_attachment_hosts=tuple(
            item.strip().lower()
            for item in os.getenv("CONTRACT_ATTACHMENT_HOSTS", "").split(",")
            if item.strip()
        ),
        contract_attachment_dir=Path(
            os.getenv("CONTRACT_ATTACHMENT_DIR", "data/contract-attachments")
        ),
        contract_attachment_allow_private=os.getenv(
            "CONTRACT_ATTACHMENT_ALLOW_PRIVATE", ""
        ).strip().lower()
        in {"1", "true", "yes"},
    )
