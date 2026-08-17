"""A written instruction compiled into an ordered program of editing operations.

The earlier edit model held one flat set of options, so it could express "make
it sepia" but not "trim to the first ten seconds, speed it up and fade out" —
the three would fight over a single time range. This module treats an edit as a
*sequence*: each clause of the prompt becomes one operation, operations carry
their own time range, and the whole program is compiled into one ffmpeg pass in
a fixed order that makes editorial sense.

Every operation is an enumerated kind with numeric parameters. Nothing the user
writes becomes a filter name, an argument or a shell string, so a prompt can
never widen what the application is able to do.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from clipdesk.actions.render import encode_args
from clipdesk.config import RenderConfig
from clipdesk.media.ffmpeg import filter_complex_args, run_with_progress

OpKind = Literal[
    "trim", "speed", "aspect", "scale", "rotate", "flip",
    "grade", "blur", "sharpen", "vignette", "denoise", "stabilise-look",
    "text", "fade-in", "fade-out", "volume", "mute", "audio-fade",
]

#: Colour treatments, mapped to fixed filter chains.
GRADES: dict[str, tuple[str, ...]] = {
    "grayscale": ("hue=s=0",),
    "sepia": ("colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131",),
    "vivid": ("eq=contrast=1.10:saturation=1.30",),
    "muted": ("eq=saturation=0.65",),
    "warm": ("colorbalance=rs=.10:gs=.02:bs=-.08",),
    "cool": ("colorbalance=rs=-.07:bs=.10",),
    "bright": ("eq=brightness=0.08",),
    "dark": ("eq=brightness=-0.10",),
    "contrast": ("eq=contrast=1.20",),
    "flat": ("eq=contrast=0.85:saturation=0.85",),
    "noir": ("hue=s=0", "eq=contrast=1.35"),
    "cinematic": ("eq=contrast=1.08:saturation=0.90", "unsharp=5:5:0.30"),
}

#: Social and broadcast shapes, as width:height.
ASPECTS: dict[str, tuple[int, int]] = {
    "vertical": (9, 16),
    "square": (1, 1),
    "widescreen": (16, 9),
    "classic": (4, 3),
    "cinema": (21, 9),
}

_POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right", "center")

_WORD_NUMBERS = {
    "half": 0.5, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "ninety": 90,
}


@dataclass(frozen=True, slots=True)
class EditOp:
    kind: OpKind
    #: When the operation applies. ``None`` end means "to the end".
    start: float = 0.0
    end: float | None = None
    value: float = 0.0
    name: str = ""
    text: str = ""
    position: str = "bottom-right"
    font_size: int = 42

    def describe(self) -> str:
        window = ""
        if self.kind not in ("trim", "speed", "fade-in", "fade-out", "audio-fade"):
            if self.start or self.end is not None:
                window = f" from {_clock(self.start)} to {_clock(self.end) if self.end is not None else 'the end'}"
        return {
            "trim": lambda: f"Keep {_clock(self.start)} to {_clock(self.end) if self.end is not None else 'the end'}",
            "speed": lambda: f"Play at {self.value:g}× speed",
            "aspect": lambda: f"Reframe to {self.name.replace('-', ' ')} ({ASPECTS[self.name][0]}:{ASPECTS[self.name][1]})",
            "scale": lambda: f"Resize to {int(self.value)}p",
            "rotate": lambda: f"Rotate {int(self.value)}°",
            "flip": lambda: f"Flip {self.name}",
            "grade": lambda: f"Colour: {self.name}{window}",
            "blur": lambda: f"Soft blur{window}",
            "sharpen": lambda: f"Sharpen{window}",
            "vignette": lambda: f"Vignette{window}",
            "denoise": lambda: f"Reduce noise{window}",
            "stabilise-look": lambda: f"Steady the framing{window}",
            "text": lambda: f'Text "{self.text}" {self.position.replace("-", " ")}{window}',
            "fade-in": lambda: f"Fade in over {self.value:g}s",
            "fade-out": lambda: f"Fade out over {self.value:g}s",
            "volume": lambda: f"Audio volume {self.value * 100:.0f}%",
            "mute": lambda: "Remove audio",
            "audio-fade": lambda: f"Fade audio over {self.value:g}s",
        }[self.kind]()


@dataclass(frozen=True, slots=True)
class EditProgram:
    ops: tuple[EditOp, ...] = ()
    source_duration: float = 0.0

    @property
    def trim(self) -> EditOp | None:
        return next((op for op in self.ops if op.kind == "trim"), None)

    @property
    def output_duration(self) -> float:
        """How long the result runs, after any trim and speed change."""
        trim = self.trim
        start = trim.start if trim else 0.0
        end = (trim.end if trim and trim.end is not None else self.source_duration)
        span = max(0.1, min(end, self.source_duration) - start)
        for op in self.ops:
            if op.kind == "speed" and op.value > 0:
                span /= op.value
        return span

    def describe(self) -> list[str]:
        return [op.describe() for op in self.ops]

    def keeping(self, indices: list[int] | None) -> EditProgram:
        """The same program with only the chosen steps, by their listed order."""
        if indices is None:
            return self
        wanted = {index for index in indices if 0 <= index < len(self.ops)}
        if not wanted:
            raise ValueError("Choose at least one step to run.")
        return EditProgram(
            ops=tuple(op for index, op in enumerate(self.ops) if index in wanted),
            source_duration=self.source_duration,
        )


def _clock(value: float) -> str:
    minutes, seconds = divmod(int(value), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def _seconds(value: str) -> float:
    total = 0.0
    for part in value.split(":"):
        total = total * 60 + float(part)
    return total


_TIME = r"\d{1,2}(?::\d{1,2}){0,2}(?:\.\d+)?"
_RANGE_RE = re.compile(
    rf"(?:from|between)\s+(?P<start>{_TIME})\s*(?:to|and|until|-|–)\s*(?P<end>{_TIME})",
    re.IGNORECASE,
)
_QUOTED_RE = re.compile(r"[\"“'](?P<value>[^\"”']{1,240})[\"”']")
_NUMBER = r"(?P<value>\d+(?:\.\d+)?|" + "|".join(_WORD_NUMBERS) + r")"
_UNIT = r"(?P<unit>seconds?|secs?|s|minutes?|mins?|m)\b"
_DURATION_RE = re.compile(rf"{_NUMBER}\s*(?:-|\s)?\s*{_UNIT}", re.IGNORECASE)
_SPEED_RE = re.compile(
    r"(?:(?P<factor>\d+(?:\.\d+)?)\s*(?:x|times)|(?:speed|fast|slow)\w*\s+(?:it\s+)?"
    r"(?:up|down)?\s*(?:by\s*)?(?P<factor2>\d+(?:\.\d+)?)?)",
    re.IGNORECASE,
)
_SCALE_RE = re.compile(r"\b(?P<height>2160|1440|1080|720|540|480|360)\s*p?\b", re.IGNORECASE)


def _amount(match: re.Match[str]) -> float:
    raw = match.group("value").lower()
    number = _WORD_NUMBERS.get(raw)
    if number is None:
        number = float(raw)
    unit = match.group("unit").lower()
    return float(number) * (60 if unit.startswith("m") else 1)


def _split_clauses(text: str) -> list[str]:
    """Break an instruction into the separate things it is asking for.

    Quoted overlay text and fixed phrases are protected first: both routinely
    contain the very words used to join clauses, and "1.5x" must not be split
    down the middle by a sentence full stop.
    """
    holds: list[str] = []

    def hold(match: re.Match[str]) -> str:
        holds.append(match.group(0))
        return f"\x00{len(holds) - 1}\x00"

    guarded = _QUOTED_RE.sub(hold, text)
    guarded = re.sub(r"\bblack and white\b", hold, guarded, flags=re.IGNORECASE)
    parts = re.split(
        r"\s*(?:,|;|(?<!\d)\.(?!\d)|\band then\b|\bthen\b|\band also\b|\band\b)\s*",
        guarded,
    )

    def restore(value: str) -> str:
        return re.sub(r"\x00(\d+)\x00", lambda m: holds[int(m.group(1))], value)

    return [restore(part).strip() for part in parts if restore(part).strip()]


def _window(clause: str, duration: float) -> tuple[float, float | None]:
    match = _RANGE_RE.search(clause)
    if match:
        return _seconds(match.group("start")), _seconds(match.group("end"))
    lowered = clause.lower()
    amount = _DURATION_RE.search(lowered)
    if amount and re.search(r"\b(first|opening|beginning|start)\b", lowered):
        return 0.0, _amount(amount)
    if amount and re.search(r"\b(last|final|ending|end)\b", lowered):
        return max(0.0, duration - _amount(amount)), None
    after = re.search(rf"\bafter\s+(?P<start>{_TIME})\b", clause, re.IGNORECASE)
    if after:
        return _seconds(after.group("start")), None
    before = re.search(rf"\b(?:before|until|up to)\s+(?P<end>{_TIME})\b", clause, re.IGNORECASE)
    if before:
        return 0.0, _seconds(before.group("end"))
    return 0.0, None


def _trim_from(clause: str, duration: float) -> EditOp | None:
    """Read any request that changes which part of the recording survives."""
    lowered = clause.lower()
    keeping = re.search(
        r"\b(trim|cut|clip|crop|shorten|keep|make|create|give|edit|take|use|first|last)\b", lowered
    )
    if not keeping:
        return None

    match = _RANGE_RE.search(clause)
    if match:
        return EditOp("trim", start=_seconds(match.group("start")), end=_seconds(match.group("end")))

    removing = re.search(r"\b(trim|cut|drop|remove|skip|lose)\b", lowered)
    # "trim to the first 30 seconds" keeps that much; "trim the first 30" drops it.
    if re.search(r"\b(trim|cut|shorten|clip|crop)\b\s+(?:it\s+)?(?:down\s+)?to\b", lowered):
        removing = None
    amount = _DURATION_RE.search(lowered)
    if amount:
        seconds = _amount(amount)
        if re.search(r"\b(first|opening|beginning|start)\b", lowered):
            # "cut the first 30s" drops it; "keep the first 30s" keeps it.
            return (
                EditOp("trim", start=seconds)
                if removing and not re.search(r"\bkeep\b", lowered)
                else EditOp("trim", start=0.0, end=seconds)
            )
        if re.search(r"\b(last|final|ending)\b", lowered):
            return (
                EditOp("trim", start=0.0, end=max(0.1, duration - seconds))
                if removing and not re.search(r"\bkeep\b", lowered)
                else EditOp("trim", start=max(0.0, duration - seconds))
            )
        # A bare length — "trim to 20 seconds", "a five second edit" — takes that
        # much from wherever the instruction points, defaulting to the start.
        if re.search(r"\bmiddle\b", lowered):
            start = max(0.0, (duration - seconds) / 2)
        elif re.search(r"\b(end|ending)\b", lowered):
            start = max(0.0, duration - seconds)
        else:
            start = 0.0
        return EditOp("trim", start=start, end=min(duration, start + seconds))
    return None


def _speed_from(clause: str) -> EditOp | None:
    lowered = clause.lower()
    if not re.search(r"\b(speed|faster|slower|slow|fast|double|half)\b|\b\d+(?:\.\d+)?x\b", lowered):
        return None
    factor = 0.0
    times = re.search(r"(?P<factor>\d+(?:\.\d+)?)\s*(?:x|times)", lowered)
    if times:
        factor = float(times.group("factor"))
    elif "double" in lowered:
        factor = 2.0
    elif "half" in lowered:
        factor = 0.5
    else:
        percent = re.search(r"(?P<percent>\d{2,3})\s*%", lowered)
        if percent:
            factor = max(0.25, min(4.0, int(percent.group("percent")) / 100))
    if not factor:
        factor = 1.5 if re.search(r"\b(speed up|faster|fast)\b", lowered) else 0.75
    if re.search(r"\b(slow|slower)\b", lowered) and factor > 1:
        factor = 1 / factor
    return EditOp("speed", value=max(0.25, min(4.0, factor)))


def _grade_name(lowered: str) -> str:
    for name in ("grayscale", "greyscale", "black and white", "monochrome"):
        if name in lowered:
            return "grayscale"
    for name in ("noir", "sepia", "vivid", "muted", "warm", "cool", "cinematic", "flat"):
        if name in lowered:
            return name
    if re.search(r"\b(brighten|brighter|bright)\b", lowered):
        return "bright"
    if re.search(r"\b(darken|darker|dark)\b", lowered):
        return "dark"
    if "saturat" in lowered:
        return "muted" if re.search(r"\b(de|less|reduce|lower)", lowered) else "vivid"
    if "contrast" in lowered:
        return "flat" if re.search(r"\b(less|reduce|lower)\b", lowered) else "contrast"
    return ""


def parse_edit_program(
    prompt: str, duration_s: float, media_names: tuple[str, ...] = ()
) -> EditProgram:
    """Compile an instruction into an ordered list of editing operations."""
    raw = prompt.strip()
    if not raw:
        raise ValueError("Describe the edit first.")

    ops: list[EditOp] = []
    seen: set[str] = set()

    def add(op: EditOp | None, *, once: bool = True) -> None:
        if op is None:
            return
        if once:
            if op.kind in seen:
                return
            seen.add(op.kind)
        ops.append(op)

    for clause in _split_clauses(raw):
        lowered = clause.lower()
        start, end = _window(clause, duration_s)
        before = len(ops)

        quoted = _QUOTED_RE.search(clause)
        if quoted and re.search(r"\b(text|caption|title|overlay|label|watermark)\b", lowered):
            position = next(
                (item for item in _POSITIONS if item.replace("-", " ") in lowered or item in lowered),
                "bottom-right",
            )
            size = re.search(r"(?:font|text)\s+size\s+(\d{1,3})", lowered)
            add(
                EditOp(
                    "text",
                    start=start,
                    end=end,
                    text=quoted.group("value").strip(),
                    position=position,
                    font_size=max(12, min(160, int(size.group(1)) if size else 42)),
                ),
                once=False,
            )
            continue

        add(_speed_from(clause))

        for name, ratio in ASPECTS.items():
            if name in lowered or f"{ratio[0]}:{ratio[1]}" in lowered:
                add(EditOp("aspect", name=name))
                break
        if re.search(r"\b(portrait|reels?|tiktok|shorts?|stor(y|ies))\b", lowered):
            add(EditOp("aspect", name="vertical"))

        scale = _SCALE_RE.search(lowered)
        if scale and re.search(r"\b(resize|scale|downscale|make it|export|render)\b", lowered):
            add(EditOp("scale", value=float(scale.group("height"))))

        rotate = re.search(r"rotate(?:\s+by)?\s+(90|180|270)", lowered)
        if rotate:
            add(EditOp("rotate", value=float(rotate.group(1))))
        if re.search(r"\b(mirror|flip horizontal(?:ly)?)\b", lowered):
            add(EditOp("flip", name="horizontal"))
        elif re.search(r"\bflip vertical(?:ly)?\b", lowered):
            add(EditOp("flip", name="vertical"))

        grade = _grade_name(lowered)
        if grade:
            add(EditOp("grade", start=start, end=end, name=grade), once=False)
        if re.search(r"\bblur\b", lowered) and "motion" not in lowered:
            add(EditOp("blur", start=start, end=end))
        if re.search(r"\bsharpen\b|\bcrisp\b", lowered):
            add(EditOp("sharpen", start=start, end=end))
        if "vignette" in lowered:
            add(EditOp("vignette", start=start, end=end))
        if re.search(r"\b(denoise|noise reduction|clean up the picture|grain)\b", lowered):
            add(EditOp("denoise", start=start, end=end))

        seconds = _DURATION_RE.search(lowered)
        length = _amount(seconds) if seconds else 1.0
        if re.search(r"\bfade\s*(?:in|from black)\b", lowered):
            add(EditOp("fade-in", value=max(0.1, min(10.0, length))))
        if re.search(r"\bfade\s*(?:out|to black)\b", lowered):
            add(EditOp("fade-out", value=max(0.1, min(10.0, length))))

        if re.search(r"\b(mute|remove the audio|no audio|silent)\b", lowered):
            add(EditOp("mute"))
        volume = re.search(r"volume(?:\s+to)?\s+(\d{1,3})\s*%", lowered)
        if volume:
            add(EditOp("volume", value=max(0.0, min(2.0, int(volume.group(1)) / 100))))
        elif re.search(r"\b(louder|turn it up)\b", lowered):
            add(EditOp("volume", value=1.4))
        elif re.search(r"\b(quieter|turn it down)\b", lowered):
            add(EditOp("volume", value=0.6))

        # "make the first 10 seconds black and white" scopes that effect to a
        # window; only a clause asking for nothing else is really a trim.
        if len(ops) == before:
            add(_trim_from(clause, duration_s))

    if not ops:
        raise ValueError(
            "That edit is not supported yet. You can trim to a length or a range, "
            "change speed, reframe to vertical or square, resize, rotate or mirror, "
            "apply a colour look, blur, sharpen, vignette, add quoted text, fade in "
            "or out, and set the audio level."
        )
    return EditProgram(ops=tuple(ops), source_duration=duration_s)


# --- rendering ---------------------------------------------------------------
#: Operations are applied in this order regardless of the order they were typed,
#: because an edit only makes sense one way round: choose the footage, set its
#: pace, then its shape, then its look, then what sits on top, then the fades.
_ORDER = {
    "trim": 0, "speed": 1, "aspect": 2, "scale": 3, "rotate": 4, "flip": 5,
    "grade": 6, "denoise": 7, "blur": 8, "sharpen": 9, "vignette": 10,
    "stabilise-look": 11, "text": 12, "fade-in": 13, "fade-out": 14,
    "volume": 15, "mute": 16, "audio-fade": 17,
}

_POSITION_EXPR = {
    "top-left": ("36", "36"),
    "top-right": ("w-text_w-36", "36"),
    "bottom-left": ("36", "h-text_h-36"),
    "bottom-right": ("w-text_w-36", "h-text_h-36"),
    "center": ("(w-text_w)/2", "(h-text_h)/2"),
}


def _enable(op: EditOp, duration: float) -> str:
    if not op.start and op.end is None:
        return ""
    end = op.end if op.end is not None else duration
    return f":enable='between(t\\,{op.start:.3f}\\,{end:.3f})'"


def render_program(
    source: Path,
    destination: Path,
    program: EditProgram,
    *,
    has_audio: bool,
    render: RenderConfig,
    ffmpeg_bin: str,
    on_progress=None,
) -> Path:
    """Apply the whole program in one pass."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    ops = sorted(program.ops, key=lambda op: _ORDER[op.kind])
    duration = program.output_duration

    video: list[str] = []
    audio: list[str] = []
    text_files: list[Path] = []
    muted = any(op.kind == "mute" for op in ops)
    speed = next((op.value for op in ops if op.kind == "speed"), 1.0)

    trim = program.trim
    if trim is not None:
        stop = "" if trim.end is None else f":end={trim.end:.3f}"
        video.append(f"trim=start={trim.start:.3f}{stop},setpts=PTS-STARTPTS")
        audio.append(f"atrim=start={trim.start:.3f}{stop},asetpts=PTS-STARTPTS")

    for op in ops:
        if op.kind == "speed":
            video.append(f"setpts={1 / op.value:.4f}*PTS")
            # atempo only accepts 0.5–2.0, so a bigger change is chained.
            remaining = op.value
            while remaining > 2.0:
                audio.append("atempo=2.0")
                remaining /= 2.0
            while remaining < 0.5:
                audio.append("atempo=0.5")
                remaining /= 0.5
            audio.append(f"atempo={remaining:.4f}")
        elif op.kind == "aspect":
            width, height = ASPECTS[op.name]
            video.append(
                f"crop='min(iw,ih*{width}/{height})':'min(ih,iw*{height}/{width})',"
                f"scale=trunc(iw/2)*2:trunc(ih/2)*2"
            )
        elif op.kind == "scale":
            video.append(f"scale=-2:'min({int(op.value)},ih)':force_divisible_by=2")
        elif op.kind == "rotate":
            if op.value == 90:
                video.append("transpose=1")
            elif op.value == 270:
                video.append("transpose=2")
            else:
                video.extend(("hflip", "vflip"))
        elif op.kind == "flip":
            video.append("hflip" if op.name == "horizontal" else "vflip")
        elif op.kind == "grade":
            window = _enable(op, duration)
            video.extend(f"{item}{window}" for item in GRADES[op.name])
        elif op.kind == "blur":
            video.append(f"boxblur=6:1{_enable(op, duration)}")
        elif op.kind == "sharpen":
            video.append(f"unsharp=5:5:0.8{_enable(op, duration)}")
        elif op.kind == "vignette":
            video.append(f"vignette=PI/5{_enable(op, duration)}")
        elif op.kind == "denoise":
            video.append(f"hqdn3d=3:3:6:6{_enable(op, duration)}")
        elif op.kind == "text":
            path = destination.parent / f".{destination.stem}-text{len(text_files)}.txt"
            path.write_text(op.text, encoding="utf-8", newline="\n")
            text_files.append(path)
            escaped = str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
            x, y = _POSITION_EXPR[op.position]
            video.append(
                f"drawtext=textfile='{escaped}':expansion=none:x={x}:y={y}:"
                f"fontsize={op.font_size}:fontcolor=white:borderw=2:bordercolor=black@0.8:"
                f"box=1:boxcolor=black@0.35:boxborderw=10{_enable(op, duration)}"
            )
        elif op.kind == "fade-in":
            video.append(f"fade=t=in:st=0:d={op.value:.3f}")
            audio.append(f"afade=t=in:st=0:d={op.value:.3f}")
        elif op.kind == "fade-out":
            begin = max(0.0, duration - op.value)
            video.append(f"fade=t=out:st={begin:.3f}:d={op.value:.3f}")
            audio.append(f"afade=t=out:st={begin:.3f}:d={op.value:.3f}")
        elif op.kind == "volume":
            audio.append(f"volume={op.value:.3f}")

    keep_audio = has_audio and not muted
    video.append("format=yuv420p")
    parts = ["[0:v]" + ",".join(video) + "[outv]"]
    if keep_audio:
        audio.append("aresample=48000")
        parts.append("[0:a]" + ",".join(audio) + "[outa]")

    graph = ";".join(parts)
    scratch = destination.parent / f".{destination.stem}-program.filter.txt"
    fc_args, script = filter_complex_args(graph, scratch, ffmpeg_bin)
    args = ["-i", str(source), *fc_args, "-map", "[outv]"]
    if keep_audio:
        args += ["-map", "[outa]"]
    args += encode_args(render, has_audio=keep_audio)
    args += ["-movflags", "+faststart", "-y", str(destination)]

    try:
        run_with_progress(
            ffmpeg_bin,
            args,
            on_elapsed=(lambda elapsed: on_progress(min(1.0, elapsed / duration)))
            if on_progress and duration
            else None,
        )
    finally:
        if script is not None:
            script.unlink(missing_ok=True)
        for path in text_files:
            path.unlink(missing_ok=True)
    return destination
