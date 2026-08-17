"""ffmpeg-backed media operations."""

from clipdesk.media.audio import detect_silence, extract_audio
from clipdesk.media.ffmpeg import (
    FFmpegError,
    FFmpegMissingError,
    FFmpegTools,
    find_tools,
    require_tools,
)
from clipdesk.media.probe import probe

__all__ = [
    "FFmpegError",
    "FFmpegMissingError",
    "FFmpegTools",
    "detect_silence",
    "extract_audio",
    "find_tools",
    "probe",
    "require_tools",
]
