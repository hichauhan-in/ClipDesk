"""Any endpoint that speaks the OpenAI chat-completions shape.

Covers Azure OpenAI, Azure AI Foundry, an internal gateway, or a local runtime
such as Ollama or LM Studio. This is the path to use if the organisation stands
up a governed endpoint later — nothing above this layer changes.

The key is read from an environment variable rather than stored in config, so a
secret never lands in a YAML file that might be committed or shared.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from clipdesk.config import OpenAICompatConfig
from clipdesk.llm.base import (
    ChatMessage,
    Completion,
    LLMError,
    LLMUnavailableError,
    ProviderStatus,
    estimate_usage,
    usage_from_payload,
)

SETUP_HINT = (
    "Set llm.openai_compat.base_url in config/local.yaml and put the key in the "
    "environment variable named by llm.openai_compat.api_key_env."
)


class OpenAICompatProvider:
    key = "openai_compat"
    label = "OpenAI-compatible endpoint"

    def __init__(self, config: OpenAICompatConfig) -> None:
        self.config = config

    def _api_key(self) -> str:
        return os.environ.get(self.config.api_key_env, "").strip()

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        key = self._api_key()
        if key:
            if self.config.auth_style.lower() == "api-key":
                headers["api-key"] = key
            else:
                headers["authorization"] = f"Bearer {key}"
        return headers

    def status(self) -> ProviderStatus:
        if not self.config.base_url:
            return ProviderStatus(
                self.key, self.label, False, "No base_url configured.", setup_hint=SETUP_HINT
            )
        if not self._api_key():
            return ProviderStatus(
                self.key,
                self.label,
                False,
                f"Environment variable {self.config.api_key_env} is not set.",
                setup_hint=SETUP_HINT,
            )

        base = self.config.base_url.rstrip("/")
        try:
            response = httpx.get(f"{base}/models", headers=self._headers(), timeout=8.0)
            if response.status_code >= 400:
                # Plenty of gateways expose /chat/completions but not /models;
                # that is not a failure worth reporting as "unavailable".
                return ProviderStatus(
                    self.key,
                    self.label,
                    True,
                    f"Configured ({base}); model listing unavailable.",
                    active_model=self.config.model,
                )
            payload: dict[str, Any] = response.json()
            models = [str(item.get("id")) for item in payload.get("data") or [] if item.get("id")]
        except Exception as exc:  # noqa: BLE001
            return ProviderStatus(
                self.key, self.label, False, f"Could not reach {base}: {exc}", setup_hint=SETUP_HINT
            )

        return ProviderStatus(
            self.key,
            self.label,
            True,
            f"Connected to {base}",
            models=sorted(models),
            active_model=self.config.model,
        )

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        expect_json: bool = False,
        model: str | None = None,
    ) -> Completion:
        if not self.config.base_url:
            raise LLMUnavailableError(f"No endpoint configured. {SETUP_HINT}")
        if not self._api_key():
            raise LLMUnavailableError(
                f"Environment variable {self.config.api_key_env} is not set."
            )

        body: dict[str, Any] = {
            "model": model or self.config.model,
            "messages": [message.to_dict() for message in messages],
            "temperature": temperature,
        }
        if max_tokens:
            body["max_tokens"] = max_tokens
        if expect_json:
            body["response_format"] = {"type": "json_object"}

        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        try:
            response = httpx.post(
                url,
                headers=self._headers(),
                json=body,
                timeout=httpx.Timeout(15.0, read=self.config.request_timeout_s),
            )
        except httpx.RequestError as exc:
            raise LLMUnavailableError(f"Could not reach {url}: {exc}") from exc

        if response.status_code == 400 and expect_json:
            # Not every endpoint supports response_format; retry without it.
            body.pop("response_format", None)
            response = httpx.post(
                url,
                headers=self._headers(),
                json=body,
                timeout=httpx.Timeout(15.0, read=self.config.request_timeout_s),
            )
        if response.status_code >= 400:
            raise LLMError(f"Endpoint returned {response.status_code}: {response.text[:400]}")

        payload = response.json()
        content = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content")
        if not content:
            raise LLMError("The endpoint returned an empty response.")
        text = str(content)
        return Completion(text, usage_from_payload(payload.get("usage")) or estimate_usage(messages, text))
