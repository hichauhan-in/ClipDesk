"""Speech-to-text behind a narrow interface.

Only one implementation ships today (faster-whisper, CPU, int8) but everything
downstream depends on this interface rather than on Whisper, so swapping in
whisper.cpp or a cloud recogniser later is a single new file.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from clipdesk.models import Transcript

#: Called with a 0..1 fraction and a status line.
ProgressFn = Callable[[float | None, str], None]


class TranscriptionError(RuntimeError):
    pass


class Transcriber(Protocol):
    name: str

    def transcribe(
        self, audio_path: str | Path, *, duration_s: float, on_progress: ProgressFn | None = None
    ) -> Transcript: ...


def create_transcriber(settings, vendor_dir: Path) -> Transcriber:
    """Build the configured transcriber.

    Imported lazily so the app starts (and the transcript-upload path works) on a
    machine where the speech-to-text extra was never installed.
    """
    from clipdesk.transcription.whisper import WhisperTranscriber

    return WhisperTranscriber(settings.transcription, vendor_dir)
