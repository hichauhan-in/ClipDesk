"""What a project has spent, and the policy that decides how much it spends.

Two separate things live here because they are two halves of one question.

:class:`TokenMeter` records what was actually used. Where a provider counts for
us — the VS Code bridge counts with the model's own tokenizer, and hosted APIs
report usage — those numbers are exact. Where it does not, they are estimated
from character counts and marked as such, because a number presented as fact
that is really a guess is worse than no number.

:class:`Budget` decides how much to spend before spending it. The levers are the
ones that genuinely change the bill:

* **Window size.** Counter-intuitively, *larger* analysis windows cost less. The
  system prompt and the instruction template are re-sent with every window, and
  consecutive windows deliberately overlap, so twice as many windows means twice
  the fixed overhead plus an extra copy of every overlap.
* **What goes into the prompt.** Timestamps, segment ids and optional
  instructions for diagrams are all real characters that need not be sent when
  the output does not use them.
* **How long the answer may be.** Output is the larger half of the notes bill.
  The only lever that works across every provider is asking for less.
* **Which model answers.** Window analysis is classification, which a small
  model does well; the overview and the article are writing tasks worth a larger
  one.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any

from clipdesk.llm.base import Usage

#: The tasks that spend tokens, in the order a user meets them.
TASKS: tuple[str, ...] = ("analyse", "notes", "article", "clips")

#: What each task is called on the settings screen.
TASK_LABELS: dict[str, str] = {
    "analyse": "Analysis",
    "notes": "Notes",
    "article": "Article",
    "clips": "Clip search",
}


class TokenMeter:
    """Accumulates usage across the many calls one action makes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        #: False as soon as any one call had to be estimated.
        self.measured = True
        self.by_task: dict[str, dict[str, int]] = {}
        self.models: set[str] = set()
        #: Per model, because credits are priced against the model that answered.
        self.by_model: dict[str, dict[str, int]] = {}

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def record(self, task: str, usage: Usage, model: str = "") -> None:
        with self._lock:
            self.calls += 1
            self.prompt_tokens += usage.prompt_tokens
            self.completion_tokens += usage.completion_tokens
            self.measured = self.measured and usage.measured
            entry = self.by_task.setdefault(task, {"calls": 0, "prompt": 0, "completion": 0})
            entry["calls"] += 1
            entry["prompt"] += usage.prompt_tokens
            entry["completion"] += usage.completion_tokens
            if model:
                self.models.add(model)
                priced = self.by_model.setdefault(
                    model, {"calls": 0, "prompt": 0, "completion": 0}
                )
                priced["calls"] += 1
                priced["prompt"] += usage.prompt_tokens
                priced["completion"] += usage.completion_tokens

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "calls": self.calls,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total,
                "measured": self.measured,
                "by_task": {task: dict(entry) for task, entry in self.by_task.items()},
                "by_model": {name: dict(entry) for name, entry in self.by_model.items()},
                "models": sorted(self.models),
            }


# --- the policy --------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Budget:
    level: int
    label: str
    note: str
    #: Bigger windows mean fewer of them, and the fixed cost is paid per window.
    window_chars: int
    window_overlap_chars: int
    #: Transcript characters one notes section may carry.
    notes_section_chars: int
    article_transcript_chars: int
    #: Words the model is asked to stay within, per notes section.
    notes_word_target: int
    #: Send segment timestamps where the output does not need them.
    include_timestamps: bool
    include_diagrams: bool
    #: Highest enrichment level allowed. Enrichment multiplies output length.
    max_enrichment: int
    #: Which end of the model list to choose from, per task.
    model_tier: dict[str, str] = field(default_factory=dict)

    def tier_for(self, task: str) -> str:
        return self.model_tier.get(task, "balanced")


#: 0 spends least. 4 is what ClipDesk did before any of this existed.
LEVELS: tuple[Budget, ...] = (
    Budget(
        level=0,
        label="Fewest tokens",
        note="Big windows, no overlap, short answers, a small model. Enough for "
        "chapters and a clean cut.",
        window_chars=26000,
        window_overlap_chars=0,
        notes_section_chars=3500,
        article_transcript_chars=9000,
        notes_word_target=250,
        include_timestamps=False,
        include_diagrams=False,
        max_enrichment=0,
        model_tier={"analyse": "small", "notes": "small", "article": "small", "clips": "small"},
    ),
    Budget(
        level=1,
        label="Lean",
        note="Noticeably cheaper, with shorter notes and no diagrams.",
        window_chars=18000,
        window_overlap_chars=200,
        notes_section_chars=5500,
        article_transcript_chars=14000,
        notes_word_target=400,
        include_timestamps=False,
        include_diagrams=False,
        max_enrichment=1,
        model_tier={"analyse": "small", "notes": "small", "article": "balanced", "clips": "small"},
    ),
    Budget(
        level=2,
        label="Balanced",
        note="The default. Full notes and diagrams, with the mechanical passes "
        "sent to a smaller model.",
        window_chars=13000,
        window_overlap_chars=400,
        notes_section_chars=8000,
        article_transcript_chars=20000,
        notes_word_target=650,
        include_timestamps=True,
        include_diagrams=True,
        max_enrichment=4,
        model_tier={
            "analyse": "small",
            "notes": "balanced",
            "article": "balanced",
            "clips": "small",
        },
    ),
    Budget(
        level=3,
        label="Thorough",
        note="Tighter windows for a more careful read of long recordings.",
        window_chars=9000,
        window_overlap_chars=600,
        notes_section_chars=11000,
        article_transcript_chars=24000,
        notes_word_target=900,
        include_timestamps=True,
        include_diagrams=True,
        max_enrichment=6,
        model_tier={
            "analyse": "balanced",
            "notes": "balanced",
            "article": "strong",
            "clips": "balanced",
        },
    ),
    Budget(
        level=4,
        label="Best quality",
        note="No economising. The largest model available for every pass.",
        window_chars=7000,
        window_overlap_chars=900,
        notes_section_chars=14000,
        article_transcript_chars=32000,
        notes_word_target=1200,
        include_timestamps=True,
        include_diagrams=True,
        max_enrichment=6,
        model_tier={
            "analyse": "strong",
            "notes": "strong",
            "article": "strong",
            "clips": "strong",
        },
    ),
)

MAX_LEVEL = len(LEVELS) - 1


def budget_for(level: int, *, duration_s: float = 0.0) -> Budget:
    """The budget for a level, widened for recordings long enough to need it.

    A three-hour recording at the same level as a five-minute one would pay the
    per-window overhead dozens more times, so long recordings get proportionally
    larger windows. The level still decides the trade-off; the length decides how
    hard it has to be applied.
    """
    chosen = LEVELS[max(0, min(MAX_LEVEL, int(level)))]
    hours = duration_s / 3600.0
    if hours <= 1.0:
        return chosen
    # Grow with length but stop somewhere a model will still read carefully.
    scale = min(2.0, 1.0 + (hours - 1.0) * 0.35)
    return replace_window(chosen, int(chosen.window_chars * scale))


def replace_window(budget: Budget, window_chars: int) -> Budget:
    return Budget(
        level=budget.level,
        label=budget.label,
        note=budget.note,
        window_chars=window_chars,
        window_overlap_chars=budget.window_overlap_chars,
        notes_section_chars=budget.notes_section_chars,
        article_transcript_chars=budget.article_transcript_chars,
        notes_word_target=budget.notes_word_target,
        include_timestamps=budget.include_timestamps,
        include_diagrams=budget.include_diagrams,
        max_enrichment=budget.max_enrichment,
        model_tier=budget.model_tier,
    )


# --- choosing a model --------------------------------------------------------
# Markers are matched against the name's *words*, not as substrings: "gemini"
# contains "mini", which would grade every Gemini model as the cheap option.
# Version numbers are not markers either — "3.5" would grade claude-3.5-sonnet
# as cheap, and "4.1" would grade gpt-4.1 as the dearest thing available.
_SMALL = frozenset({"mini", "haiku", "flash", "lite", "small", "nano", "tiny", "8b", "phi"})
_STRONG = frozenset({"opus", "ultra", "pro", "max", "thinking", "codex"})
#: Families whose whole line is a large model, matched on the leading name.
_STRONG_PREFIX = ("gpt-5", "o1", "o3")

_WORDS_RE = re.compile(r"[^a-z0-9]+")


def _words(name: str) -> set[str]:
    return {word for word in _WORDS_RE.split(name.lower()) if word}


def rank_models(models: list[str]) -> dict[str, list[str]]:
    """Split the available models into small / balanced / strong."""
    tiers: dict[str, list[str]] = {"small": [], "balanced": [], "strong": []}
    for name in dict.fromkeys(models):  # providers do repeat themselves
        words = _words(name)
        lowered = name.lower()
        # Cheap wins ties: "gpt-5-mini" is a small model from a large family.
        if words & _SMALL:
            tiers["small"].append(name)
        elif words & _STRONG or lowered.startswith(_STRONG_PREFIX):
            tiers["strong"].append(name)
        else:
            tiers["balanced"].append(name)
    return tiers


def tier_options(models: list[str], tier: str) -> list[str]:
    """Every model that would serve for a tier, best match first.

    The fallback order matters: falling towards the middle rather than to an
    extreme means a missing small model does not silently promote a cheap pass
    to the dearest thing on offer.
    """
    if not models:
        return []
    tiers = rank_models(models)
    order = {
        "small": ("small", "balanced", "strong"),
        "balanced": ("balanced", "small", "strong"),
        "strong": ("strong", "balanced", "small"),
    }[tier]
    ranked: list[str] = []
    for name in order:
        ranked += tiers[name]
    return ranked


def pick_model(models: list[str], tier: str, preferred: str = "") -> str:
    """The model to use for a tier, honouring a choice the user made for it.

    A stored preference only counts while it is still offered and still belongs
    to the tier it was chosen for; otherwise the tier's own best match is used,
    because a "cheapest" pass quietly running on an expensive model is exactly
    what this is meant to prevent.
    """
    options = tier_options(models, tier)
    if not options:
        return ""
    if preferred and preferred in rank_models(models).get(tier, []):
        return preferred
    return options[0]
