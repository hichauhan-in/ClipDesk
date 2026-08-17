"""Audio extraction and silence detection."""

from __future__ import annotations

import re
from pathlib import Path

from clipdesk.media.ffmpeg import run, run_with_progress
from clipdesk.models import SilenceSpan

_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[0-9.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[0-9.]+)")


def extract_audio(
    source: str | Path,
    dest: str | Path,
    *,
    ffmpeg_bin: str,
    sample_rate: int = 16000,
    on_elapsed=None,
) -> Path:
    """Extract mono PCM audio — the only form speech-to-text needs."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_with_progress(
        ffmpeg_bin,
        [
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s16le",
            str(dest),
        ],
        on_elapsed=on_elapsed,
    )
    return dest


def detect_silence(
    source: str | Path,
    *,
    ffmpeg_bin: str,
    noise_db: float = -32.0,
    min_silence_seconds: float = 0.5,
) -> list[SilenceSpan]:
    """Silent spans in ``source``, via ffmpeg's ``silencedetect`` filter."""
    result = run(
        ffmpeg_bin,
        [
            "-nostdin",
            "-i",
            str(source),
            "-af",
            f"silencedetect=noise={noise_db}dB:d={min_silence_seconds}",
            "-f",
            "null",
            "-",
        ],
    )

    spans: list[SilenceSpan] = []
    pending: float | None = None
    for line in (result.stderr or "").splitlines():
        if (match := _SILENCE_START_RE.search(line)) is not None:
            pending = max(0.0, float(match.group(1)))
        elif (match := _SILENCE_END_RE.search(line)) is not None:
            end = float(match.group(1))
            start = pending if pending is not None else 0.0
            if end > start:
                spans.append(SilenceSpan(start=start, end=end))
            pending = None
    return spans
