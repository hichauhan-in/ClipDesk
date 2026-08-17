"""Pluggable access to a large language model."""

from clipdesk.llm.base import (
    ChatMessage,
    LLMError,
    LLMProvider,
    LLMUnavailableError,
    ProviderStatus,
    extract_json,
)
from clipdesk.llm.presets import PRESETS, Preset
from clipdesk.llm.registry import (
    PRIMARY_KEYS,
    PROVIDER_KEYS,
    LLMClient,
    all_statuses,
    build_provider,
)
from clipdesk.llm.vscode_bridge import extension_state

__all__ = [
    "PRESETS",
    "PRIMARY_KEYS",
    "PROVIDER_KEYS",
    "ChatMessage",
    "LLMClient",
    "LLMError",
    "LLMProvider",
    "LLMUnavailableError",
    "Preset",
    "ProviderStatus",
    "all_statuses",
    "build_provider",
    "extension_state",
    "extract_json",
]
