"""Answering a question about a recording that has already been analysed.

The analysis is the expensive part and it has already been paid for, so a
question is answered from the report -- summary, chapters, decisions, action
items -- rather than by sending the transcript again. That keeps a question
cheap enough to ask casually, which is the only way a Q&A box gets used.

Two modes, because both are legitimate and conflating them is not:

* **Grounded** -- answer only from the recording, and say plainly when it does
  not cover something. The honest answer to "did they mention X" is often "no".
* **Open** -- general knowledge is allowed, but anything the recording did not
  say has to be gathered under a heading that says so, so the reader can always
  tell what came from the speaker and what came from the model.
"""

from __future__ import annotations

from dataclasses import dataclass

from clipdesk.models import AnalysisReport

#: Where the model must put anything the recording did not cover. Matched back
#: in the UI to show the disclaimer, so it is a contract, not a suggestion.
BEYOND_HEADING = "Beyond the recording"

#: A question longer than this is a prompt-injection surface rather than a
#: question, and nothing useful needs more.
MAX_QUESTION_CHARS = 2000

_GROUNDED = (
    "You answer questions about a recording, using only the material provided.\n"
    "Rules:\n"
    "- Use only what the material states. Do not add outside knowledge.\n"
    "- If the material does not cover the question, say so plainly in one "
    "sentence. Do not guess and do not pad.\n"
    "- Cite chapter titles or timestamps when they support the answer.\n"
    "- Be direct. No preamble, no restating the question."
)

_OPEN = (
    "You answer questions about a recording. You may use general knowledge, but "
    "the reader must always be able to tell the two apart.\n"
    "Rules:\n"
    "- Answer from the material first, and cite chapter titles or timestamps.\n"
    f"- Put anything the recording did not cover under a final '## {BEYOND_HEADING}' "
    "heading. Never mix it into the earlier text.\n"
    "- Omit that heading entirely when the material answers the question on its own.\n"
    "- Be direct. No preamble, no restating the question."
)


@dataclass(frozen=True, slots=True)
class Answer:
    text: str
    #: True when the model went outside the recording, so the UI can say so.
    went_beyond: bool
    grounded: bool


def build_context(report: AnalysisReport, *, limit: int = 12000) -> str:
    """The recording, as much of it as fits, cheapest and most useful first."""
    parts: list[str] = []
    if report.abstract:
        parts.append(f"ABSTRACT\n{report.abstract}")
    if report.summary:
        parts.append(f"SUMMARY\n{report.summary}")
    if report.keywords:
        parts.append("KEYWORDS\n" + ", ".join(report.keywords))

    chapters: list[str] = []
    for chapter in report.chapters:
        block = [f"[{_clock(chapter.start)}-{_clock(chapter.end)}] {chapter.title}"]
        if chapter.summary:
            block.append(f"  {chapter.summary}")
        for point in chapter.key_points or []:
            block.append(f"  - {point}")
        chapters.append("\n".join(block))
    if chapters:
        parts.append("CHAPTERS\n" + "\n".join(chapters))

    if report.decisions:
        parts.append(
            "DECISIONS\n" + "\n".join(f"- {decision.text}" for decision in report.decisions)
        )
    if report.action_items:
        parts.append(
            "ACTION ITEMS\n"
            + "\n".join(
                f"- {item.text}" + (f" (owner: {item.owner})" if item.owner else "")
                for item in report.action_items
            )
        )

    context = "\n\n".join(parts)
    # Trimmed from the end, where the least important material sits, so a very
    # long recording still answers rather than failing.
    return context[:limit]


def _clock(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def ask(report: AnalysisReport, question: str, llm, *, grounded: bool = True) -> Answer:
    """Answer ``question`` about ``report``."""
    question = (question or "").strip()
    if not question:
        raise ValueError("Ask a question first.")
    if len(question) > MAX_QUESTION_CHARS:
        raise ValueError(
            f"That question is too long — keep it under {MAX_QUESTION_CHARS} characters."
        )

    context = build_context(report)
    if not context:
        raise ValueError("This recording has not been analysed yet, so there is nothing to ask about.")

    prompt = (
        f"MATERIAL FROM THE RECORDING\n{context}\n\n"
        f"QUESTION\n{question}"
    )
    text = llm.for_task("ask").complete(
        _GROUNDED if grounded else _OPEN, prompt, temperature=0.3
    ).strip()

    return Answer(
        text=text,
        went_beyond=not grounded and BEYOND_HEADING.lower() in text.lower(),
        grounded=grounded,
    )
