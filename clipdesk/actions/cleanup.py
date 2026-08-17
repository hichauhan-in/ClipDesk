"""The cleaned-up cut: the same video with the dead weight removed.

What counts as dead weight is a choice, not a fixed rule — a design review may
want the Q&A kept while a training recording does not — so the keep list is
recomputed from the analysis against the options the user picked rather than
being baked in at analysis time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from clipdesk.analysis.heuristics import build_keep_spans, format_duration
from clipdesk.config import Settings
from clipdesk.events import EventBus
from clipdesk.models import AnalysisReport, ArtifactKind, KeepSpan, SegmentKind
from clipdesk.store import Project

STAGE = "cleanup"


@dataclass(slots=True)
class CleanupOptions:
    remove_silence: bool = True
    remove_filler: bool = True
    remove_off_topic: bool = True
    remove_admin: bool = True
    remove_qa: bool = False
    remove_intro: bool = False
    remove_outro: bool = False
    #: Overrides ``analysis.keep_importance_threshold`` when set.
    keep_threshold: float | None = None
    smooth_cuts: bool | None = None

    def dropped_kinds(self) -> set[SegmentKind]:
        kinds: set[SegmentKind] = set()
        if self.remove_filler:
            kinds.add(SegmentKind.FILLER)
        if self.remove_off_topic:
            kinds.add(SegmentKind.OFF_TOPIC)
        if self.remove_admin:
            kinds.add(SegmentKind.ADMIN)
        if self.remove_qa:
            kinds.add(SegmentKind.QA)
        if self.remove_intro:
            kinds.add(SegmentKind.INTRO)
        if self.remove_outro:
            kinds.add(SegmentKind.OUTRO)
        return kinds


# Categories where a misclassification is cheap: filler and logistics are short
# and low-value even when the label is wrong. Everything else gets the
# high-importance protection below.
_ALWAYS_SAFE_TO_DROP = {SegmentKind.FILLER, SegmentKind.ADMIN}

# Q&A is dropped unconditionally when asked for. It is an explicit, well-understood
# choice by the user ("remove the Q&A"), not a judgement call by the classifier —
# and a half-removed Q&A section is worse than either extreme. The UI says so.
_UNCONDITIONAL = {SegmentKind.QA}


def plan_cleanup(
    report: AnalysisReport, settings: Settings, options: CleanupOptions
) -> list[KeepSpan]:
    """The edit decision list, without rendering anything.

    Exposed on its own so the UI can show "this will remove 12 minutes (31%)"
    before the user commits to an encode.

    Category alone does not remove a segment. A stretch the model scored highly
    survives even if it was labelled off-topic, because classification is
    imperfect and losing a valuable tangent — the aside that turns out to be the
    most useful thing in the meeting — costs far more than leaving a few extra
    seconds in.
    """
    dropped = options.dropped_kinds()
    threshold = (
        options.keep_threshold
        if options.keep_threshold is not None
        else settings.analysis.keep_importance_threshold
    )
    protect = settings.cleanup.protect_importance

    # Work on copies so previewing different options never mutates the report.
    analyses = [analysis.model_copy(deep=True) for analysis in report.segment_analyses]
    for analysis in analyses:
        if analysis.kind not in dropped:
            continue
        if analysis.kind in _UNCONDITIONAL or analysis.kind in _ALWAYS_SAFE_TO_DROP or analysis.importance < protect:
            analysis.importance = 0.0

    keep_gap = (
        settings.cleanup.keep_gap_seconds if options.remove_silence else float("inf")
    )
    return build_keep_spans(
        analyses,
        report.silences if options.remove_silence else [],
        keep_threshold=threshold,
        pad_seconds=settings.cleanup.pad_seconds,
        merge_gap_seconds=settings.cleanup.merge_gap_seconds,
        keep_gap_seconds=keep_gap,
        total_duration=report.media.duration_s,
        # The category decision is already baked into the importances above,
        # including the protection for high-value segments. Letting the span
        # builder apply its own category rule as well would undo that.
        drop_kinds=(),
    )


def explain_cleanup(
    report: AnalysisReport, settings: Settings, options: CleanupOptions
) -> dict[str, object]:
    """A per-category breakdown of what the current options would remove.

    Lets the UI answer "what am I actually losing?" instead of only showing a
    total, which is the question people ask before trusting an automatic cut.
    """
    dropped = options.dropped_kinds()
    protect = settings.cleanup.protect_importance
    removed: dict[str, float] = {}
    protected_seconds = 0.0

    for analysis in report.segment_analyses:
        duration = max(0.0, analysis.end - analysis.start)
        if analysis.kind not in dropped:
            continue
        if (
            analysis.kind not in _UNCONDITIONAL
            and analysis.kind not in _ALWAYS_SAFE_TO_DROP
            and analysis.importance >= protect
        ):
            protected_seconds += duration
            continue
        removed[analysis.kind.value] = removed.get(analysis.kind.value, 0.0) + duration

    return {
        "removed_by_kind": {kind: round(seconds, 1) for kind, seconds in sorted(removed.items())},
        "protected_seconds": round(protected_seconds, 1),
        "protect_importance": protect,
    }


def render_cleanup(
    project: Project,
    report: AnalysisReport,
    settings: Settings,
    options: CleanupOptions,
    ffmpeg_bin: str,
    bus: EventBus,
    output_name: str = "cleaned.mp4",
) -> Path:
    from clipdesk.actions.render import render_spans

    spans = plan_cleanup(report, settings, options)
    if not spans:
        raise ValueError(
            "Those settings would remove the entire video. Lower the importance "
            "threshold or keep more of the segment types."
        )

    kept = sum(span.duration for span in spans)
    original = report.media.duration_s
    removed = max(0.0, original - kept)
    bus.stage_start(
        STAGE,
        f"Rendering the clean cut — {format_duration(kept)} kept, "
        f"{format_duration(removed)} removed",
    )

    smooth = (
        settings.cleanup.smooth_cuts if options.smooth_cuts is None else options.smooth_cuts
    )
    destination = project.output_path(output_name)

    render_spans(
        project.source_path,
        [(span.start, span.end) for span in spans],
        destination,
        has_audio=report.media.has_audio,
        render=settings.render,
        ffmpeg_bin=ffmpeg_bin,
        on_progress=lambda fraction: bus.progress(STAGE, fraction, "Encoding"),
        fade_seconds=settings.cleanup.fade_seconds if smooth else 0.0,
        fade_min_gap=settings.cleanup.fade_min_gap_seconds,
    )

    percent = (removed / original * 100) if original else 0.0
    project.add_artifact(
        ArtifactKind.CLEANUP,
        f"Clean cut ({format_duration(kept)}, {percent:.0f}% removed)",
        destination,
        duration_s=kept,
        meta={
            "original_duration": f"{original:.1f}",
            "kept_duration": f"{kept:.1f}",
            "segments_kept": str(len(spans)),
        },
    )
    bus.stage_end(STAGE, f"Clean cut ready — {format_duration(kept)}")
    return destination
