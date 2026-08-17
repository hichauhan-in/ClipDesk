"""The Copilot CLI answers as a JSONL event stream, not plain text — these cover
the parsing that turns that stream back into an answer."""

import json
from types import SimpleNamespace

from clipdesk.config import CopilotCliConfig
from clipdesk.llm.base import ChatMessage
from clipdesk.llm.copilot_cli import (
    CopilotCliProvider,
    _flatten,
    _inline_budget,
    parse_cli_events,
)


def event(kind, **data):
    if kind == "assistant.message":
        return json.dumps({"type": kind, "data": data})
    return json.dumps({"type": kind, **data})


def test_final_answer_is_extracted():
    stream = "\n".join(
        [
            event("session.mcp_servers_loaded"),
            event("assistant.message", content="hello", phase="final_answer"),
            event("result", exitCode=0),
        ]
    )
    assert parse_cli_events(stream) == ("hello", "")


def test_final_answer_wins_over_intermediate_messages():
    stream = "\n".join(
        [
            event("assistant.message", content="let me think", phase="thinking"),
            event("assistant.message", content="the answer", phase="final_answer"),
        ]
    )
    assert parse_cli_events(stream)[0] == "the answer"


def test_intermediate_message_is_used_when_there_is_no_final():
    stream = event("assistant.message", content="partial", phase="thinking")
    assert parse_cli_events(stream)[0] == "partial"


def test_non_json_noise_is_ignored():
    stream = "\n".join(
        [
            "Loading MCP servers...",
            event("assistant.message", content="clean", phase="final_answer"),
            "AI Credits 16.4 (17s)",
        ]
    )
    assert parse_cli_events(stream)[0] == "clean"


def test_a_json_payload_survives_being_the_answer():
    stream = event("assistant.message", content='{"ok": true}', phase="final_answer")
    assert parse_cli_events(stream)[0] == '{"ok": true}'


def test_failure_detail_is_surfaced():
    stream = "\n".join(
        [event("error", message="rate limited"), event("result", exitCode=1)]
    )
    answer, failure = parse_cli_events(stream)
    assert answer == ""
    assert failure == "rate limited"


def test_empty_stream_yields_nothing():
    assert parse_cli_events("") == ("", "")


# --- prompt flattening -------------------------------------------------------
def test_system_and_user_are_joined_plainly():
    # Meta-framing around the prompt measurably increases the CLI's tendency to
    # reply "no text was supplied", so the layout stays deliberately bare.
    prompt = _flatten([ChatMessage("system", "Be terse."), ChatMessage("user", "Hi")])
    assert prompt == "Be terse.\n\nHi"


def test_assistant_turns_are_labelled():
    prompt = _flatten(
        [
            ChatMessage("system", "Rules"),
            ChatMessage("user", "Q"),
            ChatMessage("assistant", "A"),
        ]
    )
    assert "[your previous answer]\nA" in prompt


def test_blank_messages_are_dropped():
    assert _flatten([ChatMessage("system", "  "), ChatMessage("user", "only this")]) == "only this"


# The CLI prints startup failures as plain text, never as an event. Without this
# the user sees "returned no answer" and no reason, which is the least useful
# possible error for the most likely mistake: a model name that does not exist.
def test_a_plain_text_startup_error_is_surfaced():
    stdout = 'Error: Model "made-up" from --model flag is not available.'

    answer, failure = parse_cli_events(stdout)

    assert answer == ""
    assert "made-up" in failure


def test_a_real_failure_event_wins_over_the_plain_text_line():
    stdout = "\n".join(
        [
            "Error: something printed early",
            json.dumps({"type": "error", "message": "the actual failure"}),
        ]
    )

    _, failure = parse_cli_events(stdout)

    assert failure == "the actual failure"


def test_stray_plain_text_does_not_lose_a_good_answer():
    stdout = "\n".join(
        [
            "Welcome to Copilot",
            json.dumps(
                {
                    "type": "assistant.message",
                    "data": {"content": '{"ok": true}', "phase": "final_answer"},
                }
            ),
        ]
    )

    answer, failure = parse_cli_events(stdout)

    assert answer == '{"ok": true}'
    assert failure == ""


# --- how the prompt gets to the CLI ------------------------------------------
# npm and the VS Code bundle install the CLI as a .BAT, and Windows runs a batch
# file through cmd.exe, which refuses a command line over 8191 characters. That
# is small enough that an ordinary transcript window exceeds it, and the failure
# is an opaque "The command line is too long."
def test_a_batch_shim_gets_a_small_inline_budget():
    assert _inline_budget(r"C:\tools\copilot.BAT") < 8191


def test_the_shim_check_is_case_insensitive():
    assert _inline_budget(r"C:\tools\copilot.bat") == _inline_budget(r"C:\tools\copilot.BAT")


def test_a_cmd_shim_is_treated_the_same_as_a_bat():
    assert _inline_budget(r"C:\tools\copilot.cmd") == _inline_budget(r"C:\tools\copilot.bat")


def test_a_real_executable_gets_the_larger_budget():
    assert _inline_budget("/usr/local/bin/copilot") > _inline_budget(r"C:\tools\copilot.bat")


def test_the_direct_budget_still_respects_the_process_limit():
    assert _inline_budget("/usr/local/bin/copilot") < 32_000


def test_model_effort_and_context_are_forwarded_to_the_cli(monkeypatch):
    from clipdesk.llm import copilot_cli as cli_module

    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)
    provider = CopilotCliProvider(
        CopilotCliConfig(
            model="gpt-test",
            reasoning_effort="high",
            context_window="long_context",
        )
    )

    provider._invoke("copilot", "short prompt")

    command = captured["command"]
    assert command[command.index("--model") + 1] == "gpt-test"
    assert command[command.index("--effort") + 1] == "high"
    assert command[command.index("--context") + 1] == "long_context"

