from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from .models import ProjectRiskResult


_LLM_FALLBACK_EXCEPTIONS = (
    OSError,
    urllib.error.URLError,
    KeyError,
    IndexError,
    TypeError,
    AttributeError,
    ValueError,
)


@dataclass(frozen=True)
class LlmExplanation:
    content: str
    configured: bool
    attempted: bool
    model: str
    fallback_used: bool
    sanitized_payload_hash: str
    sanitized_payload_chars: int
    sanitized_dimension_summary: list[dict[str, Any]]


def build_sanitized_prompt_metadata(result: ProjectRiskResult) -> dict[str, Any]:
    payload = build_sanitized_prompt_payload(result)
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return {
        "sanitized_payload_hash": sha256(serialized.encode("utf-8")).hexdigest(),
        "sanitized_payload_chars": len(serialized),
        "sanitized_dimension_summary": [
            {
                "name": dimension.name,
                "score": dimension.score,
                "hit_count": sum(1 for hit in result.hits if hit.dimension == dimension.name),
            }
            for dimension in result.dimensions
        ],
    }


def build_sanitized_prompt_payload(result: ProjectRiskResult) -> dict[str, Any]:
    return {
        "project_id": result.project_id,
        "risk_level": result.level,
        "risk_score": result.score,
        "rule_version": result.rule_version,
        "dimensions": [
            {"name": item.name, "score": item.score, "summary": item.summary}
            for item in result.dimensions
        ],
        "risk_hits": [
            {
                "dimension": hit.dimension,
                "severity": hit.severity,
                "reason": hit.reason,
                "evidence": _scrub_evidence(hit.evidence),
            }
            for hit in result.hits[:12]
        ],
        "suggestions": result.suggestions[:8],
    }


def explain_with_fallback(result: ProjectRiskResult, settings: dict[str, str] | Any) -> str:
    return explain_with_audit(result, settings).content


def explain_with_audit(result: ProjectRiskResult, settings: dict[str, str] | Any) -> LlmExplanation:
    api_key = _get_setting(settings, "api_key") or _get_setting(settings, "llm_api_key")
    base_url = _get_setting(settings, "base_url") or _get_setting(settings, "llm_base_url")
    model = normalize_model_name(_get_setting(settings, "model") or _get_setting(settings, "llm_model"))
    configured = bool(api_key and base_url and model)
    metadata = build_sanitized_prompt_metadata(result)
    if not configured:
        return LlmExplanation(
            content=_fallback_explanation(result),
            configured=False,
            attempted=False,
            model=model,
            fallback_used=True,
            **metadata,
        )
    try:
        content = _call_openai_compatible(base_url, api_key, model, build_sanitized_prompt_payload(result))
        return LlmExplanation(
            content=content,
            configured=True,
            attempted=True,
            model=model,
            fallback_used=False,
            **metadata,
        )
    except _LLM_FALLBACK_EXCEPTIONS:
        return LlmExplanation(
            content=_fallback_explanation(result),
            configured=True,
            attempted=True,
            model=model,
            fallback_used=True,
            **metadata,
        )


# The old prompt said only "输出简洁中文解释和建议", so the model picked its own
# format: emoji headings, `--- ###` runs, and a dimension table. The frontend now
# parses Markdown, but leaving the format unstated means every model and every
# version renders differently, and the parser would be chasing a moving target.
# Naming the structure makes the output predictable; the parser still degrades
# gracefully if a model ignores this.
#
# Emoji are excluded deliberately. They carry no information the severity badge
# does not already carry, and a 📊 in a heading is noise in a risk report.
SYSTEM_PROMPT = """你是项目风险评估助手，只基于脱敏规则摘要输出中文解释和建议。

严格按以下结构输出 Markdown，不要添加其他章节：

1. 一段不超过 120 字的总体判断，说明风险集中在哪里。
2. 标题 `### 各维度解读`，其下是一个表格，表头固定为
   `| 维度 | 得分 | 核心问题 | 建议 |`。每个维度一行。
3. 标题 `### 处理优先级`，其下是有序列表，每项以「立即」「尽快」「同步」开头。

格式要求：
- 表格必须包含 `|---|` 分隔行，且每行单独成行。
- 列表只用 `1.` 或 `-`，不要用 `*` 作为列表符号。
- 不要使用 emoji、水平分隔线，也不要在正文里堆叠 `**`。
- 只陈述摘要中已有的事实，不要推测项目名称、人员或金额。"""


def _call_openai_compatible(base_url: str, api_key: str, model: str, payload: dict[str, Any]) -> str:
    url = base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "temperature": 0.2,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


def normalize_model_name(model: str) -> str:
    aliases = {
        "deepseek": "deepseek-v4-flash",
    }
    clean = str(model or "").strip()
    return aliases.get(clean, clean)


def _fallback_explanation(result: ProjectRiskResult) -> str:
    top_hits = "；".join(hit.reason for hit in result.hits[:3]) or "暂未命中明显风险项"
    suggestions = "；".join(result.suggestions[:3]) or "持续跟踪项目执行和资料更新"
    return f"该项目当前为{result.level}风险，综合分{result.score}。主要风险：{top_hits}。建议：{suggestions}。"


def _scrub_evidence(value: str) -> str:
    text = str(value)
    for marker in ("partner=", "owner=", "name="):
        if marker in text:
            return marker + "[已脱敏]"
    return text[:120]


def _get_setting(settings: dict[str, str] | Any, key: str) -> str:
    if isinstance(settings, dict):
        return settings.get(key, "")
    return getattr(settings, key, "")
