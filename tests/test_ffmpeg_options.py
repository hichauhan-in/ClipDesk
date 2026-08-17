"""Talking to ffmpeg: choosing options that the actual binary understands.

A long clean cut builds a filtergraph with hundreds of segments. It has to go
to ffmpeg in a file — put on the command line, Windows refuses it and reports
the failure as a missing executable, which points the blame at the wrong thing.
"""

from pathlib import Path

import pytest

from clipdesk.media import ffmpeg as ffmpeg_module
from clipdesk.media.ffmpeg import FFmpegError, filter_complex_args, filter_script_option


@pytest.fixture(autouse=True)
def no_caching():
    filter_script_option.cache_clear()
    ffmpeg_module.major_version.cache_clear()
    yield
    filter_script_option.cache_clear()
    ffmpeg_module.major_version.cache_clear()


def fake_run(monkeypatch, *, version_output="", accepts=()):
    calls = []

    def run(_binary, args, **_kwargs):
        calls.append(list(args))
        if args[:1] == ["-version"]:
            if not version_output:
                raise FFmpegError("no version")
            return type("Result", (), {"stdout": version_output, "returncode": 0})()
        option = args[0]
        if option in accepts:
            raise FFmpegError("failed", stderr="Error opening file no-such.txt.")
        raise FFmpegError("failed", stderr=f"Unrecognized option '{option.lstrip('-/')}'.")

    monkeypatch.setattr(ffmpeg_module, "run", run)
    return calls


# --- picking the option ------------------------------------------------------
def test_a_numbered_build_is_read_from_its_version(monkeypatch):
    fake_run(monkeypatch, version_output="ffmpeg version 6.1.1 Copyright (c)")

    assert filter_script_option("ffmpeg") == "-filter_complex_script"


def test_a_modern_numbered_build_uses_the_new_spelling(monkeypatch):
    fake_run(monkeypatch, version_output="ffmpeg version 7.1 Copyright (c)")

    assert filter_script_option("ffmpeg") == "-/filter_complex"


def test_a_nightly_build_is_asked_rather_than_guessed(monkeypatch):
    # The bundled build reports "N-126039-g6bbc22dc09-20260810": no number to
    # compare, but it does take the modern spelling.
    calls = fake_run(
        monkeypatch,
        version_output="ffmpeg version N-126039-g6bbc22dc09-20260810 Copyright (c)",
        accepts=("-/filter_complex",),
    )

    assert filter_script_option("ffmpeg") == "-/filter_complex"
    assert ["-version"] in calls


def test_an_older_nightly_falls_back_to_the_old_spelling(monkeypatch):
    fake_run(
        monkeypatch,
        version_output="ffmpeg version N-1000-gabc Copyright (c)",
        accepts=("-filter_complex_script",),
    )

    assert filter_script_option("ffmpeg") == "-filter_complex_script"


def test_a_build_that_takes_neither_says_so(monkeypatch):
    fake_run(monkeypatch, version_output="ffmpeg version N-1-gx Copyright (c)")

    assert filter_script_option("ffmpeg") is None


def test_the_answer_is_worked_out_once(monkeypatch):
    calls = fake_run(
        monkeypatch,
        version_output="ffmpeg version N-1-gx Copyright (c)",
        accepts=("-/filter_complex",),
    )
    filter_script_option("ffmpeg")
    before = len(calls)
    filter_script_option("ffmpeg")

    assert len(calls) == before


# --- using it ----------------------------------------------------------------
def test_a_long_graph_goes_to_a_file_not_the_command_line(monkeypatch, tmp_path):
    fake_run(monkeypatch, version_output="ffmpeg version 7.1 Copyright (c)")
    graph = "\n".join(f"[0:v]trim=start={i}:end={i + 1}[v{i}];" for i in range(2000))

    args, script = filter_complex_args(graph, tmp_path / "cut.filter.txt", "ffmpeg")

    assert args == ["-/filter_complex", str(tmp_path / "cut.filter.txt")]
    assert Path(script).read_text(encoding="utf-8") == graph
    assert sum(len(part) for part in args) < 32000


def test_a_graph_is_only_inlined_when_there_is_no_other_way(monkeypatch, tmp_path):
    fake_run(monkeypatch, version_output="ffmpeg version N-1-gx Copyright (c)")

    args, script = filter_complex_args("nullsrc[out]", tmp_path / "cut.filter.txt", "ffmpeg")

    assert args == ["-filter_complex", "nullsrc[out]"]
    assert script is None
