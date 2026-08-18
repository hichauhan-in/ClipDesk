"""Layered configuration: ``config/default.yaml`` → ``config/local.yaml`` →
environment variables → an explicit override file.

The result is a validated :class:`Settings` object. Nothing else in the codebase
reads YAML or environment variables directly.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from clipdesk.paths import app_root, resolve

DEFAULT_CONFIG = "config/default.yaml"
LOCAL_CONFIG = "config/local.yaml"


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8760
    allowed_origins: list[str] = Field(default_factory=list)
    max_upload_mb: int = 4096
    multi_user: bool = False
    identity_header: str = "x-clipdesk-user"
    proxy_secret_header: str = "x-clipdesk-proxy-secret"
    proxy_secret_env: str = "CLIPDESK_PROXY_SECRET"
    allowed_link_hosts: list[str] = Field(default_factory=list)


class PathsConfig(BaseModel):
    workspace: str = "workspace"
    vendor: str = "vendor"

    @property
    def workspace_dir(self) -> Path:
        return resolve(self.workspace)

    @property
    def vendor_dir(self) -> Path:
        return resolve(self.vendor)


class MediaConfig(BaseModel):
    noise_db: float = -32.0
    min_silence_seconds: float = 0.5
    audio_sample_rate: int = 16000


class TranscriptionConfig(BaseModel):
    model: str = "base"
    device: str = "cpu"
    compute_type: str = "int8"
    language: str | None = None
    beam_size: int = 1
    vad_filter: bool = True


class VSCodeLLMConfig(BaseModel):
    handshake_file: str | None = None
    base_url: str | None = None
    token: str | None = None
    model: str | None = None
    reasoning_effort: Literal[
        "none", "minimal", "low", "medium", "high", "xhigh", "max"
    ] | None = None
    context_window_tokens: int | None = Field(default=None, ge=1024, le=2_000_000)
    request_timeout_s: float = 180.0


class CopilotCliConfig(BaseModel):
    command: str = "copilot"
    extra_args: list[str] = Field(default_factory=lambda: ["--no-color"])
    model: str | None = None
    reasoning_effort: Literal[
        "none", "minimal", "low", "medium", "high", "xhigh", "max"
    ] | None = None
    context_window: Literal["default", "long_context"] = "default"
    request_timeout_s: float = 300.0


class OpenAICompatConfig(BaseModel):
    #: Which entry in llm/presets.py filled these in, for the UI to show.
    preset: str = "custom"
    base_url: str | None = None
    api_key_env: str = "CLIPDESK_LLM_API_KEY"
    model: str = "gpt-4o"
    auth_style: str = "bearer"
    request_timeout_s: float = 180.0


class AnthropicConfig(BaseModel):
    base_url: str = "https://api.anthropic.com"
    api_key_env: str = "CLIPDESK_ANTHROPIC_API_KEY"
    model: str = "claude-sonnet-4-5"
    max_tokens: int = 8192
    request_timeout_s: float = 180.0


class LLMConfig(BaseModel):
    provider: str = "vscode"
    #: Let ClipDesk size each request and pick the model. Off means the provider
    #: settings below are used exactly as written.
    auto: bool = True
    #: 0 spends the fewest tokens, 4 spends the most. Ignored when auto is off.
    budget_level: int = Field(default=2, ge=0, le=4)
    vscode: VSCodeLLMConfig = Field(default_factory=VSCodeLLMConfig)
    copilot_cli: CopilotCliConfig = Field(default_factory=CopilotCliConfig)
    openai_compat: OpenAICompatConfig = Field(default_factory=OpenAICompatConfig)
    anthropic: AnthropicConfig = Field(default_factory=AnthropicConfig)


class AnalysisConfig(BaseModel):
    window_chars: int = 9000
    window_overlap_chars: int = 600
    max_concurrency: int = 2
    json_retries: int = 2
    keep_importance_threshold: float = 0.38
    filler_words: list[str] = Field(default_factory=list)


class CleanupConfig(BaseModel):
    keep_gap_seconds: float = 1.0
    pad_seconds: float = 0.25
    merge_gap_seconds: float = 0.8
    smooth_cuts: bool = True
    fade_seconds: float = 0.25
    fade_min_gap_seconds: float = 4.0
    protect_importance: float = 0.72


class ClipsConfig(BaseModel):
    default_duration_seconds: float = 90.0
    duration_tolerance: float = 0.35
    pad_seconds: float = 0.4
    candidate_count: int = 6
    highlight_count: int = 6
    highlight_max_seconds: float = 60.0
    join_transition_seconds: float = 0.5


class RenderConfig(BaseModel):
    video_codec: str = "libx264"
    preset: str = "veryfast"
    crf: int = 21
    audio_codec: str = "aac"
    audio_bitrate: str = "160k"
    width: int = 1920
    height: int = 1080
    fps: float | None = None


class NotesConfig(BaseModel):
    chapters_per_file: int = 6
    include_mermaid: bool = True
    include_timestamps: bool = True
    enrichment: int = 0


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str | None = None


class IngestConfig(BaseModel):
    """Downloading a recording from a link."""

    #: Reuse the sign-in from this browser for links that need one. Empty = off.
    browser_cookies: str = ""
    max_download_mb: int = 8192


class Settings(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    media: MediaConfig = Field(default_factory=MediaConfig)
    transcription: TranscriptionConfig = Field(default_factory=TranscriptionConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    cleanup: CleanupConfig = Field(default_factory=CleanupConfig)
    clips: ClipsConfig = Field(default_factory=ClipsConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)
    notes: NotesConfig = Field(default_factory=NotesConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


# --- loading -----------------------------------------------------------------
def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


# Environment overrides for the handful of values worth setting from a shell or
# a container definition. Everything else belongs in config/local.yaml.
_ENV_MAP: dict[str, tuple[str, ...]] = {
    "CLIPDESK_HOST": ("server", "host"),
    "CLIPDESK_PORT": ("server", "port"),
    "CLIPDESK_WORKSPACE": ("paths", "workspace"),
    "CLIPDESK_VENDOR": ("paths", "vendor"),
    "CLIPDESK_LLM_PROVIDER": ("llm", "provider"),
    "CLIPDESK_LLM_MODEL": ("llm", "vscode", "model"),
    "CLIPDESK_LLM_BASE_URL": ("llm", "openai_compat", "base_url"),
    "CLIPDESK_WHISPER_MODEL": ("transcription", "model"),
    "CLIPDESK_WHISPER_DEVICE": ("transcription", "device"),
    "CLIPDESK_LOG_LEVEL": ("logging", "level"),
    "CLIPDESK_LOG_FILE": ("logging", "file"),
}


def _apply_env(data: dict[str, Any]) -> dict[str, Any]:
    for env_name, path in _ENV_MAP.items():
        raw = os.environ.get(env_name)
        if raw is None or raw == "":
            continue
        cursor = data
        for key in path[:-1]:
            cursor = cursor.setdefault(key, {})
        cursor[path[-1]] = raw
    return data


def load_settings(override_path: str | Path | None = None) -> Settings:
    """Load defaults, then ``config/local.yaml``, then env vars, then ``override_path``."""
    root = app_root()
    data = _read_yaml(root / DEFAULT_CONFIG)
    data = _deep_merge(data, _read_yaml(root / LOCAL_CONFIG))
    data = _apply_env(data)
    if override_path:
        data = _deep_merge(data, _read_yaml(Path(override_path).expanduser().resolve()))
    return Settings.model_validate(data)


def save_local_overrides(
    overrides: dict[str, Any], path: str | Path | None = None
) -> Path:
    """Merge ``overrides`` into ``config/local.yaml`` and return its path.

    Used by the Settings screen so a choice made in the UI survives a restart
    without anyone hand-editing YAML.
    """
    path = Path(path).expanduser().resolve() if path else app_root() / LOCAL_CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = _deep_merge(_read_yaml(path), overrides)
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(merged, handle, sort_keys=False, allow_unicode=True)
    return path
