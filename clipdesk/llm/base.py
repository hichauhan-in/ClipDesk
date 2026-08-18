"""The LLM abstraction.

Everything above this layer speaks in :class:`ChatMessage` and gets back text.
Which model actually answers — Copilot through VS Code, Copilot through the CLI,
or an OpenAI-compatible endpoint — is a configuration choice, not a code change.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol


class LLMError(RuntimeError):
    """A request failed for a reason the user can usually act on."""


class LLMUnavailableError(LLMError):
    """The provider is not reachable or not configured yet."""


@dataclass(slots=True)
class ChatMessage:
    role: str  # "system" | "user" | "assistant"
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


#: Characters per token, averaged over English prose. Only used when a provider
#: does not report real counts, and always flagged as an estimate when shown.
CHARS_PER_TOKEN = 4


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: True when the provider counted these, False when we estimated them.
    measured: bool = False

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total,
            "measured": self.measured,
        }


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    usage: Usage = field(default_factory=Usage)


def estimate_usage(messages: list[ChatMessage], reply: str) -> Usage:
    """A character-count fallback for providers that report nothing."""
    prompt = sum(len(message.content) for message in messages)
    return Usage(
        prompt_tokens=-(-prompt // CHARS_PER_TOKEN),
        completion_tokens=-(-len(reply) // CHARS_PER_TOKEN),
        measured=False,
    )


def usage_from_payload(payload: dict[str, Any] | None) -> Usage | None:
    """Read an OpenAI-shaped ``usage`` block, if the provider sent one."""
    if not isinstance(payload, dict):
        return None
    prompt = payload.get("prompt_tokens", payload.get("input_tokens"))
    completion = payload.get("completion_tokens", payload.get("output_tokens"))
    if prompt is None and completion is None:
        return None
    return Usage(
        prompt_tokens=int(prompt or 0),
        completion_tokens=int(completion or 0),
        measured=True,
    )


@dataclass(slots=True)
class ProviderStatus:
    key: str
    label: str
    available: bool
    detail: str = ""
    models: list[str] = field(default_factory=list)
    active_model: str = ""
    setup_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "available": self.available,
            "detail": self.detail,
            "models": self.models,
            "active_model": self.active_model,
            "setup_hint": self.setup_hint,
        }


class LLMProvider(Protocol):
    key: str
    label: str

    def status(self) -> ProviderStatus: ...

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        expect_json: bool = False,
        model: str | None = None,
    ) -> Completion: ...


# --- JSON handling -----------------------------------------------------------
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

JSON_INSTRUCTION = (
    "Respond with a single JSON object and nothing else. Begin your reply with the "
    "character { and end it with }. No prose, no explanation, no acknowledgement, no "
    "markdown code fence."
)


def _balanced_slice(text: str) -> str | None:
    """Extract the first balanced ``{...}`` or ``[...]`` block, ignoring braces
    that appear inside string literals."""
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
    return None


def extract_json(text: str) -> Any:
    """Parse JSON out of a model reply that may be fenced or chatty.

    Models are inconsistent about honouring "JSON only", and a single stray
    sentence should not cost a whole analysis window. Tried in order: the raw
    string, the contents of a code fence, then the first balanced bracket block.
    """
    candidates: list[str] = []
    stripped = text.strip()
    if stripped:
        candidates.append(stripped)
    if (fence := _FENCE_RE.search(text)) is not None:
        candidates.append(fence.group(1).strip())
    if (block := _balanced_slice(text)) is not None:
        candidates.append(block)

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # Trailing commas are the single most common malformation.
            repaired = re.sub(r",\s*([}\]])", r"\1", candidate)
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                continue

    # Quote what the model actually said. Without it "did not return valid JSON"
    # is unactionable — the reply is usually a refusal or a request for input,
    # which points at the prompt rather than at the parser.
    preview = " ".join(text.split())[:220] or "(empty response)"
    raise LLMError(f"Model did not return JSON. It replied: {preview}")
