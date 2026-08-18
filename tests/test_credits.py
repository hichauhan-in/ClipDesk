"""Pricing token counts as GitHub AI Credits.

The numbers here are a bill, so the failure modes that matter are silent ones:
a model matched to the wrong price, a suffix that stops a model being
recognised, or an unlisted model quietly costing nothing.
"""

from __future__ import annotations

import pytest

from clipdesk.llm.credits import CREDIT_USD, PRICES, credits_for, credits_for_tokens, price_for


def test_a_credit_is_a_cent():
    """The whole conversion rests on this, so it is worth stating once."""
    assert CREDIT_USD == 0.01


@pytest.mark.parametrize(
    "model, expected",
    [
        ("gpt-5-mini", (0.25, 2.00)),
        ("claude-sonnet-4.6", (3.00, 15.00)),
        ("claude-opus-5", (5.00, 25.00)),
        ("grok-4.6", (2.00, 6.00)),
        ("gemini-3.5-flash", (1.50, 9.00)),
    ],
)
def test_published_prices_are_matched(model, expected):
    assert price_for(model) == expected


def test_a_preview_suffix_does_not_hide_the_price():
    """Providers append -preview and friends; the rate is the same."""
    assert price_for("gemini-3.1-pro-preview") == price_for("gemini-3.1-pro")


def test_a_longer_name_wins_over_a_prefix_of_it():
    """gpt-5.4-mini must not be priced as gpt-5.4, which costs three times more."""
    assert price_for("gpt-5.4-mini") == (0.75, 4.50)
    assert price_for("gpt-5.4") == (2.50, 15.00)


def test_an_unlisted_model_is_admitted_rather_than_guessed():
    assert price_for("oswe-vscode-modelD") is None

    priced = credits_for({"oswe-vscode-modelD": {"prompt": 10_000, "completion": 5_000}})

    assert priced["credits"] == 0
    assert priced["unpriced"] == ["oswe-vscode-modelD"]


def test_credits_are_the_dollar_cost_in_cents():
    """One million in and one million out of GPT-5 mini is $0.25 + $2.00."""
    priced = credits_for({"gpt-5-mini": {"prompt": 1_000_000, "completion": 1_000_000}})

    assert priced["usd"] == pytest.approx(2.25)
    assert priced["credits"] == pytest.approx(225.0)


def test_each_model_is_priced_at_its_own_rate():
    priced = credits_for(
        {
            "gpt-5-mini": {"prompt": 1_000_000, "completion": 0},  # $0.25
            "claude-opus-5": {"prompt": 0, "completion": 1_000_000},  # $25.00
        }
    )

    assert priced["usd"] == pytest.approx(25.25)


def test_nothing_recorded_costs_nothing():
    assert credits_for(None)["credits"] == 0
    assert credits_for({})["credits"] == 0


def test_output_is_never_cheaper_than_input():
    """A table typo that swapped the pair would otherwise pass unnoticed."""
    for model, (prompt_price, completion_price) in PRICES.items():
        assert completion_price > prompt_price, model

def test_a_single_model_total_recorded_before_the_split_existed_still_prices():
    """Older projects kept the models but not the tokens each one used."""
    priced = credits_for_tokens(
        {"total_tokens": 2_000_000, "prompt_tokens": 1_000_000,
         "completion_tokens": 1_000_000, "models": ["gpt-5-mini"]}
    )

    assert priced["usd"] == pytest.approx(2.25)
    assert priced["unpriced"] == []


def test_an_old_multi_model_total_is_flagged_rather_than_split():
    """There is no honest way to divide one total between two rates."""
    priced = credits_for_tokens(
        {"total_tokens": 7194, "prompt_tokens": 6662, "completion_tokens": 532,
         "models": ["gemini-3.1-pro-preview", "gpt-5-mini"]}
    )

    assert priced["credits"] == 0
    assert priced["unpriced"] == ["gemini-3.1-pro-preview", "gpt-5-mini"]
