"""Lightweight deterministic edits driven by a deliberately small prompt grammar."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from clipdesk.actions.render import encode_args
from clipdesk.config import RenderConfig
from clipdesk.media.ffmpeg import filter_complex_args, run_with_progress

_TIME_RE = re.compile(
    r"(?:from|between)\s+(?P<start>\d+(?::\d{1,2})?(?::\d{1,2})?)\s+"
    r"(?:to|and|until|-)\s+(?P<end>\d+(?::\d{1,2})?(?::\d{1,2})?)",
    re.IGNORECASE,
)
_QUOTED_RE = re.compile(r"(?:text|caption|title)\s+[\"'](?P<text>.+?)[\"']", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class EditPlan:
    text: str = ""
    position: str = "bottom-right"
    font_size: int = 42
    start: float = 0.0
    end: float | None = None
    grayscale: bool = False
    blur: bool = False
    sepia: bool = False
    sharpen: bool = False
    vignette: bool = False
    brightness: float = 0.0
    contrast: float = 1.0
    saturation: float = 1.0
    flip: str = ""
    rotate: int = 0
    mute: bool = False
    volume: float = 1.0
    fade_in: float = 0.0
    fade_out: float = 0.0
    intro_asset: str = ""
    outro_asset: str = ""

    def to_dict(self) -> dict[str, object]:
        operations: list[str] = []
        if self.text:
            operations.append(f"Text overlay: {self.text}")
        if self.grayscale:
            operations.append("Grayscale")
        if self.blur:
            operations.append("Soft blur")
        if self.sepia:
            operations.append("Sepia tone")
        if self.sharpen:
            operations.append("Sharpen")
        if self.vignette:
            operations.append("Vignette")
        if self.brightness:
            operations.append(f"Brightness: {self.brightness:+.2f}")
        if self.contrast != 1.0:
            operations.append(f"Contrast: {self.contrast:.2f}x")
        if self.saturation != 1.0:
            operations.append(f"Saturation: {self.saturation:.2f}x")
        if self.flip:
            operations.append(f"Flip: {self.flip}")
        if self.rotate:
            operations.append(f"Rotate: {self.rotate} degrees")
        if self.mute:
            operations.append("Remove audio")
        elif self.volume != 1.0:
            operations.append(f"Volume: {self.volume * 100:.0f}%")
        if self.fade_in:
            operations.append(f"Fade in: {self.fade_in:g}s")
        if self.fade_out:
            operations.append(f"Fade out: {self.fade_out:g}s")
        if self.intro_asset:
            operations.append(f"Add intro: {self.intro_asset}")
        if self.outro_asset:
            operations.append(f"Add outro: {self.outro_asset}")
        return {
            "operations": operations,
            "position": self.position,
            "start": self.start,
            "end": self.end,
        }


def _seconds(value: str) -> float:
    parts = [float(part) for part in value.split(":")]
    total = 0.0
    for part in parts:
        total = total * 60 + part
    return total


def parse_edit_prompt(
    prompt: str, duration_s: float, available_assets: tuple[str, ...] = ()
) -> EditPlan:
    """Translate safe, familiar wording into a bounded edit plan."""
    raw = prompt.strip()
    lowered = raw.lower()
    if not raw:
        raise ValueError("Describe the edit first.")

    match = _TIME_RE.search(raw)
    start = _seconds(match.group("start")) if match else 0.0
    end = _seconds(match.group("end")) if match else None
    if end is not None and end <= start:
        raise ValueError("The edit time range must end after it starts.")
    if start >= duration_s or (end is not None and end > duration_s + 0.5):
        raise ValueError("The edit time range falls outside this video.")

    text_match = _QUOTED_RE.search(raw)
    text = text_match.group("text").strip() if text_match else ""
    if any(word in lowered for word in ("text", "caption", "title")) and not text:
        raise ValueError('Put overlay text in quotes, for example: text "Confidential" bottom right.')
    if len(text) > 240:
        raise ValueError("Overlay text must be 240 characters or fewer.")

    def mentioned_asset(kind: str) -> str:
        if kind not in lowered:
            return ""
        matches = [name for name in available_assets if name.lower() in lowered]
        if not matches:
            available = ", ".join(available_assets) or "none imported"
            raise ValueError(
                f"Name an imported {kind} video exactly. Available media: {available}."
            )
        return max(matches, key=len)

    intro_asset = mentioned_asset("intro")
    outro_asset = mentioned_asset("outro")

    position = "bottom-right"
    for candidate in ("top-left", "top-right", "bottom-left", "bottom-right", "center"):
        if candidate.replace("-", " ") in lowered or candidate in lowered:
            position = candidate
            break

    size_match = re.search(r"(?:font|text)\s+size\s+(\d{1,3})", lowered)
    font_size = int(size_match.group(1)) if size_match else 42
    font_size = max(16, min(120, font_size))

    fade_in = 1.0 if "fade in" in lowered else 0.0
    fade_out = 1.0 if "fade out" in lowered else 0.0
    fade_match = re.search(r"fade(?: in| out)?(?: for| over)?\s+(\d+(?:\.\d+)?)\s*s", lowered)
    if fade_match:
        value = max(0.1, min(10.0, float(fade_match.group(1))))
        if "fade in" in lowered:
            fade_in = value
        if "fade out" in lowered:
            fade_out = value

    brightness = 0.0
    if any(phrase in lowered for phrase in ("brighten", "brighter", "increase brightness")):
        brightness = 0.08
    elif any(phrase in lowered for phrase in ("darken", "darker", "reduce brightness")):
        brightness = -0.08
    brightness_match = re.search(r"brightness\s+(-?\d+(?:\.\d+)?)", lowered)
    if brightness_match:
        brightness = max(-0.4, min(0.4, float(brightness_match.group(1))))

    contrast = 1.0
    if "increase contrast" in lowered or "high contrast" in lowered:
        contrast = 1.15
    elif "reduce contrast" in lowered or "low contrast" in lowered:
        contrast = 0.85
    contrast_match = re.search(r"contrast\s+(\d+(?:\.\d+)?)", lowered)
    if contrast_match:
        contrast = max(0.5, min(2.0, float(contrast_match.group(1))))

    saturation = 1.0
    if "more saturated" in lowered or "increase saturation" in lowered:
        saturation = 1.2
    elif "desaturate" in lowered or "reduce saturation" in lowered:
        saturation = 0.7
    saturation_match = re.search(r"saturation\s+(\d+(?:\.\d+)?)", lowered)
    if saturation_match:
        saturation = max(0.0, min(2.0, float(saturation_match.group(1))))

    flip = ""
    if "flip horizontal" in lowered or "mirror" in lowered:
        flip = "horizontal"
    elif "flip vertical" in lowered:
        flip = "vertical"

    rotate = 0
    rotate_match = re.search(r"rotate(?: by)?\s+(90|180|270)(?:\s*degrees?)?", lowered)
    if rotate_match:
        rotate = int(rotate_match.group(1))

    mute = any(phrase in lowered for phrase in ("mute audio", "remove audio", "no audio"))
    volume = 1.0
    volume_match = re.search(r"(?:audio )?volume(?: to)?\s+(\d{1,3})\s*%", lowered)
    if volume_match:
        volume = max(0.0, min(2.0, int(volume_match.group(1)) / 100))
        mute = volume == 0

    plan = EditPlan(
        text=text,
        position=position,
        font_size=font_size,
        start=start,
        end=end,
        grayscale=any(word in lowered for word in ("grayscale", "greyscale", "black and white")),
        blur="blur" in lowered,
        sepia="sepia" in lowered,
        sharpen="sharpen" in lowered,
        vignette="vignette" in lowered,
        brightness=brightness,
        contrast=contrast,
        saturation=saturation,
        flip=flip,
        rotate=rotate,
        mute=mute,
        volume=volume,
        fade_in=fade_in,
        fade_out=fade_out,
        intro_asset=intro_asset,
        outro_asset=outro_asset,
    )
    visual_operations = bool(
        plan.text or plan.grayscale or plan.blur or plan.sepia or plan.sharpen
        or plan.vignette or plan.brightness or plan.contrast != 1.0
        or plan.saturation != 1.0 or plan.flip or plan.rotate
        or plan.mute or plan.volume != 1.0 or plan.fade_in or plan.fade_out
    )
    if visual_operations and (intro_asset or outro_asset):
        raise ValueError(
            "Apply imported intro/outro media as one edit, then run visual effects as a "
            "separate prompt so each result can be reviewed."
        )
    if not plan.to_dict()["operations"]:
        raise ValueError(
            "That edit is not supported yet. Try a quoted text overlay, grayscale, "
            "soft blur, color adjustments, rotate/flip, mute/volume, fades, and an "
            "optional 'from 00:10 to 00:20' range."
        )
    return plan


def _position(position: str) -> tuple[str, str]:
    margin = "36"
    return {
        "top-left": (margin, margin),
        "top-right": (f"w-text_w-{margin}", margin),
        "bottom-left": (margin, f"h-text_h-{margin}"),
        "bottom-right": (f"w-text_w-{margin}", f"h-text_h-{margin}"),
        "center": ("(w-text_w)/2", "(h-text_h)/2"),
    }[position]


def render_edit(
    source: Path,
    destination: Path,
    plan: EditPlan,
    *,
    duration_s: float,
    has_audio: bool,
    render: RenderConfig,
    ffmpeg_bin: str,
    on_progress=None,
) -> Path:
    """Apply a validated edit plan in one ffmpeg pass."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    filters: list[str] = []
    if plan.grayscale:
        filters.append("hue=s=0")
    if plan.blur:
        filters.append("boxblur=5:1")
    if plan.sepia:
        filters.append(
            "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131"
        )
    if plan.sharpen:
        filters.append("unsharp=5:5:0.8:5:5:0")
    if plan.vignette:
        filters.append("vignette=PI/5")
    if plan.brightness or plan.contrast != 1.0 or plan.saturation != 1.0:
        filters.append(
            f"eq=brightness={plan.brightness:.3f}:contrast={plan.contrast:.3f}:"
            f"saturation={plan.saturation:.3f}"
        )
    if plan.flip == "horizontal":
        filters.append("hflip")
    elif plan.flip == "vertical":
        filters.append("vflip")
    if plan.rotate == 90:
        filters.append("transpose=1")
    elif plan.rotate == 180:
        filters.extend(("hflip", "vflip"))
    elif plan.rotate == 270:
        filters.append("transpose=2")
    if plan.fade_in:
        filters.append(f"fade=t=in:st={plan.start:.3f}:d={plan.fade_in:.3f}")
    if plan.fade_out:
        fade_end = plan.end if plan.end is not None else duration_s
        filters.append(
            f"fade=t=out:st={max(plan.start, fade_end - plan.fade_out):.3f}:d={plan.fade_out:.3f}"
        )

    text_file: Path | None = None
    if plan.text:
        text_file = destination.parent / f".{destination.stem}-overlay.txt"
        text_file.write_text(plan.text, encoding="utf-8")
        x, y = _position(plan.position)
        enable_end = plan.end if plan.end is not None else duration_s
        escaped_path = str(text_file).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        filters.append(
            "drawtext="
            f"textfile='{escaped_path}':x={x}:y={y}:fontsize={plan.font_size}:"
            "fontcolor=white:borderw=2:bordercolor=black@0.8:box=1:boxcolor=black@0.35:boxborderw=10:"
            f"enable='between(t,{plan.start:.3f},{enable_end:.3f})'"
        )

    graph = "[0:v]" + ",".join(filters) + "[outv]"
    scratch = destination.parent / f".{destination.stem}-edit.filter.txt"
    fc_args, script = filter_complex_args(graph, scratch, ffmpeg_bin)
    args = ["-i", str(source), *fc_args, "-map", "[outv]"]
    if has_audio and not plan.mute:
        args += ["-map", "0:a?"]
        if plan.volume != 1.0:
            args += ["-af", f"volume={plan.volume:.3f}"]
    args += encode_args(render, has_audio=has_audio and not plan.mute)
    args += ["-movflags", "+faststart", str(destination)]
    try:
        run_with_progress(
            ffmpeg_bin,
            args,
            on_elapsed=(lambda elapsed: on_progress(min(1.0, elapsed / duration_s)))
            if on_progress and duration_s
            else None,
        )
    finally:
        if script is not None:
            script.unlink(missing_ok=True)
        if text_file is not None:
            text_file.unlink(missing_ok=True)
    return destination
