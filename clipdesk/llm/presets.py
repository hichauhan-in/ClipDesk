"""Ready-made settings for the endpoints people actually use.

Every one of these speaks the OpenAI chat-completions shape, so they are the same
provider with different values — except Anthropic, which has its own adapter.
Selecting a preset fills in the URL, the auth style and the environment variable
name, so nobody has to know that Azure sends the key in `api-key` while everyone
else uses a bearer token.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Preset:
    key: str
    label: str
    #: The provider implementation this preset configures.
    provider: str = "openai_compat"
    base_url: str = ""
    auth_style: str = "bearer"
    api_key_env: str = "CLIPDESK_LLM_API_KEY"
    suggested_models: tuple[str, ...] = ()
    #: True when the URL contains something only the user knows (a tenant, a host).
    needs_base_url: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "provider": self.provider,
            "base_url": self.base_url,
            "auth_style": self.auth_style,
            "api_key_env": self.api_key_env,
            "suggested_models": list(self.suggested_models),
            "needs_base_url": self.needs_base_url,
            "note": self.note,
        }


PRESETS: tuple[Preset, ...] = (
    Preset(
        key="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        api_key_env="CLIPDESK_OPENAI_API_KEY",
        suggested_models=("gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini"),
        note="Pay-as-you-go OpenAI. Check with your organisation before sending "
        "internal recordings to a personal account.",
    ),
    Preset(
        key="azure_openai",
        label="Azure OpenAI",
        auth_style="api-key",
        api_key_env="CLIPDESK_AZURE_OPENAI_KEY",
        needs_base_url=True,
        suggested_models=("gpt-4o", "gpt-4o-mini"),
        note="Use the v1 endpoint: https://<resource>.openai.azure.com/openai/v1 — "
        "the model name is your deployment name. Usually the right answer for a "
        "governed corporate setup.",
    ),
    Preset(
        key="anthropic",
        label="Anthropic Claude",
        provider="anthropic",
        base_url="https://api.anthropic.com",
        api_key_env="CLIPDESK_ANTHROPIC_API_KEY",
        suggested_models=(
            "claude-sonnet-4-5",
            "claude-opus-4-1",
            "claude-haiku-4-5",
        ),
        note="Claude uses its own API shape, so ClipDesk talks to it directly "
        "rather than through the OpenAI-compatible adapter.",
    ),
    Preset(
        key="gemini",
        label="Google Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key_env="CLIPDESK_GEMINI_API_KEY",
        suggested_models=("gemini-2.5-flash", "gemini-2.5-pro"),
        note="Uses Google's OpenAI-compatible endpoint.",
    ),
    Preset(
        key="openrouter",
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="CLIPDESK_OPENROUTER_API_KEY",
        suggested_models=(
            "anthropic/claude-sonnet-4.5",
            "openai/gpt-4o",
            "google/gemini-2.5-flash",
        ),
        note="One key, many models. Useful for trying providers before committing.",
    ),
    Preset(
        key="ollama",
        label="Ollama (runs locally)",
        base_url="http://127.0.0.1:11434/v1",
        api_key_env="CLIPDESK_LLM_API_KEY",
        suggested_models=("llama3.1:8b", "qwen2.5:14b", "mistral-nemo"),
        note="Nothing leaves the machine, but a laptop-sized model is noticeably "
        "weaker at this than Copilot. Ollama ignores the key, so any value works.",
    ),
    Preset(
        key="custom",
        label="Other OpenAI-compatible endpoint",
        needs_base_url=True,
        note="Any internal gateway or self-hosted server that implements "
        "/chat/completions.",
    ),
)

BY_KEY: dict[str, Preset] = {preset.key: preset for preset in PRESETS}


def get(key: str) -> Preset | None:
    return BY_KEY.get(key)


def as_dicts() -> list[dict[str, object]]:
    return [preset.to_dict() for preset in PRESETS]
