"""Locating and running the bundled ffmpeg / ffprobe.

Resolution order: the copy inside ``vendor/`` first, then anything already on
``PATH``. The bundled copy wins so a machine with an ancient system ffmpeg still
gets predictable behaviour.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from clipdesk.bootstrap.provision import ffmpeg_dir

# ffmpeg reports elapsed output time on stderr as `time=00:01:23.45`.
_TIME_RE = re.compile(r"time=(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_VERSION_RE = re.compile(r"ffmpeg version (\d+)")
#: A name no filtergraph file will have, used to make ffmpeg reject an option.
_MISSING_GRAPH = "clipdesk-option-probe.not-a-file"

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class FFmpegError(RuntimeError):
    def __init__(self, message: str, *, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr


class FFmpegMissingError(FFmpegError):
    pass


@dataclass(frozen=True, slots=True)
class FFmpegTools:
    ffmpeg: str
    ffprobe: str
    source: str  # "bundled" | "path"


def _exe(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


def find_tools(vendor: Path) -> FFmpegTools | None:
    bundled = ffmpeg_dir(vendor) / "bin"
    ffmpeg = bundled / _exe("ffmpeg")
    ffprobe = bundled / _exe("ffprobe")
    if ffmpeg.is_file() and ffprobe.is_file():
        return FFmpegTools(str(ffmpeg), str(ffprobe), "bundled")

    on_path = shutil.which("ffmpeg")
    probe_on_path = shutil.which("ffprobe")
    if on_path and probe_on_path:
        return FFmpegTools(on_path, probe_on_path, "path")
    return None


def require_tools(vendor: Path) -> FFmpegTools:
    tools = find_tools(vendor)
    if tools is None:
        raise FFmpegMissingError(
            "ffmpeg is not available. Open Setup in the app (or run "
            "`clipdesk bootstrap`) to install the bundled copy."
        )
    return tools


def run(binary: str, args: Sequence[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """Run ffmpeg/ffprobe and raise :class:`FFmpegError` on a non-zero exit."""
    command = [binary, "-hide_banner", *args]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise FFmpegMissingError(f"{binary} could not be started") from exc
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"{Path(binary).name} timed out after {timeout}s") from exc

    if result.returncode != 0:
        tail = "\n".join((result.stderr or "").strip().splitlines()[-12:])
        raise FFmpegError(
            f"{Path(binary).name} failed (exit {result.returncode}):\n{tail}",
            stderr=result.stderr or "",
        )
    return result


def run_with_progress(
    ffmpeg_bin: str,
    args: Sequence[str],
    *,
    on_elapsed: Callable[[float], None] | None = None,
) -> None:
    """Run an ffmpeg render, reporting output position in seconds as it goes."""
    command = [ffmpeg_bin, "-hide_banner", "-nostdin", "-y", *args]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise FFmpegMissingError(f"{ffmpeg_bin} could not be started") from exc

    tail: list[str] = []
    assert process.stderr is not None
    for line in process.stderr:
        tail.append(line.rstrip())
        del tail[:-40]
        if on_elapsed and (match := _TIME_RE.search(line)) is not None:
            hours, minutes, seconds = match.groups()
            on_elapsed(int(hours) * 3600 + int(minutes) * 60 + float(seconds))
    process.wait()

    if process.returncode != 0:
        raise FFmpegError(
            f"ffmpeg failed (exit {process.returncode}):\n" + "\n".join(tail[-12:]),
            stderr="\n".join(tail),
        )


@lru_cache(maxsize=8)
def major_version(ffmpeg_bin: str) -> int | None:
    try:
        result = run(ffmpeg_bin, ["-version"], timeout=20)
    except FFmpegError:
        return None
    match = _VERSION_RE.search(result.stdout or "")
    return int(match.group(1)) if match else None


def _accepts(ffmpeg_bin: str, option: str) -> bool:
    """Does this binary know ``option``? Asked by using it and reading the refusal."""
    try:
        result = run(ffmpeg_bin, [option, _MISSING_GRAPH, "-f", "null", "-"], timeout=20)
    except FFmpegError as exc:
        # Naming a file that is not there is the point: the binary gets far
        # enough to say whether it understood the option before it fails.
        return "unrecognized option" not in (exc.stderr or "").lower()
    return result.returncode == 0


@lru_cache(maxsize=8)
def filter_script_option(ffmpeg_bin: str) -> str | None:
    """The spelling this build uses for reading a filtergraph from a file.

    ffmpeg 7 replaced ``-filter_complex_script`` with ``-/filter_complex``.
    Nightly builds report a version like ``N-126039-g6bbc22dc09`` with no number
    to compare, and guessing wrong is not a small mistake: falling back to an
    inline graph puts a filtergraph with hundreds of segments on the command
    line, where Windows refuses it and reports it as a missing executable. So
    when the version is unreadable, the binary is asked directly.
    """
    version = major_version(ffmpeg_bin)
    if version is not None:
        return "-/filter_complex" if version >= 7 else "-filter_complex_script"
    for option in ("-/filter_complex", "-filter_complex_script"):
        if _accepts(ffmpeg_bin, option):
            return option
    return None


def probe_json(ffprobe_bin: str, source: str | Path) -> dict:
    result = run(
        ffprobe_bin,
        [
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(source),
        ],
        timeout=120,
    )
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise FFmpegError("ffprobe returned output that could not be parsed") from exc


def filter_complex_args(graph: str, scratch: Path, ffmpeg_bin: str) -> tuple[list[str], Path | None]:
    """Pass a filtergraph via a file so a long one never hits the OS arg limit.

    Only a build that takes neither spelling gets the graph inline, and that is
    a build ClipDesk cannot render long cuts with anyway.
    """
    option = filter_script_option(ffmpeg_bin)
    if option is None:
        return ["-filter_complex", graph], None
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_text(graph, encoding="utf-8")
    return [option, str(scratch)], scratch
