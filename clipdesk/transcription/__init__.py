"""Turning audio (or an uploaded subtitle file) into a timed transcript."""

from clipdesk.transcription.base import (
    ProgressFn,
    Transcriber,
    TranscriptionError,
    create_transcriber,
)
from clipdesk.transcription.subtitles import (
    SUPPORTED_SUFFIXES,
    SubtitleParseError,
    load_transcript,
)

__all__ = [
    "SUPPORTED_SUFFIXES",
    "ProgressFn",
    "SubtitleParseError",
    "Transcriber",
    "TranscriptionError",
    "create_transcriber",
    "load_transcript",
]
