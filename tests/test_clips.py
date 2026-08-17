"""Clip discovery: ranking, id resolution and the no-model fallbacks."""

import pytest

from clipdesk.actions.clips import (
    _dedupe,
    _fallback_summary,
    _span_from_ids,
    _to_candidate,
    find_candidates,
    rank_spans,
)
from clipdesk.config import Settings
from clipdesk.models import (
    AnalysisReport,
    ClipCandidate,
    MediaInfo,
    SegmentAnalysis,
    SegmentKind,
    Transcript,
    TranscriptSegment,
)

SENTENCES = [
    "We are covering the checkout retry policy today.",
    "The gateway times out about two percent of the time under load.",
    "So the design is exponential backoff with jitter.",
    "Without jitter every failed request retries at the same moment.",
    "The idempotency key makes retrying a payment safe.",
    "It is a hash of the order id and the attempt window.",
    "Observability is a counter tagged by outcome and a histogram.",
    "The alert fires above five percent for fifteen minutes.",
]


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
def report():
    segments = [
        TranscriptSegment(id=i, start=i * 10.0, end=i * 10.0 + 10.0, text=text)
        for i, text in enumerate(SENTENCES)
    ]
    analyses = [
        SegmentAnalysis(
            segment_id=segment.id,
            start=segment.start,
            end=segment.end,
            kind=SegmentKind.ON_TOPIC,
            importance=0.7,
        )
        for segment in segments
    ]
    return AnalysisReport(
        project_id="test",
        title="Retry policy",
        media=MediaInfo(path="x.mp4", duration_s=80.0, audios=[]),
        transcript=Transcript(segments=segments, duration_s=80.0),
        segment_analyses=analyses,
    )


# --- id resolution -----------------------------------------------------------
def test_span_from_ids_resolves_a_range(report):
    span = _span_from_ids(report, 2, 4)
    assert span[:2] == (20.0, 50.0)


def test_span_from_ids_orders_a_reversed_range(report):
    assert _span_from_ids(report, 4, 2)[:2] == (20.0, 50.0)


def test_span_from_ids_snaps_out_of_range_ids(report):
    # Models occasionally invent ids just past the end of a window; snapping to
    # the nearest real segment saves the candidate instead of discarding it.
    span = _span_from_ids(report, 0, 99)
    assert span[:2] == (0.0, 80.0)


def test_span_from_ids_rejects_garbage(report):
    assert _span_from_ids(report, "abc", None) is None


def test_candidate_is_clamped_to_the_video(report):
    candidate = _to_candidate(
        report, {"title": "T", "from_id": 6, "to_id": 7, "score": 0.9}, source="topic", pad=30.0
    )
    assert candidate.end <= report.media.duration_s
    assert candidate.start >= 0.0


def test_candidate_gets_a_summary_when_the_model_omits_one(report):
    candidate = _to_candidate(report, {"title": "T", "from_id": 0, "to_id": 1}, source="topic")
    assert candidate.summary
    assert "checkout retry policy" in candidate.summary


def test_candidate_keeps_a_supplied_summary(report):
    candidate = _to_candidate(
        report, {"title": "T", "summary": "Two sentences here.", "from_id": 0, "to_id": 1},
        source="topic",
    )
    assert candidate.summary == "Two sentences here."


def test_relevance_is_read_for_topic_matches(report):
    candidate = _to_candidate(
        report, {"title": "T", "from_id": 0, "to_id": 1, "relevance": 0.9},
        source="topic", score_key="relevance",
    )
    assert candidate.score == pytest.approx(0.9)


# --- ranking -----------------------------------------------------------------
def test_ranking_returns_spans_near_the_target(report):
    ranked = rank_spans(report, target_seconds=30, tolerance=0.4)
    assert ranked
    for candidate in ranked:
        assert 15 <= candidate.duration <= 45


def test_ranking_prefers_query_relevance(report):
    ranked = rank_spans(report, target_seconds=20, tolerance=0.6, query="idempotency key payment")
    assert ranked[0].start <= 50.0


def test_ranking_skips_low_value_openings(report):
    report.segment_analyses[0].kind = SegmentKind.FILLER
    ranked = rank_spans(report, target_seconds=20, tolerance=0.5)
    assert all(candidate.start > 0.0 for candidate in ranked)


def test_ranking_of_an_empty_transcript_is_empty(report):
    report.transcript.segments = []
    assert rank_spans(report, target_seconds=30, tolerance=0.3) == []


# --- dedupe ------------------------------------------------------------------
def test_dedupe_drops_near_identical_spans():
    a = ClipCandidate(title="A", start=0, end=30, score=0.9)
    b = ClipCandidate(title="B", start=2, end=31, score=0.5)
    assert [c.title for c in _dedupe([a, b])] == ["A"]


def test_dedupe_keeps_distinct_spans():
    a = ClipCandidate(title="A", start=0, end=30, score=0.9)
    b = ClipCandidate(title="B", start=40, end=70, score=0.5)
    assert len(_dedupe([a, b])) == 2


def test_dedupe_returns_chronological_order():
    a = ClipCandidate(title="late", start=60, end=80, score=0.9)
    b = ClipCandidate(title="early", start=0, end=20, score=0.4)
    assert [c.title for c in _dedupe([a, b])] == ["early", "late"]


# --- find_candidates ---------------------------------------------------------
def test_duration_mode_without_a_model_still_returns_options(report, settings):
    result = find_candidates(report, settings, mode="duration", target_seconds=30, llm=None)
    assert result.candidates
    assert result.warnings


def test_topic_mode_requires_a_query(report, settings):
    with pytest.raises(ValueError, match="what the clip should be about"):
        find_candidates(report, settings, mode="topic", llm=None)


def test_topic_mode_without_a_model_falls_back_to_keywords(report, settings):
    result = find_candidates(report, settings, mode="topic", query="jitter backoff", llm=None)
    assert result.note
    assert result.warnings


def test_highlight_mode_without_a_model_uses_the_analysis(report, settings):
    report.clip_candidates = [ClipCandidate(title="Backoff", start=20, end=45, score=0.8)]
    result = find_candidates(report, settings, mode="highlight", llm=None)
    assert [c.title for c in result.candidates] == ["Backoff"]


def test_highlight_mode_caps_candidate_length(report, settings):
    settings.clips.highlight_max_seconds = 10
    report.clip_candidates = [ClipCandidate(title="Long", start=0, end=80, score=0.8)]
    result = find_candidates(report, settings, mode="highlight", llm=None)
    assert result.candidates[0].duration == pytest.approx(10.0)


def test_an_unknown_mode_is_rejected(report, settings):
    with pytest.raises(ValueError, match="Unknown mode"):
        find_candidates(report, settings, mode="magic", llm=None)


def test_a_transcript_free_report_is_rejected(report, settings):
    report.transcript.segments = []
    with pytest.raises(ValueError, match="no transcript"):
        find_candidates(report, settings, mode="duration", llm=None)


# --- summaries ---------------------------------------------------------------
def test_fallback_summary_uses_two_sentences(report):
    summary = _fallback_summary(report, 0.0, 30.0)
    assert summary.count(".") <= 2
    assert summary.startswith("We are covering")
