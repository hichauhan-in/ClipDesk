"""Transcript exports: SRT, WebVTT, Markdown and plain text."""

from __future__ import annotations

from pathlib import Path

from clipdesk.analysis.windows import format_timestamp
from clipdesk.models import AnalysisReport, ArtifactKind, Chapter, Transcript
from clipdesk.store import Project

TRANSCRIPT_FORMATS = ("srt", "vtt", "md", "txt")


def _cue_time(seconds: float, *, comma: bool) -> str:
    seconds = max(0.0, seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    whole = int(secs)
    millis = round((secs - whole) * 1000)
    if millis == 1000:  # rounding can tip a whole second
        whole += 1
        millis = 0
    separator = "," if comma else "."
    return f"{int(hours):02d}:{int(minutes):02d}:{whole:02d}{separator}{millis:03d}"


def to_srt(transcript: Transcript) -> str:
    blocks: list[str] = []
    for index, segment in enumerate(transcript.segments, start=1):
        speaker = f"{segment.speaker}: " if segment.speaker else ""
        blocks.append(
            f"{index}\n"
            f"{_cue_time(segment.start, comma=True)} --> {_cue_time(segment.end, comma=True)}\n"
            f"{speaker}{segment.text.strip()}\n"
        )
    return "\n".join(blocks)


def to_vtt(transcript: Transcript) -> str:
    lines = ["WEBVTT", ""]
    for segment in transcript.segments:
        speaker = f"<v {segment.speaker}>" if segment.speaker else ""
        lines.append(
            f"{_cue_time(segment.start, comma=False)} --> {_cue_time(segment.end, comma=False)}"
        )
        lines.append(f"{speaker}{segment.text.strip()}")
        lines.append("")
    return "\n".join(lines)


def to_text(transcript: Transcript) -> str:
    lines: list[str] = []
    current_speaker: str | None = None
    for segment in transcript.segments:
        if segment.speaker and segment.speaker != current_speaker:
            current_speaker = segment.speaker
            lines.append("")
            lines.append(f"{current_speaker}:")
        lines.append(segment.text.strip())
    return "\n".join(lines).strip() + "\n"


def to_markdown(report: AnalysisReport) -> str:
    """A readable transcript grouped under the chapter headings from analysis."""
    transcript = report.transcript
    lines = [f"# {report.title or 'Transcript'}", ""]
    if report.abstract:
        lines += [f"> {report.abstract}", ""]
    lines += [
        f"- **Duration:** {format_timestamp(report.media.duration_s)}",
        f"- **Segments:** {len(transcript.segments)}",
        f"- **Words:** {transcript.word_count:,}",
    ]
    if transcript.language:
        lines.append(f"- **Language:** {transcript.language}")
    lines += [
        f"- **Source:** {transcript.source.value}"
        + (f" ({transcript.model})" if transcript.model else ""),
        "",
    ]

    chapters = sorted(report.chapters, key=lambda chapter: chapter.start)
    if not chapters:
        lines.append("## Transcript")
        lines.append("")
        for segment in transcript.segments:
            speaker = f"**{segment.speaker}:** " if segment.speaker else ""
            lines.append(f"`{format_timestamp(segment.start)}` {speaker}{segment.text.strip()}")
            lines.append("")
        return "\n".join(lines)

    lines += ["## Contents", ""]
    for chapter in chapters:
        lines.append(
            f"- `{format_timestamp(chapter.start)}` **{chapter.title}**"
            + (f" — {chapter.summary}" if chapter.summary else "")
        )
    lines.append("")

    for chapter in chapters:
        lines += [
            f"## {chapter.title}",
            "",
            f"`{format_timestamp(chapter.start)} – {format_timestamp(chapter.end)}`",
            "",
        ]
        if chapter.summary:
            lines += [chapter.summary, ""]
        for segment in transcript.segments:
            if segment.start < chapter.start or segment.start >= chapter.end:
                continue
            speaker = f"**{segment.speaker}:** " if segment.speaker else ""
            lines.append(f"`{format_timestamp(segment.start)}` {speaker}{segment.text.strip()}")
            lines.append("")
    return "\n".join(lines)


def export_transcript(project: Project, report: AnalysisReport, fmt: str) -> Path:
    fmt = fmt.lower().strip()
    if fmt not in TRANSCRIPT_FORMATS:
        raise ValueError(f"Unsupported transcript format '{fmt}'.")

    renderers = {
        "srt": lambda: to_srt(report.transcript),
        "vtt": lambda: to_vtt(report.transcript),
        "txt": lambda: to_text(report.transcript),
        "md": lambda: to_markdown(report),
    }
    destination = project.output_path(f"transcript.{fmt}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(renderers[fmt](), encoding="utf-8")
    project.add_artifact(
        ArtifactKind.TRANSCRIPT, f"Transcript ({fmt.upper()})", destination
    )
    return destination


def _chapter_diagram(chapters: list[Chapter], limit: int = 14) -> list[str]:
    """The running order as a diagram, built from the chapters rather than a model.

    Nothing here is guessed, so the picture cannot disagree with the table below
    it. Past a certain length a chain stops being readable, so it is truncated.
    """
    if len(chapters) < 2:
        return []
    shown = sorted(chapters, key=lambda chapter: chapter.start)[:limit]
    nodes = [
        f'C{index}["{format_timestamp(chapter.start)}<br/>{_diagram_label(chapter.title)}"]'
        for index, chapter in enumerate(shown)
    ]
    if len(chapters) > limit:
        nodes.append(f'Cx["+{len(chapters) - limit} more"]')
    return ["## Running order", "", "```mermaid", "flowchart LR", "    " + " --> ".join(nodes), "```", ""]


def _diagram_label(title: str, limit: int = 34) -> str:
    # Mermaid has no escape inside a quoted label, so a quote becomes an entity.
    text = " ".join(title.split()).replace('"', "#quot;")
    return text if len(text) <= limit else f"{text[: limit - 1].rstrip()}…"


def export_summary(project: Project, report: AnalysisReport) -> Path:
    """A one-page overview: abstract, summary, chapters, decisions, actions."""
    lines = [f"# {report.title or 'Summary'}", ""]
    if report.abstract:
        lines += [f"> {report.abstract}", ""]
    if report.summary:
        lines += ["## Summary", "", report.summary, ""]
    if report.keywords:
        lines += ["## Keywords", "", ", ".join(f"`{word}`" for word in report.keywords), ""]

    if report.chapters:
        lines += _chapter_diagram(report.chapters)
        lines += ["## Chapters", "", "| Time | Topic | Notes |", "| --- | --- | --- |"]
        for chapter in report.chapters:
            summary = (chapter.summary or "").replace("|", "\\|").replace("\n", " ")
            title = chapter.title.replace("|", "\\|")
            lines.append(f"| `{format_timestamp(chapter.start)}` | {title} | {summary} |")
        lines.append("")

    if report.decisions:
        lines += ["## Decisions", ""]
        for decision in report.decisions:
            stamp = f" `{format_timestamp(decision.time)}`" if decision.time is not None else ""
            lines.append(f"- {decision.text}{stamp}")
        lines.append("")

    if report.action_items:
        lines += ["## Action items", ""]
        for item in report.action_items:
            owner = f" — **{item.owner}**" if item.owner else ""
            stamp = f" `{format_timestamp(item.time)}`" if item.time is not None else ""
            lines.append(f"- [ ] {item.text}{owner}{stamp}")
        lines.append("")

    if report.clip_candidates:
        lines += ["## Suggested clips", "", "| Time | Length | Clip |", "| --- | --- | --- |"]
        for clip in sorted(report.clip_candidates, key=lambda c: -c.score)[:10]:
            title = clip.title.replace("|", "\\|")
            lines.append(
                f"| `{format_timestamp(clip.start)}` | {clip.duration:.0f}s | {title} |"
            )
        lines.append("")

    destination = project.output_path("summary.md")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines), encoding="utf-8")
    project.add_artifact(ArtifactKind.SUMMARY, "Summary (Markdown)", destination)
    return destination
