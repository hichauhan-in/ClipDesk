"""Anthropic's Messages API.

Claude does not expose an OpenAI-compatible endpoint, so it needs its own
adapter rather than another entry in the OpenAI-compatible presets. Three
differences matter: the system prompt is a top-level field rather than a
message, ``max_tokens`` is required, and the reply is a list of content blocks.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from clipdesk.config import AnthropicConfig
from clipdesk.llm.base import (
    ChatMessage,
    LLMError,
    LLMUnavailableError,
    ProviderStatus,
)

SETUP_HINT = (
    "Set the API key in the environment variable named below, then choose a model. "
    "Keys are read from the environment so they never land in a config file."
)

API_VERSION = "2023-06-01"


class AnthropicProvider:
    key = "anthropic"
    label = "Anthropic Claude"

    def __init__(self, config: AnthropicConfig) -> None:
        self.config = config

    def _api_key(self) -> str:
        return os.environ.get(self.config.api_key_env, "").strip()

    def _headers(self) -> dict[str, str]:
        return {
            "content-type": "application/json",
            "x-api-key": self._api_key(),
            "anthropic-version": API_VERSION,
        }

    def status(self) -> ProviderStatus:
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
            response = httpx.get(f"{base}/v1/models", headers=self._headers(), timeout=8.0)
            if response.status_code == 401:
                return ProviderStatus(
                    self.key, self.label, False, "The API key was rejected.", setup_hint=SETUP_HINT
                )
            if response.status_code >= 400:
                # Some gateways proxy /v1/messages without exposing /v1/models.
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
    ) -> str:
        if not self._api_key():
            raise LLMUnavailableError(
                f"Environment variable {self.config.api_key_env} is not set."
            )

        system = "\n\n".join(m.content for m in messages if m.role == "system").strip()
        turns = [
            {"role": "assistant" if m.role == "assistant" else "user", "content": m.content}
            for m in messages
            if m.role != "system" and m.content.strip()
        ]
        if not turns:
            raise LLMError("No user message to send.")

        body: dict[str, Any] = {
            "model": self.config.model,
            # Required by the API, unlike the OpenAI shape where it is optional.
            "max_tokens": max_tokens or self.config.max_tokens,
            "temperature": temperature,
            "messages": turns,
        }
        if system:
            body["system"] = system

        url = f"{self.config.base_url.rstrip('/')}/v1/messages"
        try:
            response = httpx.post(
                url,
                headers=self._headers(),
                json=body,
                timeout=httpx.Timeout(15.0, read=self.config.request_timeout_s),
            )
        except httpx.RequestError as exc:
            raise LLMUnavailableError(f"Could not reach {url}: {exc}") from exc

        if response.status_code == 401:
            raise LLMUnavailableError("Anthropic rejected the API key.")
        if response.status_code == 429:
            raise LLMError("Anthropic rate limit reached. Try again shortly.")
        if response.status_code >= 400:
            raise LLMError(f"Anthropic returned {response.status_code}: {response.text[:400]}")

        payload = response.json()
        text = "".join(
            block.get("text", "")
            for block in payload.get("content") or []
            if block.get("type") == "text"
        ).strip()
        if not text:
            raise LLMError("Anthropic returned an empty response.")
        return text
