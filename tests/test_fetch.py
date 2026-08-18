"""Downloading from a link, and explaining it when it fails.

The yt-dlp command is worth pinning because YouTube's requirements move and the
symptoms mislead: a missing JavaScript runtime and a genuine sign-in wall both
end as HTTP 403, and ClipDesk once sent everyone hunting for cookies over a
problem cookies could not fix.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from clipdesk.ingest.fetch import YOUTUBE_CLIENTS, FetchError, _ytdlp_error, fetch_with_ytdlp
from clipdesk.ingest.links import classify

# The package re-exports the fetch() function under the submodule's own name, so
# importing it as an attribute would hand back the function.
fetch_module = importlib.import_module("clipdesk.ingest.fetch")


@pytest.fixture
def recorded_command(tmp_path, monkeypatch):
    """Capture the command instead of running it."""
    seen: dict[str, list[str]] = {}

    monkeypatch.setattr(fetch_module, "_resolve_extractor", lambda _vendor: ["yt-dlp"])

    class _Stopped(Exception):
        pass

    def _popen(command, **_kwargs):
        seen["command"] = command
        raise _Stopped

    monkeypatch.setattr(fetch_module.subprocess, "Popen", _popen)

    def run(**kwargs) -> list[str]:
        with pytest.raises((_Stopped, FetchError)):
            fetch_with_ytdlp(
                classify("https://www.youtube.com/watch?v=abc"),
                Path(tmp_path),
                lambda _fraction, _message: None,
                **kwargs,
            )
        return seen.get("command", [])

    return run


def test_an_installed_js_runtime_is_offered_to_the_extractor(recorded_command, monkeypatch):
    """Only deno is enabled by default, and almost nobody has deno.

    Without this the signature challenge goes unsolved, the formats are dropped
    and the download ends as a 403.
    """
    monkeypatch.setattr(
        fetch_module.shutil, "which", lambda name: r"C:\node\node.exe" if name == "node" else None
    )

    command = recorded_command()

    assert "--js-runtimes" in command
    assert command[command.index("--js-runtimes") + 1] == r"node:C:\node\node.exe"


def test_no_runtime_flag_when_none_is_installed(recorded_command, monkeypatch):
    monkeypatch.setattr(fetch_module.shutil, "which", lambda _name: None)

    assert "--js-runtimes" not in recorded_command()


def test_the_youtube_player_clients_are_pinned(recorded_command):
    """The extractor's own default resolves to a client whose media URLs 403."""
    command = recorded_command()

    assert "--extractor-args" in command
    assert command[command.index("--extractor-args") + 1] == f"youtube:player_client={YOUTUBE_CLIENTS}"


def test_the_player_clients_can_be_left_to_the_extractor(recorded_command):
    assert "--extractor-args" not in recorded_command(youtube_clients="")


# --- explaining the failure --------------------------------------------------
def test_a_missing_runtime_is_not_reported_as_a_sign_in_problem(monkeypatch):
    monkeypatch.setattr(fetch_module.shutil, "which", lambda _name: None)
    tail = [
        "WARNING: [youtube] No supported JavaScript runtime could be found. Only deno is enabled",
        "ERROR: unable to download video data: HTTP Error 403: Forbidden",
    ]

    message = _ytdlp_error(tail, classify("https://youtu.be/abc"), "")

    assert "JavaScript runtime" in message
    assert "nodejs.org" in message
    assert "Paste a signed-in session" not in message


def test_the_runtime_warning_is_found_beyond_the_shown_tail(monkeypatch):
    """It is printed before the download starts, so the error can be far below."""
    monkeypatch.setattr(fetch_module.shutil, "which", lambda _name: None)
    tail = [
        "WARNING: [youtube] No supported JavaScript runtime could be found.",
        *[f"[download] {n}% of 10MiB" for n in range(20)],
        "ERROR: unable to download video data: HTTP Error 403: Forbidden",
    ]

    assert "JavaScript runtime" in _ytdlp_error(tail, classify("https://youtu.be/abc"), "")


def test_a_real_sign_in_wall_still_asks_for_a_session(monkeypatch):
    monkeypatch.setattr(fetch_module.shutil, "which", lambda name: "/usr/bin/node")
    tail = ["ERROR: [youtube] abc: Sign in to confirm your age"]

    message = _ytdlp_error(tail, classify("https://youtu.be/abc"), "")

    assert "Paste a signed-in session" in message
    assert "nodejs.org" not in message
