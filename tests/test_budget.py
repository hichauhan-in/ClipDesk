"""Spending fewer tokens, and knowing how many were spent.

The budget only earns its place if the numbers actually fall, so these check the
levers rather than the labels: fewer windows, smaller prompts, cheaper models.
"""

import pytest

from clipdesk.analysis.windows import build_windows
from clipdesk.llm.base import ChatMessage, Completion, ProviderStatus, Usage, estimate_usage
from clipdesk.llm.budget import (
    LEVELS,
    MAX_LEVEL,
    TokenMeter,
    budget_for,
    pick_model,
    rank_models,
    tier_options,
)
from clipdesk.llm.registry import LLMClient
from clipdesk.models import TranscriptSegment


class FakeProvider:
    key = "fake"
    label = "Fake"

    def __init__(self, models=("gpt-4o-mini", "gpt-4o", "claude-opus-4"), usage=None):
        self.models = list(models)
        self.calls = []
        self.usage = usage or Usage(100, 20, measured=True)

    def status(self):
        return ProviderStatus(
            "fake", "Fake", True, models=self.models, active_model=self.models[0]
        )

    def complete(self, messages, *, temperature=0.2, max_tokens=None, expect_json=False, model=None):
        self.calls.append({"model": model, "messages": messages})
        return Completion("ok", self.usage)


def segments(count=400, words=12):
    return [
        TranscriptSegment(id=index, start=index * 3.0, end=index * 3.0 + 3.0, text=" ".join(["word"] * words))
        for index in range(count)
    ]


# --- the levels are ordered ---------------------------------------------------
def test_spending_less_means_fewer_larger_windows():
    # Counter-intuitive but the point of the whole thing: the system prompt and
    # the overlap are paid once per window, so fewer windows is cheaper.
    sizes = [budget_for(level).window_chars for level in range(MAX_LEVEL + 1)]

    assert sizes == sorted(sizes, reverse=True)


def test_every_lever_moves_the_same_way_across_the_levels():
    notes = [budget_for(level).notes_word_target for level in range(MAX_LEVEL + 1)]
    sections = [budget_for(level).notes_section_chars for level in range(MAX_LEVEL + 1)]
    article = [budget_for(level).article_transcript_chars for level in range(MAX_LEVEL + 1)]

    assert notes == sorted(notes)
    assert sections == sorted(sections)
    assert article == sorted(article)


def test_the_cheapest_level_drops_what_is_optional():
    cheapest = budget_for(0)

    assert cheapest.include_diagrams is False
    assert cheapest.include_timestamps is False
    assert cheapest.max_enrichment == 0


def test_a_level_outside_the_range_is_clamped():
    assert budget_for(-5).level == 0
    assert budget_for(99).level == MAX_LEVEL


# --- it actually costs less --------------------------------------------------
def test_the_cheapest_level_sends_fewer_windows_than_the_dearest():
    body = segments()
    cheap = budget_for(0)
    dear = budget_for(MAX_LEVEL)

    few = build_windows(body, window_chars=cheap.window_chars, overlap_chars=cheap.window_overlap_chars)
    many = build_windows(body, window_chars=dear.window_chars, overlap_chars=dear.window_overlap_chars)

    assert len(few) < len(many)


def test_the_cheapest_level_sends_fewer_characters_in_total():
    body = segments()
    totals = []
    for level in (0, MAX_LEVEL):
        budget = budget_for(level)
        windows = build_windows(
            body, window_chars=budget.window_chars, overlap_chars=budget.window_overlap_chars
        )
        # What reaches the model: every window body, plus the prompt each time.
        totals.append(sum(len(window.render()) for window in windows) + len(windows) * 1600)

    assert totals[0] < totals[1]


def test_a_long_recording_gets_wider_windows_at_the_same_level():
    short = budget_for(2, duration_s=20 * 60)
    long = budget_for(2, duration_s=3 * 3600)

    assert long.window_chars > short.window_chars
    assert long.level == short.level


def test_the_widening_stops_somewhere_sensible():
    enormous = budget_for(2, duration_s=40 * 3600)

    assert enormous.window_chars <= budget_for(2).window_chars * 2


# --- choosing a model --------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "tier"),
    [
        ("gpt-4o-mini", "small"),
        ("o3-mini", "small"),
        ("gemini-2.0-flash", "small"),
        ("claude-haiku-4.5", "small"),
        ("copilot-utility-small", "small"),
        ("gpt-4o", "balanced"),
        ("gpt-4.1", "balanced"),
        ("claude-3.5-sonnet", "balanced"),
        ("claude-sonnet-4.6", "balanced"),
        ("grok-4.6", "balanced"),
        ("claude-opus-4.1", "strong"),
        ("gpt-5.5", "strong"),
        ("gemini-3.1-pro-preview", "strong"),
    ],
)
def test_models_are_graded_by_name(name, tier):
    assert name in rank_models([name])[tier]


def test_a_word_inside_another_word_is_not_a_marker():
    # "gemini" contains "mini". Grading every Gemini model as the cheap option
    # sent the mechanical passes to a Pro model.
    assert rank_models(["gemini-3.1-pro-preview"])["small"] == []


def test_a_small_model_from_a_large_family_is_still_small():
    assert rank_models(["gpt-5-mini"])["small"] == ["gpt-5-mini"]


def test_an_unknown_model_is_treated_as_middling():
    # Being wrong in the middle is cheap; being wrong at either end is not.
    assert rank_models(["some-new-thing"])["balanced"] == ["some-new-thing"]


def test_a_missing_tier_falls_towards_the_middle():
    only_middle = ["gpt-4o"]

    assert pick_model(only_middle, "small") == "gpt-4o"
    assert pick_model(only_middle, "strong") == "gpt-4o"


def test_no_models_means_no_opinion():
    # An empty answer leaves the provider's own default in place.
    assert pick_model([], "small") == ""


def test_a_provider_that_repeats_itself_is_only_listed_once():
    assert rank_models(["gpt-4o", "gpt-4o", "gpt-4o-mini"])["balanced"] == ["gpt-4o"]


# --- picking an equivalent within a tier -------------------------------------
def test_every_model_of_the_same_size_is_offered():
    models = ["gemini-3.5-flash", "gpt-5-mini", "claude-haiku-4.5", "gpt-4o", "claude-opus-5"]

    options = tier_options(models, "small")

    assert options[:3] == ["gemini-3.5-flash", "gpt-5-mini", "claude-haiku-4.5"]


def test_a_chosen_equivalent_is_used_instead_of_the_default():
    models = ["gemini-3.5-flash", "gpt-5-mini", "claude-haiku-4.5"]

    assert pick_model(models, "small") == "gemini-3.5-flash"
    assert pick_model(models, "small", "gpt-5-mini") == "gpt-5-mini"


def test_a_choice_from_another_size_is_ignored():
    # Letting a "strong" name stand in for the cheapest pass would undo the
    # whole point of the budget.
    models = ["gemini-3.5-flash", "claude-opus-5"]

    assert pick_model(models, "small", "claude-opus-5") == "gemini-3.5-flash"


def test_a_choice_the_provider_no_longer_offers_is_ignored():
    assert pick_model(["gemini-3.5-flash"], "small", "gpt-5-mini") == "gemini-3.5-flash"


def test_the_chosen_equivalent_reaches_the_provider():
    provider = FakeProvider(models=("gemini-3.5-flash", "gpt-5-mini", "claude-opus-5"))
    client = LLMClient(
        provider, budget=budget_for(0), tier_models={"small": "gpt-5-mini"}
    )

    client.for_task("analyse").complete("s", "u")

    assert provider.calls[0]["model"] == "gpt-5-mini"


def test_passes_of_the_same_size_follow_the_same_choice():
    provider = FakeProvider(models=("gemini-3.5-flash", "gpt-5-mini", "claude-opus-5"))
    client = LLMClient(
        provider, budget=budget_for(0), tier_models={"small": "gpt-5-mini"}
    )

    for task in ("analyse", "notes", "clips"):
        client.for_task(task).complete("s", "u")

    assert {call["model"] for call in provider.calls} == {"gpt-5-mini"}


def test_the_mechanical_pass_goes_to_a_cheaper_model_than_the_writing():
    provider = FakeProvider()
    client = LLMClient(provider, budget=budget_for(3))

    client.for_task("analyse").complete("s", "u")
    client.for_task("article").complete("s", "u")

    analyse_model, article_model = (call["model"] for call in provider.calls)
    assert analyse_model != article_model
    assert analyse_model in rank_models(provider.models)["balanced"]
    assert article_model in rank_models(provider.models)["strong"]


def test_turning_auto_off_lets_the_provider_choose():
    provider = FakeProvider()
    LLMClient(provider, budget=None).complete("s", "u")

    assert provider.calls[0]["model"] is None


def test_the_model_list_is_only_fetched_once():
    provider = FakeProvider()
    seen = []
    original = provider.status
    provider.status = lambda: (seen.append(1), original())[1]
    client = LLMClient(provider, budget=budget_for(2))

    for _ in range(5):
        client.complete("s", "u")

    assert len(seen) == 1


# --- counting ----------------------------------------------------------------
def test_usage_is_recorded_against_the_task_that_spent_it():
    provider = FakeProvider(usage=Usage(100, 20, measured=True))
    client = LLMClient(provider, budget=budget_for(2))

    client.for_task("analyse").complete("s", "u")
    client.for_task("notes").complete("s", "u")
    client.for_task("notes").complete("s", "u")
    report = client.meter.to_dict()

    assert report["calls"] == 3
    assert report["total_tokens"] == 360
    assert report["by_task"]["notes"]["calls"] == 2
    assert report["by_task"]["analyse"]["prompt"] == 100


def test_counts_from_the_provider_are_marked_as_measured():
    provider = FakeProvider(usage=Usage(10, 5, measured=True))
    client = LLMClient(provider)
    client.complete("s", "u")

    assert client.meter.to_dict()["measured"] is True


def test_one_estimated_call_makes_the_whole_total_an_estimate():
    provider = FakeProvider(usage=Usage(10, 5, measured=True))
    client = LLMClient(provider)
    client.complete("s", "u")
    provider.usage = Usage(10, 5, measured=False)
    client.complete("s", "u")

    assert client.meter.to_dict()["measured"] is False


def test_an_estimate_is_derived_from_the_text_that_was_sent():
    usage = estimate_usage([ChatMessage("user", "x" * 400)], "y" * 40)

    assert usage.prompt_tokens == 100
    assert usage.completion_tokens == 10
    assert usage.measured is False


def test_the_meter_survives_several_threads():
    import threading

    meter = TokenMeter()
    threads = [
        threading.Thread(target=lambda: [meter.record("analyse", Usage(1, 1, True)) for _ in range(200)])
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert meter.calls == 800
    assert meter.total == 1600


def test_the_models_used_are_remembered():
    meter = TokenMeter()
    meter.record("analyse", Usage(1, 1, True), "gpt-4o-mini")
    meter.record("notes", Usage(1, 1, True), "gpt-4o")
    meter.record("notes", Usage(1, 1, True), "gpt-4o")

    assert meter.to_dict()["models"] == ["gpt-4o", "gpt-4o-mini"]


def test_every_level_describes_itself():
    for budget in LEVELS:
        assert budget.label
        assert budget.note


# --- what the Library shows --------------------------------------------------
def test_usage_accumulates_on_the_project(tmp_path):
    from clipdesk.store import ProjectStore

    project = ProjectStore(tmp_path / "workspace").create("talk.mp4", title="Talk")
    provider = FakeProvider(usage=Usage(100, 20, measured=True))
    client = LLMClient(provider, budget=budget_for(2))
    client.for_task("analyse").complete("s", "u")
    project.record_tokens(client.meter.to_dict())

    later = LLMClient(FakeProvider(usage=Usage(50, 10, measured=True)), budget=budget_for(2))
    later.for_task("notes").complete("s", "u")
    project.record_tokens(later.meter.to_dict())

    tokens = project.meta.tokens
    assert tokens["total_tokens"] == 180
    assert tokens["by_task"]["analyse"]["prompt"] == 100
    assert tokens["by_task"]["notes"]["completion"] == 10
    assert tokens["calls"] == 2


def test_a_total_survives_reloading_the_project(tmp_path):
    from clipdesk.store import ProjectStore

    store = ProjectStore(tmp_path / "workspace")
    project = store.create("talk.mp4", title="Talk")
    project.record_tokens({"calls": 1, "prompt_tokens": 9, "completion_tokens": 1,
                           "total_tokens": 10, "measured": True, "by_task": {}, "models": []})

    assert store.get(project.id).meta.tokens["total_tokens"] == 10


def test_an_action_that_spent_nothing_does_not_write_a_total(tmp_path):
    from clipdesk.store import ProjectStore

    project = ProjectStore(tmp_path / "workspace").create("talk.mp4", title="Talk")
    project.record_tokens(TokenMeter().to_dict())

    assert project.meta.tokens == {}


def test_one_estimated_action_marks_the_project_total_as_estimated(tmp_path):
    from clipdesk.store import ProjectStore

    project = ProjectStore(tmp_path / "workspace").create("talk.mp4", title="Talk")
    measured = LLMClient(FakeProvider(usage=Usage(10, 2, measured=True)))
    measured.complete("s", "u")
    project.record_tokens(measured.meter.to_dict())
    guessed = LLMClient(FakeProvider(usage=Usage(10, 2, measured=False)))
    guessed.complete("s", "u")
    project.record_tokens(guessed.meter.to_dict())

    assert project.meta.tokens["measured"] is False
