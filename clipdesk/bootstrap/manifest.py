"""What ClipDesk needs on disk, and where to get it.

Nothing here is installed system-wide. Every component lands inside ``vendor/``
so the whole folder stays portable — copy it to another machine and it runs.

Air-gapped machines: drop the archive named by ``offline_filename`` into
``vendor/downloads/`` and provisioning uses it instead of the network.
"""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ArchiveSource:
    url: str
    #: Optional hex sha256. When set the download is rejected unless it matches.
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class Component:
    key: str
    label: str
    description: str
    #: Path under ``vendor/`` this component owns.
    target_dir: str
    #: Executables (relative to ``target_dir``) that must exist when installed.
    required_files: tuple[str, ...] = ()
    sources: tuple[ArchiveSource, ...] = ()
    offline_filename: str = ""
    approx_mb: int = 0
    #: Members inside the archive are only extracted when their path contains
    #: one of these fragments. Keeps a 600 MB ffmpeg build down to two binaries.
    extract_only: tuple[str, ...] = ()
    #: Drop this many leading path components when extracting.
    strip_components: int = 0
    #: When true the sources are complementary parts, not mirrors, so all of
    #: them must be fetched (macOS ships ffmpeg and ffprobe separately).
    mirrors_are_parts: bool = False
    #: The download is the executable itself, not an archive to unpack.
    is_single_binary: bool = False
    notes: str = ""


def _exe(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


def _ffmpeg_component() -> Component:
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "windows":
        return Component(
            key="ffmpeg",
            label="ffmpeg",
            description="Reads and writes video. Used for audio extraction, silence "
            "detection and every render.",
            target_dir="ffmpeg",
            required_files=("bin/ffmpeg.exe", "bin/ffprobe.exe"),
            sources=(
                ArchiveSource(
                    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
                    "ffmpeg-master-latest-win64-gpl.zip"
                ),
                ArchiveSource(
                    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
                ),
            ),
            offline_filename="ffmpeg-win64.zip",
            approx_mb=90,
            extract_only=("/bin/ffmpeg.exe", "/bin/ffprobe.exe"),
            strip_components=1,
            notes="Any recent ffmpeg build works. To supply your own, put "
            "ffmpeg.exe and ffprobe.exe in vendor/ffmpeg/bin/.",
        )

    if system == "linux":
        arch = "arm64" if "aarch64" in machine or "arm64" in machine else "amd64"
        return Component(
            key="ffmpeg",
            label="ffmpeg",
            description="Reads and writes video.",
            target_dir="ffmpeg",
            required_files=("bin/ffmpeg", "bin/ffprobe"),
            sources=(
                ArchiveSource(
                    f"https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-{arch}-static.tar.xz"
                ),
            ),
            offline_filename=f"ffmpeg-linux-{arch}.tar.xz",
            approx_mb=80,
            extract_only=("/ffmpeg", "/ffprobe"),
            strip_components=1,
            notes="To supply your own, put ffmpeg and ffprobe in vendor/ffmpeg/bin/.",
        )

    # macOS: the published archives are single binaries, handled by the same
    # extractor because each source is fetched and unpacked independently.
    return Component(
        key="ffmpeg",
        label="ffmpeg",
        description="Reads and writes video.",
        target_dir="ffmpeg",
        required_files=("bin/ffmpeg", "bin/ffprobe"),
        sources=(
            ArchiveSource("https://evermeet.cx/ffmpeg/getrelease/zip"),
            ArchiveSource("https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip"),
        ),
        offline_filename="ffmpeg-macos.zip",
        approx_mb=80,
        extract_only=("ffmpeg", "ffprobe"),
        mirrors_are_parts=True,
        notes="To supply your own, put ffmpeg and ffprobe in vendor/ffmpeg/bin/.",
    )


FFMPEG = _ffmpeg_component()


def _ytdlp_component() -> Component:
    """The media extractor, shipped as a Python zipapp.

    Three ways to obtain yt-dlp were tried on a managed corporate machine:

    * ``pip install yt-dlp`` — rejected by the internal package proxy (HTTP 400,
      "package description exceeds the maximum length").
    * ``yt-dlp.exe`` — blocked by Application Control, because the PyInstaller
      bundle unpacks a native ``python310.dll`` into temp and runs it.
    * The ``yt-dlp`` zipapp — works. It is an ordinary zip of Python source run
      by the interpreter ClipDesk is already using, so there is no new binary to
      allow-list and no package feed involved.
    """
    return Component(
        key="ytdlp",
        label="Media extractor",
        description="Downloads recordings from links that are not a plain file URL — "
        "SharePoint, Stream and OneDrive.",
        target_dir="ytdlp",
        required_files=("yt-dlp.pyz",),
        sources=(
            ArchiveSource("https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"),
        ),
        offline_filename="yt-dlp.pyz",
        approx_mb=3,
        is_single_binary=True,
        notes="Optional. Without it, only direct file links and pre-authorised "
        "download URLs can be imported. To supply your own, save the yt-dlp "
        "zipapp as vendor/ytdlp/yt-dlp.pyz.",
    )


YTDLP = _ytdlp_component()

#: Whisper weights are not a plain archive — they come from the model hub via
#: faster-whisper, so provisioning is handled separately. Sizes are the on-disk
#: footprint of the int8 CTranslate2 conversion.
WHISPER_MODEL_SIZES_MB: dict[str, int] = {
    "tiny": 75,
    "tiny.en": 75,
    "base": 145,
    "base.en": 145,
    "small": 480,
    "small.en": 480,
    "medium": 1530,
    "large-v3": 3090,
}


def whisper_component(model: str) -> Component:
    return Component(
        key="whisper",
        label=f"Speech-to-text model ({model})",
        description="Turns the audio track into a timestamped transcript. Runs on "
        "the CPU and never leaves this machine.",
        target_dir="models/whisper",
        approx_mb=WHISPER_MODEL_SIZES_MB.get(model, 300),
        notes="Only needed when you upload a video without a transcript. Supply an "
        ".srt or .vtt alongside the video to skip speech-to-text entirely.",
    )


def all_components(whisper_model: str) -> list[Component]:
    return [FFMPEG, YTDLP, whisper_component(whisper_model)]


#: Extra fragments an offline archive may be dropped under.
DOWNLOAD_CACHE_DIR = "downloads"

__all__ = [
    "DOWNLOAD_CACHE_DIR",
    "FFMPEG",
    "WHISPER_MODEL_SIZES_MB",
    "YTDLP",
    "ArchiveSource",
    "Component",
    "all_components",
    "whisper_component",
]
