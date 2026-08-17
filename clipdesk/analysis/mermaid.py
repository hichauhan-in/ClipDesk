"""Keep generated Mermaid diagrams renderable.

A model asked for a diagram usually produces valid Mermaid, and when it does not
the failure is loud: GitHub and VS Code draw a red parse error where the picture
should be, which is worse than having no diagram at all.

Nearly all of those failures come from one cause — punctuation inside a node
label. Brackets, quotes and pipes all mean something to the parser, so a label
carrying any of them is quoted. A block that cannot be salvaged is dropped
rather than shipped broken.
"""

from __future__ import annotations

import re

#: First token of the first line. Anything else is not a diagram we can trust.
DIAGRAM_TYPES: frozenset[str] = frozenset(
    {
        "flowchart",
        "graph",
        "sequenceDiagram",
        "classDiagram",
        "stateDiagram",
        "stateDiagram-v2",
        "erDiagram",
        "journey",
        "gantt",
        "pie",
        "mindmap",
        "timeline",
        "gitGraph",
        "quadrantChart",
        "requirementDiagram",
        "sankey-beta",
        "block-beta",
        "C4Context",
    }
)

#: Characters that end a label early unless the whole label is quoted.
_RISKY = set("()[]{}|<>#\"'`,;:")

_FENCE_RE = re.compile(r"^```(\w*)\s*$")
#: A label between single brackets. Nested brackets are excluded so the match
#: stops at the first closer, which is where the parser stops too.
_SQUARE_RE = re.compile(r"\[([^\[\]\n]*)\]")
_BRACE_RE = re.compile(r"\{([^{}\n]*)\}")
_EMPHASIS_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
#: "end" is a keyword. As a node id it breaks the parse; alone on a line it
#: closes a subgraph and must be left alone.
_END_ID_RE = re.compile(r"(?<![\w-])end(?=\s*(?:\[|\(|\{|--|==|-\.))")


def _clean_label(label: str) -> str:
    text = _EMPHASIS_RE.sub(lambda match: match.group(1) or match.group(2), label).strip()
    if not text or (text.startswith('"') and text.endswith('"')):
        return text
    if not any(char in _RISKY for char in text):
        return text
    # A quote inside a quoted label needs the entity form; Mermaid has no escape.
    return '"' + text.replace('"', "#quot;") + '"'


def _quote_labels(line: str) -> str:
    line = _SQUARE_RE.sub(lambda m: f"[{_clean_label(m.group(1))}]", line)
    return _BRACE_RE.sub(lambda m: f"{{{_clean_label(m.group(1))}}}", line)


def repair(body: str) -> str | None:
    """Return a renderable version of one diagram, or ``None`` to drop it."""
    lines = [line.rstrip() for line in body.strip("\n").split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return None

    header = lines[0].strip()
    kind = header.split(maxsplit=1)[0]
    if kind not in DIAGRAM_TYPES:
        return None
    if not any(line.strip() for line in lines[1:]):
        return None
    # Brackets and braces are node syntax only in a flowchart. Elsewhere they
    # mean something else — a class body, a note — and quoting would break it.
    if kind not in ("flowchart", "graph"):
        return "\n".join(lines).strip("\n")

    fixed = [header]
    for line in lines[1:]:
        if not line.strip():
            fixed.append("")
            continue
        fixed.append(_END_ID_RE.sub("endStep", _quote_labels(line)))
    return "\n".join(fixed).strip("\n")


def repair_markdown(markdown: str) -> str:
    """Repair every ```mermaid block in a document, dropping any that cannot be."""
    if "```mermaid" not in markdown:
        return markdown

    out: list[str] = []
    lines = markdown.replace("\r\n", "\n").split("\n")
    index = 0
    while index < len(lines):
        fence = _FENCE_RE.match(lines[index])
        if not fence or fence.group(1) != "mermaid":
            out.append(lines[index])
            index += 1
            continue

        index += 1
        body: list[str] = []
        while index < len(lines) and not lines[index].startswith("```"):
            body.append(lines[index])
            index += 1
        index += 1  # the closing fence

        diagram = repair("\n".join(body))
        if diagram is not None:
            out += ["```mermaid", diagram, "```"]
        elif out and out[-1].strip():
            # Dropping the block would otherwise weld the paragraphs either side
            # of it together.
            out.append("")
    return "\n".join(out)
