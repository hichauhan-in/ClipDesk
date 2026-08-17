"""Span arithmetic — the maths every render depends on."""

import pytest

from clipdesk.analysis.heuristics import (
    build_keep_spans,
    filler_ratio,
    merge_spans,
    subtract_spans,
)
from clipdesk.models import SegmentAnalysis, SegmentKind, SilenceSpan

FILLERS = ["um", "uh", "you know", "basically"]


def analysis(segment_id, start, end, importance, kind=SegmentKind.ON_TOPIC):
    return SegmentAnalysis(
        segment_id=segment_id, start=start, end=end, importance=importance, kind=kind
    )


# --- merge / subtract --------------------------------------------------------
def test_merge_spans_sorts_and_coalesces_overlaps():
    assert merge_spans([(5, 8), (0, 3), (2, 6)]) == [(0, 8)]


def test_merge_spans_respects_the_gap_budget():
    assert merge_spans([(0, 2), (2.5, 4)], merge_gap=1.0) == [(0, 4)]
    assert merge_spans([(0, 2), (2.5, 4)], merge_gap=0.1) == [(0, 2), (2.5, 4)]


def test_merge_spans_drops_empty_and_inverted():
    assert merge_spans([(3, 3), (5, 2)]) == []


def test_subtract_removes_an_interior_hole():
    assert subtract_spans([(0, 10)], [(4, 6)]) == [(0, 4), (6, 10)]


def test_subtract_handles_holes_at_the_edges():
    assert subtract_spans([(0, 10)], [(0, 3)]) == [(3, 10)]
    assert subtract_spans([(0, 10)], [(8, 20)]) == [(0, 8)]


def test_subtract_can_erase_a_span_entirely():
    assert subtract_spans([(2, 5)], [(0, 9)]) == []


def test_subtract_with_no_holes_is_identity():
    assert subtract_spans([(0, 4), (6, 9)], []) == [(0, 4), (6, 9)]


# --- filler ------------------------------------------------------------------
def test_filler_ratio_counts_single_and_multi_word_fillers():
    assert filler_ratio("um uh", FILLERS) == pytest.approx(1.0)
    assert filler_ratio("The retry policy backs off exponentially", FILLERS) == 0.0
    assert filler_ratio("", FILLERS) == 1.0


def test_filler_ratio_is_bounded():
    assert 0.0 <= filler_ratio("you know, um, basically, you know", FILLERS) <= 1.0


# --- keep spans --------------------------------------------------------------
def test_low_importance_segments_are_dropped():
    analyses = [
        analysis(0, 0, 5, 0.9),
        analysis(1, 5, 10, 0.1),
        analysis(2, 10, 15, 0.8),
    ]
    spans = build_keep_spans(
        analyses,
        [],
        keep_threshold=0.4,
        pad_seconds=0.0,
        merge_gap_seconds=0.0,
        keep_gap_seconds=1.0,
        total_duration=15,
    )
    assert [(span.start, span.end) for span in spans] == [(0, 5), (10, 15)]
    assert analyses[1].keep is False


def test_filler_is_dropped_regardless_of_importance():
    analyses = [analysis(0, 0, 5, 0.99, SegmentKind.FILLER), analysis(1, 5, 10, 0.6)]
    spans = build_keep_spans(
        analyses,
        [],
        keep_threshold=0.4,
        pad_seconds=0.0,
        merge_gap_seconds=0.0,
        keep_gap_seconds=1.0,
        total_duration=10,
    )
    assert [(span.start, span.end) for span in spans] == [(5, 10)]


def test_adjacent_kept_segments_merge_into_one_span():
    analyses = [analysis(0, 0, 5, 0.8), analysis(1, 5, 10, 0.8)]
    spans = build_keep_spans(
        analyses,
        [],
        keep_threshold=0.4,
        pad_seconds=0.0,
        merge_gap_seconds=0.5,
        keep_gap_seconds=1.0,
        total_duration=10,
    )
    assert len(spans) == 1
    assert (spans[0].start, spans[0].end) == (0, 10)


def test_short_silences_are_left_alone():
    analyses = [analysis(0, 0, 20, 0.8)]
    spans = build_keep_spans(
        analyses,
        [SilenceSpan(start=8, end=8.6)],
        keep_threshold=0.4,
        pad_seconds=0.0,
        merge_gap_seconds=0.0,
        keep_gap_seconds=1.0,
        total_duration=20,
    )
    assert len(spans) == 1


def test_long_silence_is_cut_but_leaves_breathing_room():
    analyses = [analysis(0, 0, 30, 0.8)]
    spans = build_keep_spans(
        analyses,
        [SilenceSpan(start=10, end=20)],
        keep_threshold=0.4,
        pad_seconds=0.0,
        merge_gap_seconds=0.0,
        keep_gap_seconds=2.0,
        total_duration=30,
    )
    assert len(spans) == 2
    # A full second of the silence is retained on each side of the cut.
    assert spans[0].end == pytest.approx(11.0)
    assert spans[1].start == pytest.approx(19.0)


def test_padding_never_runs_past_the_end_of_the_video():
    analyses = [analysis(0, 0, 10, 0.9)]
    spans = build_keep_spans(
        analyses,
        [],
        keep_threshold=0.4,
        pad_seconds=5.0,
        merge_gap_seconds=0.0,
        keep_gap_seconds=1.0,
        total_duration=10,
    )
    assert spans[0].start == 0.0
    assert spans[0].end == 10.0


def test_everything_below_threshold_yields_nothing():
    analyses = [analysis(0, 0, 5, 0.1), analysis(1, 5, 10, 0.05)]
    spans = build_keep_spans(
        analyses,
        [],
        keep_threshold=0.5,
        pad_seconds=0.0,
        merge_gap_seconds=0.0,
        keep_gap_seconds=1.0,
        total_duration=10,
    )
    assert spans == []
