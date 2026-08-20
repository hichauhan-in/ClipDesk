"""Request and response bodies for the HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from clipdesk.actions.intro import (
    Backdrop,
    Grade,
    IntroTransition,
    ShotMotion,
    TitleAnimation,
)


class Queueable(BaseModel):
    """Actions that can be lined up instead of run straight away."""

    #: Hold this step in the project's queue rather than starting it now.
    queue: bool = False


class AnalyzeRequest(BaseModel):
    #: Override the configured provider for this run only.
    llm_provider: str | None = None
    #: Skip the model entirely and use transcript heuristics.
    skip_llm: bool = False


class NotesRequest(Queueable):
    include_mermaid: bool = True
    include_timestamps: bool = True
    chapters_per_file: int | None = Field(default=None, ge=1, le=50)
    #: 0 transcript-only … 6 expert reference. Anything above 0 is marked in the
    #: output so a reader can tell added material from what was said.
    enrichment: int = Field(default=0, ge=0, le=6)


class ArticleRequest(Queueable):
    """Write one article from the recording, as Markdown or as the Word template."""

    format: Literal["md", "docx"] = "docx"
    #: Which sections of the template carry content.
    shape: Literal["break-fix", "how-to", "reference"] = "break-fix"
    #: Blank means the analysed title.
    title: str = Field(default="", max_length=256)
    #: Extra headings appended after the template's own sections.
    extra_sections: list[str] = Field(default_factory=list, max_length=8)
    audience: str = Field(default="", max_length=200)
    #: 0 transcript-only … 6 expert reference. Anything the model adds above 0 is
    #: marked in the article so a reader can tell it from what was said.
    enrichment: int = Field(default=0, ge=0, le=6)
    #: Ask for a Mermaid diagram. Ignored for Word, which cannot render one.
    include_diagram: bool = True
    output_name: str = Field(default="", max_length=200)


class AskRequest(BaseModel):
    """One question about a recording that has already been analysed."""

    question: str = Field(min_length=1, max_length=2000)
    #: True answers only from the recording. False allows general knowledge,
    #: which the answer then has to separate out under its own heading.
    grounded: bool = True


class ClipFindRequest(Queueable):
    #: "duration" | "topic" | "highlight"
    mode: str = "duration"
    #: Soft target for duration mode. Ignored by the others.
    target_seconds: float = Field(default=90.0, gt=1.0, le=7200.0)
    #: What the clip should be about. Required for topic mode, optional elsewhere.
    query: str = ""
    count: int | None = Field(default=None, ge=1, le=20)
    use_llm: bool = True


class ClipSelection(BaseModel):
    title: str = "Clip"
    start: float = Field(ge=0.0)
    end: float = Field(gt=0.0)
    summary: str = ""


class ClipRenderRequest(Queueable):
    selections: list[ClipSelection] = Field(min_length=1)
    #: Join the selections into one video with transitions, instead of separate files.
    combine: bool = False
    #: Fit onto the configured 16:9 canvas.
    reframe: bool = False
    prefix: str = "clip"
    title: str = ""
    output_name: str = ""


class CleanupRequest(Queueable):
    remove_silence: bool = True
    remove_filler: bool = True
    remove_off_topic: bool = True
    remove_admin: bool = True
    remove_qa: bool = False
    remove_intro: bool = False
    remove_outro: bool = False
    keep_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    smooth_cuts: bool | None = None
    output_name: str = "cleaned.mp4"


class OutputSelection(BaseModel):
    filenames: list[str] = Field(min_length=1)
    archive_name: str = ""


class OutputRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=180)


class BookendRequest(Queueable):
    #: A file in the project's output folder, or "" to wrap the original upload.
    body_filename: str = ""
    header_asset: str | None = None
    footer_asset: str | None = None
    intro_transition: Literal["cut", "fade", "dissolve", "wipe-left", "slide-left"] = "cut"
    outro_transition: Literal["cut", "fade", "dissolve", "wipe-left", "slide-left"] = "cut"
    #: Trim applied to the main video before anything is joined onto it.
    trim_start: float = Field(default=0.0, ge=0.0)
    trim_end: float | None = Field(default=None, ge=0.0)
    output_name: str = "final.mp4"


class IntroRequest(Queueable):
    duration_seconds: float = Field(default=14.0, ge=5.0, le=60.0)
    shot_count: int = Field(default=5, ge=2, le=12)
    style_id: str = "prestige"
    #: Blank means "use the analysed title".
    title: str = Field(default="", max_length=120)
    subtitle: str = Field(default="", max_length=200)
    end_card_text: str = Field(default="", max_length=120)
    include_final_message: bool = False
    show_shot_tags: bool = True
    repeat_title: bool = False
    voiceover: bool = False
    voiceover_text: str = Field(default="", max_length=1200)
    voice_id: str = Field(default="", max_length=180)
    audio_id: str = Field(default="elevate", max_length=220)
    output_name: str = "intro.mp4"


class IntroStyleInstallRequest(BaseModel):
    style_id: str


class IntroStyleImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str = Field(min_length=2, max_length=50)
    description: str = Field(min_length=4, max_length=200)
    accent: str = Field(default="#3f8cff", pattern=r"^#[0-9a-fA-F]{6}$")
    # Mirrored from the intro engine so an imported style can use every bundled look.
    backdrop: Backdrop = "source-blur"
    title_animation: TitleAnimation = "band-reveal"
    shot_motions: list[ShotMotion] = Field(
        default=["punch-in", "drift"], min_length=1, max_length=6
    )
    transition: IntroTransition = "dissolve"
    transition_seconds: float = Field(default=0.42, ge=0.08, le=1.2)
    grade: Grade = "cinematic"
    letterbox: bool = True
    grain: bool = True
    vignette: bool = True
    light_sweep: bool = True
    hook: bool = True
    kicker: bool = True
    end_card: bool = True
    title_seconds: float = Field(default=2.8, ge=1.2, le=6.0)
    end_card_seconds: float = Field(default=2.0, ge=1.0, le=5.0)
    default_shots: int = Field(default=5, ge=2, le=12)
    default_duration_seconds: float = Field(default=14.0, ge=5.0, le=60.0)


class PromptEditRequest(Queueable):
    prompt: str = Field(min_length=1, max_length=2000)
    output_name: str = "edited.mp4"
    preview_only: bool = False
    #: Which planned steps to keep, as indices into the plan. None means all.
    steps: list[int] | None = Field(default=None, max_length=40)


class AssetLinkItem(BaseModel):
    url: str
    name: str = ""


class AssetLinksRequest(BaseModel):
    items: list[AssetLinkItem] = Field(min_length=1, max_length=20)
    project_id: str = ""


class MediaAdoptRequest(BaseModel):
    """Copy one media file from another project, or from the legacy shared library."""

    name: str = Field(min_length=1, max_length=200)
    #: Blank means the legacy shared library.
    source_project_id: str = ""


class ExportRequest(Queueable):
    """Re-encode one existing file to a chosen container and quality."""

    source: str = Field(min_length=1, max_length=200)
    source_kind: Literal["output", "media", "source"] = "output"
    format: Literal["mp4", "webm", "mov", "gif", "mp3", "m4a", "wav"] = "mp4"
    quality: Literal["source", "high", "balanced", "small", "tiny"] = "balanced"
    output_name: str = Field(default="", max_length=200)


class TranscriptExportRequest(BaseModel):
    format: str = "srt"


class LocalImportRequest(BaseModel):
    """A file picked out of a synced cloud folder."""

    #: Which detected root, never an absolute path from the browser.
    root: str
    #: Path relative to that root.
    path: str
    title: str = ""


class SettingsUpdate(BaseModel):
    llm_provider: str | None = None
    #: Let ClipDesk size requests and pick the model.
    llm_auto: bool | None = None
    llm_budget_level: int | None = Field(default=None, ge=0, le=4)
    #: Preferred model within each size tier, keyed small/balanced/strong.
    llm_tier_models: dict[str, str] | None = None
    llm_model: str | None = None
    vscode_reasoning_effort: Literal[
        "", "none", "minimal", "low", "medium", "high", "xhigh", "max"
    ] | None = None
    vscode_context_window_tokens: int | None = Field(default=None, ge=0, le=2_000_000)
    copilot_cli_model: str | None = None
    copilot_cli_reasoning_effort: Literal[
        "", "none", "minimal", "low", "medium", "high", "xhigh", "max"
    ] | None = None
    copilot_cli_context_window: Literal["default", "long_context"] | None = None
    #: One of the entries in llm/presets.py. Fills in URL, auth style and key name.
    llm_preset: str | None = None
    openai_base_url: str | None = None
    openai_model: str | None = None
    openai_auth_style: str | None = None
    openai_api_key_env: str | None = None
    anthropic_model: str | None = None
    anthropic_base_url: str | None = None
    whisper_model: str | None = None
    keep_importance_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    notes_chapters_per_file: int | None = Field(default=None, ge=1, le=50)
    render_crf: int | None = Field(default=None, ge=0, le=51)
    render_preset: str | None = None
    ingest_browser_cookies: str | None = None


class ImportLinkRequest(BaseModel):
    url: str
    title: str = ""
    #: Reuse a browser sign-in. Needed for tenant SharePoint and Stream.
    browser_cookies: str = ""


class InspectLinkRequest(BaseModel):
    url: str


class SessionSaveRequest(BaseModel):
    """A signed-in session pasted from the browser's developer tools."""

    #: A "Copy as cURL", a bare Cookie header, or the contents of a cookies.txt.
    pasted: str
    #: The link being imported, used to work out the host when the paste alone
    #: does not say (a bare Cookie header has no URL in it).
    url: str = ""


class ProvisionRequest(BaseModel):
    component: str = "all"


class QueueMoveRequest(BaseModel):
    #: -1 to run this step sooner, 1 to run it later.
    offset: Literal[-1, 1] = -1


class ShutdownRequest(BaseModel):
    force: bool = False


class JobAccepted(BaseModel):
    job_id: str
    project_id: str
    kind: str


class ApiError(BaseModel):
    detail: str
    hint: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
