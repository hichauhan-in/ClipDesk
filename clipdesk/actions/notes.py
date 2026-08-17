"""Generate Markdown study notes from the transcript.

Notes are written per section rather than in one giant call: it keeps each
prompt well inside the context window, lets progress be reported, and means one
bad section does not lose the whole document.
"""

from __future__ import annotations

import re
from pathlib import Path

from clipdesk.analysis.mermaid import repair_markdown
from clipdesk.analysis.prompts import (
    ENRICHMENT_LABELS,
    ENRICHMENT_LEVELS,
    MERMAID_HINT,
    NOTES_SYSTEM,
    NOTES_USER_TEMPLATE,
    TIMESTAMP_HINT,
)
from clipdesk.analysis.windows import format_timestamp
from clipdesk.config import Settings
from clipdesk.events import EventBus
from clipdesk.llm import LLMClient, LLMError
from clipdesk.models import AnalysisReport, ArtifactKind, Chapter
from clipdesk.store import Project

STAGE = "notes"

_FENCE_RE = re.compile(r"^```", re.MULTILINE)
_WRAPPED_RE = re.compile(r"^\s*```(?:markdown|md)?\s*\n(.*)\n```\s*$", re.DOTALL)


def _unwrap(markdown: str) -> str:
    """Models sometimes wrap the whole answer in a fence; unwrap it if so."""
    match = _WRAPPED_RE.match(markdown.strip())
    return match.group(1) if match else markdown.strip()


def _balance_fences(markdown: str) -> str:
    """Close a code fence the model forgot to close, so the file still renders."""
    if len(_FENCE_RE.findall(markdown)) % 2 == 1:
        return markdown.rstrip() + "\n```\n"
    return markdown


def _demote_headings(markdown: str, minimum_level: int = 2) -> str:
    """Force the section to start at ``##`` so the assembled file nests properly."""
    lines = markdown.split("\n")
    levels = [
        len(match.group(1))
        for line in lines
        if (match := re.match(r"^(#{1,6})\s+\S", line)) is not None
    ]
    if not levels:
        return markdown
    shift = minimum_level - min(levels)
    if shift <= 0:
        return markdown
    out: list[str] = []
    in_fence = False
    for line in lines:
        if line.startswith("```"):
            in_fence = not in_fence
        if not in_fence and (match := re.match(r"^(#{1,6})(\s+)", line)) is not None:
            level = min(6, len(match.group(1)) + shift)
            line = "#" * level + match.group(2) + line[match.end() :]
        out.append(line)
    return "\n".join(out)


def _section_transcript(report: AnalysisReport, chapter: Chapter, limit: int = 11000) -> str:
    lines: list[str] = []
    used = 0
    for segment in report.transcript.segments:
        if segment.start < chapter.start or segment.start >= chapter.end:
            continue
        speaker = f"{segment.speaker}: " if segment.speaker else ""
        line = f"{format_timestamp(segment.start)} {speaker}{segment.text.strip()}"
        if used + len(line) > limit:
            lines.append("… (section truncated)")
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)


def _write_section(
    llm: LLMClient, report: AnalysisReport, chapter: Chapter, settings: Settings
) -> str:
    transcript = _section_transcript(report, chapter)
    if not transcript.strip():
        return ""

    level = max(0, min(max(ENRICHMENT_LEVELS), settings.notes.enrichment))
    user = NOTES_USER_TEMPLATE.format(
        title=report.title or report.media.filename,
        section_title=chapter.title,
        start=format_timestamp(chapter.start),
        end=format_timestamp(chapter.end),
        transcript=transcript,
        mermaid_hint=MERMAID_HINT if settings.notes.include_mermaid else "",
        timestamp_hint=TIMESTAMP_HINT if settings.notes.include_timestamps else "",
        enrichment_hint=ENRICHMENT_LEVELS[level],
    )
    # A little more latitude when the model is expected to contribute material of
    # its own; near-zero when it must stay inside the transcript.
    temperature = min(0.5, 0.3 + 0.04 * level)
    markdown = llm.complete(NOTES_SYSTEM, user, temperature=temperature)
    return repair_markdown(_balance_fences(_demote_headings(_unwrap(markdown))))


def _document_header(
    report: AnalysisReport, part: int, total_parts: int, enrichment: int
) -> list[str]:
    suffix = f" — part {part} of {total_parts}" if total_parts > 1 else ""
    lines = [f"# {report.title or 'Notes'}{suffix}", ""]
    if part == 1:
        if report.abstract:
            lines += [f"> {report.abstract}", ""]
        if report.summary:
            lines += ["## Overview", "", report.summary, ""]
        if report.keywords:
            lines += [
                "**Topics:** " + ", ".join(f"`{word}`" for word in report.keywords),
                "",
            ]
        if enrichment > 0:
            # State this up front. Someone forwarding these notes needs to know
            # they contain material the speaker did not say.
            lines += [
                (
                    "> **Note on sources.** These notes were generated with AI enrichment "
                    f"set to *{ENRICHMENT_LABELS[enrichment].lower()}*. Everything the model "
                    "added beyond the recording is marked **Added context**. Treat those "
                    "parts as background to verify, not as something that was said."
                ),
                "",
            ]
    return lines


def _document_footer(report: AnalysisReport) -> list[str]:
    lines: list[str] = []
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
    return lines


def generate_notes(
    project: Project,
    report: AnalysisReport,
    settings: Settings,
    llm: LLMClient,
    bus: EventBus,
) -> list[Path]:
    chapters = sorted(report.chapters, key=lambda chapter: chapter.start)
    if not chapters:
        raise LLMError("This project has no chapters yet — run the analysis first.")

    per_file = max(1, settings.notes.chapters_per_file)
    groups = [chapters[i : i + per_file] for i in range(0, len(chapters), per_file)]
    total = len(chapters)
    level = max(0, min(max(ENRICHMENT_LEVELS), settings.notes.enrichment))

    bus.stage_start(
        STAGE,
        f"Writing notes for {total} section(s) — {ENRICHMENT_LABELS[level].lower()}",
    )
    written: list[Path] = []
    done = 0

    for part, group in enumerate(groups, start=1):
        body: list[str] = _document_header(report, part, len(groups), level)
        for chapter in group:
            bus.progress(STAGE, done / total, f"Writing '{chapter.title}'")
            try:
                section = _write_section(llm, report, chapter, settings)
            except LLMError as exc:
                section = (
                    f"## {chapter.title}\n\n"
                    f"> Notes for this section could not be generated: {exc}\n"
                )
            if section:
                body.append(section)
                body.append("")
            done += 1
            bus.progress(STAGE, done / total, f"Wrote '{chapter.title}'")

        if part == len(groups):
            body += _document_footer(report)

        name = "notes.md" if len(groups) == 1 else f"notes-part-{part}.md"
        destination = project.output_path(name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("\n".join(body).rstrip() + "\n", encoding="utf-8")
        label = "Notes" if len(groups) == 1 else f"Notes — part {part}"
        if level > 0:
            label += f" ({ENRICHMENT_LABELS[level].lower()})"
        project.add_artifact(
            ArtifactKind.NOTES, label, destination, meta={"enrichment": str(level)}
        )
        written.append(destination)

    bus.stage_end(STAGE, f"Wrote {len(written)} notes file(s)")
    return written
