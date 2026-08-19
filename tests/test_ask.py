"""Answering questions about a finished analysis.

The part worth pinning is the honesty contract: in open mode the model may add
its own knowledge, but only under a heading that says so, and the UI shows the
disclaimer off the back of that heading. If the heading and the detection drift
apart, added material silently reads as something the speaker said.
"""

from __future__ import annotations

import pytest

from clipdesk.actions.ask import BEYOND_HEADING, Answer, ask, build_context
from clipdesk.models import AnalysisReport, Chapter, MediaInfo


class _Recorded:
    """Captures the prompt instead of calling a model."""

    def __init__(self, reply: str = "an answer") -> None:
        self.reply = reply
        self.task = ""
        self.system = ""
        self.user = ""

    def for_task(self, task):
        self.task = task
        return self

    def complete(self, system, user, **_kwargs):
        self.system = system
        self.user = user
        return self.reply


@pytest.fixture
def report():
    return AnalysisReport(
        project_id="p",
        title="DNS failure",
        media=MediaInfo(path="clip.mp4", duration_s=600.0),
        abstract="A short abstract.",
        summary="A longer summary.",
        keywords=["dns", "timeout"],
        chapters=[
            Chapter(
                start=0.0,
                end=120.0,
                title="Reproducing the failure",
                summary="How the fault was triggered.",
                key_points=["Resolve-DnsName returned an unspecified error"],
            )
        ],
    )


def test_the_context_carries_what_the_recording_established(report):
    context = build_context(report)

    assert "A short abstract." in context
    assert "Reproducing the failure" in context
    assert "Resolve-DnsName returned an unspecified error" in context
    # Timestamps let the answer cite where something was said.
    assert "[00:00-02:00]" in context


def test_a_long_recording_is_trimmed_rather_than_refused(report):
    report.summary = "x" * 50_000

    assert len(build_context(report, limit=5_000)) == 5_000


def test_grounded_mode_forbids_outside_knowledge(report):
    llm = _Recorded()

    ask(report, "What failed?", llm)

    assert llm.task == "ask"
    assert "Do not add outside knowledge" in llm.system
    assert BEYOND_HEADING not in llm.system


def test_open_mode_demands_added_material_be_separated(report):
    llm = _Recorded()

    ask(report, "What failed?", llm, grounded=False)

    assert BEYOND_HEADING in llm.system


def test_added_material_is_flagged_so_the_reader_is_told(report):
    llm = _Recorded(f"The recording says X.\n\n## {BEYOND_HEADING}\nGenerally, Y.")

    answer = ask(report, "What failed?", llm, grounded=False)

    assert answer.went_beyond is True


def test_an_open_answer_that_stayed_put_claims_nothing(report):
    llm = _Recorded("The recording says X.")

    assert ask(report, "What failed?", llm, grounded=False).went_beyond is False


def test_a_grounded_answer_is_never_flagged_as_beyond(report):
    """Grounded mode cannot go beyond, so the heading must not be trusted there."""
    llm = _Recorded(f"## {BEYOND_HEADING}\nsomething")

    assert ask(report, "What failed?", llm).went_beyond is False


def test_an_empty_question_is_refused(report):
    with pytest.raises(ValueError, match="Ask a question"):
        ask(report, "   ", _Recorded())


def test_an_absurdly_long_question_is_refused(report):
    with pytest.raises(ValueError, match="too long"):
        ask(report, "x" * 5_000, _Recorded())


def test_an_unanalysed_recording_has_nothing_to_answer_from():
    empty = AnalysisReport(project_id="p", title="t", media=MediaInfo(path="clip.mp4", duration_s=1.0))

    with pytest.raises(ValueError, match="not been analysed"):
        ask(empty, "What failed?", _Recorded())


def test_the_answer_reports_which_mode_produced_it(report):
    assert ask(report, "q", _Recorded()).grounded is True
    assert ask(report, "q", _Recorded(), grounded=False).grounded is False
    assert isinstance(ask(report, "q", _Recorded()), Answer)
