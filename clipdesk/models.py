"""The shared vocabulary of the whole pipeline.

Everything here is serialisable, because the analysis result is written to disk
as ``analysis.json`` and is the single contract between the analysis stage and
every action (notes, clips, cleanup, highlights, bookends) as well as the UI.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- media -------------------------------------------------------------------
class VideoStreamInfo(BaseModel):
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None


class AudioStreamInfo(BaseModel):
    codec: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    language: str | None = None


class MediaInfo(BaseModel):
    path: str
    filename: str = ""
    container: str | None = None
    duration_s: float = 0.0
    size_bytes: int = 0
    video: VideoStreamInfo | None = None
    audios: list[AudioStreamInfo] = Field(default_factory=list)

    @property
    def has_audio(self) -> bool:
        return bool(self.audios)

    @property
    def has_video(self) -> bool:
        return self.video is not None


# --- transcript --------------------------------------------------------------
class TranscriptSegment(BaseModel):
    id: int
    start: float
    end: float
    text: str
    speaker: str | None = None
    no_speech_prob: float | None = None

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


class TranscriptSource(str, Enum):
    WHISPER = "whisper"
    UPLOADED = "uploaded"


class Transcript(BaseModel):
    source: TranscriptSource = TranscriptSource.WHISPER
    language: str | None = None
    model: str | None = None
    duration_s: float = 0.0
    segments: list[TranscriptSegment] = Field(default_factory=list)

    @property
    def full_text(self) -> str:
        return " ".join(seg.text.strip() for seg in self.segments if seg.text.strip())

    @property
    def word_count(self) -> int:
        return len(self.full_text.split())


class SilenceSpan(BaseModel):
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


# --- analysis ----------------------------------------------------------------
class SegmentKind(str, Enum):
    ON_TOPIC = "on_topic"
    OFF_TOPIC = "off_topic"
    QA = "qa"
    FILLER = "filler"
    ADMIN = "admin"          # scheduling, "can everyone hear me", roll call
    INTRO = "intro"
    OUTRO = "outro"
    TRANSITION = "transition"


class SegmentAnalysis(BaseModel):
    """Per-segment verdict. Drives cleanup and clip selection."""

    segment_id: int
    start: float
    end: float
    kind: SegmentKind = SegmentKind.ON_TOPIC
    topic: str | None = None
    importance: float = 0.5
    keep: bool = True
    reason: str = ""
    # Provenance, so the UI can explain a decision.
    llm_importance: float | None = None
    heuristic_importance: float | None = None


class Chapter(BaseModel):
    title: str
    start: float
    end: float
    summary: str = ""
    key_points: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


class ClipCandidate(BaseModel):
    """A span that stands on its own as a clip, offered to the user to choose from."""

    title: str
    start: float
    end: float
    score: float = 0.5
    #: Two lines describing what is covered, so the user can choose without
    #: scrubbing through the video.
    summary: str = ""
    hook: str = ""
    reason: str = ""
    topics: list[str] = Field(default_factory=list)
    #: "analysis" | "duration" | "topic" | "highlight" — how it was found.
    source: str = "analysis"

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


class KeepSpan(BaseModel):
    start: float
    end: float
    reason: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


class ActionItem(BaseModel):
    text: str
    owner: str | None = None
    time: float | None = None


class Decision(BaseModel):
    text: str
    time: float | None = None


class AnalysisReport(BaseModel):
    schema_version: int = 1
    project_id: str
    created_at: str = Field(default_factory=utcnow)
    title: str = ""

    media: MediaInfo
    transcript: Transcript = Field(default_factory=Transcript)
    silences: list[SilenceSpan] = Field(default_factory=list)

    summary: str = ""
    abstract: str = ""
    keywords: list[str] = Field(default_factory=list)
    chapters: list[Chapter] = Field(default_factory=list)
    segment_analyses: list[SegmentAnalysis] = Field(default_factory=list)
    clip_candidates: list[ClipCandidate] = Field(default_factory=list)
    cleanup_keep_spans: list[KeepSpan] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)

    llm_provider: str = ""
    llm_model: str = ""
    warnings: list[str] = Field(default_factory=list)

    @property
    def kept_duration_s(self) -> float:
        return sum(span.duration for span in self.cleanup_keep_spans)


# --- artifacts ---------------------------------------------------------------
class ArtifactKind(str, Enum):
    TRANSCRIPT = "transcript"
    NOTES = "notes"
    ARTICLE = "article"
    SUMMARY = "summary"
    CLIP = "clip"
    CLEANUP = "cleanup"
    HIGHLIGHT = "highlight"
    INTRO = "intro"
    EDIT = "edit"
    BOOKEND = "bookend"
    EXPORT = "export"
    BUNDLE = "bundle"


class Artifact(BaseModel):
    """A file produced by an action, listed in the project's Outputs panel."""

    id: str
    kind: ArtifactKind
    label: str
    filename: str
    created_at: str = Field(default_factory=utcnow)
    size_bytes: int = 0
    duration_s: float | None = None
    meta: dict[str, str] = Field(default_factory=dict)
