"""Turn a transcript into an :class:`AnalysisReport`.

The transcript is the only input. Windows of it go to the model in parallel
(bounded, because a Copilot quota is shared), the per-window verdicts are merged
back onto one timeline, and a final pass writes the whole-video overview.

Every LLM result is treated as a suggestion, not gospel: ids are validated
against the transcript, ranges are clamped, and anything the model did not cover
falls back to the deterministic score.
"""

from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from clipdesk.analysis import heuristics
from clipdesk.analysis.prompts import (
    ANALYST_SYSTEM,
    OVERVIEW_SYSTEM,
    OVERVIEW_USER_TEMPLATE,
    WINDOW_USER_TEMPLATE,
)
from clipdesk.analysis.windows import TranscriptWindow, build_windows, format_timestamp
from clipdesk.config import Settings
from clipdesk.events import EventBus
from clipdesk.llm import LLMClient, LLMError, LLMUnavailableError
from clipdesk.models import (
    ActionItem,
    AnalysisReport,
    Chapter,
    ClipCandidate,
    Decision,
    MediaInfo,
    SegmentAnalysis,
    SegmentKind,
    SilenceSpan,
    Transcript,
    TranscriptSegment,
)

STAGE = "analyze"


@dataclass(slots=True)
class WindowResult:
    index: int
    segments: list[dict[str, Any]] = field(default_factory=list)
    chapters: list[dict[str, Any]] = field(default_factory=list)
    clips: list[dict[str, Any]] = field(default_factory=list)
    action_items: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""


# --- coercion helpers --------------------------------------------------------
def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _as_kind(value: Any) -> SegmentKind:
    try:
        return SegmentKind(str(value).strip().lower())
    except ValueError:
        return SegmentKind.ON_TOPIC


def _str_list(value: Any, limit: int = 12) -> list[str]:
    items = [str(item).strip() for item in _as_list(value) if str(item).strip()]
    return items[:limit]


class TranscriptAnalyzer:
    def __init__(self, settings: Settings, llm: LLMClient | None, bus: EventBus) -> None:
        self.settings = settings
        self.llm = llm
        self.bus = bus
        self.warnings: list[str] = []

    # --- public API --------------------------------------------------------
    def analyze(
        self,
        *,
        project_id: str,
        media: MediaInfo,
        transcript: Transcript,
        silences: list[SilenceSpan],
        title: str = "",
    ) -> AnalysisReport:
        segments = transcript.segments
        duration = media.duration_s or transcript.duration_s
        config = self.settings.analysis

        analyses = heuristics.score_segments(
            segments, filler_words=config.filler_words, total_duration=duration
        )
        by_id = {segment.id: segment for segment in segments}
        analysis_by_id = {analysis.segment_id: analysis for analysis in analyses}

        chapters: list[Chapter] = []
        clips: list[ClipCandidate] = []
        action_items: list[ActionItem] = []
        decisions: list[Decision] = []

        if self.llm is not None and segments:
            windows = build_windows(
                segments,
                window_chars=config.window_chars,
                overlap_chars=config.window_overlap_chars,
            )
            results = self._run_windows(windows, title or media.filename)
            for result in results:
                if result.error:
                    continue
                self._apply_segments(result.segments, by_id, analysis_by_id)
                chapters.extend(self._to_chapters(result.chapters, by_id))
                clips.extend(self._to_clips(result.clips, by_id))
                action_items.extend(self._to_action_items(result.action_items, by_id))
                decisions.extend(self._to_decisions(result.decisions, by_id))
        elif self.llm is None:
            self.warnings.append(
                "No language model was available, so this analysis used transcript "
                "heuristics only. Chapters and clip suggestions are missing."
            )

        chapters = _dedupe_chapters(chapters, duration)
        clips = _dedupe_clips(clips)
        if not chapters and segments:
            chapters = _chapters_from_analyses(analyses, by_id)

        overview = self._overview(title or media.filename, duration, chapters)

        keep_spans = heuristics.build_keep_spans(
            analyses,
            silences,
            keep_threshold=config.keep_importance_threshold,
            pad_seconds=self.settings.cleanup.pad_seconds,
            merge_gap_seconds=self.settings.cleanup.merge_gap_seconds,
            keep_gap_seconds=self.settings.cleanup.keep_gap_seconds,
            total_duration=duration,
        )

        if not clips:
            clips = _clips_from_chapters(chapters)

        return AnalysisReport(
            project_id=project_id,
            title=overview.get("title") or title or media.filename,
            media=media,
            transcript=transcript,
            silences=silences,
            summary=overview.get("summary", ""),
            abstract=overview.get("abstract", ""),
            keywords=_str_list(overview.get("keywords"), 16),
            chapters=chapters,
            segment_analyses=analyses,
            clip_candidates=clips,
            cleanup_keep_spans=keep_spans,
            action_items=_dedupe_action_items(action_items),
            decisions=_dedupe_decisions(decisions),
            llm_provider=self.llm.key if self.llm else "",
            llm_model=(overview.get("_model") or "") if self.llm else "",
            warnings=self.warnings,
        )

    # --- windows -----------------------------------------------------------
    def _run_windows(self, windows: list[TranscriptWindow], title: str) -> list[WindowResult]:
        total = len(windows)
        if total == 0:
            return []
        self.bus.stage_start(STAGE, f"Analysing {total} transcript window(s)")

        results: list[WindowResult] = []
        completed = 0
        workers = max(1, min(self.settings.analysis.max_concurrency, total))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._analyze_window, window, title, total): window
                for window in windows
            }
            for future in as_completed(futures):
                window = futures[future]
                try:
                    results.append(future.result())
                except LLMUnavailableError as exc:
                    # The model went away entirely — stop asking and degrade.
                    self.warnings.append(str(exc))
                    for pending in futures:
                        pending.cancel()
                    break
                except Exception as exc:  # noqa: BLE001
                    self.warnings.append(f"Window {window.index + 1} failed: {exc}")
                    results.append(WindowResult(window.index, error=str(exc)))
                completed += 1
                self.bus.progress(
                    STAGE, completed / total, f"Analysed {completed}/{total} windows"
                )

        results.sort(key=lambda result: result.index)
        self.bus.stage_end(STAGE, f"Analysed {completed}/{total} windows")
        return results

    def _analyze_window(
        self, window: TranscriptWindow, title: str, window_count: int
    ) -> WindowResult:
        assert self.llm is not None
        context = (
            "Earlier windows covered the material before this point; do not "
            "re-summarise it.\n"
            if window.index > 0
            else ""
        )
        user = WINDOW_USER_TEMPLATE.format(
            title=title or "(untitled)",
            window_index=window.index + 1,
            window_count=window_count,
            start=format_timestamp(window.start),
            end=format_timestamp(window.end),
            context=context,
            transcript=window.render(),
        )
        payload = self.llm.complete_json(ANALYST_SYSTEM, user, temperature=0.1)
        if not isinstance(payload, dict):
            raise LLMError("Expected a JSON object for the window analysis.")

        return WindowResult(
            index=window.index,
            segments=_as_list(payload.get("segments")),
            chapters=_as_list(payload.get("chapters")),
            clips=_as_list(payload.get("clips")),
            action_items=_as_list(payload.get("action_items")),
            decisions=_as_list(payload.get("decisions")),
        )

    # --- merge -------------------------------------------------------------
    def _apply_segments(
        self,
        entries: list[dict[str, Any]],
        by_id: dict[int, TranscriptSegment],
        analysis_by_id: dict[int, SegmentAnalysis],
    ) -> None:
        """Blend the model's verdict with the heuristic score.

        The model is weighted more heavily because it is the only thing that can
        tell "off-topic chat" from "the important bit", but the heuristic keeps a
        vote so a hallucinated 0.9 on three seconds of "um, so, yeah" does not
        survive.
        """
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            from_id = _as_int(entry.get("from_id"))
            to_id = _as_int(entry.get("to_id", entry.get("from_id")))
            if from_id is None:
                continue
            if to_id is None or to_id < from_id:
                to_id = from_id

            kind = _as_kind(entry.get("kind"))
            topic = str(entry.get("topic") or "").strip() or None
            reason = str(entry.get("reason") or "").strip()[:120]
            llm_importance = _clamp01(_as_float(entry.get("importance"), 0.5))

            for segment_id in range(from_id, to_id + 1):
                analysis = analysis_by_id.get(segment_id)
                if analysis is None or segment_id not in by_id:
                    continue
                heuristic = (
                    analysis.heuristic_importance
                    if analysis.heuristic_importance is not None
                    else analysis.importance
                )
                analysis.kind = kind
                analysis.topic = topic
                analysis.reason = reason
                analysis.llm_importance = llm_importance
                analysis.importance = round(
                    _clamp01(0.7 * llm_importance + 0.3 * heuristic), 3
                )

    def _span(
        self, entry: dict[str, Any], by_id: dict[int, TranscriptSegment]
    ) -> tuple[float, float] | None:
        from_id = _as_int(entry.get("from_id"))
        to_id = _as_int(entry.get("to_id", entry.get("from_id")))
        if from_id is None:
            return None
        if to_id is None or to_id < from_id:
            to_id = from_id
        start_segment = by_id.get(from_id)
        end_segment = by_id.get(to_id)
        if start_segment is None:
            # The model referenced an id outside the window; fall back to the
            # nearest real segment rather than dropping the whole entry.
            candidates = [sid for sid in by_id if sid >= from_id]
            if not candidates:
                return None
            start_segment = by_id[min(candidates)]
        if end_segment is None:
            candidates = [sid for sid in by_id if sid <= to_id]
            end_segment = by_id[max(candidates)] if candidates else start_segment
        start, end = start_segment.start, max(end_segment.end, start_segment.end)
        return (start, end) if end > start else None

    def _to_chapters(
        self, entries: list[dict[str, Any]], by_id: dict[int, TranscriptSegment]
    ) -> list[Chapter]:
        chapters: list[Chapter] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            span = self._span(entry, by_id)
            if span is None:
                continue
            chapters.append(
                Chapter(
                    title=str(entry.get("title") or "Untitled section").strip()[:120],
                    start=span[0],
                    end=span[1],
                    summary=str(entry.get("summary") or "").strip(),
                    key_points=_str_list(entry.get("key_points"), 10),
                    keywords=_str_list(entry.get("keywords"), 10),
                )
            )
        return chapters

    def _to_clips(
        self, entries: list[dict[str, Any]], by_id: dict[int, TranscriptSegment]
    ) -> list[ClipCandidate]:
        clips: list[ClipCandidate] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            span = self._span(entry, by_id)
            if span is None:
                continue
            clips.append(
                ClipCandidate(
                    title=str(entry.get("title") or "Clip").strip()[:120],
                    start=span[0],
                    end=span[1],
                    score=_clamp01(_as_float(entry.get("score"), 0.5)),
                    hook=str(entry.get("hook") or "").strip()[:200],
                    reason=str(entry.get("reason") or "").strip()[:200],
                    topics=_str_list(entry.get("topics"), 6),
                )
            )
        return clips

    def _to_action_items(
        self, entries: list[dict[str, Any]], by_id: dict[int, TranscriptSegment]
    ) -> list[ActionItem]:
        items: list[ActionItem] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("text") or "").strip()
            if not text:
                continue
            segment = by_id.get(_as_int(entry.get("id")) or -1)
            owner = entry.get("owner")
            items.append(
                ActionItem(
                    text=text[:300],
                    owner=str(owner).strip() if owner else None,
                    time=segment.start if segment else None,
                )
            )
        return items

    def _to_decisions(
        self, entries: list[dict[str, Any]], by_id: dict[int, TranscriptSegment]
    ) -> list[Decision]:
        decisions: list[Decision] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("text") or "").strip()
            if not text:
                continue
            segment = by_id.get(_as_int(entry.get("id")) or -1)
            decisions.append(Decision(text=text[:300], time=segment.start if segment else None))
        return decisions

    # --- overview ----------------------------------------------------------
    def _overview(
        self, title: str, duration: float, chapters: list[Chapter]
    ) -> dict[str, Any]:
        if self.llm is None or not chapters:
            return {}

        lines = [
            f"- {format_timestamp(chapter.start)}–{format_timestamp(chapter.end)} "
            f"{chapter.title}: {chapter.summary}"
            for chapter in chapters
        ]
        user = OVERVIEW_USER_TEMPLATE.format(
            title=title or "(untitled)",
            duration=heuristics.format_duration(duration),
            chapters="\n".join(lines)[:12000],
        )
        self.bus.progress(STAGE, None, "Writing the overview")
        try:
            payload = self.llm.complete_json(OVERVIEW_SYSTEM, user, temperature=0.2)
        except LLMError as exc:
            self.warnings.append(f"Could not write the overview: {exc}")
            return {}
        if not isinstance(payload, dict):
            return {}
        payload["_model"] = self.llm.model
        return payload


# --- merge helpers -----------------------------------------------------------
def _overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def _dedupe_chapters(chapters: list[Chapter], duration: float) -> list[Chapter]:
    """Overlapping windows produce the same chapter twice; keep the richer copy
    and make the surviving list non-overlapping and in order."""
    if not chapters:
        return []
    ordered = sorted(chapters, key=lambda chapter: (chapter.start, -chapter.duration))
    kept: list[Chapter] = []
    for chapter in ordered:
        if chapter.duration < 1.0:
            continue
        duplicate = None
        for existing in kept:
            shared = _overlap((chapter.start, chapter.end), (existing.start, existing.end))
            shorter = min(chapter.duration, existing.duration)
            if shorter > 0 and shared / shorter > 0.6:
                duplicate = existing
                break
        if duplicate is None:
            kept.append(chapter)
            continue
        # Keep whichever carries more detail.
        if len(chapter.summary) + len(chapter.key_points) > len(duplicate.summary) + len(
            duplicate.key_points
        ):
            duplicate.title = chapter.title
            duplicate.summary = chapter.summary
            duplicate.key_points = chapter.key_points
            duplicate.keywords = chapter.keywords
        duplicate.start = min(duplicate.start, chapter.start)
        duplicate.end = max(duplicate.end, chapter.end)

    kept.sort(key=lambda chapter: chapter.start)
    for index in range(len(kept) - 1):
        kept[index].end = min(kept[index].end, kept[index + 1].start)
    if duration and kept:
        kept[-1].end = min(max(kept[-1].end, kept[-1].start + 1.0), duration)
    return [chapter for chapter in kept if chapter.duration >= 1.0]


def _dedupe_clips(clips: list[ClipCandidate]) -> list[ClipCandidate]:
    ordered = sorted(clips, key=lambda clip: (-clip.score, clip.start))
    kept: list[ClipCandidate] = []
    for clip in ordered:
        if clip.duration < 3.0:
            continue
        if any(
            _overlap((clip.start, clip.end), (existing.start, existing.end))
            / min(clip.duration, existing.duration)
            > 0.5
            for existing in kept
        ):
            continue
        kept.append(clip)
    kept.sort(key=lambda clip: clip.start)
    return kept


def _dedupe_action_items(items: list[ActionItem]) -> list[ActionItem]:
    seen: set[str] = set()
    unique: list[ActionItem] = []
    for item in items:
        key = item.text.lower().strip()
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _dedupe_decisions(decisions: list[Decision]) -> list[Decision]:
    seen: set[str] = set()
    unique: list[Decision] = []
    for decision in decisions:
        key = decision.text.lower().strip()
        if key in seen:
            continue
        seen.add(key)
        unique.append(decision)
    return unique


def _chapters_from_analyses(
    analyses: list[SegmentAnalysis], by_id: dict[int, TranscriptSegment]
) -> list[Chapter]:
    """A crude chapter list for when there was no model: break every ~5 minutes."""
    if not analyses:
        return []
    chapters: list[Chapter] = []
    window = 300.0
    start = analyses[0].start
    bucket: list[SegmentAnalysis] = []

    def flush(end: float) -> None:
        if not bucket:
            return
        words = " ".join(
            by_id[item.segment_id].text for item in bucket if item.segment_id in by_id
        )
        keywords = [word for word, _ in heuristics.keyword_counts(
            [by_id[item.segment_id] for item in bucket if item.segment_id in by_id]
        ).most_common(6)]
        chapters.append(
            Chapter(
                title=f"{format_timestamp(bucket[0].start)} onward",
                start=bucket[0].start,
                end=end,
                summary=words[:280],
                keywords=keywords,
            )
        )

    for analysis in analyses:
        if analysis.start - start >= window and bucket:
            flush(bucket[-1].end)
            bucket = []
            start = analysis.start
        bucket.append(analysis)
    if bucket:
        flush(bucket[-1].end)
    return chapters


def _clips_from_chapters(chapters: Iterable[Chapter]) -> list[ClipCandidate]:
    return [
        ClipCandidate(
            title=chapter.title,
            start=chapter.start,
            end=chapter.end,
            score=0.5,
            reason="derived from chapter boundaries",
        )
        for chapter in chapters
    ]
