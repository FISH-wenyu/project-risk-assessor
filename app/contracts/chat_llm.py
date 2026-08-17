"""Adapter between `ContractChatService` and the shared LLM transport.

The transport (`OpenAICompatibleChatClient`) already handles auth, timeouts and
error wrapping, so this only translates the contract payload into messages and
pulls the text back out. Reusing it means contract chat inherits the same
network behaviour as project chat rather than growing a second one.

The payload has already passed selection, redaction and the fail-closed gate
before it reaches here. This module adds no data; it only serialises what it
is given, so it cannot widen what leaves the machine.
"""

from __future__ import annotations

import json
from typing import Any

# Bounded so a large clause set cannot produce an unbounded request body. The
# pipeline already caps clause characters; this is a second, cruder ceiling on
# the serialised whole.
MAX_PAYLOAD_CHARS = 24000


class ContractChatLlmClient:
    def __init__(self, transport: Any, *, model: str | None = None):
        self.transport = transport
        self.model = model

    @property
    def available(self) -> bool:
        return bool(getattr(self.transport, "available", False))

    def complete(self, system_prompt: str, payload: dict[str, Any]) -> str:
        if not self.available:
            raise RuntimeError("llm not configured")
        body = json.dumps(payload, ensure_ascii=False)[:MAX_PAYLOAD_CHARS]
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    "以下是合同风险规则命中与相关条款片段（JSON）。"
                    "请只依据这些内容回答，引用时写明合同编号与条款序号。\n\n" + body
                ),
            },
        ]
        # Prose, not JSON: the answer is read by a person, and the citation
        # check runs over the text itself.
        decoded = self.transport.complete(messages, model=self.model, json_response=False)
        return _first_message_text(decoded)


def _first_message_text(decoded: Any) -> str:
    """Pull the assistant text out of an OpenAI-shaped response.

    Returns "" rather than raising on an unexpected shape: the service treats
    an empty answer as a degraded result and falls back, which is better than
    a 500 for a provider that changed its envelope.
    """
    if not isinstance(decoded, dict):
        return ""
    choices = decoded.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return ""
    return str(message.get("content") or "").strip()
