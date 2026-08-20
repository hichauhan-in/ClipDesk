"""Attach a header (intro) and/or footer (outro) video to a rendered output.

Corporate recordings usually need a branded top-and-tail. The intro is very
likely a different resolution and frame rate from a screen recording, so every
part is normalised onto one canvas before joining — see
:func:`clipdesk.actions.render.concat_normalised`.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from clipdesk.analysis.heuristics import format_duration
from clipdesk.config import Settings
from clipdesk.events import EventBus
from clipdesk.media.probe import probe
from clipdesk.models import ArtifactKind
from clipdesk.store import Project

STAGE = "bookend"

ASSETS_DIRNAME = "assets"
MEDIA_DIRNAME = "media"
ASSET_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm"}


def assets_dir(workspace: Path) -> Path:
    """Legacy shared library. Still readable so nothing imported before is lost."""
    directory = workspace / ASSETS_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def media_dir(project: Project) -> Path:
    """Imported media belonging to one project only."""
    directory = project.root / MEDIA_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def list_media(project: Project) -> list[dict[str, object]]:
    return [
        {"name": path.name, "size_bytes": path.stat().st_size}
        for path in sorted(media_dir(project).iterdir())
        # A leading dot marks in-progress staging, which is not media yet.
        if path.is_file()
        and not path.name.startswith(".")
        and path.suffix.lower() in ASSET_SUFFIXES
    ]


def resolve_media(project: Project, name: str) -> Path:
    """Resolve one of this project's media files, refusing anything outside it."""
    directory = media_dir(project).resolve()
    candidate = (directory / name).resolve()
    if candidate.parent != directory or not candidate.is_file():
        raise ValueError(f"No such media in this project: {name}")
    return candidate


def resolve_attachment(project: Project, name: str) -> Path:
    """Media to attach, from what was imported or from what this project rendered.

    An intro built with the Intro tool is an output, not an import, so restricting
    this to the media folder made a project unable to attach the very thing it
    had just produced. Both folders are matched by exact parent, so a name can
    still only ever reach inside this project.
    """
    try:
        return resolve_media(project, name)
    except ValueError:
        pass

    directory = project.output_dir.resolve()
    candidate = (directory / name).resolve()
    if candidate.parent != directory or not candidate.is_file():
        raise ValueError(f"No such media in this project: {name}")
    if candidate.suffix.lower() not in ASSET_SUFFIXES:
        raise ValueError(f"{name} is not a video that can be attached.")
    return candidate


def store_media(source: Path, project: Project, name: str = "") -> Path:
    """Move a staged video into one project without overwriting an existing file."""
    suffix = source.suffix.lower()
    if not source.is_file() or suffix not in ASSET_SUFFIXES:
        raise ValueError("Imported media must be MP4, MOV, MKV or WebM video files.")

    directory = media_dir(project)
    requested = Path(name).name if name else source.name
    if Path(requested).suffix.lower() not in ASSET_SUFFIXES:
        requested = f"{Path(requested).stem or source.stem}{suffix}"
    destination = directory / requested
    counter = 2
    while destination.exists():
        destination = directory / f"{Path(requested).stem}-{counter}{Path(requested).suffix}"
        counter += 1
    source.replace(destination)
    return destination


def copy_into_project(source: Path, project: Project, name: str = "") -> Path:
    """Copy media from elsewhere into this project, leaving the original in place."""
    requested = Path(name).name if name else source.name
    directory = media_dir(project)
    # A unique staging name, so two concurrent copies of the same file cannot
    # overwrite each other's partial download.
    descriptor, staged_name = tempfile.mkstemp(
        prefix=".adopt-", suffix=Path(requested).suffix, dir=directory
    )
    os.close(descriptor)
    staged = Path(staged_name)
    try:
        shutil.copyfile(source, staged)
        return store_media(staged, project, requested)
    except Exception:
        staged.unlink(missing_ok=True)
        raise


def list_assets(workspace: Path) -> list[dict[str, object]]:
    directory = assets_dir(workspace)
    assets: list[dict[str, object]] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.lower() not in ASSET_SUFFIXES:
            continue
        assets.append(
            {
                "name": path.name,
                "size_bytes": path.stat().st_size,
            }
        )
    return assets


def resolve_asset(workspace: Path, name: str) -> Path:
    """Resolve an asset by name, refusing anything outside the asset folder."""
    directory = assets_dir(workspace).resolve()
    candidate = (directory / name).resolve()
    if candidate.parent != directory or not candidate.is_file():
        raise ValueError(f"No such intro/outro asset: {name}")
    return candidate


def store_asset(source: Path, workspace: Path, name: str = "") -> Path:
    """Move a staged video into the library without overwriting an existing asset."""
    suffix = source.suffix.lower()
    if not source.is_file() or suffix not in ASSET_SUFFIXES:
        raise ValueError("Intro and outro assets must be MP4, MOV, MKV or WebM video files.")

    directory = assets_dir(workspace)
    requested = Path(name).name if name else source.name
    if Path(requested).suffix.lower() not in ASSET_SUFFIXES:
        requested = f"{Path(requested).stem or source.stem}{suffix}"
    destination = directory / requested
    counter = 2
    while destination.exists():
        destination = directory / f"{Path(requested).stem}-{counter}{Path(requested).suffix}"
        counter += 1
    source.replace(destination)
    return destination


def apply_bookends(
    project: Project,
    settings: Settings,
    ffprobe_bin: str,
    ffmpeg_bin: str,
    bus: EventBus,
    *,
    body_filename: str,
    header: Path | None = None,
    footer: Path | None = None,
    output_name: str = "final.mp4",
    intro_transition: str = "cut",
    outro_transition: str = "cut",
    trim_start: float = 0.0,
    trim_end: float | None = None,
) -> Path:
    from clipdesk.actions.render import ConcatInput, concat_normalised

    trimming = trim_start > 0.0 or trim_end is not None
    if header is None and footer is None and not trimming:
        raise ValueError("Choose an intro, an outro, or a trim range.")

    body = project.output_path(body_filename)
    if not body.is_file():
        # The user may want to top-and-tail the original upload rather than a
        # rendered output.
        body = project.source_path
        if not body.is_file():
            raise ValueError(f"Could not find the video to wrap: {body_filename}")

    parts = [path for path in (header, body, footer) if path is not None]
    bus.stage_start(STAGE, f"Joining {len(parts)} part(s)")

    inputs: list[ConcatInput] = []
    for path in parts:
        info = probe(path, ffprobe_bin)
        is_body = path == body
        if is_body and trim_start >= info.duration_s:
            raise ValueError("The trim start is past the end of the video.")
        inputs.append(
            ConcatInput(
                path=path,
                duration_s=info.duration_s,
                has_audio=info.has_audio,
                start=trim_start if is_body else 0.0,
                end=trim_end if is_body else None,
            )
        )

    total = sum(item.effective_duration for item in inputs)
    transitions = []
    if header is not None:
        transitions.append(intro_transition)
    if footer is not None:
        transitions.append(outro_transition)
    destination = project.output_path(output_name)
    concat_normalised(
        inputs,
        destination,
        render=settings.render,
        ffmpeg_bin=ffmpeg_bin,
        transitions=transitions,
        on_progress=lambda fraction: bus.progress(STAGE, fraction, "Encoding"),
    )

    labels = []
    if header is not None:
        labels.append("intro")
    if footer is not None:
        labels.append("outro")
    if trimming:
        labels.append("trim")
    project.add_artifact(
        ArtifactKind.BOOKEND,
        f"Final cut with {' + '.join(labels)} ({format_duration(total)})",
        destination,
        duration_s=total,
        meta={
            "header": header.name if header else "",
            "footer": footer.name if footer else "",
            "body": body.name,
            "intro_transition": intro_transition if header else "",
            "outro_transition": outro_transition if footer else "",
            "trim_start": f"{trim_start:.3f}" if trimming else "",
            "trim_end": f"{trim_end:.3f}" if trim_end is not None else "",
        },
    )
    bus.stage_end(STAGE, "Final cut ready")
    return destination
