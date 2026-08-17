"""Deterministic transcript scoring and span maths.

Two jobs:

* Score every segment without a model, so an analysis still produces something
  useful when the LLM is unreachable — and so LLM output can be sanity-checked
  against a second opinion rather than trusted blindly.
* Turn per-segment keep/drop verdicts into the clean list of time spans the
  renderer actually cuts on.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable

from clipdesk.models import (
    KeepSpan,
    SegmentAnalysis,
    SegmentKind,
    SilenceSpan,
    TranscriptSegment,
)

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'\-]*", re.IGNORECASE)

# Words that carry no topical signal, excluded from keyword scoring.
_STOPWORDS = {
    "a", "about", "all", "also", "am", "an", "and", "any", "are", "as", "at", "back",
    "be", "because", "been", "but", "by", "can", "could", "did", "do", "does", "doing",
    "done", "down", "for", "from", "get", "go", "going", "good", "got", "had", "has",
    "have", "he", "her", "here", "hey", "him", "his", "how", "i", "if", "in", "into",
    "is", "it", "its", "just", "know", "let", "like", "look", "make", "me", "more",
    "much", "my", "need", "no", "not", "now", "of", "off", "on", "one", "only", "or",
    "other", "our", "out", "over", "really", "right", "said", "same", "say", "see",
    "she", "should", "so", "some", "something", "take", "than", "that", "the", "their",
    "them", "then", "there", "these", "they", "thing", "things", "think", "this",
    "those", "through", "time", "to", "too", "two", "up", "us", "use", "very", "want",
    "was", "way", "we", "well", "were", "what", "when", "where", "which", "while",
    "who", "why", "will", "with", "would", "yeah", "yes", "you", "your",
}

# Phrases that reliably mark logistics rather than content.
_ADMIN_MARKERS = (
    "can you hear me", "can everyone hear", "is my screen", "are you seeing",
    "let's wait", "waiting for", "give people a minute", "recording has started",
    "this meeting is being recorded", "we'll get started in", "sorry i was on mute",
    "you're on mute", "let me share my screen", "next meeting", "book time",
    "calendar invite",
)

_CLOSING_MARKERS = (
    "thanks everyone", "thank you all", "that's all for today", "we'll wrap up",
    "any last questions", "have a good", "see you next", "that's it from me",
)

_QUESTION_MARKERS = ("?", "what about", "how do", "how does", "can we", "could you", "why is")


def tokenize(text: str) -> list[str]:
    return [match.group(0).lower() for match in _WORD_RE.finditer(text)]


def content_words(text: str) -> list[str]:
    return [word for word in tokenize(text) if word not in _STOPWORDS and len(word) > 2]


def keyword_counts(segments: list[TranscriptSegment], limit: int = 40) -> Counter[str]:
    counter: Counter[str] = Counter()
    for segment in segments:
        counter.update(content_words(segment.text))
    return Counter(dict(counter.most_common(limit)))


def filler_ratio(text: str, filler_words: list[str]) -> float:
    lowered = f" {text.lower()} "
    words = tokenize(text)
    if not words:
        return 1.0
    hits = 0
    for filler in filler_words:
        needle = f" {filler.lower()} "
        if " " in filler:
            hits += lowered.count(needle) * len(filler.split())
        else:
            hits += sum(1 for word in words if word == filler.lower())
    return min(1.0, hits / len(words))


def score_segments(
    segments: list[TranscriptSegment],
    *,
    filler_words: list[str],
    total_duration: float,
) -> list[SegmentAnalysis]:
    """A model-free verdict for every segment.

    The score blends information density (content words per second), how much of
    the segment is filler, how strongly its vocabulary matches the video's overall
    vocabulary, and where it sits in the runtime.
    """
    if not segments:
        return []

    vocabulary = keyword_counts(segments)
    peak = max(vocabulary.values(), default=1)
    duration = total_duration or max(segment.end for segment in segments)

    analyses: list[SegmentAnalysis] = []
    for segment in segments:
        text = segment.text.strip()
        lowered = text.lower()
        words = tokenize(text)
        seconds = max(0.5, segment.duration)

        density = min(1.0, len(content_words(text)) / (seconds * 2.2))
        filler = filler_ratio(text, filler_words)
        relevance = (
            min(1.0, sum(vocabulary.get(word, 0) for word in content_words(text)) / (peak * 3))
            if peak
            else 0.0
        )

        kind = SegmentKind.ON_TOPIC
        position = segment.start / duration if duration else 0.0
        if any(marker in lowered for marker in _ADMIN_MARKERS):
            kind = SegmentKind.ADMIN
        elif any(marker in lowered for marker in _CLOSING_MARKERS) and position > 0.75:
            kind = SegmentKind.OUTRO
        elif filler > 0.4 or len(words) <= 2:
            kind = SegmentKind.FILLER
        elif any(marker in lowered for marker in _QUESTION_MARKERS):
            kind = SegmentKind.QA
        elif position < 0.03 and len(words) < 25:
            kind = SegmentKind.INTRO

        importance = 0.45 * density + 0.35 * relevance + 0.20 * (1.0 - filler)
        if kind in (SegmentKind.ADMIN, SegmentKind.FILLER):
            importance *= 0.3
        elif kind in (SegmentKind.INTRO, SegmentKind.OUTRO):
            importance *= 0.55
        elif kind is SegmentKind.QA:
            importance *= 0.9

        analyses.append(
            SegmentAnalysis(
                segment_id=segment.id,
                start=segment.start,
                end=segment.end,
                kind=kind,
                importance=round(min(1.0, max(0.0, importance)), 3),
                keep=True,
                reason="heuristic",
                heuristic_importance=round(min(1.0, max(0.0, importance)), 3),
            )
        )
    return analyses


# --- span maths --------------------------------------------------------------
def merge_spans(
    spans: list[tuple[float, float]], *, merge_gap: float = 0.0
) -> list[tuple[float, float]]:
    """Sort, clamp and coalesce overlapping or near-touching spans."""
    cleaned = sorted((max(0.0, s), e) for s, e in spans if e > s)
    if not cleaned:
        return []
    merged = [cleaned[0]]
    for start, end in cleaned[1:]:
        last_start, last_end = merged[-1]
        if start - last_end <= merge_gap:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def subtract_spans(
    base: list[tuple[float, float]], holes: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """Remove ``holes`` from ``base``."""
    result: list[tuple[float, float]] = []
    holes = merge_spans(holes)
    for start, end in base:
        cursor = start
        for hole_start, hole_end in holes:
            if hole_end <= cursor or hole_start >= end:
                continue
            if hole_start > cursor:
                result.append((cursor, min(hole_start, end)))
            cursor = max(cursor, hole_end)
            if cursor >= end:
                break
        if cursor < end:
            result.append((cursor, end))
    return [(s, e) for s, e in result if e > s]


#: Categories the analyzer's own first pass drops. Callers that make their own
#: category decisions (the cleanup action does) pass their own set, or an empty
#: one to rely on importance alone.
DEFAULT_DROP_KINDS = frozenset(
    {SegmentKind.FILLER, SegmentKind.OFF_TOPIC, SegmentKind.ADMIN}
)


def build_keep_spans(
    analyses: list[SegmentAnalysis],
    silences: list[SilenceSpan],
    *,
    keep_threshold: float,
    pad_seconds: float,
    merge_gap_seconds: float,
    keep_gap_seconds: float,
    total_duration: float,
    drop_kinds: Iterable[SegmentKind] | None = None,
) -> list[KeepSpan]:
    """The cleanup edit decision list.

    Start from every segment worth keeping, pad it so words are not clipped,
    merge what is adjacent, then carve out the long silences that fall inside.
    Short silences are deliberately left in — stripping every pause makes speech
    sound unnatural and rushed.
    """
    dropped = (
        DEFAULT_DROP_KINDS if drop_kinds is None else frozenset(drop_kinds)
    )
    kept: list[tuple[float, float]] = []
    for analysis in analyses:
        if analysis.kind in dropped or analysis.importance < keep_threshold:
            analysis.keep = False
            continue
        analysis.keep = True
        kept.append((max(0.0, analysis.start - pad_seconds), analysis.end + pad_seconds))

    merged = merge_spans(kept, merge_gap=merge_gap_seconds)
    if not merged:
        return []

    long_silences = [
        (silence.start, silence.end)
        for silence in silences
        if silence.duration > keep_gap_seconds
    ]
    # Leave `keep_gap_seconds` of every long silence in place so a cut lands on a
    # natural breath rather than immediately on the next word.
    trimmed_silences = [
        (start + keep_gap_seconds / 2, end - keep_gap_seconds / 2)
        for start, end in long_silences
        if end - start > keep_gap_seconds
    ]

    final = subtract_spans(merged, trimmed_silences)
    limit = total_duration or (final[-1][1] if final else 0.0)

    spans: list[KeepSpan] = []
    for start, end in final:
        end = min(end, limit) if limit else end
        if end - start < 0.25:
            continue
        spans.append(KeepSpan(start=round(start, 3), end=round(end, 3), reason="kept"))
    return spans


def summarise_reduction(total_duration: float, kept: float) -> str:
    if total_duration <= 0:
        return ""
    removed = max(0.0, total_duration - kept)
    percent = removed / total_duration * 100
    return f"{format_duration(removed)} removed ({percent:.0f}%)"


def format_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(round(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {secs:02d}s"


def sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-value))
