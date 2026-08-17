"""The deflection guard: the CLI's coding-agent persona sometimes replies asking
for input that was already in the prompt."""

import pytest

from clipdesk.llm.copilot_cli import _looks_like_deflection


@pytest.mark.parametrize(
    "reply",
    [
        "Please provide the recording and the analysis criteria.",
        "No text or requested output format was included.",
        "No text-processing instructions or source text were provided.",
        "I need the transcript before I can continue.",
        "Could you share the file you want analysed?",
        "Ready to analyze the corporate recording.",
        "Ready to process the transcript when you send it.",
        "Awaiting the transcript.",
    ],
)
def test_deflections_are_recognised(reply):
    assert _looks_like_deflection(reply) is True


@pytest.mark.parametrize(
    "reply",
    [
        '{"segments": []}',
        "[1, 2, 3]",
        "# Notes\n\nThe retry policy uses exponential backoff.",
        "| Time | Topic |\n| --- | --- |",
        "",
        "   ",
    ],
)
def test_real_answers_are_not_flagged(reply):
    assert _looks_like_deflection(reply) is False


def test_a_long_answer_that_merely_mentions_a_marker_is_not_flagged():
    # "was provided" appearing inside genuine prose must not trip the guard.
    reply = (
        "The session covered the checkout retry policy in detail. Context was provided "
        "on why the previous attempt was reverted, and the team agreed the idempotency "
        "key is what makes retrying a payment safe. " * 3
    )
    assert _looks_like_deflection(reply) is False
