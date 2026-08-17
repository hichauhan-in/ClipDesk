"""The summary document, and the diagram it draws from the chapters.

The running order is built from the analysis rather than asked for, so the
picture cannot disagree with the table underneath it.
"""

import pytest

from clipdesk.actions.exports import export_summary
from clipdesk.analysis.mermaid import repair
from clipdesk.models import AnalysisReport, Chapter, MediaInfo
from clipdesk.store import ProjectStore


@pytest.fixture
def project(tmp_path):
    store = ProjectStore(tmp_path / "workspace")
    return store.create("meeting.mp4", title="Meeting")


def report_with(chapters):
    return AnalysisReport(
        project_id="p",
        title="Networking basics",
        media=MediaInfo(path="meeting.mp4", duration_s=600.0),
        chapters=chapters,
    )


def chapter(start, title):
    return Chapter(id=title, title=title, start=start, end=start + 60.0)


def test_the_running_order_is_drawn_as_a_diagram(project):
    report = report_with([chapter(0, "Opening"), chapter(60, "TCP"), chapter(120, "UDP")])

    text = export_summary(project, report).read_text(encoding="utf-8")

    assert "## Running order" in text
    assert "```mermaid" in text
    assert "flowchart LR" in text
    assert "Opening" in text and "UDP" in text


def test_the_diagram_it_draws_is_valid(project):
    report = report_with([chapter(0, 'A "quoted" (tricky) one'), chapter(60, "Second")])

    text = export_summary(project, report).read_text(encoding="utf-8")
    body = text.split("```mermaid\n")[1].split("```")[0]

    assert repair(body) is not None
    assert '"' not in body.replace('C0["', "").replace('"]', "").replace('C1["', "")


def test_a_single_chapter_gets_no_diagram(project):
    text = export_summary(project, report_with([chapter(0, "All of it")])).read_text(
        encoding="utf-8"
    )

    assert "```mermaid" not in text


def test_no_chapters_gets_no_diagram(project):
    text = export_summary(project, report_with([])).read_text(encoding="utf-8")

    assert "```mermaid" not in text


def test_a_long_recording_does_not_draw_an_unreadable_chain(project):
    report = report_with([chapter(index * 60, f"Part {index}") for index in range(30)])

    text = export_summary(project, report).read_text(encoding="utf-8")
    body = text.split("```mermaid\n")[1].split("```")[0]

    assert body.count("-->") <= 14
    assert "more" in body


def test_a_long_chapter_title_is_shortened_in_the_diagram(project):
    long_title = "A chapter title that goes on well past what fits inside a node"
    report = report_with([chapter(0, long_title), chapter(60, "Second")])

    text = export_summary(project, report).read_text(encoding="utf-8")
    body = text.split("```mermaid\n")[1].split("```")[0]

    assert long_title not in body
    assert "…" in body
    # The table underneath still carries it in full.
    assert long_title in text
