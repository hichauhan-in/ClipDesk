"""Finding clips, and cutting the ones the user picks.

The flow is deliberately two-stage — **find**, then **render** — because the
alternative ("give me 5 clips") asks the user to commit before they know what is
in the recording, and asks the model to invent five worthwhile moments whether or
not five exist. Finding is cheap and reversible; encoding is neither.

Three ways to find:

* ``duration`` — "about 90 seconds", optionally about something. Length is a soft
  target: a clean thought that runs 20 seconds long beats one cut mid-sentence.
* ``topic``    — "the bit about the idempotency key". Returns every place the
  topic is discussed, which may be several separate stretches.
* ``highlight`` — the moments worth sharing on their own, optionally filtered by
  a description.

All three return the same shape, so the UI and the renderer only know about
candidates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from clipdesk.analysis import heuristics
from clipdesk.analysis.prompts import (
    CLIP_DURATION_USER,
    CLIP_FIND_SYSTEM,
    CLIP_TOPIC_USER,
    HIGHLIGHT_FIND_USER,
)
from clipdesk.analysis.windows import format_timestamp
from clipdesk.config import Settings
from clipdesk.events import EventBus
from clipdesk.llm import LLMClient, LLMError
from clipdesk.models import (
    AnalysisReport,
    ArtifactKind,
    ClipCandidate,
    SegmentKind,
    TranscriptSegment,
)
from clipdesk.store import Project, slugify

STAGE = "clip"

MODES = ("duration", "topic", "highlight")

_SENTENCE_END_RE = re.compile(r"[.!?][\"')\]]?\s*$")
_LOW_VALUE_KINDS = {SegmentKind.FILLER, SegmentKind.ADMIN, SegmentKind.OFF_TOPIC}

# Transcript budget per prompt. Comfortably inside every model's window while
# still covering a long recording at one line per segment.
_PROMPT_TRANSCRIPT_CHARS = 12_000


@dataclass(slots=True)
class CandidateResult:
    candidates: list[ClipCandidate] = field(default_factory=list)
    #: False when a topic search genuinely found nothing, as opposed to erroring.
    found: bool = True
    note: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "candidates": [candidate.model_dump(mode="json") for candidate in self.candidates],
            "found": self.found,
            "note": self.note,
            "warnings": self.warnings,
        }


# --- shared helpers ----------------------------------------------------------
def _importance_map(report: AnalysisReport) -> dict[int, float]:
    return {item.segment_id: item.importance for item in report.segment_analyses}


def _kind_map(report: AnalysisReport) -> dict[int, SegmentKind]:
    return {item.segment_id: item.kind for item in report.segment_analyses}


def _is_sentence_end(segment: TranscriptSegment) -> bool:
    return bool(_SENTENCE_END_RE.search(segment.text.strip()))


def _chapter_title_at(report: AnalysisReport, time: float) -> str:
    for chapter in report.chapters:
        if chapter.start <= time < chapter.end:
            return chapter.title
    return ""


def _overlap_ratio(a: ClipCandidate, b: ClipCandidate) -> float:
    shared = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    shortest = min(a.duration, b.duration)
    return shared / shortest if shortest > 0 else 0.0


def _dedupe(candidates: list[ClipCandidate], threshold: float = 0.5) -> list[ClipCandidate]:
    kept: list[ClipCandidate] = []
    for candidate in sorted(candidates, key=lambda c: -c.score):
        if any(_overlap_ratio(candidate, existing) > threshold for existing in kept):
            continue
        kept.append(candidate)
    kept.sort(key=lambda c: c.start)
    return kept


def _render_transcript(segments: list[TranscriptSegment], limit: int) -> str:
    """One line per segment, truncated to a character budget."""
    lines: list[str] = []
    used = 0
    for segment in segments:
        speaker = f"{segment.speaker}: " if segment.speaker else ""
        line = f"[{segment.id}] {format_timestamp(segment.start)} {speaker}{segment.text.strip()}"
        if used + len(line) > limit:
            lines.append("… (transcript truncated)")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def _fallback_summary(report: AnalysisReport, start: float, end: float) -> str:
    """Two sentences lifted from the transcript, for when there is no model."""
    text = " ".join(
        segment.text.strip()
        for segment in report.transcript.segments
        if segment.start >= start and segment.end <= end
    )
    sentences = re.split(r"(?<=[.!?])\s+", text)
    summary = " ".join(sentences[:2]).strip()
    return summary[:280] if summary else text[:280]


def _span_from_ids(
    report: AnalysisReport, from_id, to_id
) -> tuple[float, float, int, int] | None:
    """Resolve a model-supplied id range to a real time span, clamped to reality."""
    by_id = {segment.id: segment for segment in report.transcript.segments}
    if not by_id:
        return None
    try:
        first = int(from_id)
        last = int(to_id if to_id is not None else from_id)
    except (TypeError, ValueError):
        return None
    if last < first:
        first, last = last, first

    ids = sorted(by_id)
    start_segment = by_id.get(first) or by_id[min(ids, key=lambda i: abs(i - first))]
    end_segment = by_id.get(last) or by_id[min(ids, key=lambda i: abs(i - last))]
    start, end = start_segment.start, max(end_segment.end, start_segment.end)
    if end <= start:
        return None
    return start, end, start_segment.id, end_segment.id


def _to_candidate(
    report: AnalysisReport,
    entry: dict,
    *,
    source: str,
    score_key: str = "score",
    pad: float = 0.0,
) -> ClipCandidate | None:
    if not isinstance(entry, dict):
        return None
    span = _span_from_ids(report, entry.get("from_id"), entry.get("to_id"))
    if span is None:
        return None
    start, end, _, _ = span
    limit = report.media.duration_s or end
    start = max(0.0, start - pad)
    end = min(end + pad, limit) if limit else end + pad

    try:
        score = float(entry.get(score_key, entry.get("score", 0.6)))
    except (TypeError, ValueError):
        score = 0.6

    summary = str(entry.get("summary") or "").strip()
    return ClipCandidate(
        title=str(entry.get("title") or "Clip").strip()[:120],
        start=round(start, 3),
        end=round(end, 3),
        score=max(0.0, min(1.0, score)),
        summary=summary or _fallback_summary(report, start, end),
        hook=str(entry.get("hook") or "").strip()[:200],
        reason=str(entry.get("reason") or "").strip()[:200],
        source=source,
    )


# --- heuristic ranking (the no-model path, and the shortlist for the model) ---
def rank_spans(
    report: AnalysisReport,
    *,
    target_seconds: float,
    tolerance: float,
    query: str = "",
    limit: int = 12,
) -> list[ClipCandidate]:
    """Score contiguous runs of segments near the target length.

    Blends how important the material is, how well it matches the request, how
    cleanly it starts and ends, and how little filler it contains. Overlapping
    options are thinned so the user sees genuinely different choices rather than
    the same moment shifted by two seconds.
    """
    segments = report.transcript.segments
    if not segments:
        return []

    importance = _importance_map(report)
    kinds = _kind_map(report)
    terms = set(heuristics.content_words(query))
    minimum = target_seconds * (1 - tolerance)
    maximum = target_seconds * (1 + tolerance)

    scored: list[ClipCandidate] = []
    for start_index, start_segment in enumerate(segments):
        if kinds.get(start_segment.id) in _LOW_VALUE_KINDS:
            continue
        for end_index in range(start_index, len(segments)):
            end_segment = segments[end_index]
            duration = end_segment.end - start_segment.start
            if duration < minimum:
                continue
            if duration > maximum:
                break

            window = segments[start_index : end_index + 1]
            scores = [importance.get(segment.id, 0.5) for segment in window]
            mean_importance = sum(scores) / len(scores)
            low_value = sum(
                1 for segment in window if kinds.get(segment.id) in _LOW_VALUE_KINDS
            ) / len(window)
            relevance = (
                len(terms & set(heuristics.content_words(" ".join(s.text for s in window))))
                / len(terms)
                if terms
                else 0.0
            )
            boundary = (0.5 if _is_sentence_end(end_segment) else 0.0) + (
                0.5 if start_index == 0 or _is_sentence_end(segments[start_index - 1]) else 0.0
            )
            fit = 1.0 - min(1.0, abs(duration - target_seconds) / max(1.0, target_seconds))

            if terms:
                score = (
                    0.34 * mean_importance
                    + 0.30 * relevance
                    + 0.14 * fit
                    + 0.12 * boundary
                    + 0.10 * (1.0 - low_value)
                )
            else:
                score = (
                    0.50 * mean_importance
                    + 0.20 * fit
                    + 0.16 * boundary
                    + 0.14 * (1.0 - low_value)
                )

            scored.append(
                ClipCandidate(
                    title=_chapter_title_at(report, start_segment.start)
                    or f"Clip at {format_timestamp(start_segment.start)}",
                    start=round(start_segment.start, 3),
                    end=round(end_segment.end, 3),
                    score=round(score, 4),
                    summary=_fallback_summary(report, start_segment.start, end_segment.end),
                    reason="ranked on transcript importance and fit",
                    source="duration",
                )
            )
            break  # one candidate per start position keeps the search linear

    return _dedupe(scored, threshold=0.4)[:limit]


# --- finding -----------------------------------------------------------------
def find_candidates(
    report: AnalysisReport,
    settings: Settings,
    *,
    mode: str = "duration",
    target_seconds: float | None = None,
    query: str = "",
    count: int | None = None,
    llm: LLMClient | None = None,
    bus: EventBus | None = None,
) -> CandidateResult:
    if mode not in MODES:
        raise ValueError(f"Unknown mode '{mode}'. Choose one of: {', '.join(MODES)}")
    if not report.transcript.segments:
        raise ValueError("This recording has no transcript, so there is nothing to search.")

    query = (query or "").strip()
    wanted = count or settings.clips.candidate_count

    if mode == "topic":
        if not query:
            raise ValueError("Describe what the clip should be about.")
        return _find_by_topic(report, settings, query, llm, bus)
    if mode == "highlight":
        return _find_highlights(report, settings, query, wanted, llm, bus)
    return _find_by_duration(
        report,
        settings,
        target_seconds or settings.clips.default_duration_seconds,
        query,
        wanted,
        llm,
        bus,
    )


def _find_by_duration(
    report: AnalysisReport,
    settings: Settings,
    target_seconds: float,
    query: str,
    wanted: int,
    llm: LLMClient | None,
    bus: EventBus | None,
) -> CandidateResult:
    tolerance = settings.clips.duration_tolerance
    ranked = rank_spans(
        report, target_seconds=target_seconds, tolerance=tolerance, query=query, limit=wanted * 2
    )
    if not ranked:
        # The requested length does not fit the material — widen rather than
        # returning nothing, and let the user judge the result.
        ranked = rank_spans(
            report,
            target_seconds=target_seconds,
            tolerance=min(0.9, tolerance * 2.5),
            query=query,
            limit=wanted * 2,
        )
    if not ranked:
        raise ValueError(
            "No stretch of roughly that length could be found. Try a shorter target."
        )

    if llm is None:
        return CandidateResult(
            candidates=ranked[:wanted],
            warnings=["No language model available — options were ranked from the transcript."],
        )

    if bus:
        bus.progress(STAGE, None, "Looking for the best sections")

    request_line = f"The user is looking for: {query}\n" if query else ""
    user = CLIP_DURATION_USER.format(
        title=report.title or report.media.filename,
        request_line=request_line,
        target_seconds=target_seconds,
        tolerance_seconds=target_seconds * tolerance,
        transcript=_render_transcript(report.transcript.segments, _PROMPT_TRANSCRIPT_CHARS),
        max_candidates=wanted,
    )

    try:
        payload = llm.complete_json(CLIP_FIND_SYSTEM, user, temperature=0.2)
    except LLMError as exc:
        return CandidateResult(
            candidates=ranked[:wanted],
            warnings=[f"Falling back to transcript ranking: {exc}"],
        )

    entries = payload.get("candidates") if isinstance(payload, dict) else None
    found = [
        candidate
        for entry in (entries or [])
        if (candidate := _to_candidate(report, entry, source="duration", pad=settings.clips.pad_seconds))
    ]
    if not found:
        return CandidateResult(
            candidates=ranked[:wanted],
            warnings=["The model returned no usable options; showing transcript ranking."],
        )
    return CandidateResult(candidates=_dedupe(found)[:wanted])


def _find_by_topic(
    report: AnalysisReport,
    settings: Settings,
    query: str,
    llm: LLMClient | None,
    bus: EventBus | None,
) -> CandidateResult:
    if llm is None:
        # Word overlap is a poor substitute for understanding, but it is better
        # than refusing outright, and the result is clearly labelled.
        ranked = rank_spans(
            report,
            target_seconds=settings.clips.default_duration_seconds,
            tolerance=0.9,
            query=query,
            limit=6,
        )
        return CandidateResult(
            candidates=ranked,
            found=bool(ranked),
            note="Matched on keywords only — no language model was available.",
            warnings=["Connect a model for topic search that understands meaning."],
        )

    if bus:
        bus.progress(STAGE, None, f"Searching the transcript for: {query}")

    user = CLIP_TOPIC_USER.format(
        title=report.title or report.media.filename,
        request=query,
        transcript=_render_transcript(report.transcript.segments, _PROMPT_TRANSCRIPT_CHARS),
    )
    payload = llm.complete_json(CLIP_FIND_SYSTEM, user, temperature=0.15)
    if not isinstance(payload, dict):
        raise LLMError("The model did not return a usable result for that search.")

    matches = payload.get("matches") or []
    candidates = [
        candidate
        for entry in matches
        if (
            candidate := _to_candidate(
                report,
                entry,
                source="topic",
                score_key="relevance",
                pad=settings.clips.pad_seconds,
            )
        )
    ]
    candidates.sort(key=lambda c: c.start)

    if not candidates:
        return CandidateResult(
            candidates=[],
            found=False,
            note=str(payload.get("note") or "").strip()
            or "That topic does not appear to be discussed in this recording.",
        )
    return CandidateResult(candidates=candidates, found=True)


def _find_highlights(
    report: AnalysisReport,
    settings: Settings,
    query: str,
    wanted: int,
    llm: LLMClient | None,
    bus: EventBus | None,
) -> CandidateResult:
    maximum = settings.clips.highlight_max_seconds

    if llm is None:
        existing = [
            candidate.model_copy(
                update={
                    "end": min(candidate.end, candidate.start + maximum),
                    "summary": candidate.summary
                    or _fallback_summary(report, candidate.start, candidate.end),
                    "source": "highlight",
                }
            )
            for candidate in report.clip_candidates
            if candidate.duration >= 5
        ]
        return CandidateResult(
            candidates=_dedupe(existing)[:wanted],
            warnings=["No language model available — using the moments found during analysis."],
        )

    if bus:
        bus.progress(STAGE, None, "Looking for shareable moments")

    request_line = f"The user is specifically after: {query}\n" if query else ""
    user = HIGHLIGHT_FIND_USER.format(
        title=report.title or report.media.filename,
        duration=heuristics.format_duration(report.media.duration_s),
        request_line=request_line,
        max_seconds=maximum,
        transcript=_render_transcript(report.transcript.segments, _PROMPT_TRANSCRIPT_CHARS),
        max_candidates=wanted,
    )

    try:
        payload = llm.complete_json(CLIP_FIND_SYSTEM, user, temperature=0.25)
    except LLMError as exc:
        return CandidateResult(
            candidates=_dedupe(list(report.clip_candidates))[:wanted],
            warnings=[f"Falling back to the analysis results: {exc}"],
        )

    entries = payload.get("candidates") if isinstance(payload, dict) else None
    candidates: list[ClipCandidate] = []
    for entry in entries or []:
        candidate = _to_candidate(
            report, entry, source="highlight", pad=settings.clips.pad_seconds
        )
        if candidate is None:
            continue
        if candidate.duration > maximum:
            candidate.end = candidate.start + maximum
        candidates.append(candidate)

    result = CandidateResult(candidates=_dedupe(candidates)[:wanted], found=bool(candidates))
    if not candidates:
        result.note = "No self-contained highlights were found in this recording."
    elif len(result.candidates) < wanted:
        # Saying so is better than silently returning fewer, which reads as a bug.
        result.warnings.append(
            f"Found {len(result.candidates)} genuinely standalone moment(s) rather than "
            f"the {wanted} requested — the rest of the recording does not stand on its own."
        )
    return result


# --- rendering ---------------------------------------------------------------
def render_selection(
    project: Project,
    report: AnalysisReport,
    clips: list[ClipCandidate],
    settings: Settings,
    ffmpeg_bin: str,
    bus: EventBus,
    *,
    combine: bool = False,
    reframe: bool = False,
    kind: ArtifactKind = ArtifactKind.CLIP,
    prefix: str = "clip",
    title: str = "",
    output_name: str = "",
    source_path: Path | None = None,
    source_has_audio: bool | None = None,
) -> list[Path]:
    """Render the chosen candidates, either as separate files or joined into one."""
    from clipdesk.actions.render import render_clip, render_spans

    if not clips:
        raise ValueError("Nothing selected to render.")

    source = source_path or project.source_path
    has_audio = report.media.has_audio if source_has_audio is None else source_has_audio

    if combine and len(clips) > 1:
        ordered = sorted(clips, key=lambda clip: clip.start)
        spans = [(clip.start, clip.end) for clip in ordered]
        total = sum(end - start for start, end in spans)
        name = output_name or f"{prefix}-{slugify(title or ordered[0].title, fallback='selection')}.mp4"
        destination = project.output_path(name)

        bus.stage_start(
            STAGE,
            f"Joining {len(spans)} sections into one clip "
            f"({heuristics.format_duration(total)})",
        )
        render_spans(
            source,
            spans,
            destination,
            has_audio=has_audio,
            render=settings.render,
            ffmpeg_bin=ffmpeg_bin,
            on_progress=lambda fraction: bus.progress(STAGE, fraction, "Encoding"),
            # Every join here is between unrelated parts of the recording, so
            # each one gets a transition rather than only the large gaps.
            fade_seconds=settings.clips.join_transition_seconds,
            fade_min_gap=0.0,
        )
        project.add_artifact(
            kind,
            f"{title or ordered[0].title} — {len(spans)} sections "
            f"({heuristics.format_duration(total)})",
            destination,
            duration_s=total,
            meta={"sections": str(len(spans)), "combined": "true"},
        )
        bus.stage_end(STAGE, "Clip ready")
        return [destination]

    written: list[Path] = []
    total_clips = len(clips)
    bus.stage_start(STAGE, f"Rendering {total_clips} clip(s)")

    for index, clip in enumerate(clips, start=1):
        if output_name:
            stem = Path(output_name).stem
            suffix = Path(output_name).suffix or ".mp4"
            name = output_name if total_clips == 1 else f"{stem}-{index:02d}{suffix}"
        else:
            name = f"{prefix}-{index:02d}-{slugify(clip.title, fallback='clip')}.mp4"
        destination = project.output_path(name)
        bus.progress(
            STAGE,
            (index - 1) / total_clips,
            f"Rendering {index}/{total_clips}: {clip.title} "
            f"({heuristics.format_duration(clip.duration)})",
        )

        def report_progress(fraction: float, position: int = index) -> None:
            bus.progress(
                STAGE,
                ((position - 1) + fraction) / total_clips,
                f"Rendering {position}/{total_clips}",
            )

        render_clip(
            source,
            (clip.start, clip.end),
            destination,
            has_audio=has_audio,
            render=settings.render,
            ffmpeg_bin=ffmpeg_bin,
            reframe=reframe,
            on_progress=report_progress,
        )
        project.add_artifact(
            kind,
            f"{clip.title} ({heuristics.format_duration(clip.duration)})",
            destination,
            duration_s=clip.duration,
            meta={
                "start": f"{clip.start:.3f}",
                "end": f"{clip.end:.3f}",
                "summary": clip.summary[:400],
            },
        )
        written.append(destination)

    bus.stage_end(STAGE, f"Rendered {len(written)} clip(s)")
    return written
