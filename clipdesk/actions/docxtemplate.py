"""Fill the Word knowledge-article template without a Word install.

A ``.docx`` is a zip of XML parts. The supplied template marks each field with a
Word *content control* carrying a stable tag — ``CI_Template_Title``,
``CI_Template_Symptoms`` and so on — so filling it in means replacing the body of
those controls and copying every other part through byte for byte. Nothing about
the page setup, styles, headers, footers or the instruction block at the top is
touched, which is the whole point: the file has to stay the shape it arrived in.

Two details are easy to get wrong:

* A control that still shows grey prompt text carries ``<w:showingPlcHdr/>``.
  Leaving it in place makes Word treat real content as a placeholder and wipe it
  on the first click.
* One control is bound to a document property. A bound control is refilled from
  that property when the document opens, which would flatten multi-paragraph
  content back to a single line. The binding is dropped as the field is written
  and the property is set to match, so both the body and the metadata are right.
"""

from __future__ import annotations

import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

from clipdesk.paths import app_root

TEMPLATE_RELATIVE = Path("tools") / "template" / "file.docx"

#: Content-control tags in the order they appear in the template.
FIELD_TAGS: tuple[str, ...] = (
    "CI_Template_Title",
    "CI_Template_Symptoms",
    "CI_Template_Cause",
    "CI_Template_Resolution",
    "CI_Template_MoreInformation",
    "CI_Template_Keywords",
)

#: Bullets and numbers, taken from the template's own numbering definitions.
_BULLET_NUM_ID = 3
_NUMBER_NUM_ID = 10

#: Body text in this template is 12pt.
_RUN_PROPS = '<w:rPr><w:sz w:val="24"/><w:szCs w:val="24"/></w:rPr>'


class TemplateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Block:
    """One paragraph destined for the document."""

    text: str
    #: "body" | "bullet" | "number" | "heading"
    kind: str = "body"


def template_path() -> Path:
    return app_root() / TEMPLATE_RELATIVE


def template_available() -> bool:
    return template_path().is_file()


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _runs(text: str) -> str:
    """Split on ``**bold**`` so emphasis survives into Word."""
    out: list[str] = []
    for index, piece in enumerate(re.split(r"\*\*(.+?)\*\*", text)):
        if not piece:
            continue
        bold = index % 2 == 1
        props = (
            f'<w:rPr><w:b/><w:bCs/><w:sz w:val="24"/><w:szCs w:val="24"/></w:rPr>'
            if bold
            else _RUN_PROPS
        )
        out.append(
            f"<w:r>{props}<w:t xml:space=\"preserve\">{_escape(piece)}</w:t></w:r>"
        )
    return "".join(out) or f"<w:r>{_RUN_PROPS}<w:t/></w:r>"


def _paragraph(block: Block) -> str:
    if block.kind == "heading":
        return (
            '<w:p><w:pPr><w:pStyle w:val="Heading3"/></w:pPr>'
            f"<w:r><w:t xml:space=\"preserve\">{_escape(block.text)}</w:t></w:r></w:p>"
        )
    if block.kind in ("bullet", "number"):
        num_id = _BULLET_NUM_ID if block.kind == "bullet" else _NUMBER_NUM_ID
        properties = (
            '<w:pPr><w:pStyle w:val="ListParagraph"/>'
            f'<w:numPr><w:ilvl w:val="0"/><w:numId w:val="{num_id}"/></w:numPr>'
            f"<w:rPr><w:sz w:val=\"24\"/><w:szCs w:val=\"24\"/></w:rPr></w:pPr>"
        )
        return f"<w:p>{properties}{_runs(block.text)}</w:p>"
    return f"<w:p><w:pPr>{_RUN_PROPS}</w:pPr>{_runs(block.text)}</w:p>"


def _find_control(xml: str, tag: str) -> tuple[int, int]:
    """Locate the ``<w:sdt>`` element carrying ``tag``, respecting nesting."""
    marker = xml.find(f'<w:tag w:val="{tag}"/>')
    if marker < 0:
        raise TemplateError(f"The template has no '{tag}' field.")
    start = xml.rfind("<w:sdt>", 0, marker)
    if start < 0:
        raise TemplateError(f"The '{tag}' field is malformed.")
    depth = 0
    for match in re.finditer(r"<w:sdt>|</w:sdt>", xml[start:]):
        depth += 1 if match.group(0) == "<w:sdt>" else -1
        if depth == 0:
            return start, start + match.end()
    raise TemplateError(f"The '{tag}' field is not closed.")


def _fill_control(xml: str, tag: str, blocks: list[Block]) -> str:
    start, end = _find_control(xml, tag)
    control = xml[start:end]

    content_start = control.find("<w:sdtContent>")
    content_end = control.rfind("</w:sdtContent>")
    if content_start < 0 or content_end < 0:
        raise TemplateError(f"The '{tag}' field has no content region.")

    head = control[: content_start + len("<w:sdtContent>")]
    # Grey prompt text, and the binding that would overwrite what we write.
    head = head.replace("<w:showingPlcHdr/>", "")
    head = re.sub(r"<w15:dataBinding[^>]*/>", "", head)
    body = "".join(_paragraph(block) for block in blocks) or _paragraph(Block(""))
    return xml[:start] + head + body + control[content_end:] + xml[end:]


def _set_property(core_xml: str, name: str, value: str) -> str:
    """Set one docProps/core.xml element, adding it if the template lacks it."""
    escaped = _escape(value)
    pattern = re.compile(rf"<{name}(\s[^>]*)?>.*?</{name}>|<{name}\s*/>", re.S)
    if pattern.search(core_xml):
        return pattern.sub(f"<{name}>{escaped}</{name}>", core_xml, count=1)
    return core_xml.replace("</cp:coreProperties>", f"<{name}>{escaped}</{name}></cp:coreProperties>")


def render_docx(
    destination: Path,
    fields: dict[str, list[Block]],
    *,
    properties: dict[str, str] | None = None,
    template: Path | None = None,
) -> Path:
    """Write ``destination`` as the template with its fields filled in.

    ``fields`` is keyed by content-control tag. A tag left out keeps whatever the
    template had, so a section that does not apply still reads as an unfilled
    form field rather than an empty heading.
    """
    source = template or template_path()
    if not source.is_file():
        raise TemplateError(
            "The Word template is missing. Expected it at "
            f"{TEMPLATE_RELATIVE.as_posix()} under the ClipDesk folder."
        )

    unknown = set(fields) - set(FIELD_TAGS)
    if unknown:
        raise TemplateError(f"Unknown template field(s): {', '.join(sorted(unknown))}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(f".{destination.name}.tmp")
    try:
        with zipfile.ZipFile(source) as archive:
            names = archive.namelist()
            document = archive.read("word/document.xml").decode("utf-8")
            for tag, blocks in fields.items():
                document = _fill_control(document, tag, blocks)

            core = archive.read("docProps/core.xml").decode("utf-8")
            for name, value in (properties or {}).items():
                core = _set_property(core, name, value)

            rewritten = {
                "word/document.xml": document.encode("utf-8"),
                "docProps/core.xml": core.encode("utf-8"),
            }
            with zipfile.ZipFile(staging, "w", zipfile.ZIP_DEFLATED) as out:
                for name in names:
                    item = archive.getinfo(name)
                    out.writestr(item, rewritten.get(name) or archive.read(name))
        shutil.move(str(staging), str(destination))
    finally:
        staging.unlink(missing_ok=True)
    return destination


# --- reading back ------------------------------------------------------------
_PARAGRAPH_RE = re.compile(r"<w:p[ >].*?</w:p>", re.S)
_RUN_RE = re.compile(r"<w:r[ >].*?</w:r>", re.S)
_TEXT_RE = re.compile(r"<w:t[^>]*>(.*?)</w:t>", re.S)
_TABLE_RE = re.compile(r"<w:tbl>.*?</w:tbl>", re.S)
_HEADING_RE = re.compile(r'<w:pStyle w:val="Heading(\d)"')
_NUM_ID_RE = re.compile(r'<w:numId w:val="(\d+)"')
_NUM_MAP_RE = re.compile(
    r'<w:num w:numId="(\d+)"[^>]*>\s*<w:abstractNumId w:val="(\d+)"', re.S
)
_ABSTRACT_RE = re.compile(
    r'<w:abstractNum w:abstractNumId="(\d+)".*?</w:abstractNum>', re.S
)
_FORMAT_RE = re.compile(r'<w:numFmt w:val="([^"]+)"')
#: The template puts author guidance inside its own headings, e.g.
#: "Title(Note: 256 is the maximum length of title)".
_HEADING_NOTE_RE = re.compile(r"\s*\((?:Note:|Applies only to)[^)]*\)\s*$", re.I)


def _unescape(text: str) -> str:
    return (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&amp;", "&")
    )


def _list_kinds(numbering: str) -> dict[str, str]:
    """numId → "bullet" or "decimal", read from the document's own definitions."""
    formats: dict[str, str] = {}
    for match in _ABSTRACT_RE.finditer(numbering):
        found = _FORMAT_RE.search(match.group(0))
        formats[match.group(1)] = found.group(1) if found else "bullet"
    return {
        num_id: formats.get(abstract_id, "bullet")
        for num_id, abstract_id in _NUM_MAP_RE.findall(numbering)
    }


def _paragraph_markdown(paragraph: str, *, emphasis: bool = True) -> str:
    # Word splits a sentence across runs wherever it was edited, so "Symptoms"
    # and "/Summary" can be two bold runs. Emitting markers per run would give
    # "**Symptoms****/Summary**", so runs are coalesced by weight first.
    pieces: list[list] = []
    for run in _RUN_RE.findall(paragraph):
        text = _unescape("".join(_TEXT_RE.findall(run)))
        if not text:
            continue
        properties = run[: run.find("</w:rPr>")] if "<w:rPr>" in run else ""
        bold = emphasis and "<w:b/>" in properties
        if pieces and pieces[-1][0] == bold:
            pieces[-1][1] += text
        else:
            pieces.append([bold, text])

    out: list[str] = []
    for bold, text in pieces:
        body = text.strip()
        if not bold or not body:
            out.append(text)
            continue
        # Markdown will not close a delimiter that sits against a space.
        lead = text[: len(text) - len(text.lstrip())]
        trail = text[len(text.rstrip()) :]
        out.append(f"{lead}**{body}**{trail}")
    return "".join(out).strip()


def read_markdown(source: Path) -> str:
    """Read a document back as Markdown, for previewing without opening Word.

    This is a reader for documents ClipDesk wrote, not a general converter. The
    instructions the template carries are left out — they tell an author how to
    fill the form in, which is not what someone previewing the result wants —
    and a section still showing its grey prompt is rendered in italics so an
    unfilled field is visible as one.
    """
    with zipfile.ZipFile(source) as archive:
        body = archive.read("word/document.xml").decode("utf-8")
        try:
            numbering = archive.read("word/numbering.xml").decode("utf-8")
        except KeyError:
            numbering = ""

    kinds = _list_kinds(numbering)
    paragraphs = _PARAGRAPH_RE.findall(_TABLE_RE.sub("", body))
    # Anything before the first heading is the form's own preamble.
    first = next(
        (index for index, item in enumerate(paragraphs) if _HEADING_RE.search(item)), 0
    )

    counters: dict[str, int] = {}
    lines: list[str] = []

    for paragraph in paragraphs[first:]:
        heading = _HEADING_RE.search(paragraph)
        text = _paragraph_markdown(paragraph, emphasis=heading is None)
        if not text:
            continue

        num_id = _NUM_ID_RE.search(paragraph)
        if heading:
            counters.clear()
            level = "#" * min(6, int(heading.group(1)))
            lines += ["", f"{level} {_HEADING_NOTE_RE.sub('', text)}", ""]
        elif num_id and kinds.get(num_id.group(1)) == "decimal":
            counters[num_id.group(1)] = counters.get(num_id.group(1), 0) + 1
            lines.append(f"{counters[num_id.group(1)]}. {text}")
        elif num_id:
            lines.append(f"- {text}")
        elif '<w:rStyle w:val="PlaceholderText"/>' in paragraph:
            lines += [f"*{text}*", ""]
        else:
            counters.clear()
            lines += [text, ""]

    return "\n".join(lines).strip() + "\n"
