"""Parse an uploaded ``.srt`` / ``.vtt`` / ``.json`` transcript.

This is the fast path: Teams, Stream, Zoom and most corporate recorders already
produce a timed transcript, so when one is supplied there is no speech-to-text
work to do at all.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from clipdesk.models import Transcript, TranscriptSegment, TranscriptSource

# 00:01:02,500 / 00:01:02.500 / 01:02.500
_TIMESTAMP_RE = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")
_CUE_RE = re.compile(r"(?P<start>[\d:.,]+)\s*-->\s*(?P<end>[\d:.,]+)")
_TAG_RE = re.compile(r"<[^>]+>")
# WebVTT voice spans and "Name: text" speaker prefixes.
_VOICE_RE = re.compile(r"<v\s+([^>]+)>", re.IGNORECASE)
_SPEAKER_PREFIX_RE = re.compile(r"^([A-Z][\w .'\-]{1,40}):\s+")

SUPPORTED_SUFFIXES = {".srt", ".vtt", ".webvtt", ".json"}


class SubtitleParseError(ValueError):
    pass


def parse_timestamp(value: str) -> float:
    match = _TIMESTAMP_RE.search(value.strip())
    if not match:
        raise SubtitleParseError(f"Unrecognised timestamp: {value!r}")
    hours, minutes, seconds, millis = match.groups()
    return (
        int(hours or 0) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(millis.ljust(3, "0")) / 1000.0
    )


def _clean(text: str) -> tuple[str, str | None]:
    speaker: str | None = None
    if (voice := _VOICE_RE.search(text)) is not None:
        speaker = voice.group(1).strip()
    text = _TAG_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    if speaker is None and (prefix := _SPEAKER_PREFIX_RE.match(text)) is not None:
        speaker = prefix.group(1).strip()
        text = text[prefix.end() :]
    return text, speaker


def parse_cue_text(content: str) -> list[TranscriptSegment]:
    """Parse SRT or WebVTT. Both are cue blocks separated by blank lines."""
    content = content.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    segments: list[TranscriptSegment] = []
    index = 0

    for block in re.split(r"\n\s*\n", content):
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        cue_line = next((line for line in lines if "-->" in line), None)
        if cue_line is None:
            continue
        match = _CUE_RE.search(cue_line)
        if match is None:
            continue
        try:
            start = parse_timestamp(match.group("start"))
            end = parse_timestamp(match.group("end"))
        except SubtitleParseError:
            continue

        body = " ".join(lines[lines.index(cue_line) + 1 :])
        text, speaker = _clean(body)
        if not text:
            continue
        # Some recorders emit overlapping or zero-length cues; keep the timeline
        # monotonic so downstream span maths stays sane.
        if segments and start < segments[-1].end:
            start = max(start, segments[-1].end)
        if end <= start:
            end = start + 0.5
        segments.append(
            TranscriptSegment(id=index, start=start, end=end, text=text, speaker=speaker)
        )
        index += 1

    return segments


def _parse_json(content: str) -> list[TranscriptSegment]:
    """Accept a Whisper-style ``{"segments": [...]}`` document or a bare list."""
    payload = json.loads(content)
    raw = payload.get("segments") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        raise SubtitleParseError("JSON transcript must contain a list of segments")

    segments: list[TranscriptSegment] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        text, speaker = _clean(str(item.get("text") or ""))
        if not text:
            continue
        try:
            start = float(item.get("start", 0.0))
            end = float(item.get("end", start))
        except (TypeError, ValueError):
            continue
        segments.append(
            TranscriptSegment(
                id=index,
                start=start,
                end=max(end, start + 0.1),
                text=text,
                speaker=item.get("speaker") or speaker,
            )
        )
    return segments


def load_transcript(path: str | Path, *, media_duration_s: float = 0.0) -> Transcript:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise SubtitleParseError(
            f"Unsupported transcript format '{suffix}'. Use .srt, .vtt or .json."
        )

    content = path.read_text(encoding="utf-8", errors="replace")
    segments = _parse_json(content) if suffix == ".json" else parse_cue_text(content)
    if not segments:
        raise SubtitleParseError(f"No usable cues found in {path.name}")

    duration = media_duration_s or max(seg.end for seg in segments)
    return Transcript(
        source=TranscriptSource.UPLOADED,
        language=None,
        model=path.name,
        duration_s=duration,
        segments=segments,
    )
