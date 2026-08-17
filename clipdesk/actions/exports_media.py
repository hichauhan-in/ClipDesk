"""Re-encode an existing output to a chosen format and quality.

Exports are deliberately preset-driven rather than free-form: the user picks a
named container and a named quality, and every ffmpeg argument is derived from
those enumerations. Nothing the user types reaches the command line.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from clipdesk.analysis.heuristics import format_duration
from clipdesk.media.ffmpeg import run_with_progress
from clipdesk.media.probe import probe
from clipdesk.models import ArtifactKind
from clipdesk.store import Project

STAGE = "export"

ExportFormat = Literal["mp4", "webm", "mov", "gif", "mp3", "m4a", "wav"]
ExportQuality = Literal["source", "high", "balanced", "small", "tiny"]

#: Target height in lines, and the CRF used for the H.264/VP9 encoders. Height is
#: what "720p" means, so a landscape video keeps its full width at that height.
_QUALITY: dict[str, tuple[int | None, int, str]] = {
    "source": (None, 18, "Same size as the source, visually lossless"),
    "high": (1080, 20, "1080p, high quality"),
    "balanced": (720, 24, "720p, good quality and a smaller file"),
    "small": (540, 28, "540p, sized for chat and email"),
    "tiny": (360, 32, "360p, smallest usable file"),
}

_AUDIO_ONLY = {"mp3", "m4a", "wav"}

_FORMAT_LABEL = {
    "mp4": "MP4 (H.264)",
    "webm": "WebM (VP9)",
    "mov": "MOV (H.264)",
    "gif": "Animated GIF",
    "mp3": "MP3 audio",
    "m4a": "M4A audio",
    "wav": "WAV audio",
}


def export_options() -> dict[str, object]:
    """Everything the UI needs to offer, so the two sides cannot drift apart."""
    return {
        "formats": [
            {"id": key, "label": label, "audio_only": key in _AUDIO_ONLY}
            for key, label in _FORMAT_LABEL.items()
        ],
        "qualities": [
            {"id": key, "label": key.title(), "description": description}
            for key, (_edge, _crf, description) in _QUALITY.items()
        ],
    }


@dataclass(frozen=True, slots=True)
class ExportPlan:
    source_name: str
    output_name: str
    export_format: str
    quality: str
    audio_only: bool

    def describe(self) -> str:
        return f"{_FORMAT_LABEL[self.export_format]} · {_QUALITY[self.quality][2]}"


def plan_export(
    source_name: str, export_format: str, quality: str, output_name: str = ""
) -> ExportPlan:
    if export_format not in _FORMAT_LABEL:
        raise ValueError(f"Unsupported export format: {export_format}")
    if quality not in _QUALITY:
        raise ValueError(f"Unsupported export quality: {quality}")
    stem = Path(source_name).stem or "export"
    suffix = f".{export_format}"
    requested = Path(output_name).name if output_name else ""
    if requested:
        stem = Path(requested).stem or stem
    return ExportPlan(
        source_name=source_name,
        output_name=f"{stem}-{quality}{suffix}" if not requested else f"{stem}{suffix}",
        export_format=export_format,
        quality=quality,
        audio_only=export_format in _AUDIO_ONLY,
    )


def _scale_filter(height: int | None) -> list[str]:
    if height is None:
        return []
    # Only ever downscale, and keep both dimensions even for the encoders.
    return [
        "-vf",
        f"scale=-2:'min({height},ih)':force_divisible_by=2",
    ]


def _gif_filter(height: int | None) -> list[str]:
    lines = min(height or 480, 480)
    return [
        "-vf",
        f"fps=12,scale=-2:'min({lines},ih)':flags=lanczos:force_divisible_by=2,"
        "split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer",
    ]


def export_args(plan: ExportPlan) -> list[str]:
    height, crf, _description = _QUALITY[plan.quality]
    if plan.export_format == "mp3":
        return ["-vn", "-c:a", "libmp3lame", "-q:a", "3"]
    if plan.export_format == "m4a":
        return ["-vn", "-c:a", "aac", "-b:a", "192k"]
    if plan.export_format == "wav":
        return ["-vn", "-c:a", "pcm_s16le"]
    if plan.export_format == "gif":
        return [*_gif_filter(height), "-loop", "0", "-an"]
    if plan.export_format == "webm":
        return [
            *_scale_filter(height),
            "-c:v", "libvpx-vp9", "-crf", str(crf + 8), "-b:v", "0",
            "-row-mt", "1", "-c:a", "libopus", "-b:a", "128k",
        ]
    return [
        *_scale_filter(height),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
    ]


def render_export(
    project: Project,
    plan: ExportPlan,
    *,
    source: Path,
    ffmpeg_bin: str,
    ffprobe_bin: str,
    on_progress=None,
) -> Path:
    if not source.is_file():
        raise ValueError(f"Could not find {plan.source_name}.")
    info = probe(source, ffprobe_bin)
    if plan.audio_only and not info.has_audio:
        raise ValueError(f"{plan.source_name} has no audio track to extract.")
    destination = project.output_path(plan.output_name)
    args = ["-i", str(source), *export_args(plan), "-y", str(destination)]
    run_with_progress(
        ffmpeg_bin,
        args,
        on_elapsed=(
            lambda elapsed: on_progress(min(1.0, elapsed / info.duration_s))
            if info.duration_s
            else None
        )
        if on_progress
        else None,
    )
    project.add_artifact(
        ArtifactKind.EXPORT,
        f"{Path(plan.source_name).stem} — {plan.describe()}"
        + (f" ({format_duration(info.duration_s)})" if info.duration_s else ""),
        destination,
        duration_s=info.duration_s or None,
        meta={
            "source": plan.source_name,
            "format": plan.export_format,
            "quality": plan.quality,
        },
    )
    return destination
