"""Provider selection plus the retry/JSON behaviour every caller wants."""

from __future__ import annotations

import time

from clipdesk.config import LLMConfig, Settings
from clipdesk.llm.anthropic import AnthropicProvider
from clipdesk.llm.base import (
    JSON_INSTRUCTION,
    ChatMessage,
    LLMError,
    LLMProvider,
    LLMUnavailableError,
    ProviderStatus,
    extract_json,
)
from clipdesk.llm.copilot_cli import CopilotCliProvider
from clipdesk.llm.openai_compat import OpenAICompatProvider
from clipdesk.llm.vscode_bridge import VSCodeBridgeProvider

PROVIDER_KEYS = ("vscode", "copilot_cli", "openai_compat", "anthropic")

#: Providers offered as top-level choices; the rest sit behind "other provider".
PRIMARY_KEYS = ("vscode", "copilot_cli")


def build_provider(config: LLMConfig, key: str | None = None) -> LLMProvider:
    key = key or config.provider
    if key == "vscode":
        return VSCodeBridgeProvider(config.vscode)
    if key == "copilot_cli":
        return CopilotCliProvider(config.copilot_cli)
    if key == "openai_compat":
        return OpenAICompatProvider(config.openai_compat)
    if key == "anthropic":
        return AnthropicProvider(config.anthropic)
    raise LLMError(f"Unknown LLM provider '{key}'. Choose one of: {', '.join(PROVIDER_KEYS)}")


def all_statuses(config: LLMConfig) -> list[ProviderStatus]:
    """Status of every provider, so the Settings screen can show the options."""
    statuses: list[ProviderStatus] = []
    for key in PROVIDER_KEYS:
        try:
            statuses.append(build_provider(config, key).status())
        except Exception as exc:  # noqa: BLE001
            statuses.append(ProviderStatus(key, key, False, str(exc)))
    return statuses


class LLMClient:
    """A provider wrapped in the retry and JSON-coercion logic the analysis needs.

    Analysis makes many calls and any one of them can come back as prose instead
    of JSON, or hit a transient rate limit. Handling that here keeps the analyzer
    readable.
    """

    def __init__(self, provider: LLMProvider, *, json_retries: int = 2) -> None:
        self.provider = provider
        self.json_retries = max(0, json_retries)

    @classmethod
    def from_settings(cls, settings: Settings, provider_key: str | None = None) -> LLMClient:
        return cls(
            build_provider(settings.llm, provider_key),
            json_retries=settings.analysis.json_retries,
        )

    @property
    def key(self) -> str:
        return self.provider.key

    @property
    def model(self) -> str:
        return self.provider.status().active_model

    def status(self) -> ProviderStatus:
        return self.provider.status()

    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> str:
        messages = [ChatMessage("system", system), ChatMessage("user", user)]
        return self._with_retry(
            lambda: self.provider.complete(
                messages, temperature=temperature, max_tokens=max_tokens
            )
        )

    def complete_json(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.1,
        max_tokens: int | None = None,
    ):
        """Ask for JSON and keep asking until it parses (or attempts run out)."""
        system_prompt = f"{system}\n\n{JSON_INSTRUCTION}"
        messages = [ChatMessage("system", system_prompt), ChatMessage("user", user)]

        last_error: Exception | None = None
        for attempt in range(self.json_retries + 1):
            # Bound explicitly: the lambda runs before `messages` is reassigned
            # below, but relying on that is the kind of thing that quietly breaks
            # the moment the call becomes deferred.
            attempt_messages = messages
            try:
                raw = self._with_retry(
                    lambda payload=attempt_messages: self.provider.complete(
                        payload,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        expect_json=True,
                    )
                )
                return extract_json(raw)
            except LLMUnavailableError:
                raise
            except LLMError as exc:
                last_error = exc
                if attempt >= self.json_retries:
                    break
                messages = [
                    ChatMessage("system", system_prompt),
                    ChatMessage("user", user),
                    ChatMessage(
                        "user",
                        "Your previous reply could not be used. Everything needed is "
                        "in the message above — do not ask for more input. Reply with "
                        "one raw JSON object starting with { and ending with }, and "
                        "nothing else: no prose, no apology, no markdown fence.",
                    ),
                ]
        raise LLMError(str(last_error) if last_error else "The model did not return JSON.")

    def _with_retry(self, call, attempts: int = 3):
        """Retry transient failures with a short backoff.

        A missing or unconfigured provider is not transient, so it fails fast —
        the user needs to go and fix something.
        """
        delay = 1.5
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                return call()
            except LLMUnavailableError:
                raise
            except LLMError as exc:
                last_error = exc
                if attempt == attempts - 1:
                    break
                time.sleep(delay)
                delay *= 2
        raise last_error if last_error else LLMError("The model request failed.")
