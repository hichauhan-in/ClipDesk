"""Turn a recording into a knowledge-base article.

The model is asked for structured fields rather than a finished document, which
keeps the two output formats honest: Markdown and the Word template are two
renderings of the same six fields, not two separate pieces of writing that drift
apart.

The Word output has to land inside the supplied template exactly, so nothing is
composed as free text there — each field is written into the content control that
already carries its heading. Markdown has no such constraint and is laid out for
reading.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from clipdesk.actions.docxtemplate import Block, render_docx
from clipdesk.analysis.mermaid import repair, repair_markdown
from clipdesk.analysis.prompts import (
    ARTICLE_DIAGRAM_HINT,
    ARTICLE_ENRICHMENT_HINT,
    ARTICLE_ENRICHMENT_MARKER,
    ARTICLE_EXTRA_HINT,
    ARTICLE_SHAPE_GUIDANCE,
    ARTICLE_SYSTEM,
    ARTICLE_USER_TEMPLATE,
    ENRICHMENT_LABELS,
    ENRICHMENT_LEVELS,
    MERMAID_HINT,
)
from clipdesk.analysis.windows import format_timestamp
from clipdesk.config import Settings
from clipdesk.events import EventBus
from clipdesk.llm import LLMClient, LLMError
from clipdesk.models import AnalysisReport, ArtifactKind
from clipdesk.store import Project

STAGE = "article"

ARTICLE_FORMATS: tuple[str, ...] = ("docx", "md")
ARTICLE_SHAPES: tuple[str, ...] = ("break-fix", "how-to", "reference")

SHAPE_LABEL: dict[str, str] = {
    "break-fix": "Break/fix help article",
    "how-to": "How-to article",
    "reference": "Reference article",
}

#: Field name → the template's content-control tag and its heading in Markdown.
SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("symptoms", "CI_Template_Symptoms", "Symptoms/Summary"),
    ("cause", "CI_Template_Cause", "Cause"),
    ("resolution", "CI_Template_Resolution", "Resolution/Workaround"),
    ("more_information", "CI_Template_MoreInformation", "More Information"),
)

#: The template says so on its own first page, and it is worth enforcing.
TITLE_LIMIT = 256
KEYWORDS_LIMIT = 256

#: How much transcript one call may carry.
_TRANSCRIPT_LIMIT = 24000

_JSON_RE = re.compile(r"\{.*\}", re.S)
_STEP_PREFIX_RE = re.compile(r"^\s*(?:\d+[.)]|[-*\u2022])\s+")


@dataclass(slots=True)
class Article:
    title: str = ""
    symptoms: list[str] = field(default_factory=list)
    cause: list[str] = field(default_factory=list)
    resolution: list[str] = field(default_factory=list)
    more_information: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    #: Headings the user asked for on top of the template's own.
    extra: list[tuple[str, list[str]]] = field(default_factory=list)
    #: A Mermaid diagram of what the article describes. Markdown output only.
    diagram: str = ""

    def section(self, name: str) -> list[str]:
        return list(getattr(self, name, []) or [])


def _clean_lines(value: object, *, strip_markers: bool = False) -> list[str]:
    if isinstance(value, str):
        items = [piece for piece in value.split("\n")]
    elif isinstance(value, (list, tuple)):
        items = [str(piece) for piece in value]
    else:
        return []
    out: list[str] = []
    for item in items:
        text = item.strip()
        if strip_markers:
            text = _STEP_PREFIX_RE.sub("", text).strip()
        if text:
            out.append(text)
    return out


def _transcript(report: AnalysisReport, limit: int = _TRANSCRIPT_LIMIT) -> str:
    lines: list[str] = []
    used = 0
    for segment in report.transcript.segments:
        text = segment.text.strip()
        if not text:
            continue
        line = f"{format_timestamp(segment.start)} {text}"
        if used + len(line) > limit:
            lines.append("… (transcript truncated)")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def _parse(raw: str) -> dict[str, object]:
    match = _JSON_RE.search(raw)
    if match is None:
        raise LLMError("The model did not return an article.")
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise LLMError(f"The model returned malformed article JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise LLMError("The model did not return an article object.")
    return payload


def write_article(
    report: AnalysisReport,
    llm: LLMClient,
    *,
    shape: str = "break-fix",
    title: str = "",
    audience: str = "",
    extra_sections: list[str] | None = None,
    enrichment: int = 0,
    want_diagram: bool = False,
) -> Article:
    """Ask the model for the article fields. One call: the fields cross-reference."""
    if shape not in ARTICLE_SHAPES:
        raise ValueError(f"Unknown article type: {shape}")

    transcript = _transcript(report)
    if not transcript.strip():
        raise LLMError("There is no transcript to write an article from.")

    # Extra headings are asked for by name in the same call, so the model can
    # place material there instead of padding the standard sections.
    wanted = [(name.strip(), _slug(name)) for name in extra_sections or [] if name.strip()]
    extra_hint = (
        ARTICLE_EXTRA_HINT.format(
            keys="\n".join(f'  "{key}": []   // {name}' for name, key in wanted)
        )
        if wanted
        else ""
    )
    level = max(0, min(max(ENRICHMENT_LEVELS), enrichment))
    enrichment_hint = (
        ARTICLE_ENRICHMENT_HINT.format(
            level=ENRICHMENT_LEVELS[level], marker=ARTICLE_ENRICHMENT_MARKER
        )
        if level
        else ""
    )

    user = ARTICLE_USER_TEMPLATE.format(
        title=title.strip() or report.title or report.media.filename,
        shape_hint=SHAPE_LABEL[shape],
        audience_hint=f"Written for: {audience.strip()}\n" if audience.strip() else "",
        summary=(report.abstract or report.summary or "Not summarised.").strip(),
        transcript=transcript,
        diagram_hint=ARTICLE_DIAGRAM_HINT.format(mermaid=MERMAID_HINT) if want_diagram else "",
        extra_hint=extra_hint,
        enrichment_hint=enrichment_hint,
        shape_guidance=ARTICLE_SHAPE_GUIDANCE[shape],
    )
    # A little more latitude when the model is expected to contribute material of
    # its own; near-zero when it must stay inside the transcript.
    payload = _parse(llm.complete(ARTICLE_SYSTEM, user, temperature=min(0.5, 0.2 + 0.05 * level)))

    article = Article(
        title=(title.strip() or str(payload.get("title") or "").strip())[:TITLE_LIMIT],
        symptoms=_clean_lines(payload.get("symptoms")),
        cause=_clean_lines(payload.get("cause")),
        resolution=_clean_lines(payload.get("resolution"), strip_markers=True),
        more_information=_clean_lines(payload.get("more_information")),
        keywords=_clean_lines(payload.get("keywords")),
        diagram=repair(str(payload.get("diagram") or "")) or "" if want_diagram else "",
    )
    if not article.title:
        article.title = (report.title or report.media.filename)[:TITLE_LIMIT]

    for name, key in wanted:
        body = _clean_lines(payload.get(key))
        # A heading with nothing under it is worse than no heading at all.
        if body:
            article.extra.append((name, body))
    return article


def _slug(heading: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", heading.lower()).strip("_")


def _plain(text: str) -> str:
    """Drop emphasis markers. Document properties are read as literal text."""
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text)


def _keyword_line(keywords: list[str]) -> str:
    line = ""
    for word in keywords:
        candidate = f"{line}, {word}" if line else word
        if len(candidate) > KEYWORDS_LIMIT:
            break
        line = candidate
    return line


def render_markdown(article: Article, report: AnalysisReport) -> str:
    lines = [f"# {article.title}", ""]
    if report.title and report.title != article.title:
        lines += [f"*From the recording “{report.title}”.*", ""]
    if article.diagram:
        lines += ["```mermaid", article.diagram, "```", ""]

    for name, _tag, heading in SECTIONS:
        body = article.section(name)
        if not body:
            continue
        lines += [f"## {heading}", ""]
        if name == "resolution":
            lines += [f"{index}. {step}" for index, step in enumerate(body, start=1)]
        elif len(body) > 1:
            lines += [f"- {item}" for item in body]
        else:
            lines.append(body[0])
        lines.append("")

    for heading, body in article.extra:
        lines += [f"## {heading}", ""]
        lines += [f"- {item}" for item in body]
        lines.append("")

    if article.keywords:
        lines += ["## Keywords", "", ", ".join(article.keywords), ""]
    return repair_markdown("\n".join(lines).rstrip() + "\n")


def _blocks(name: str, body: list[str]) -> list[Block]:
    if name == "resolution":
        return [Block(step, "number") for step in body]
    if len(body) > 1:
        return [Block(item, "bullet") for item in body]
    return [Block(item) for item in body]


def render_template_docx(destination: Path, article: Article) -> Path:
    """Fill the supplied Word template. Sections with no content are left blank."""
    fields: dict[str, list[Block]] = {
        "CI_Template_Title": [Block(article.title)],
        "CI_Template_Keywords": [Block(_keyword_line(article.keywords))],
    }
    for name, tag, _heading in SECTIONS:
        body = article.section(name)
        if not body:
            continue
        blocks = _blocks(name, body)
        # Anything the user asked for beyond the template's own sections is
        # appended under More Information rather than bolted onto the form.
        if name == "more_information":
            for heading, extra_body in article.extra:
                blocks.append(Block(heading, "heading"))
                blocks.extend(Block(item, "bullet") for item in extra_body)
        fields[tag] = blocks

    if "CI_Template_MoreInformation" not in fields and article.extra:
        blocks = []
        for heading, extra_body in article.extra:
            blocks.append(Block(heading, "heading"))
            blocks.extend(Block(item, "bullet") for item in extra_body)
        fields["CI_Template_MoreInformation"] = blocks

    return render_docx(
        destination,
        fields,
        properties={
            "dc:title": article.title,
            "dc:subject": _plain(" ".join(article.section("symptoms")))[:400],
            "cp:keywords": _keyword_line(article.keywords),
        },
    )


def generate_article(
    project: Project,
    report: AnalysisReport,
    settings: Settings,
    llm: LLMClient,
    bus: EventBus,
    *,
    article_format: str = "docx",
    shape: str = "break-fix",
    title: str = "",
    audience: str = "",
    extra_sections: list[str] | None = None,
    enrichment: int = 0,
    include_diagram: bool = True,
    output_name: str = "",
) -> Path:
    if article_format not in ARTICLE_FORMATS:
        raise ValueError(f"Unknown article format: {article_format}")

    level = max(0, min(max(ENRICHMENT_LEVELS), enrichment))
    # Word has no way to render Mermaid, so a diagram is only worth asking for
    # when the output is Markdown.
    want_diagram = include_diagram and article_format == "md"

    bus.stage_start(
        STAGE,
        f"Writing a {SHAPE_LABEL[shape].lower()} — {ENRICHMENT_LABELS[level].lower()}",
    )
    bus.progress(STAGE, 0.15, "Reading the transcript")
    article = write_article(
        report,
        llm,
        shape=shape,
        title=title,
        audience=audience,
        extra_sections=extra_sections,
        enrichment=level,
        want_diagram=want_diagram,
    )

    suffix = f".{article_format}"
    stem = Path(output_name).stem if output_name else "article"
    destination = project.output_path(f"{stem or 'article'}{suffix}")
    bus.progress(STAGE, 0.75, f"Writing {destination.name}")

    if article_format == "md":
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(render_markdown(article, report), encoding="utf-8")
    else:
        render_template_docx(destination, article)

    label = f"{SHAPE_LABEL[shape]} ({article_format.upper()})"
    if level:
        label += f" — {ENRICHMENT_LABELS[level].lower()}"
    project.add_artifact(
        ArtifactKind.ARTICLE,
        label,
        destination,
        meta={
            "shape": shape,
            "format": article_format,
            "title": article.title,
            "enrichment": str(level),
            "diagram": "yes" if article.diagram else "no",
        },
    )
    bus.stage_end(STAGE, f"{destination.name} ready")
    return destination
