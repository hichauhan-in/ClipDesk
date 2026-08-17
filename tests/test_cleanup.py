"""Cleanup category handling.

The rule under test: a category tick alone must not remove a segment the model
rated highly. Classification is imperfect, and losing the aside that turned out
to be the most useful thing in the meeting costs far more than leaving a few
extra seconds in. Q&A is the deliberate exception.
"""

import pytest

from clipdesk.actions.cleanup import CleanupOptions, explain_cleanup, plan_cleanup
from clipdesk.config import Settings
from clipdesk.models import AnalysisReport, MediaInfo, SegmentAnalysis, SegmentKind


def build_report(*analyses: SegmentAnalysis, duration: float = 100.0) -> AnalysisReport:
    return AnalysisReport(
        project_id="test",
        media=MediaInfo(path="x.mp4", duration_s=duration),
        segment_analyses=list(analyses),
    )


def segment(segment_id, start, end, kind, importance):
    return SegmentAnalysis(
        segment_id=segment_id, start=start, end=end, kind=kind, importance=importance
    )


@pytest.fixture
def settings():
    return Settings()


def kept_spans(report, settings, options):
    return [(round(s.start, 2), round(s.end, 2)) for s in plan_cleanup(report, settings, options)]


# --- protection --------------------------------------------------------------
def test_low_value_off_topic_is_removed(settings):
    report = build_report(
        segment(0, 0, 40, SegmentKind.ON_TOPIC, 0.8),
        segment(1, 40, 60, SegmentKind.OFF_TOPIC, 0.2),
        segment(2, 60, 100, SegmentKind.ON_TOPIC, 0.8),
    )
    spans = kept_spans(report, settings, CleanupOptions(remove_silence=False))
    assert len(spans) == 2
    assert spans[0][1] < 45 and spans[1][0] > 55


def test_high_value_off_topic_survives(settings):
    # The tangent the model rated 0.9 is exactly the thing that must not be lost.
    report = build_report(
        segment(0, 0, 40, SegmentKind.ON_TOPIC, 0.8),
        segment(1, 40, 60, SegmentKind.OFF_TOPIC, 0.9),
        segment(2, 60, 100, SegmentKind.ON_TOPIC, 0.8),
    )
    spans = kept_spans(report, settings, CleanupOptions(remove_silence=False))
    assert len(spans) == 1
    assert spans[0][0] == 0.0


def test_filler_is_removed_even_when_scored_highly(settings):
    # A wrong "filler" label costs a second or two, so it is not worth protecting.
    report = build_report(
        segment(0, 0, 40, SegmentKind.ON_TOPIC, 0.8),
        segment(1, 40, 60, SegmentKind.FILLER, 0.95),
        segment(2, 60, 100, SegmentKind.ON_TOPIC, 0.8),
    )
    assert len(kept_spans(report, settings, CleanupOptions(remove_silence=False))) == 2


def test_admin_is_removed_even_when_scored_highly(settings):
    report = build_report(
        segment(0, 0, 40, SegmentKind.ADMIN, 0.95),
        segment(1, 40, 100, SegmentKind.ON_TOPIC, 0.8),
    )
    spans = kept_spans(report, settings, CleanupOptions(remove_silence=False))
    assert spans[0][0] >= 39.0


def test_qa_removal_is_unconditional(settings):
    # Ticking "remove Q&A" is an explicit instruction, and a half-removed Q&A
    # section is worse than either extreme. The UI carries the warning.
    report = build_report(
        segment(0, 0, 40, SegmentKind.ON_TOPIC, 0.8),
        segment(1, 40, 60, SegmentKind.QA, 0.99),
        segment(2, 60, 100, SegmentKind.ON_TOPIC, 0.8),
    )
    spans = kept_spans(report, settings, CleanupOptions(remove_silence=False, remove_qa=True))
    assert len(spans) == 2


def test_untick_a_category_and_it_is_kept(settings):
    report = build_report(
        segment(0, 0, 50, SegmentKind.OFF_TOPIC, 0.5),
        segment(1, 50, 100, SegmentKind.ON_TOPIC, 0.8),
    )
    spans = kept_spans(
        report, settings, CleanupOptions(remove_silence=False, remove_off_topic=False)
    )
    assert len(spans) == 1


def test_intro_and_outro_respect_the_toggles(settings):
    report = build_report(
        segment(0, 0, 20, SegmentKind.INTRO, 0.4),
        segment(1, 20, 80, SegmentKind.ON_TOPIC, 0.8),
        segment(2, 80, 100, SegmentKind.OUTRO, 0.4),
    )
    assert len(kept_spans(report, settings, CleanupOptions(remove_silence=False))) == 1

    trimmed = kept_spans(
        report,
        settings,
        CleanupOptions(remove_silence=False, remove_intro=True, remove_outro=True),
    )
    assert trimmed[0][0] >= 19.0
    assert trimmed[-1][1] <= 81.0


# --- explanation -------------------------------------------------------------
def test_explanation_reports_removed_time_by_category(settings):
    report = build_report(
        segment(0, 0, 40, SegmentKind.ON_TOPIC, 0.8),
        segment(1, 40, 50, SegmentKind.FILLER, 0.1),
        segment(2, 50, 70, SegmentKind.ADMIN, 0.1),
        segment(3, 70, 100, SegmentKind.ON_TOPIC, 0.8),
    )
    detail = explain_cleanup(report, settings, CleanupOptions())
    assert detail["removed_by_kind"] == {"admin": 20.0, "filler": 10.0}
    assert detail["protected_seconds"] == 0.0


def test_explanation_reports_protected_time(settings):
    report = build_report(
        segment(0, 0, 40, SegmentKind.ON_TOPIC, 0.8),
        segment(1, 40, 60, SegmentKind.OFF_TOPIC, 0.9),
    )
    detail = explain_cleanup(report, settings, CleanupOptions())
    assert detail["protected_seconds"] == 20.0
    assert detail["removed_by_kind"] == {}
