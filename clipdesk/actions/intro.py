"""Animated intro sequences: a designed scene arc, not a clip concatenation.

An intro is planned as a short film with a shape — an optional hook, a title
reveal, a rhythm of shots, an optional kicker line and an end card — and then
composed in one ffmpeg pass. Scenes are joined with real transitions rather than
butt-cut together, titles are revealed through an animated mask, and every
movement uses an eased curve so nothing arrives at a constant speed.

Everything a style can express is enumerated. A style is data: colours, an
animation name, a motion list. No style, imported or otherwise, can contribute a
filter string, an argument or a command.
"""

from __future__ import annotations

import json
import math
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from clipdesk.actions.render import Span, encode_args
from clipdesk.config import RenderConfig
from clipdesk.media.ffmpeg import filter_complex_args, run_with_progress
from clipdesk.models import AnalysisReport

Backdrop = Literal[
    "source-blur", "gradient", "dark-panel", "duotone", "stage", "grid"
]
TitleAnimation = Literal[
    "band-reveal", "stack-lines", "center-pop", "side-panel", "flash-cut",
    "split-bars", "lower-third",
]
ShotMotion = Literal[
    "punch-in", "pull-back", "whip", "drift", "hold", "tilt", "glide"
]
#: Every name here is an ffmpeg xfade transition verified against the bundled build.
IntroTransition = Literal[
    "fade", "fadefast", "fadeslow", "fadeblack", "fadewhite", "fadegrays",
    "dissolve", "pixelize", "zoomin", "distance", "hblur", "radial",
    "wipeleft", "wiperight", "wipeup", "wipedown", "wipetl", "wipebr",
    "slideup", "slidedown", "slideleft", "slideright",
    "smoothleft", "smoothright", "smoothup", "smoothdown",
    "circleopen", "circleclose", "circlecrop", "rectcrop",
    "horzopen", "vertopen", "squeezeh", "squeezev",
    "revealleft", "revealright", "coverleft", "coverup",
    "hlslice", "hrslice", "vuslice", "vdslice", "hlwind", "vuwind",
    "diagtl", "diagbr",
]
Grade = Literal[
    "clean", "cinematic", "vivid", "mono", "warm", "cool", "soft", "crisp",
    "noir", "teal-orange", "bleach", "faded", "midnight", "sunset",
]
SceneKind = Literal["hook", "title", "shot", "kicker", "end-card"]

#: Rhythm of the shot sequence. Uneven on purpose: equal shots read as a slideshow.
_RHYTHM = (1.0, 0.68, 1.22, 0.82, 1.32, 0.74)
_MIN_SHOT_SECONDS = 0.55

_GRADE_FILTERS: dict[Grade, tuple[str, ...]] = {
    "clean": (),
    "cinematic": ("eq=contrast=1.09:saturation=0.86:brightness=-0.015", "unsharp=5:5:0.35"),
    "vivid": ("eq=contrast=1.07:saturation=1.26",),
    "mono": ("hue=s=0", "eq=contrast=1.10"),
    "warm": ("colorbalance=rs=.07:gs=.02:bs=-.06",),
    "cool": ("colorbalance=rs=-.05:bs=.08",),
    "soft": ("boxblur=1.5:1", "eq=saturation=.92:brightness=.015"),
    "crisp": ("unsharp=5:5:0.55", "eq=contrast=1.05"),
    "noir": ("hue=s=0", "eq=contrast=1.34:brightness=-0.04", "unsharp=5:5:0.40"),
    "teal-orange": (
        "colorbalance=rs=.10:gs=.01:bs=-.06:rm=-.03:bm=.12",
        "eq=contrast=1.08:saturation=1.12",
    ),
    "bleach": ("eq=saturation=.42:contrast=1.24:brightness=.05", "unsharp=3:3:0.30"),
    "faded": ("eq=saturation=.80:contrast=.92:brightness=.06",),
    "midnight": (
        "colorbalance=rs=-.07:bs=.17:bm=.08",
        "eq=contrast=1.13:brightness=-0.06",
    ),
    "sunset": ("colorbalance=rs=.13:gs=.04:bs=-.11", "eq=saturation=1.16:contrast=1.05"),
}


class IntroStyle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,39}$")
    name: str = Field(min_length=2, max_length=50)
    description: str = Field(min_length=4, max_length=200)
    accent: str = Field(default="#3f8cff", pattern=r"^#[0-9a-fA-F]{6}$")
    backdrop: Backdrop = "source-blur"
    title_animation: TitleAnimation = "band-reveal"
    shot_motions: tuple[ShotMotion, ...] = Field(
        default=("punch-in", "drift"), min_length=1, max_length=6
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
    source: Literal["built-in", "catalog", "custom"] = "custom"

    def public(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def _style(
    id: str,
    name: str,
    description: str,
    *,
    accent: str,
    backdrop: Backdrop,
    title_animation: TitleAnimation,
    shot_motions: tuple[ShotMotion, ...],
    transition: IntroTransition,
    grade: Grade,
    duration: float,
    shots: int,
    title_seconds: float = 2.8,
    end_card_seconds: float = 2.0,
    transition_seconds: float = 0.42,
    letterbox: bool = True,
    grain: bool = True,
    vignette: bool = True,
    light_sweep: bool = True,
    hook: bool = True,
    kicker: bool = True,
    end_card: bool = True,
    source: Literal["built-in", "catalog"] = "built-in",
) -> IntroStyle:
    return IntroStyle(
        id=id,
        name=name,
        description=description,
        accent=accent,
        backdrop=backdrop,
        title_animation=title_animation,
        shot_motions=shot_motions,
        transition=transition,
        transition_seconds=transition_seconds,
        grade=grade,
        letterbox=letterbox,
        grain=grain,
        vignette=vignette,
        light_sweep=light_sweep,
        hook=hook,
        kicker=kicker,
        end_card=end_card,
        title_seconds=title_seconds,
        end_card_seconds=end_card_seconds,
        default_shots=shots,
        default_duration_seconds=duration,
        source=source,
    )


BUILT_IN_STYLES = (
    _style(
        "prestige", "Prestige",
        "Slow cinematic build: masked title rise, letterboxed grain and long dissolves.",
        accent="#c9a227", backdrop="source-blur", title_animation="band-reveal",
        shot_motions=("punch-in", "drift", "pull-back"), transition="dissolve",
        grade="cinematic", duration=16, shots=5, title_seconds=3.2, transition_seconds=0.55,
    ),
    _style(
        "signal", "Signal",
        "Technical opener on a moving gradient with stacked lines and sliced transitions.",
        accent="#22d3ee", backdrop="gradient", title_animation="stack-lines",
        shot_motions=("punch-in", "hold"), transition="hlslice",
        grade="crisp", duration=13, shots=5, transition_seconds=0.3, grain=False,
    ),
    _style(
        "momentum", "Momentum",
        "High-energy flash title, whip-pans between shots and hard directional wipes.",
        accent="#ff5a1f", backdrop="duotone", title_animation="flash-cut",
        shot_motions=("whip", "punch-in"), transition="wipeleft",
        grade="vivid", duration=11, shots=6, title_seconds=2.0, transition_seconds=0.22,
        vignette=False,
    ),
    _style(
        "editorial", "Editorial",
        "Documentary side panel, drifting frames and soft smooth transitions.",
        accent="#e5e7eb", backdrop="source-blur", title_animation="side-panel",
        shot_motions=("drift", "hold"), transition="smoothleft",
        grade="mono", duration=15, shots=4, title_seconds=3.0, transition_seconds=0.5,
    ),
    _style(
        "keynote", "Keynote",
        "Corporate stage opener: centred title pop, measured push-ins, clean fades.",
        accent="#3f8cff", backdrop="dark-panel", title_animation="center-pop",
        shot_motions=("punch-in", "hold"), transition="fade",
        grade="clean", duration=12, shots=4, grain=False, light_sweep=False,
    ),
    _style(
        "neon-pulse", "Neon pulse",
        "Social-first neon gradient, stuttered flash title and zoom transitions.",
        accent="#f43f9d", backdrop="gradient", title_animation="flash-cut",
        shot_motions=("whip", "punch-in"), transition="zoomin",
        grade="vivid", duration=8, shots=5, title_seconds=1.8, transition_seconds=0.24,
        letterbox=False, kicker=False,
    ),
    _style(
        "studio-warm", "Studio warm",
        "Warm, unhurried reveal with drifting frames and a generous end card.",
        accent="#f0a13c", backdrop="source-blur", title_animation="band-reveal",
        shot_motions=("drift", "pull-back"), transition="dissolve",
        grade="warm", duration=17, shots=5, end_card_seconds=2.6, transition_seconds=0.5,
    ),
    _style(
        "blueprint", "Blueprint",
        "Engineering-brief look: dark panel, stacked lines and precise wipes.",
        accent="#60a5fa", backdrop="dark-panel", title_animation="stack-lines",
        shot_motions=("hold", "punch-in"), transition="wiperight",
        grade="cool", duration=14, shots=5, grain=False, transition_seconds=0.32,
    ),
    _style(
        "spotlight", "Spotlight",
        "Product hero: centred pop title, pull-back reveals and circular transitions.",
        accent="#a78bfa", backdrop="duotone", title_animation="center-pop",
        shot_motions=("pull-back", "punch-in"), transition="circleopen",
        grade="crisp", duration=12, shots=4, transition_seconds=0.45,
    ),
    _style(
        "minimal-frame", "Minimal frame",
        "Restrained typography, still frames and quiet fades. Nothing shouts.",
        accent="#9ca3af", backdrop="dark-panel", title_animation="band-reveal",
        shot_motions=("hold",), transition="fade",
        grade="soft", duration=10, shots=3, grain=False, vignette=False,
        light_sweep=False, hook=False,
    ),
    _style(
        "midnight-drive", "Midnight drive",
        "Pooled stage light, bars that split open and long gliding frames.",
        accent="#818cf8", backdrop="stage", title_animation="split-bars",
        shot_motions=("glide", "drift"), transition="smoothup",
        grade="midnight", duration=14, shots=5, transition_seconds=0.48,
    ),
    _style(
        "newsroom", "Newsroom",
        "Broadcast lower third sliding in under held frames and quick pushes.",
        accent="#ef4444", backdrop="dark-panel", title_animation="lower-third",
        shot_motions=("hold", "punch-in"), transition="slideleft",
        grade="crisp", duration=12, shots=5, transition_seconds=0.26, grain=False,
    ),
    _style(
        "grid-lab", "Grid lab",
        "Measured accent grid, stacked type and vertical opening transitions.",
        accent="#2dd4bf", backdrop="grid", title_animation="stack-lines",
        shot_motions=("tilt", "hold"), transition="vertopen",
        grade="cool", duration=13, shots=5, transition_seconds=0.34, grain=False,
    ),
    _style(
        "noir-cut", "Noir cut",
        "Hard monochrome contrast, splitting bars and cuts through black.",
        accent="#f8fafc", backdrop="duotone", title_animation="split-bars",
        shot_motions=("drift", "hold"), transition="fadeblack",
        grade="noir", duration=15, shots=4, title_seconds=3.2, transition_seconds=0.5,
    ),
    _style(
        "sunset-reel", "Sunset reel",
        "Warm golden grade with gliding frames and soft lateral smoothing.",
        accent="#fb923c", backdrop="source-blur", title_animation="band-reveal",
        shot_motions=("glide", "punch-in"), transition="smoothright",
        grade="sunset", duration=14, shots=5, transition_seconds=0.46,
    ),
    _style(
        "pitch-deck", "Pitch deck",
        "Slide-clean panel, centred title pop and squeezed slide transitions.",
        accent="#38bdf8", backdrop="dark-panel", title_animation="center-pop",
        shot_motions=("hold", "tilt"), transition="squeezeh",
        grade="clean", duration=11, shots=4, grain=False, light_sweep=False,
        transition_seconds=0.34,
    ),
    _style(
        "festival", "Festival",
        "Saturated teal-and-orange energy, stuttered title and circular cuts.",
        accent="#facc15", backdrop="gradient", title_animation="flash-cut",
        shot_motions=("whip", "glide", "punch-in"), transition="circlecrop",
        grade="teal-orange", duration=9, shots=6, title_seconds=1.9,
        transition_seconds=0.25, letterbox=False,
    ),
    _style(
        "archive", "Archive",
        "Faded stock, drifting frames and a lower third that slides in quietly.",
        accent="#d4d4d8", backdrop="duotone", title_animation="lower-third",
        shot_motions=("drift", "tilt"), transition="fadewhite",
        grade="faded", duration=16, shots=5, transition_seconds=0.52,
    ),
)

CATALOG_STYLES = (
    _style(
        "cinema-bars", "Cinema bars",
        "Anamorphic bars, slow pull-backs and film grain for a trailer-style open.",
        accent="#eab308", backdrop="source-blur", title_animation="band-reveal",
        shot_motions=("pull-back", "drift"), transition="fadeblack",
        grade="cinematic", duration=20, shots=6, title_seconds=3.4,
        transition_seconds=0.6, source="catalog",
    ),
    _style(
        "broadcast-news", "Broadcast news",
        "Studio lower-third language, sliding panel title and crisp wipes.",
        accent="#dc2626", backdrop="dark-panel", title_animation="side-panel",
        shot_motions=("hold", "punch-in"), transition="wipeleft",
        grade="crisp", duration=12, shots=5, transition_seconds=0.28, source="catalog",
    ),
    _style(
        "retro-film", "Retro film",
        "Monochrome duotone, heavy grain and gentle drift for an archival feel.",
        accent="#d6d3d1", backdrop="duotone", title_animation="stack-lines",
        shot_motions=("drift", "hold"), transition="fadewhite",
        grade="mono", duration=15, shots=5, source="catalog",
    ),
    _style(
        "launch-countdown", "Launch countdown",
        "Escalating whip-pans, stuttered title and pixel transitions for a reveal.",
        accent="#22c55e", backdrop="gradient", title_animation="flash-cut",
        shot_motions=("whip", "punch-in", "whip"), transition="pixelize",
        grade="vivid", duration=10, shots=6, title_seconds=1.9,
        transition_seconds=0.26, source="catalog",
    ),
    _style(
        "soft-focus", "Soft focus",
        "Diffused light, slow drifts and a long unhurried title hold.",
        accent="#fbcfe8", backdrop="source-blur", title_animation="center-pop",
        shot_motions=("drift",), transition="dissolve",
        grade="soft", duration=16, shots=4, title_seconds=3.4,
        transition_seconds=0.65, source="catalog",
    ),
    _style(
        "kinetic-grid", "Kinetic grid",
        "Sliced transitions, alternating whips and a tight typographic stack.",
        accent="#38bdf8", backdrop="gradient", title_animation="stack-lines",
        shot_motions=("whip", "punch-in", "pull-back"), transition="slideup",
        grade="crisp", duration=9, shots=6, title_seconds=2.0,
        transition_seconds=0.24, source="catalog",
    ),
    _style(
        "night-market", "Night market",
        "Cool night grade, split bars and windswept cuts between quick frames.",
        accent="#a855f7", backdrop="gradient", title_animation="split-bars",
        shot_motions=("whip", "glide"), transition="hlwind",
        grade="midnight", duration=10, shots=6, title_seconds=2.0,
        transition_seconds=0.28, source="catalog",
    ),
    _style(
        "documentary-open", "Documentary open",
        "Long faded frames, a patient lower third and downward smoothing.",
        accent="#e7e5e4", backdrop="source-blur", title_animation="lower-third",
        shot_motions=("drift", "hold"), transition="smoothdown",
        grade="faded", duration=18, shots=6, title_seconds=3.2,
        transition_seconds=0.55, source="catalog",
    ),
    _style(
        "sports-cut", "Sports cut",
        "Punchy duotone, stuttered title and upward wipes on every beat.",
        accent="#f97316", backdrop="duotone", title_animation="flash-cut",
        shot_motions=("whip", "punch-in"), transition="wipeup",
        grade="vivid", duration=9, shots=7, title_seconds=1.8,
        transition_seconds=0.2, letterbox=False, source="catalog",
    ),
    _style(
        "boardroom", "Boardroom",
        "Quiet corporate panel, held frames and gentle tilts between fades.",
        accent="#64748b", backdrop="dark-panel", title_animation="side-panel",
        shot_motions=("hold", "tilt"), transition="fade",
        grade="clean", duration=13, shots=4, grain=False, source="catalog",
    ),
    _style(
        "teal-trailer", "Teal trailer",
        "Blockbuster teal-and-orange, slow pull-backs and cuts through black.",
        accent="#f59e0b", backdrop="source-blur", title_animation="band-reveal",
        shot_motions=("pull-back", "glide"), transition="fadeblack",
        grade="teal-orange", duration=20, shots=6, title_seconds=3.4,
        transition_seconds=0.6, source="catalog",
    ),
    _style(
        "mono-brief", "Mono brief",
        "Technical grid, monochrome frames and horizontally opening cuts.",
        accent="#94a3b8", backdrop="grid", title_animation="stack-lines",
        shot_motions=("hold",), transition="horzopen",
        grade="mono", duration=12, shots=5, grain=False,
        transition_seconds=0.32, source="catalog",
    ),
    _style(
        "sunrise-vlog", "Sunrise vlog",
        "Warm gradient, centred pop title and circular openings between drifts.",
        accent="#fb7185", backdrop="gradient", title_animation="center-pop",
        shot_motions=("drift", "tilt"), transition="circleopen",
        grade="sunset", duration=12, shots=5, letterbox=False,
        transition_seconds=0.4, source="catalog",
    ),
    _style(
        "hard-cut", "Hard cut",
        "Spotlit black-and-white, splitting bars and a distance-warp transition.",
        accent="#ffffff", backdrop="stage", title_animation="split-bars",
        shot_motions=("whip", "hold"), transition="distance",
        grade="noir", duration=11, shots=6, title_seconds=2.2,
        transition_seconds=0.3, source="catalog",
    ),
    _style(
        "gallery", "Gallery",
        "Bleached high-key frames, pooled light and blurred crossings.",
        accent="#a3a3a3", backdrop="stage", title_animation="center-pop",
        shot_motions=("pull-back", "drift"), transition="hblur",
        grade="bleach", duration=16, shots=4, title_seconds=3.2,
        transition_seconds=0.5, source="catalog",
    ),
    _style(
        "ticker", "Ticker",
        "Rapid lower thirds sliding in over held frames and quick pushes.",
        accent="#0ea5e9", backdrop="dark-panel", title_animation="lower-third",
        shot_motions=("hold", "punch-in"), transition="slideright",
        grade="crisp", duration=10, shots=6, title_seconds=2.0,
        transition_seconds=0.22, grain=False, source="catalog",
    ),
    _style(
        "deep-focus", "Deep focus",
        "Cold night grade, steady push-ins and radial sweeps between shots.",
        accent="#60a5fa", backdrop="source-blur", title_animation="band-reveal",
        shot_motions=("punch-in", "hold"), transition="radial",
        grade="midnight", duration=15, shots=5, transition_seconds=0.45,
        source="catalog",
    ),
    _style(
        "vhs-rewind", "VHS rewind",
        "Washed-out tape look, stuttered title and sliced vertical transitions.",
        accent="#c084fc", backdrop="duotone", title_animation="flash-cut",
        shot_motions=("whip", "tilt"), transition="vdslice",
        grade="faded", duration=10, shots=6, title_seconds=1.9,
        transition_seconds=0.26, source="catalog",
    ),
)


# --- persistence -------------------------------------------------------------
def custom_styles_path(state_dir: Path) -> Path:
    return state_dir / "intro-styles.json"


def load_custom_styles(state_dir: Path) -> list[IntroStyle]:
    path = custom_styles_path(state_dir)
    if not path.is_file():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
        return [
            IntroStyle.model_validate({**row, "source": row.get("source", "custom")})
            for row in rows
        ]
    except (OSError, json.JSONDecodeError, ValueError):
        return []


def save_custom_styles(state_dir: Path, styles: list[IntroStyle]) -> None:
    path = custom_styles_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps([style.public() for style in styles], indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def available_styles(state_dir: Path) -> list[IntroStyle]:
    return [*BUILT_IN_STYLES, *load_custom_styles(state_dir)]


def resolve_style(state_dir: Path, style_id: str) -> IntroStyle:
    style = next((item for item in available_styles(state_dir) if item.id == style_id), None)
    if style is None:
        raise ValueError(f"No intro style named '{style_id}'.")
    return style


def install_catalog_style(state_dir: Path, style_id: str) -> IntroStyle:
    style = next((item for item in CATALOG_STYLES if item.id == style_id), None)
    if style is None:
        raise ValueError(f"No catalog intro style named '{style_id}'.")
    existing = load_custom_styles(state_dir)
    if not any(item.id == style.id for item in existing):
        existing.append(style)
        save_custom_styles(state_dir, existing)
    return style


def import_custom_style(state_dir: Path, definition: dict[str, object]) -> IntroStyle:
    clean_id = re.sub(r"[^a-z0-9-]+", "-", str(definition.get("id") or "").lower()).strip("-")
    style = IntroStyle.model_validate({**definition, "id": clean_id, "source": "custom"})
    reserved = {item.id for item in (*BUILT_IN_STYLES, *CATALOG_STYLES)}
    if style.id in reserved:
        raise ValueError("Custom style ids cannot replace bundled styles.")
    existing = [item for item in load_custom_styles(state_dir) if item.id != style.id]
    existing.append(style)
    save_custom_styles(state_dir, existing)
    return style


# --- planning ----------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class IntroScene:
    kind: SceneKind
    duration: float
    span: Span | None = None
    text: str = ""
    motion: ShotMotion = "hold"


@dataclass(frozen=True, slots=True)
class IntroPlan:
    scenes: tuple[IntroScene, ...]
    total_seconds: float
    transition_seconds: float

    @property
    def shots(self) -> tuple[IntroScene, ...]:
        return tuple(scene for scene in self.scenes if scene.kind == "shot")

    def describe(self) -> list[str]:
        label = {
            "hook": "Cold open",
            "title": "Title reveal",
            "shot": "Shot",
            "kicker": "Kicker line",
            "end-card": "End card",
        }
        return [
            f"{label[scene.kind]} — {scene.duration:.1f}s"
            + (f" ({scene.motion})" if scene.kind == "shot" else "")
            for scene in self.scenes
        ]


def shot_anchors(source_duration: float, count: int, report: AnalysisReport | None) -> list[float]:
    """Pick the moments an intro should be built from, best first, in order."""
    if source_duration <= 0:
        raise ValueError("The source duration is unknown.")
    spacing = source_duration / max(1, count)
    chosen: list[float] = []

    if report is not None:
        scored = sorted(
            (
                (segment.importance, (segment.start + segment.end) / 2)
                for segment in report.segment_analyses
                if segment.keep and segment.end > segment.start
            ),
            reverse=True,
        )
        for _score, midpoint in scored:
            if all(abs(midpoint - taken) >= spacing * 0.5 for taken in chosen):
                chosen.append(midpoint)
            if len(chosen) >= count:
                break

    for index in range(count):
        if len(chosen) >= count:
            break
        candidate = spacing * (index + 0.5)
        if all(abs(candidate - taken) >= spacing * 0.5 for taken in chosen):
            chosen.append(candidate)

    return sorted(chosen[:count]) or [source_duration / 2]


def _span_at(anchor: float, duration: float, source_duration: float) -> Span:
    start = max(0.0, min(source_duration - duration, anchor - duration / 2))
    return (max(0.0, start), max(0.0, start) + duration)


def plan_intro(
    style: IntroStyle,
    *,
    total_seconds: float,
    shot_count: int,
    source_duration: float,
    report: AnalysisReport | None = None,
    subtitle: str = "",
    labels: list[str] | None = None,
) -> IntroPlan:
    """Lay out the whole sequence so it lands exactly on the requested runtime."""
    if total_seconds <= 0:
        raise ValueError("An intro needs a positive length.")
    shots = max(2, min(shot_count, 12))
    overlap = style.transition_seconds

    use_hook = style.hook and total_seconds >= 8
    use_kicker = style.kicker and bool(subtitle.strip()) and total_seconds >= 10
    use_end_card = style.end_card and total_seconds >= 7

    def budget(count: int, scale: float) -> tuple[float, float, float, float, float, int]:
        hook_seconds = 0.8 * scale if use_hook else 0.0
        title_seconds = style.title_seconds * scale
        kicker_seconds = 1.7 * scale if use_kicker else 0.0
        end_seconds = style.end_card_seconds * scale if use_end_card else 0.0
        scene_total = count + 1 + (1 if use_hook else 0) + (1 if use_kicker else 0) + (1 if use_end_card else 0)
        overlaps = overlap * max(0, scene_total - 1)
        fixed = hook_seconds + title_seconds + kicker_seconds + end_seconds
        return hook_seconds, title_seconds, kicker_seconds, end_seconds, total_seconds + overlaps - fixed, count

    hook_s, title_s, kicker_s, end_s, shot_budget, shots = budget(shots, 1.0)
    while shot_budget < shots * _MIN_SHOT_SECONDS and shots > 2:
        shots -= 1
        hook_s, title_s, kicker_s, end_s, shot_budget, shots = budget(shots, 1.0)

    if shot_budget < shots * _MIN_SHOT_SECONDS:
        # Very short runtimes: give the shots room by tightening the fixed scenes.
        for scale in (0.85, 0.7, 0.6):
            hook_s, title_s, kicker_s, end_s, shot_budget, shots = budget(shots, scale)
            if shot_budget >= shots * _MIN_SHOT_SECONDS:
                break
    shot_budget = max(shot_budget, shots * _MIN_SHOT_SECONDS)

    def projected(candidate: float) -> tuple[float, list[float]]:
        weights = [_RHYTHM[index % len(_RHYTHM)] for index in range(shots)]
        unit = candidate / sum(weights)
        candidate_durations = [max(_MIN_SHOT_SECONDS, unit * weight) for weight in weights]
        sequence = [*([hook_s] if use_hook else []), title_s, *candidate_durations]
        if use_kicker:
            sequence.append(kicker_s)
        if use_end_card:
            sequence.append(end_s)
        overlaps = sum(
            min(overlap, left / 2, right / 2)
            for left, right in zip(sequence, sequence[1:])
        )
        return sum(sequence) - overlaps, candidate_durations

    # Find the shot budget whose capped overlaps produce the requested encoded
    # runtime. This mirrors the xfade loop below instead of assuming every scene
    # is long enough for the style's full transition.
    low = shots * _MIN_SHOT_SECONDS
    while projected(low)[0] > total_seconds and shots > 2:
        shots -= 1
        low = shots * _MIN_SHOT_SECONDS
    high = max(shot_budget, total_seconds + overlap * (shots + 3))
    durations: list[float] = []
    for _ in range(48):
        midpoint = (low + high) / 2
        projected_total, durations = projected(midpoint)
        if projected_total < total_seconds:
            low = midpoint
        else:
            high = midpoint
    _projected_total, durations = projected((low + high) / 2)

    anchors = shot_anchors(source_duration, shots + (1 if use_hook else 0), report)
    hook_anchor = anchors[0]
    shot_anchor_list = list(anchors[1:]) if use_hook else list(anchors)
    while len(shot_anchor_list) < shots:
        shot_anchor_list.append(shot_anchor_list[-1] if shot_anchor_list else source_duration / 2)

    scenes: list[IntroScene] = []
    if use_hook:
        scenes.append(
            IntroScene(
                kind="hook",
                duration=hook_s,
                span=_span_at(hook_anchor, min(hook_s + 0.4, source_duration), source_duration),
                motion="whip",
            )
        )
    scenes.append(IntroScene(kind="title", duration=title_s, span=_span_at(anchors[0], 0.4, source_duration)))

    names = labels or []
    for index in range(shots):
        duration = durations[index]
        scenes.append(
            IntroScene(
                kind="shot",
                duration=duration,
                span=_span_at(
                    shot_anchor_list[index], min(duration + 0.5, source_duration), source_duration
                ),
                text=names[index] if index < len(names) else "",
                motion=style.shot_motions[index % len(style.shot_motions)],
            )
        )

    if use_kicker:
        scenes.append(
            IntroScene(
                kind="kicker",
                duration=kicker_s,
                span=_span_at(shot_anchor_list[-1], min(kicker_s + 0.4, source_duration), source_duration),
                text=subtitle.strip(),
            )
        )
    if use_end_card:
        scenes.append(
            IntroScene(
                kind="end-card",
                duration=end_s,
                span=_span_at(anchors[-1], 0.4, source_duration),
            )
        )

    total = sum(scene.duration for scene in scenes) - sum(
        min(overlap, left.duration / 2, right.duration / 2)
        for left, right in zip(scenes, scenes[1:])
    )
    return IntroPlan(scenes=tuple(scenes), total_seconds=total, transition_seconds=overlap)


def shot_labels(spans: list[Span], report: AnalysisReport | None) -> list[str]:
    if report is None:
        return [""] * len(spans)
    labels: list[str] = []
    for start, end in spans:
        chapter = max(
            report.chapters,
            key=lambda item: max(0.0, min(end, item.end) - max(start, item.start)),
            default=None,
        )
        labels.append(
            chapter.title if chapter and chapter.end > start and chapter.start < end else ""
        )
    return labels


def fit_narration(text: str, duration_seconds: float) -> str:
    """Trim overview prose to a conservative local-speech budget."""
    clean = " ".join(text.split()).strip()
    if not clean:
        return "A concise visual overview of the recording."
    word_budget = max(6, int(duration_seconds * 2.1))
    words = clean.split()
    if len(words) <= word_budget:
        return clean
    shortened = " ".join(words[:word_budget]).rstrip(",;:-. ")
    return f"{shortened}."


# --- rendering ---------------------------------------------------------------
def _ease(delay: float, span: float) -> str:
    """Cosine ease-in-out in ffmpeg expression form, 0 → 1 over ``span``."""
    return f"(1-cos(min(1\\,max(0\\,(t-{delay:.3f}))/{span:.3f})*PI))/2"


#: Measured advance width of the bundled font, as a fraction of the font size.
_CHAR_ADVANCE = 0.62


def _fit_text(
    text: str, *, max_width: int, ideal_size: int, min_size: int, max_lines: int = 2
) -> tuple[str, int, int]:
    """Wrap and size a line so it always fits the frame.

    drawtext cannot measure itself, so the size is derived from the font's
    advance width. Titles are real sentences and would otherwise run off frame.
    """
    clean = " ".join(text.split())
    if not clean:
        return "", ideal_size, 1

    best: tuple[str, int, int] | None = None
    for lines in range(1, max_lines + 1):
        width_chars = max(6, math.ceil(len(clean) / lines))
        wrapped = textwrap.wrap(clean, width=width_chars)
        # Widen until it genuinely fits: wrapping must never drop words.
        while len(wrapped) > lines and width_chars < len(clean):
            width_chars += 2
            wrapped = textwrap.wrap(clean, width=width_chars)
        if len(wrapped) > lines or not wrapped:
            continue
        longest = max(len(line) for line in wrapped)
        size = min(ideal_size, int(max_width / (_CHAR_ADVANCE * longest)))
        if best is None or size > best[1]:
            best = ("\n".join(wrapped), size, len(wrapped))

    if best is None:
        return clean, min_size, 1
    body, size, lines = best
    return body, max(1, size), lines


def render_intro(
    source: Path,
    destination: Path,
    plan: IntroPlan,
    *,
    style: IntroStyle,
    render: RenderConfig,
    ffmpeg_bin: str,
    soundtrack_path: Path,
    title: str,
    subtitle: str = "",
    end_card_text: str = "",
    repeat_title: bool = False,
    voiceover_path: Path | None = None,
    on_progress=None,
) -> Path:
    """Compose the planned intro in a single ffmpeg pass."""
    if not plan.scenes:
        raise ValueError("The intro plan is empty.")
    destination.parent.mkdir(parents=True, exist_ok=True)

    width, height = render.width, render.height
    fps = render.fps or 30
    accent = "0x" + style.accent.lstrip("#")
    total = plan.total_seconds
    text_files: list[Path] = []
    parts: list[str] = []

    voice_index = 2 if voiceover_path is not None else None
    canvas_index = 3 if voiceover_path is not None else 2
    gradient_index = canvas_index + 1 if style.backdrop == "gradient" else None

    def tfile(suffix: str, value: str) -> Path:
        path = destination.parent / f".{destination.stem}-{suffix}.txt"
        # drawtext treats a stray CR as its own line, which doubles the leading.
        path.write_text(value, encoding="utf-8", newline="\n")
        text_files.append(path)
        return path

    def esc(path: Path) -> str:
        return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")

    def finish(src: str, label: str, duration: float) -> None:
        parts.append(
            f"[{src}]setsar=1,fps={fps},format=yuv420p,"
            f"trim=duration={duration:.3f},setpts=PTS-STARTPTS[{label}];"
        )

    def backdrop(label: str, duration: float, anchor: float) -> None:
        if style.backdrop == "gradient":
            parts.append(
                f"[{gradient_index}:v]trim=duration={duration:.3f},setpts=PTS-STARTPTS,"
                f"scale={width}:{height},format=yuv420p[{label}];"
            )
        elif style.backdrop == "dark-panel":
            # A lower third already owns that band; a second rule would poke out past it.
            rule = (
                ""
                if style.title_animation == "lower-third"
                else (
                    f"drawbox=x=0:y='ih*0.70':w=iw:h={max(2, int(height * 0.004))}:"
                    f"color={accent}@0.65:t=fill,"
                )
            )
            parts.append(
                f"[{canvas_index}:v]trim=duration={duration:.3f},setpts=PTS-STARTPTS,"
                f"scale={width}:{height},"
                f"drawbox=x=0:y=0:w=iw:h=ih:color=0x0a0e16@1:t=fill,"
                f"{rule}format=yuv420p[{label}];"
            )
        elif style.backdrop == "duotone":
            parts.append(
                f"[0:v]trim=start={anchor:.3f}:end={anchor + 0.05:.3f},setpts=PTS-STARTPTS,"
                f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
                f"tpad=stop_mode=clone:stop_duration={duration + 0.5:.3f},"
                f"hue=s=0,eq=contrast=1.22:brightness=-0.24,colorbalance=rs=-0.12:bs=0.18,"
                f"boxblur=8:1,format=yuv420p[{label}];"
            )
        elif style.backdrop == "stage":
            parts.append(
                f"[0:v]trim=start={anchor:.3f}:end={anchor + 0.05:.3f},setpts=PTS-STARTPTS,"
                f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
                f"tpad=stop_mode=clone:stop_duration={duration + 0.5:.3f},"
                f"boxblur=30:2,eq=brightness=-0.50:saturation=0.28,vignette=PI/3,"
                f"format=yuv420p[{label}];"
            )
        elif style.backdrop == "grid":
            rule = max(1, int(height * 0.0016))
            lines = "".join(
                f"drawbox=x={int(width * (column + 1) / 12)}:y=0:w={rule}:h=ih:"
                f"color={accent}@0.16:t=fill,"
                for column in range(11)
            ) + "".join(
                f"drawbox=x=0:y={int(height * (row + 1) / 7)}:w=iw:h={rule}:"
                f"color={accent}@0.16:t=fill,"
                for row in range(6)
            )
            parts.append(
                f"[{canvas_index}:v]trim=duration={duration:.3f},setpts=PTS-STARTPTS,"
                f"scale={width}:{height},"
                f"drawbox=x=0:y=0:w=iw:h=ih:color=0x070a11@1:t=fill,"
                f"{lines}format=yuv420p[{label}];"
            )
        else:
            parts.append(
                f"[0:v]trim=start={anchor:.3f}:end={anchor + 0.05:.3f},setpts=PTS-STARTPTS,"
                f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
                f"tpad=stop_mode=clone:stop_duration={duration + 0.5:.3f},"
                f"boxblur=22:2,eq=brightness=-0.36:saturation=0.70,"
                f"zoompan=z='min(1.0+0.0006*on\\,1.10)':x='iw/2-(iw/zoom/2)':"
                f"y='ih/2-(ih/zoom/2)':d=1:s={width}x{height}:fps={fps},format=yuv420p[{label}];"
            )

    def masked_line(
        bg: str,
        out: str,
        duration: float,
        path: Path,
        *,
        font_size: int,
        y_text: int,
        delay: float,
        rise: float,
        lines: int = 1,
        x_expr: str = "(w-text_w)/2",
    ) -> None:
        """Reveal a line by sliding it up inside a cropped window — a real mask."""
        band = int(font_size * (1.28 * lines + 0.34))
        band_y = max(0, y_text - int(font_size * 0.32))
        layer = f"{out}l"
        parts.append(
            f"[{canvas_index}:v]trim=duration={duration:.3f},setpts=PTS-STARTPTS,"
            f"scale={width}:{height},format=rgba,"
            f"drawtext=textfile='{esc(path)}':expansion=none:fontsize={font_size}:fontcolor=white:"
            f"line_spacing={max(2, int(font_size * 0.12))}:"
            f"x='{x_expr}':y='{y_text}+(1-{_ease(delay, rise)})*{band}':"
            f"alpha='min(1\\,max(0\\,(t-{delay:.3f})/0.32))',"
            f"crop=w={width}:h={band}:x=0:y={band_y}[{layer}];"
        )
        parts.append(f"[{bg}][{layer}]overlay=x=0:y={band_y}[{out}];")

    def accent_rule(src: str, out: str, *, y: int, delay: float, half_width: int) -> str:
        grow = f"min({half_width}\\,max(0\\,(t-{delay:.3f}))*{half_width * 2.4:.0f})"
        parts.append(
            f"[{src}]drawbox=x='(iw/2)-{grow}':y={y}:w='2*{grow}':"
            f"h={max(3, int(height * 0.006))}:color={accent}@1:t=fill[{out}];"
        )
        return out

    def light_sweep(src: str, out: str, duration: float) -> str:
        parts.append(
            f"[{src}]drawbox=x='-iw*0.2+(t/{duration:.3f})*iw*1.45':y=0:w='iw*0.1':h=ih:"
            f"color=white@0.09:t=fill[{out}];"
        )
        return out

    def title_scene(index: int, scene: IntroScene) -> str:
        duration = scene.duration
        anchor = scene.span[0] if scene.span else 0.0
        label = f"sc{index}"
        backdrop(f"t{index}bg", duration, anchor)
        current = f"t{index}bg"
        if style.light_sweep:
            current = light_sweep(current, f"t{index}sw", duration)

        title_width = {
            "side-panel": int(width * 0.46),
            "stack-lines": int(width * 0.82),
            "lower-third": int(width * 0.60),
            "split-bars": int(width * 0.78),
        }.get(style.title_animation, int(width * 0.86))
        title_scale = 0.085 if style.title_animation == "lower-third" else 0.125
        title_body, big, title_lines = _fit_text(
            title,
            max_width=title_width,
            ideal_size=max(28, int(height * title_scale)),
            min_size=max(18, int(height * 0.045)),
        )
        sub_body, small, sub_lines = _fit_text(
            subtitle,
            max_width=min(title_width, int(width * 0.74)),
            ideal_size=max(16, int(height * 0.040)),
            min_size=max(12, int(height * 0.024)),
        )
        title_path = tfile(f"title-{index}", title_body)
        subtitle_path = tfile(f"sub-{index}", sub_body) if sub_body else None
        block = int(big * 1.28 * title_lines)

        if style.title_animation == "side-panel":
            panel_w = int(width * 0.56)
            slide = f"-{panel_w}+{_ease(0.0, 0.55)}*{panel_w}"
            edge = max(3, int(width * 0.004))
            title_top = max(0, int(height * 0.5 - block * 0.5 - small * 0.9))
            parts.append(
                f"[{current}]drawbox=x='{slide}':y=0:"
                f"w={panel_w}:h=ih:color=0x080b12@0.93:t=fill,"
                f"drawbox=x='{slide}+{panel_w - edge}':"
                f"y=0:w={edge}:h=ih:color={accent}@1:t=fill[t{index}p];"
            )
            masked_line(
                f"t{index}p", f"t{index}a", duration, title_path,
                font_size=big, y_text=title_top, lines=title_lines,
                delay=0.35, rise=0.5, x_expr=f"{int(width * 0.06)}",
            )
            current = f"t{index}a"
            if subtitle_path is not None:
                masked_line(
                    current, f"t{index}b", duration, subtitle_path,
                    font_size=small, y_text=title_top + block + int(big * 0.22),
                    lines=sub_lines, delay=0.7, rise=0.45, x_expr=f"{int(width * 0.06)}",
                )
                current = f"t{index}b"
        elif style.title_animation == "flash-cut":
            jitter = "6*sin(t*46)*exp(-t*5)"
            title_top = max(0, int(height * 0.46 - block * 0.5))
            parts.append(
                f"[{current}]drawbox=x=0:y={max(0, title_top - int(big * 0.35))}:w=iw:"
                f"h={block + int(big * 0.7)}:color=0x05070c@0.55:t=fill,"
                f"drawtext=textfile='{esc(title_path)}':expansion=none:fontsize={big}:fontcolor=white:"
                f"line_spacing={max(2, int(big * 0.12))}:"
                f"x='(w-text_w)/2+{jitter}':y={title_top}:"
                f"alpha='if(lt(t\\,0.10)\\,0\\,if(lt(t\\,0.16)\\,1\\,if(lt(t\\,0.22)\\,0.15\\,1)))'"
                f"[t{index}a];"
            )
            current = accent_rule(
                f"t{index}a", f"t{index}r",
                y=title_top + block + int(big * 0.14), delay=0.28, half_width=int(width * 0.20),
            )
            if subtitle_path is not None:
                masked_line(
                    current, f"t{index}b", duration, subtitle_path,
                    font_size=small, y_text=title_top + block + int(big * 0.45),
                    lines=sub_lines, delay=0.5, rise=0.4,
                )
                current = f"t{index}b"
        elif style.title_animation == "center-pop":
            title_top = max(0, int(height * 0.48 - block * 0.5))
            bracket_h = block + int(big * 0.4)
            bracket_y = max(0, title_top - int(big * 0.2))
            bracket_w = max(3, int(width * 0.003))
            bracket_left = int(width * 0.045)
            bracket_right = width - bracket_left - bracket_w
            parts.append(
                f"[{current}]drawtext=textfile='{esc(title_path)}':expansion=none:fontsize={big}:fontcolor=white:"
                f"line_spacing={max(2, int(big * 0.12))}:"
                f"x='(w-text_w)/2':y={title_top}:alpha='min(1\\,max(0\\,(t-0.15)/0.45))',"
                f"drawbox=x={bracket_left}:y={bracket_y}:"
                f"w={bracket_w}:h='{_ease(0.25, 0.5)}*{bracket_h}':"
                f"color={accent}@1:t=fill,"
                f"drawbox=x={bracket_right}:y={bracket_y}:"
                f"w={bracket_w}:h='{_ease(0.25, 0.5)}*{bracket_h}':"
                f"color={accent}@1:t=fill[t{index}a];"
            )
            current = f"t{index}a"
            if subtitle_path is not None:
                masked_line(
                    current, f"t{index}b", duration, subtitle_path,
                    font_size=small, y_text=title_top + block + int(big * 0.22),
                    lines=sub_lines, delay=0.55, rise=0.45,
                )
                current = f"t{index}b"
            parts.append(
                f"[{current}]zoompan=z='max(1\\,1.09-0.09*min(1\\,on/{max(1, int(fps * 0.6))}))':"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={width}x{height}:fps={fps}"
                f"[t{index}z];"
            )
            current = f"t{index}z"
        elif style.title_animation == "stack-lines":
            title_top = max(0, int(height * 0.5 - block * 0.5 - small * 0.9))
            rule_h = block + int(small * (1.4 * sub_lines if subtitle_path else 0)) + int(big * 0.2)
            parts.append(
                f"[{current}]drawbox=x={int(width * 0.08)}:y={max(0, title_top - int(big * 0.18))}:"
                f"w={max(3, int(width * 0.004))}:h='{_ease(0.1, 0.5)}*{rule_h}':"
                f"color={accent}@1:t=fill[t{index}v];"
            )
            masked_line(
                f"t{index}v", f"t{index}a", duration, title_path,
                font_size=big, y_text=title_top, lines=title_lines,
                delay=0.18, rise=0.5, x_expr=f"{int(width * 0.11)}",
            )
            current = f"t{index}a"
            if subtitle_path is not None:
                masked_line(
                    current, f"t{index}b", duration, subtitle_path,
                    font_size=small, y_text=title_top + block + int(big * 0.22),
                    lines=sub_lines, delay=0.55, rise=0.45, x_expr=f"{int(width * 0.11)}",
                )
                current = f"t{index}b"
        elif style.title_animation == "split-bars":
            title_top = max(0, int(height * 0.47 - block * 0.5))
            bar_h = max(3, int(height * 0.006))
            reach = int(width * 0.30)
            grow = f"min({reach}\\,max(0\\,(t-0.08))*{int(reach * 2.6)})"
            parts.append(
                f"[{current}]drawbox=x='(iw/2)-{grow}':"
                f"y={max(0, title_top - int(big * 0.46))}:w='2*{grow}':h={bar_h}:"
                f"color={accent}@1:t=fill,"
                f"drawbox=x='(iw/2)-{grow}':"
                f"y={title_top + block + int(big * 0.26)}:w='2*{grow}':h={bar_h}:"
                f"color={accent}@1:t=fill[t{index}bars];"
            )
            masked_line(
                f"t{index}bars", f"t{index}a", duration, title_path,
                font_size=big, y_text=title_top, lines=title_lines, delay=0.3, rise=0.55,
            )
            current = f"t{index}a"
            if subtitle_path is not None:
                masked_line(
                    current, f"t{index}b", duration, subtitle_path,
                    font_size=small, y_text=title_top + block + int(big * 0.60),
                    lines=sub_lines, delay=0.7, rise=0.4,
                )
                current = f"t{index}b"
        elif style.title_animation == "lower-third":
            sub_block = int(small * 1.4 * sub_lines) if subtitle_path is not None else 0
            bar_h = block + sub_block + int(big * 0.65)
            bar_y = max(0, int(height * 0.86) - bar_h)
            bar_w = int(width * 0.74)
            slide = f"-{bar_w}+{_ease(0.0, 0.5)}*{bar_w}"
            edge = max(4, int(width * 0.005))
            title_top = bar_y + int(big * 0.30)
            parts.append(
                f"[{current}]drawbox=x='{slide}':y={bar_y}:w={bar_w}:h={bar_h}:"
                f"color=0x080b12@0.90:t=fill,"
                f"drawbox=x='{slide}':y={bar_y}:w={edge}:h={bar_h}:"
                f"color={accent}@1:t=fill[t{index}p];"
            )
            masked_line(
                f"t{index}p", f"t{index}a", duration, title_path,
                font_size=big, y_text=title_top, lines=title_lines,
                delay=0.4, rise=0.45, x_expr=f"{int(width * 0.055)}",
            )
            current = f"t{index}a"
            if subtitle_path is not None:
                masked_line(
                    current, f"t{index}b", duration, subtitle_path,
                    font_size=small, y_text=title_top + block + int(big * 0.14),
                    lines=sub_lines, delay=0.72, rise=0.4, x_expr=f"{int(width * 0.055)}",
                )
                current = f"t{index}b"
        else:  # band-reveal
            title_top = max(0, int(height * 0.47 - block * 0.5))
            masked_line(
                current, f"t{index}a", duration, title_path,
                font_size=big, y_text=title_top, lines=title_lines, delay=0.15, rise=0.6,
            )
            current = accent_rule(
                f"t{index}a", f"t{index}r",
                y=title_top + block + int(big * 0.12), delay=0.5, half_width=int(width * 0.22),
            )
            if subtitle_path is not None:
                masked_line(
                    current, f"t{index}b", duration, subtitle_path,
                    font_size=small, y_text=title_top + block + int(big * 0.42),
                    lines=sub_lines, delay=0.75, rise=0.45,
                )
                current = f"t{index}b"

        finish(current, label, duration)
        return label

    def shot_scene(index: int, scene: IntroScene, *, flash_out: bool) -> str:
        label = f"sc{index}"
        start, end = scene.span or (0.0, scene.duration)
        duration = scene.duration
        base = f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS"
        travel = max(0.25, duration)

        if scene.motion == "punch-in":
            body = (
                f"{base},scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},"
                f"zoompan=z='min(1.0+0.0020*on\\,1.18)':x='iw/2-(iw/zoom/2)':"
                f"y='ih/2-(ih/zoom/2)':d=1:s={width}x{height}:fps={fps}"
            )
        elif scene.motion == "pull-back":
            body = (
                f"{base},scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},"
                f"zoompan=z='max(1.0\\,1.18-0.0020*on)':x='iw/2-(iw/zoom/2)':"
                f"y='ih/2-(ih/zoom/2)':d=1:s={width}x{height}:fps={fps}"
            )
        elif scene.motion == "whip":
            body = (
                f"{base},scale={int(width * 1.4)}:{int(height * 1.4)}:"
                f"force_original_aspect_ratio=increase,"
                f"crop={width}:{height}:x='(in_w-out_w)*min(1\\,t/{travel * 0.6:.3f})':"
                f"y='(in_h-out_h)/2',tmix=frames=3"
            )
        elif scene.motion == "drift":
            body = (
                f"{base},scale={int(width * 1.14)}:{int(height * 1.14)}:"
                f"force_original_aspect_ratio=increase,"
                f"crop={width}:{height}:x='(in_w-out_w)*(0.5+0.5*sin(t*0.7))':"
                f"y='(in_h-out_h)*0.5'"
            )
        elif scene.motion == "tilt":
            body = (
                f"{base},scale={int(width * 1.16)}:{int(height * 1.16)}:"
                f"force_original_aspect_ratio=increase,"
                f"crop={width}:{height}:x='(in_w-out_w)*0.5':"
                f"y='(in_h-out_h)*(0.5+0.5*sin(t*0.62))'"
            )
        elif scene.motion == "glide":
            progress = f"min(1\\,t/{travel:.3f})"
            body = (
                f"{base},scale={int(width * 1.20)}:{int(height * 1.20)}:"
                f"force_original_aspect_ratio=increase,"
                f"crop={width}:{height}:x='(in_w-out_w)*{progress}':"
                f"y='(in_h-out_h)*(1-{progress})'"
            )
        else:
            body = (
                f"{base},scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height}"
            )

        grade = ",".join(_GRADE_FILTERS[style.grade])
        if grade:
            body += "," + grade
        parts.append(f"{body}[s{index}m];")
        current = f"s{index}m"

        if scene.text:
            bar_x = int(width * 0.055)
            font = max(14, int(height * 0.034))
            tag_body, font, _tag_lines = _fit_text(
                scene.text,
                max_width=int(width * 0.52),
                ideal_size=font,
                min_size=max(11, int(height * 0.022)),
                max_lines=1,
            )
            tag_path = tfile(f"tag-{index}", tag_body)
            bar_h = max(10, int(font * 1.5))
            bar_y = int(height * 0.80)
            parts.append(
                f"[{current}]drawbox=x={bar_x}:y={bar_y}:w={max(4, int(width * 0.0045))}:"
                f"h={bar_h}:color={accent}@1:t=fill:enable='gt(t\\,0.12)',"
                f"drawtext=textfile='{esc(tag_path)}':expansion=none:fontsize={font}:fontcolor=white:"
                f"x='{bar_x + 20}+(1-{_ease(0.12, 0.4)})*42':y={bar_y + int(bar_h * 0.2)}:"
                f"alpha='min(1\\,max(0\\,(t-0.12)/0.3))':box=1:boxcolor=black@0.42:boxborderw=12"
                f"[s{index}t];"
            )
            current = f"s{index}t"

        if repeat_title and index == 0:
            hero_body, hero_size, _hero_lines = _fit_text(
                title,
                max_width=int(width * 0.82),
                ideal_size=max(20, int(height * 0.062)),
                min_size=max(14, int(height * 0.030)),
                max_lines=1,
            )
            hero_path = tfile(f"hero-{index}", hero_body)
            parts.append(
                f"[{current}]drawtext=textfile='{esc(hero_path)}':expansion=none:"
                f"fontsize={hero_size}:fontcolor=white:"
                f"x='(w-text_w)/2':y='h*0.10':"
                f"alpha='min(1\\,max(0\\,(t-0.1)/0.35))*min(1\\,max(0\\,({duration:.3f}-t)/0.4))':"
                f"box=1:boxcolor=black@0.42:boxborderw=14[s{index}h];"
            )
            current = f"s{index}h"

        if flash_out:
            parts.append(
                f"[{current}]fade=t=out:st={max(0.0, duration - 0.12):.3f}:d=0.12:color=white"
                f"[s{index}f];"
            )
            current = f"s{index}f"

        finish(current, label, duration)
        return label

    def kicker_scene(index: int, scene: IntroScene) -> str:
        label = f"sc{index}"
        start, end = scene.span or (0.0, scene.duration)
        duration = scene.duration
        parts.append(
            f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS,"
            f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
            f"tpad=stop_mode=clone:stop_duration={duration + 0.5:.3f},"
            f"boxblur=10:1,eq=brightness=-0.30:saturation=0.85[k{index}bg];"
        )
        current = f"k{index}bg"
        line_body, font, line_count = _fit_text(
            scene.text,
            max_width=int(width * 0.78),
            ideal_size=max(18, int(height * 0.052)),
            min_size=max(13, int(height * 0.028)),
        )
        line_path = tfile(f"kicker-{index}", line_body)
        block = int(font * 1.28 * line_count)
        top = max(0, int(height * 0.5 - block * 0.5))
        masked_line(
            current, f"k{index}a", duration, line_path,
            font_size=font, y_text=top, lines=line_count, delay=0.12, rise=0.5,
        )
        current = accent_rule(
            f"k{index}a", f"k{index}r",
            y=top + block + int(font * 0.35), delay=0.35, half_width=int(width * 0.16),
        )
        finish(current, label, duration)
        return label

    def end_card_scene(index: int, scene: IntroScene) -> str:
        label = f"sc{index}"
        duration = scene.duration
        anchor = scene.span[0] if scene.span else 0.0
        backdrop(f"e{index}bg", duration, anchor)
        current = f"e{index}bg"
        lock_body, font, lock_lines = _fit_text(
            end_card_text or title,
            max_width=int(width * 0.80),
            ideal_size=max(22, int(height * 0.078)),
            min_size=max(15, int(height * 0.034)),
        )
        lock_path = tfile(f"end-{index}", lock_body)
        block = int(font * 1.28 * lock_lines)
        top = max(0, int(height * 0.5 - block * 0.5))
        masked_line(
            current, f"e{index}a", duration, lock_path,
            font_size=font, y_text=top, lines=lock_lines, delay=0.1, rise=0.5,
        )
        current = accent_rule(
            f"e{index}a", f"e{index}r",
            y=top + block + int(font * 0.3), delay=0.3, half_width=int(width * 0.14),
        )
        parts.append(
            f"[{current}]fade=t=out:st={max(0.0, duration - 0.6):.3f}:d=0.6[e{index}f];"
        )
        finish(f"e{index}f", label, duration)
        return label

    labels: list[str] = []
    for index, scene in enumerate(plan.scenes):
        if scene.kind == "title":
            labels.append(title_scene(index, scene))
        elif scene.kind == "hook":
            labels.append(shot_scene(index, scene, flash_out=True))
        elif scene.kind == "kicker":
            labels.append(kicker_scene(index, scene))
        elif scene.kind == "end-card":
            labels.append(end_card_scene(index, scene))
        else:
            labels.append(shot_scene(index, scene, flash_out=False))

    # Scenes are cross-faded, not butt-joined: each transition eats into the pair.
    chain = labels[0]
    elapsed = plan.scenes[0].duration
    for index in range(1, len(labels)):
        overlap = min(
            plan.transition_seconds,
            plan.scenes[index - 1].duration / 2,
            plan.scenes[index].duration / 2,
        )
        offset = max(0.0, elapsed - overlap)
        out = f"xf{index}"
        parts.append(
            f"[{chain}][{labels[index]}]xfade=transition={style.transition}:"
            f"duration={overlap:.3f}:offset={offset:.3f}[{out}];"
        )
        elapsed = offset + plan.scenes[index].duration
        chain = out

    finishing: list[str] = [f"fade=t=in:st=0:d=0.35"]
    if style.grain:
        finishing.append("noise=alls=6:allf=t+u")
    if style.vignette:
        finishing.append("vignette=PI/5")
    if style.letterbox:
        bar = max(8, int(height * 0.075))
        finishing.append(f"drawbox=x=0:y=0:w=iw:h={bar}:color=black@1:t=fill")
        finishing.append(f"drawbox=x=0:y=ih-{bar}:w=iw:h={bar}:color=black@1:t=fill")
    finishing.append(f"fade=t=out:st={max(0.0, elapsed - 0.45):.3f}:d=0.45")
    parts.append(f"[{chain}]" + ",".join(finishing) + ",format=yuv420p[outv];")

    parts.append(
        f"[1:a]atrim=duration={elapsed:.3f},asetpts=PTS-STARTPTS,aresample=48000,"
        f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"afade=t=in:st=0:d=0.4,afade=t=out:st={max(0.0, elapsed - 0.8):.3f}:d=0.8[music];"
    )
    if voice_index is not None:
        parts.append(
            "[music]volume=0.26[musicduck];"
            f"[{voice_index}:a]apad,atrim=duration={elapsed:.3f},asetpts=PTS-STARTPTS,"
            f"aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[narration];"
            "[musicduck][narration]amix=inputs=2:duration=first:dropout_transition=0[outa]"
        )
    else:
        parts.append("[music]anull[outa]")

    graph = "\n".join(parts)
    scratch = destination.parent / f".{destination.stem}-intro.filter.txt"
    fc_args, script = filter_complex_args(graph, scratch, ffmpeg_bin)

    args = ["-i", str(source), "-i", str(soundtrack_path)]
    if voiceover_path is not None:
        args += ["-i", str(voiceover_path)]
    # The format must be pinned inside the lavfi input: negotiation does not reach
    # back into an input device, and without it the "transparent" canvas is opaque.
    args += [
        "-f", "lavfi",
        "-i", f"color=c=black@0:s={width}x{height}:r={fps},format=rgba",
    ]
    if gradient_index is not None:
        args += [
            "-f", "lavfi",
            "-i",
            f"gradients=s={width}x{height}:r={fps}:c0=0x0a0e16:c1={accent}:speed=0.05",
        ]
    args += [*fc_args, "-map", "[outv]", "-map", "[outa]"]
    args += encode_args(render, has_audio=True)
    args += ["-movflags", "+faststart", str(destination)]

    try:
        run_with_progress(
            ffmpeg_bin,
            args,
            on_elapsed=(lambda seconds: on_progress(min(1.0, seconds / elapsed)))
            if on_progress and elapsed
            else None,
        )
    finally:
        if script is not None:
            script.unlink(missing_ok=True)
        for path in text_files:
            path.unlink(missing_ok=True)
    return destination
