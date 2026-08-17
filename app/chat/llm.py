from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import UUID


_DATABASE_URL = re.compile(
    r"(?i)\b(?:mysql|mariadb|postgres(?:ql)?|sqlserver|oracle|redis|mongodb)"
    r"(?:\+[A-Za-z0-9_.-]+)?://[^\s,;,\u3002'\"<>]+"
)
_HTTP_URL = re.compile(r"(?i)\bhttps?://[^\s,;,\u3002'\"<>]+")
_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
    r"[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]+=*")
_COOKIE = re.compile(r"(?im)\bcookie\s*[:=:\uff1a\uff1d]\s*[^\r\n]+")
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(?<![\w-])[\"']?"
    r"(x[-_]api[-_]key|x[-_]auth[-_]token|api[_-]?key|access[_-]?key|"
    r"secret[_-]?key|token|password|passwd|secret|credential|account|"
    r"username|user[_-]?name|login(?:[_-]?name)?|cookie|"
    r"\u8d26\u53f7|\u8d26\u6237|\u7528\u6237\u540d|\u767b\u5f55\u540d|"
    r"\u5bc6\u7801|\u53e3\u4ee4|\u5bc6\u94a5)[\"']?"
    r"\s*[:=:\uff1a\uff1d]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;,\u3002]+)"
)
_EMAIL = re.compile(r"(?<![\w.-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PHONE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
_ACCOUNT_LIKE = re.compile(
    r"(\u8d26\u53f7|\u8d26\u6237|\u94f6\u884c\u5361\u53f7|"
    r"\u8eab\u4efd\u8bc1\u53f7|\u8bc1\u4ef6\u53f7)"
    r"\s*[:=:\uff1a\uff1d]\s*[A-Za-z0-9*_-]{6,}"
)
_SAFE_HISTORY_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class PreparedPrompt:
    messages: list[dict[str, str]]
    payload: dict[str, Any]
    payload_json: str
    allowed_citations: dict[str, dict[str, str]]


@dataclass(frozen=True)
class LlmAnswer:
    answer: str
    citation_ids: list[str]
    raw_text: str
    model: str


class NullChatLlmClient:
    available = False
    reason = "llm not configured"

    def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        model: str | None = None,
    ) -> Mapping[str, Any]:
        raise RuntimeError(self.reason)


class OpenAICompatibleChatClient:
    def __init__(
        self, *, base_url: str, api_key: str, model: str, timeout: float = 45.0
    ):
        self.base_url = str(base_url or "").rstrip("/")
        self.api_key = str(api_key or "")
        self.model = str(model or "")
        self.timeout = float(timeout)

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        model: str | None = None,
        json_response: bool = True,
    ) -> Mapping[str, Any]:
        # `json_response` defaults to True so the existing project chat, which
        # parses a JSON object out of the reply, is unaffected. Contract chat
        # wants prose and passes False.
        if not self.available:
            raise RuntimeError("llm not configured")
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": list(messages),
            "temperature": 0.2,
        }
        if json_response:
            payload["response_format"] = {"type": "json_object"}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as exc:
            raise RuntimeError(type(exc).__name__) from exc
        decoded = json.loads(body)
        if not isinstance(decoded, dict):
            raise RuntimeError("llm response was not an object")
        return decoded


def sanitize_outbound_text(value: Any, *, limit: int = 2000) -> str:
    text = str(value or "")
    text = _DATABASE_URL.sub("[REDACTED_DATABASE_URL]", text)
    text = _HTTP_URL.sub(_redact_url, text)
    text = _JWT.sub("[REDACTED_JWT]", text)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _COOKIE.sub("cookie=[REDACTED]", text)
    text = _ACCOUNT_LIKE.sub(
        lambda match: f"{match.group(1)}=[REDACTED]",
        text,
    )
    text = _SENSITIVE_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}=[REDACTED]",
        text,
    )
    text = _EMAIL.sub("[REDACTED_EMAIL]", text)
    text = _PHONE.sub("[REDACTED_PHONE]", text)
    text = text.replace("\r", " ").strip()
    if len(text) > limit:
        return text[: max(0, limit - 12)] + "[TRUNCATED]"
    return text


def build_prompt(
    *,
    question: str,
    risk_context: Mapping[str, Any] | None,
    conversation_summary: str,
    recent_messages: Sequence[Mapping[str, Any]],
    memories: Sequence[Mapping[str, Any]],
    total_char_limit: int = 12000,
) -> PreparedPrompt:
    risk_summary, risk_citations = _compact_risk_summary(risk_context)
    memory_payload: list[dict[str, Any]] = []
    allowed = dict(risk_citations)
    for memory in memories:
        citation_id = _safe_memory_citation_id(memory.get("citation_id"))
        text = sanitize_outbound_text(memory.get("canonical_text"), limit=1000)
        if not citation_id or not text:
            continue
        memory_payload.append(
            {
                "citation_id": citation_id,
                "memory_type": sanitize_outbound_text(
                    memory.get("memory_type"), limit=80
                ),
                "canonical_text": text,
                "confidence": _safe_float(memory.get("confidence")),
            }
        )
        allowed[citation_id] = {
            "source_type": "memory",
            "source_id": citation_id,
            "label": "Project memory",
        }

    payload: dict[str, Any] = {
        "question": sanitize_outbound_text(question, limit=2000),
        "risk_summary": risk_summary,
        "conversation_summary": sanitize_outbound_text(
            conversation_summary, limit=2500
        ),
        "recent_messages": [
            {
                "role": str(message.get("role") or "")[:20],
                "content": sanitize_outbound_text(message.get("content"), limit=1200),
            }
            for message in recent_messages
            if str(message.get("role") or "") in {"user", "assistant", "system"}
        ],
        "memories": memory_payload,
    }
    payload_json = _bounded_json(payload, total_char_limit)
    return PreparedPrompt(
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a project risk analysis assistant. Use only the "
                    "sanitized payload. Return JSON shaped as "
                    '{"answer":"...","citation_ids":["risk-history:42","memory:uuid"]}.'
                ),
            },
            {"role": "user", "content": payload_json},
        ],
        payload=json.loads(payload_json),
        payload_json=payload_json,
        allowed_citations=allowed,
    )


def parse_llm_answer(
    response: Mapping[str, Any] | str, *, requested_model: str = ""
) -> LlmAnswer:
    model = requested_model
    raw: str
    if isinstance(response, str):
        raw = response
    elif "choices" in response:
        model = str(response.get("model") or requested_model)
        choices = response.get("choices") or []
        first = choices[0] if isinstance(choices, list) and choices else {}
        message = first.get("message") if isinstance(first, Mapping) else {}
        raw = str((message or {}).get("content") or "")
    elif "answer" in response:
        answer = sanitize_outbound_text(response.get("answer"), limit=4000)
        citation_ids = _citation_ids(response.get("citation_ids"))
        return LlmAnswer(
            answer or "No answer is available.",
            citation_ids,
            json.dumps(response, ensure_ascii=False),
            model,
        )
    else:
        raw = str(response.get("content") or response.get("text") or "")

    parsed = _json_object(raw)
    if parsed is not None:
        answer = sanitize_outbound_text(parsed.get("answer"), limit=4000)
        citation_ids = _citation_ids(parsed.get("citation_ids"))
        return LlmAnswer(answer or "No answer is available.", citation_ids, raw, model)
    return LlmAnswer(
        sanitize_outbound_text(raw, limit=4000) or "No answer is available.",
        [],
        raw,
        model,
    )


def _compact_risk_summary(
    risk_context: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    if not risk_context:
        return {}, {}
    latest = risk_context.get("latest") if isinstance(risk_context, Mapping) else None
    if not isinstance(latest, Mapping):
        latest = risk_context
    history_id = _safe_history_id(latest.get("history_id") or latest.get("id"))
    summary = {
        "score": _safe_int(latest.get("score")),
        "level": sanitize_outbound_text(latest.get("level"), limit=20),
        "dimensions": _sanitize_dimensions(latest.get("dimensions"), limit=8),
        "risk_hits": _sanitize_hits(
            latest.get("risk_hits") or latest.get("hits"),
            limit=12,
        ),
        "suggestions": _sanitize_suggestions(latest.get("suggestions"), limit=10),
        "history_id": history_id,
        "created_at": sanitize_outbound_text(latest.get("created_at"), limit=40),
    }
    citations: dict[str, dict[str, str]] = {}
    if history_id is not None and str(history_id).strip():
        citation_id = f"risk-history:{history_id}"
        citations[citation_id] = {
            "source_type": "risk_history",
            "source_id": citation_id,
            "label": f"Latest risk result #{history_id}",
        }
    return summary, citations


def _sanitize_dimensions(value: Any, *, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value[:limit]:
        if not isinstance(item, Mapping):
            continue
        result.append(
            {
                "name": sanitize_outbound_text(item.get("name"), limit=80),
                "score": _safe_int(item.get("score")),
                "summary": sanitize_outbound_text(item.get("summary"), limit=240),
            }
        )
    return result


def _sanitize_hits(value: Any, *, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value[:limit]:
        if isinstance(item, Mapping):
            result.append(
                {
                    "rule": sanitize_outbound_text(
                        item.get("rule") or item.get("rule_id") or item.get("name"),
                        limit=120,
                    ),
                    "severity": sanitize_outbound_text(item.get("severity"), limit=40),
                    "summary": sanitize_outbound_text(
                        item.get("summary")
                        or item.get("evidence")
                        or item.get("reason"),
                        limit=240,
                    ),
                }
            )
        else:
            result.append(
                {
                    "rule": sanitize_outbound_text(item, limit=180),
                    "severity": "",
                    "summary": "",
                }
            )
    return result


def _sanitize_suggestions(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:limit]:
        if isinstance(item, (str, int, float)) and not isinstance(item, bool):
            result.append(sanitize_outbound_text(item, limit=240))
    return result


def _safe_history_id(value: Any) -> str | int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    text = sanitize_outbound_text(value, limit=80)
    if _SAFE_HISTORY_ID.fullmatch(text):
        return text
    return None


def _safe_memory_citation_id(value: Any) -> str:
    text = str(value or "").strip()
    prefix = "memory:"
    if not text.startswith(prefix):
        return ""
    memory_id = text[len(prefix):]
    try:
        UUID(memory_id)
    except (TypeError, ValueError):
        return ""
    return text


def _bounded_json(payload: dict[str, Any], total_char_limit: int) -> str:
    limit = max(1000, int(total_char_limit or 12000))
    current = deepcopy(payload)
    for key in ("recent_messages", "memories"):
        while _json_len(current) > limit and current.get(key):
            current[key] = list(current[key])[:-1]
    while _json_len(current) > limit and current.get("conversation_summary"):
        current["conversation_summary"] = str(current["conversation_summary"])[
            : max(0, len(str(current["conversation_summary"])) // 2)
        ]
    while _json_len(current) > limit and current.get("question"):
        current["question"] = str(current["question"])[
            : max(0, len(str(current["question"])) // 2)
        ]
    risk = current.get("risk_summary")
    if isinstance(risk, dict):
        for key in ("risk_hits", "suggestions", "dimensions"):
            while _json_len(current) > limit and risk.get(key):
                risk[key] = list(risk[key])[:-1]
    if _json_len(current) > limit:
        risk = current.get("risk_summary")
        risk = risk if isinstance(risk, dict) else {}
        current = {
            "question": str(current.get("question") or "")[:200],
            "risk_summary": {
                "score": risk.get("score"),
                "level": risk.get("level") or "",
                "dimensions": [],
                "risk_hits": [],
                "suggestions": [],
                "history_id": risk.get("history_id"),
                "created_at": risk.get("created_at") or "",
            },
            "conversation_summary": "",
            "recent_messages": [],
            "memories": [],
        }
    while _json_len(current) > limit and current.get("question"):
        current["question"] = str(current["question"])[:-20]
    return json.dumps(current, ensure_ascii=False, separators=(",", ":"))


def _json_len(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _json_object(raw: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _citation_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        text = str(item or "").strip()
        if text:
            result.append(text)
    return result


def _redact_url(match: re.Match[str]) -> str:
    url = match.group(0)
    return "[REDACTED_URL_WITH_QUERY]" if "?" in url else "[REDACTED_URL]"


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
