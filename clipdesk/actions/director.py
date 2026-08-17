"""Turn one written instruction into a reviewable plan for any editing action.

The director is a router, not an interpreter. It decides *which* capability the
user is asking for — an intro, an outro, a clip, a clean cut, an assembly, an
export or a visual effect — and fills in that capability's own typed request.
Every value it produces is either a number it parsed, a name it matched against
something that already exists, or an enumerated constant. It never produces an
ffmpeg fragment, an argument or a shell string, so a prompt cannot widen what
the application is able to do.

The plan is always shown before anything runs, because a prompt is a guess about
intent and the user is the only one who can confirm it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from clipdesk.actions.editprogram import EditProgram, parse_edit_program

Intent = Literal["effects", "intro", "outro", "clip", "clean", "assemble", "export"]

_TIME = r"\d{1,2}(?::\d{1,2}){0,2}(?:\.\d+)?"
_RANGE_RE = re.compile(
    rf"(?:from|between)\s+(?P<start>{_TIME})\s*(?:to|and|until|-|–)\s*(?P<end>{_TIME})",
    re.IGNORECASE,
)
_AFTER_RE = re.compile(rf"(?:after|from)\s+(?P<start>{_TIME})\b", re.IGNORECASE)
_BEFORE_RE = re.compile(rf"(?:before|until|up to)\s+(?P<end>{_TIME})\b", re.IGNORECASE)
_FIRST_RE = re.compile(
    r"(?:drop|remove|cut|trim|skip)\s+the\s+first\s+(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>seconds?|secs?|s|minutes?|mins?|m)\b",
    re.IGNORECASE,
)
_LAST_RE = re.compile(
    r"(?:drop|remove|cut|trim|skip)\s+the\s+last\s+(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>seconds?|secs?|s|minutes?|mins?|m)\b",
    re.IGNORECASE,
)
_LENGTH_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>seconds?|secs?|s|minutes?|mins?|m)\b",
    re.IGNORECASE,
)
_COUNT_RE = re.compile(r"\b(?P<count>\d{1,2})\s+(?:moments?|shots?|clips?|highlights?)\b", re.IGNORECASE)
_QUOTED_RE = re.compile(r"[\"“'](?P<value>[^\"”']{2,160})[\"”']")

_INTRO_WORDS = ("intro", "opener", "opening title", "title sequence", "title card")
_OUTRO_WORDS = ("outro", "end card", "endcard", "closing card", "sign-off", "sign off")
_CLEAN_WORDS = (
    "clean cut", "clean up", "cleanup", "tighten", "remove silence", "remove filler",
    "cut the pauses", "cut out the pauses", "remove the pauses", "dead air",
)
#: "clean it up", "clean the recording up" and friends.
_CLEAN_RE = re.compile(r"\bclean\b(?:\s+\w+){0,3}\s+\bup\b|\bclean\s+(?:the\s+)?\w*cut\b", re.IGNORECASE)
_CLIP_WORDS = ("clip", "highlight", "excerpt", "extract", "pull out", "find the part", "cut out the part")
_EXPORT_WORDS = (
    "export", "convert", "compress", "shrink", "smaller file", "save as", "render as",
    "audio only", "just the audio", "extract the audio", "as mp3", "to mp3",
    "as gif", "to gif", "as webm", "to webm", "as a gif", "as an mp3",
)
_ASSEMBLE_WORDS = (
    "attach", "join", "stitch", "combine", "append", "prepend",
    "add intro", "add outro",
)

_QUALITY_WORDS = {
    "source": ("original quality", "full quality", "lossless", "same quality"),
    "high": ("high quality", "1080", "best quality", "hd"),
    "balanced": ("balanced", "720", "medium"),
    "small": ("small", "540", "compress", "smaller"),
    "tiny": ("tiny", "360", "smallest", "email"),
}
_FORMAT_WORDS = {
    "gif": ("gif",),
    "webm": ("webm",),
    "mov": ("mov",),
    "mp3": ("mp3", "audio only", "just the audio", "audio track"),
    "m4a": ("m4a",),
    "wav": ("wav",),
    "mp4": ("mp4",),
}

#: Words that pick an intro style without naming one exactly.
_STYLE_HINTS = {
    "prestige": ("cinematic", "prestige", "film"),
    "momentum": ("energetic", "fast", "punchy", "momentum"),
    "keynote": ("corporate", "keynote", "professional", "clean"),
    "editorial": ("documentary", "editorial"),
    "newsroom": ("news", "broadcast", "newsroom", "lower third"),
    "neon-pulse": ("social", "neon", "tiktok", "reel"),
    "minimal-frame": ("minimal", "simple", "quiet", "restrained"),
    "noir-cut": ("noir", "black and white", "monochrome"),
    "sunset-reel": ("warm", "sunset", "golden"),
    "grid-lab": ("technical", "grid", "engineering"),
}


def _seconds(value: str) -> float:
    total = 0.0
    for part in value.split(":"):
        total = total * 60 + float(part)
    return total


def _scaled(value: str, unit: str) -> float:
    amount = float(value)
    return amount * 60 if unit.lower().startswith("m") else amount


@dataclass(frozen=True, slots=True)
class DirectorPlan:
    intent: Intent
    summary: str
    steps: tuple[str, ...]
    #: The typed request body for the capability this intent maps to.
    params: dict[str, object] = field(default_factory=dict)
    #: Populated only for the ``effects`` intent.
    edit: EditProgram | None = None
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "intent": self.intent,
            "summary": self.summary,
            "steps": list(self.steps),
            "params": self.params,
            "notes": list(self.notes),
            # Kept so existing prompt-edit clients keep working unchanged.
            "operations": list(self.steps),
            "start": self.params.get("trim_start", 0.0),
            "end": self.params.get("trim_end"),
        }


@dataclass(frozen=True, slots=True)
class PromptContext:
    duration_s: float
    media_names: tuple[str, ...] = ()
    output_names: tuple[str, ...] = ()
    style_ids: tuple[str, ...] = ()
    title: str = ""
    source_filename: str = ""
    has_analysis: bool = False


def _detect_intent(lowered: str) -> Intent:
    def any_of(words: tuple[str, ...]) -> bool:
        return any(word in lowered for word in words)

    # A clean cut is checked first: "clean up and remove the intro" names the
    # intro as a category to drop, not as something to build.
    if any_of(_CLEAN_WORDS) or _CLEAN_RE.search(lowered):
        return "clean"
    # Order matters: the more specific product wins over the generic verb that
    # also appears in its description.
    if any_of(_OUTRO_WORDS) and not any_of(_INTRO_WORDS):
        return "outro"
    if any_of(_INTRO_WORDS):
        return "assemble" if any_of(("attach", "add the", "append", "join")) and _looks_like_file(lowered) else "intro"
    if any_of(_EXPORT_WORDS):
        return "export"
    if any_of(_CLIP_WORDS):
        return "clip"
    # Anything else that changes the footage itself is a multi-step edit, which
    # handles trims, speed and looks together rather than one at a time.
    if any_of(_ASSEMBLE_WORDS):
        return "assemble"
    return "effects"


def _looks_like_file(lowered: str) -> bool:
    return bool(re.search(r"\.(mp4|mov|mkv|webm)\b", lowered))


def _match_media(lowered: str, names: tuple[str, ...]) -> str:
    """Pick the file named earliest in the text, so "intro a.mp4 outro b.mp4" splits."""
    found = [(lowered.find(name.lower()), -len(name), name) for name in names]
    hits = sorted(item for item in found if item[0] >= 0)
    return hits[0][2] if hits else ""


def _trim_range(raw: str, lowered: str, duration_s: float) -> tuple[float, float | None]:
    match = _RANGE_RE.search(raw)
    if match:
        return _seconds(match.group("start")), _seconds(match.group("end"))
    start = 0.0
    end: float | None = None
    first = _FIRST_RE.search(lowered)
    if first:
        start = _scaled(first.group("value"), first.group("unit"))
    last = _LAST_RE.search(lowered)
    if last and duration_s:
        end = max(0.0, duration_s - _scaled(last.group("value"), last.group("unit")))
    if not first:
        after = _AFTER_RE.search(raw)
        if after:
            start = _seconds(after.group("start"))
    if end is None and not last:
        before = _BEFORE_RE.search(raw)
        if before:
            end = _seconds(before.group("end"))
    return start, end


def _requested_length(lowered: str, fallback: float, low: float, high: float) -> float:
    match = _LENGTH_RE.search(lowered)
    if not match:
        return fallback
    return max(low, min(high, _scaled(match.group("value"), match.group("unit"))))


def _pick_style(lowered: str, style_ids: tuple[str, ...]) -> str:
    for style_id in style_ids:
        if style_id.replace("-", " ") in lowered or style_id in lowered:
            return style_id
    for style_id, hints in _STYLE_HINTS.items():
        if style_id in style_ids and any(hint in lowered for hint in hints):
            return style_id
    return style_ids[0] if style_ids else "prestige"


def _mentions(lowered: str, phrase: str) -> bool:
    """Whole-word match, so "remove" never counts as a request for "mov"."""
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", lowered) is not None


def _pick_format(lowered: str) -> str:
    for name, words in _FORMAT_WORDS.items():
        if any(_mentions(lowered, word) for word in words):
            return name
    return "mp4"


def _pick_quality(lowered: str) -> str:
    for name, words in _QUALITY_WORDS.items():
        if any(_mentions(lowered, word) for word in words):
            return name
    return "balanced"


def _timecode(value: float) -> str:
    minutes, seconds = divmod(int(value), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:d}:{seconds:02d}"


def plan_prompt(prompt: str, context: PromptContext) -> DirectorPlan:
    """Route one instruction to the capability that can carry it out."""
    raw = prompt.strip()
    if not raw:
        raise ValueError("Describe what you want first.")
    lowered = raw.lower()
    intent = _detect_intent(lowered)

    if intent == "intro":
        return _plan_intro(raw, lowered, context)
    if intent == "outro":
        return _plan_outro(raw, lowered, context)
    if intent == "clean":
        return _plan_clean(lowered, context)
    if intent == "clip":
        return _plan_clip(raw, lowered, context)
    if intent == "export":
        return _plan_export(lowered, context)
    if intent == "assemble":
        return _plan_assemble(raw, lowered, context)
    return _plan_effects(raw, context)


def _plan_intro(raw: str, lowered: str, context: PromptContext) -> DirectorPlan:
    seconds = _requested_length(lowered, 14.0, 5.0, 60.0)
    count = _COUNT_RE.search(lowered)
    shots = max(2, min(12, int(count.group("count")))) if count else 5
    style_id = _pick_style(lowered, context.style_ids)
    quoted = _QUOTED_RE.search(raw)
    title = quoted.group("value").strip() if quoted else context.title
    steps = (
        f"Build a {style_id.replace('-', ' ')} intro",
        f"Run for {seconds:g} seconds",
        f"Feature {shots} moments from the recording",
        f"Title: {title or 'the analysed title'}",
    )
    notes: list[str] = []
    if not context.has_analysis:
        notes.append("Without an analysis the moments are spaced evenly through the video.")
    return DirectorPlan(
        intent="intro",
        summary=f"Create a {seconds:g}s intro in the {style_id.replace('-', ' ')} style.",
        steps=steps,
        params={
            "style_id": style_id,
            "duration_seconds": seconds,
            "shot_count": shots,
            "title": title,
            "output_name": "intro.mp4",
        },
        notes=tuple(notes),
    )


def _plan_outro(raw: str, lowered: str, context: PromptContext) -> DirectorPlan:
    seconds = _requested_length(lowered, 8.0, 5.0, 30.0)
    style_id = _pick_style(lowered, context.style_ids)
    quoted = _QUOTED_RE.search(raw)
    end_card = quoted.group("value").strip() if quoted else "Thanks for watching"
    return DirectorPlan(
        intent="outro",
        summary=f"Create a {seconds:g}s outro ending on “{end_card}”.",
        steps=(
            f"Build a {style_id.replace('-', ' ')} closing sequence",
            f"Run for {seconds:g} seconds",
            f"End card: {end_card}",
        ),
        params={
            "style_id": style_id,
            "duration_seconds": seconds,
            "shot_count": 3,
            "end_card_text": end_card,
            "title": end_card,
            "output_name": "outro.mp4",
        },
    )


def _plan_clean(lowered: str, context: PromptContext) -> DirectorPlan:
    options = {
        "remove_silence": True,
        "remove_filler": True,
        "remove_off_topic": "off topic" in lowered or "off-topic" in lowered or "everything" in lowered,
        "remove_admin": "logistics" in lowered or "admin" in lowered or "everything" in lowered,
        "remove_qa": "q&a" in lowered or "questions" in lowered,
        "remove_intro": "intro" in lowered,
        "remove_outro": "outro" in lowered or "sign off" in lowered,
    }
    if "keep everything" in lowered or "only silence" in lowered or "just the silence" in lowered:
        options.update(
            remove_filler=False, remove_off_topic=False, remove_admin=False,
            remove_qa=False, remove_intro=False, remove_outro=False,
        )
    removing = [key.replace("remove_", "").replace("_", " ") for key, on in options.items() if on]
    if not context.has_analysis:
        raise ValueError("A clean cut needs the analysis. Run it from the Overview tab first.")
    return DirectorPlan(
        intent="clean",
        summary="Render a clean cut of the recording.",
        steps=("Remove: " + ", ".join(removing), "Join every kept span on a word boundary"),
        params={**options, "output_name": "cleaned.mp4"},
    )


def _plan_clip(raw: str, lowered: str, context: PromptContext) -> DirectorPlan:
    start, end = _trim_range(raw, lowered, context.duration_s)
    quoted = _QUOTED_RE.search(raw)
    if start > 0 and end is None:
        end = context.duration_s
    if end is not None and end > start:
        return DirectorPlan(
            intent="clip",
            summary=f"Cut {_timecode(start)}–{_timecode(end)} out of the recording.",
            steps=(f"Take the span {_timecode(start)} to {_timecode(end)}",),
            params={
                "mode": "span",
                "start": start,
                "end": end,
                "title": quoted.group("value").strip() if quoted else "Clip",
                "output_name": "clip.mp4",
            },
        )
    if not context.has_analysis:
        raise ValueError(
            "Searching for a topic needs the analysis. Give an explicit range instead, "
            "for example: clip from 04:10 to 06:00."
        )
    count = _COUNT_RE.search(lowered)
    query = quoted.group("value").strip() if quoted else ""
    highlights = "highlight" in lowered
    return DirectorPlan(
        intent="clip",
        summary=(
            f"Find {count.group('count') if count else 'the best'} highlight(s)."
            if highlights
            else f"Find the parts about “{query}”." if query else "Find clip options."
        ),
        steps=(
            "Search the transcript",
            "Show every match so you can choose before rendering",
        ),
        params={
            "mode": "highlight" if highlights else ("topic" if query else "duration"),
            "query": query,
            "count": int(count.group("count")) if count else 5,
            "target_seconds": _requested_length(lowered, 90.0, 5.0, 3600.0),
        },
        notes=("Matches are listed for you to pick from; nothing renders automatically.",),
    )


def _plan_export(lowered: str, context: PromptContext) -> DirectorPlan:
    export_format = _pick_format(lowered)
    quality = _pick_quality(lowered)
    named = _match_media(lowered, context.output_names)
    source = named or (context.output_names[0] if context.output_names else "")
    source_kind = "output" if source else "source"
    if not source:
        source = context.source_filename or "the original recording"
    return DirectorPlan(
        intent="export",
        summary=f"Export {source} as {export_format.upper()} at {quality} quality.",
        steps=(f"Re-encode {source}", f"Container: {export_format}", f"Quality: {quality}"),
        params={
            "source": source,
            "source_kind": source_kind,
            "format": export_format,
            "quality": quality,
        },
    )


def _plan_assemble(raw: str, lowered: str, context: PromptContext) -> DirectorPlan:
    header = ""
    footer = ""
    for keyword, slot in (("intro", "header"), ("outro", "footer")):
        if keyword not in lowered:
            continue
        # Look only at the words after the keyword so "intro a.mp4 outro b.mp4"
        # does not assign the same file to both slots.
        tail = lowered.split(keyword, 1)[1]
        name = _match_media(tail, context.media_names)
        if not name:
            raise ValueError(
                f"Name the {keyword} file exactly. Media in this project: "
                + (", ".join(context.media_names) or "none imported yet")
            )
        if slot == "header":
            header = name
        else:
            footer = name

    start, end = _trim_range(raw, lowered, context.duration_s)
    body = _match_media(lowered, context.output_names)
    if not header and not footer and not (start or end is not None):
        raise ValueError(
            "Say what to attach or what to trim, for example: attach intro opener.mp4, "
            "or trim the first 30 seconds."
        )

    steps: list[str] = []
    if start or end is not None:
        steps.append(
            f"Keep {_timecode(start)} to {_timecode(end) if end is not None else 'the end'}"
        )
    if header:
        steps.append(f"Add {header} to the front")
    if footer:
        steps.append(f"Add {footer} to the end")
    steps.append("Normalize every part onto one canvas before joining")
    return DirectorPlan(
        intent="assemble",
        summary="Assemble a final cut.",
        steps=tuple(steps),
        params={
            "body_filename": body,
            "header_asset": header or None,
            "footer_asset": footer or None,
            "intro_transition": "fade" if header else "cut",
            "outro_transition": "fade" if footer else "cut",
            "trim_start": start,
            "trim_end": end,
            "output_name": "final.mp4",
        },
    )


def _plan_effects(raw: str, context: PromptContext) -> DirectorPlan:
    program = parse_edit_program(raw, context.duration_s, context.media_names)
    trim = program.trim
    return DirectorPlan(
        intent="effects",
        summary=(
            f"Edit the recording in {len(program.ops)} step(s) — "
            f"result runs {_timecode(program.output_duration)}."
        ),
        steps=tuple(program.describe()),
        params={
            "trim_start": trim.start if trim else 0.0,
            "trim_end": trim.end if trim else None,
            "output_name": "edited.mp4",
        },
        edit=program,
    )
