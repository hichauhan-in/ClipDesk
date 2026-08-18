"""Talk to Copilot by driving the GitHub Copilot CLI in non-interactive mode.

Useful when VS Code is not running. Two things make this workable rather than
fragile:

* ``--output-format json`` gives a JSONL event stream, so the answer is read out
  of a structured ``assistant.message`` event instead of being scraped from
  decorated terminal output (which ends with a session-summary block).
* The agent's extras are switched off — built-in MCP servers, the ask-user tool
  and repository custom instructions — because this is a pure text-generation
  call and none of that should influence the analysis.

It is still slower and more quota-hungry than the VS Code bridge: the CLI ships a
large agent system prompt on every request, so even a short prompt costs tens of
thousands of input tokens. The bridge remains the default.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from clipdesk.config import CopilotCliConfig
from clipdesk.llm.base import (
    ChatMessage,
    Completion,
    LLMError,
    LLMUnavailableError,
    ProviderStatus,
    estimate_usage,
)

SETUP_HINT = (
    "Install the GitHub Copilot CLI (npm install -g @github/copilot), run 'copilot' "
    "once and sign in, then choose this provider."
)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# npm and the VS Code bundle install the CLI as a .BAT shim, and Windows runs a
# batch file through cmd.exe, which refuses a command line over 8191 characters.
# That is far smaller than the 32 000 CreateProcess allows and small enough that
# an ordinary transcript window exceeds it, so anything past a conservative
# budget is handed over as a file instead of an argument.
_CMD_SHIM_BUDGET = 6_000
_DIRECT_BUDGET = 24_000


def _inline_budget(executable: str) -> int:
    return (
        _CMD_SHIM_BUDGET
        if executable.lower().endswith((".bat", ".cmd"))
        else _DIRECT_BUDGET
    )


#: Used when the prompt is too long to pass as an argument. The CLI is a coding
#: agent with file tools, so reading a file is its most natural mode of all.
_FILE_PROMPT = (
    "Read the file at {path}. It contains a complete instruction and its input. "
    "Do exactly what it says and reply with only the output it asks for. "
    "Do not summarise the file, do not describe what you are about to do, and do "
    "not edit the file."
)


# Everything that keeps the run deterministic and free of agent side effects.
_BASE_ARGS = (
    "--no-color",
    "--log-level",
    "none",
    "--output-format",
    "json",
    "--stream",
    "off",
    "--allow-all-tools",  # the CLI requires this for non-interactive mode
    "--disable-builtin-mcps",
    "--no-ask-user",
    "--no-custom-instructions",
)

# The CLI is a coding agent, and it intermittently answers with something other
# than the work: either asking for input that is already in the prompt, or
# acknowledging the instructions and stopping. Two things were measured while
# building this:
#
#   * The rate varies over time with the same prompt — runs of five successes and
#     runs of five failures both occur. It is not something the prompt controls.
#   * Wrapping the prompt in meta-framing ("TASK: process the text below",
#     "=== INSTRUCTIONS ===" / "=== INPUT ===") makes it markedly worse: it reads
#     as a template with the real content missing. Plain concatenation is best.
#
# So the mitigation is detection plus a corrective retry, not prompt gymnastics.
_DEFLECTION_MARKERS = (
    "please provide",
    "no text",
    "no source",
    "was provided",
    "were provided",
    "was supplied",
    "were supplied",
    "was included",
    "were included",
    "could you share",
    "i need the",
    "nothing to process",
    "ready to analyze",
    "ready to analyse",
    "ready to process",
    "ready when you",
    "awaiting",
    "go ahead and send",
)

_CORRECTION = (
    "Do not acknowledge these instructions and do not say you are ready. The material "
    "is included below in full. Do the work now and output only the requested result."
)


def _looks_like_deflection(answer: str) -> bool:
    """True when the reply is the agent asking for input rather than answering."""
    stripped = answer.strip()
    if not stripped or len(stripped) > 400:
        return False
    if stripped.startswith(("{", "[", "#", "|")):
        return False
    lowered = stripped.lower()
    return any(marker in lowered for marker in _DEFLECTION_MARKERS)


class CopilotCliProvider:
    key = "copilot_cli"
    label = "GitHub Copilot (CLI)"

    def __init__(self, config: CopilotCliConfig) -> None:
        self.config = config

    def _resolve(self) -> str | None:
        return shutil.which(self.config.command)

    def status(self) -> ProviderStatus:
        executable = self._resolve()
        if executable is None:
            return ProviderStatus(
                self.key,
                self.label,
                False,
                f"'{self.config.command}' was not found on PATH.",
                setup_hint=SETUP_HINT,
            )
        try:
            result = subprocess.run(
                [executable, "--version"],
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=_NO_WINDOW,
            )
        except Exception as exc:  # noqa: BLE001
            return ProviderStatus(
                self.key, self.label, False, f"Could not run the CLI: {exc}", setup_hint=SETUP_HINT
            )

        lines = (result.stdout or result.stderr or "").strip().splitlines()
        detail = lines[0] if lines else "Copilot CLI found"
        model = self.config.model or "auto"
        return ProviderStatus(
            self.key,
            self.label,
            result.returncode == 0,
            f"{detail} · model: {model}",
            active_model=model,
            setup_hint="" if result.returncode == 0 else SETUP_HINT,
        )

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        expect_json: bool = False,
        model: str | None = None,
    ) -> Completion:
        executable = self._resolve()
        if executable is None:
            raise LLMUnavailableError(
                f"'{self.config.command}' was not found on PATH. {SETUP_HINT}"
            )

        prompt = _flatten(messages)
        # The CLI prints an answer, never a usage block, so counts are estimated.
        reply = lambda text: Completion(text, estimate_usage(messages, text))  # noqa: E731

        answer, failure, returncode, stderr = self._invoke(executable, prompt)
        if answer and not _looks_like_deflection(answer):
            return reply(answer)

        if answer:
            answer, failure, returncode, stderr = self._invoke(
                executable, f"{_CORRECTION}\n\n{prompt}"
            )
            if answer and not _looks_like_deflection(answer):
                return reply(answer)
            if answer:
                raise LLMError(
                    "The Copilot CLI did not do the work — it replied "
                    f'"{answer.strip()[:120]}". This happens intermittently when driving '
                    "a coding agent. Retry, or switch to the VS Code bridge, which talks "
                    "to the model directly and does not have this problem."
                )

        detail = failure or stderr.strip()[-500:]
        if returncode != 0:
            raise LLMError(f"Copilot CLI failed (exit {returncode}): {detail}")
        raise LLMError(f"The Copilot CLI returned no answer. {detail}".strip())
    def _invoke(self, executable: str, prompt: str) -> tuple[str, str, int, str]:
        command = [executable, *_BASE_ARGS, *self.config.extra_args]
        if self.config.model:
            command += ["--model", self.config.model]
        if self.config.reasoning_effort:
            command += ["--effort", self.config.reasoning_effort]
        if self.config.context_window != "default":
            command += ["--context", self.config.context_window]

        handover: Path | None = None
        if len(prompt) > _inline_budget(executable):
            handover = _write_prompt_file(prompt)
            command += ["-p", _FILE_PROMPT.format(path=handover)]
        else:
            command += ["-p", prompt]

        environment = {**os.environ, "NO_COLOR": "1", "CI": "1"}
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.config.request_timeout_s,
                env=environment,
                stdin=subprocess.DEVNULL,
                creationflags=_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMError(
                f"The Copilot CLI did not answer within {self.config.request_timeout_s:.0f}s. "
                "Raise llm.copilot_cli.request_timeout_s, or use the VS Code bridge."
            ) from exc
        finally:
            if handover is not None:
                handover.unlink(missing_ok=True)

        answer, failure = parse_cli_events(result.stdout or "")
        return answer, failure, result.returncode, result.stderr or ""


def _write_prompt_file(prompt: str) -> Path:
    handle, name = tempfile.mkstemp(prefix="clipdesk-prompt-", suffix=".md", text=True)
    os.close(handle)
    path = Path(name)
    path.write_text(prompt, encoding="utf-8")
    return path


def _flatten(messages: list[ChatMessage]) -> str:
    """Fold the conversation into the single prompt the CLI accepts.

    Deliberately plain: system content, then the user turn, joined by blank
    lines. Adding structure around this measurably hurts — see the note on
    ``_DEFLECTION_MARKERS`` above.
    """
    parts: list[str] = []
    for message in messages:
        content = message.content.strip()
        if not content:
            continue
        if message.role == "assistant":
            parts.append(f"[your previous answer]\n{content}")
        else:
            parts.append(content)
    return "\n\n".join(parts)


def parse_cli_events(stdout: str) -> tuple[str, str]:
    """Pull the final answer out of the JSONL stream.

    Returns ``(answer, failure_detail)``. Non-JSON lines are ignored — the CLI
    occasionally interleaves plain output, and one stray line should not lose an
    otherwise good answer. The exception is a startup failure such as an unknown
    ``--model``: that is printed as plain text and never appears as an event, so
    it has to be picked up here or the user sees "no answer" with no reason.
    """
    answer = ""
    fallback = ""
    failure = ""
    plain_error = ""

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if not line.startswith("{"):
            if line.startswith("Error:") and not plain_error:
                plain_error = line[len("Error:") :].strip()
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        kind = event.get("type")
        if kind == "assistant.message":
            data = event.get("data") or {}
            content = str(data.get("content") or "").strip()
            if not content:
                continue
            if data.get("phase") == "final_answer":
                answer = content
            else:
                fallback = content
        elif kind == "error" or (kind == "result" and event.get("exitCode")):
            detail = str(event.get("message") or event.get("error") or "").strip()
            # A `result` event carries the exit code but often no text, so it must
            # not wipe out the earlier `error` event that explains the failure.
            if detail:
                failure = detail

    return answer or fallback, failure or plain_error
