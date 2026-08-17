"""One instruction becomes an ordered program of enumerated editing operations."""

import pytest

from clipdesk.actions.editprogram import (
    ASPECTS,
    GRADES,
    parse_edit_program,
    render_program,
)

DURATION = 600.0


def kinds(prompt: str, duration: float = DURATION) -> list[str]:
    return [op.kind for op in parse_edit_program(prompt, duration).ops]


def test_a_length_alone_becomes_a_trim():
    program = parse_edit_program("create an edit of five seconds", DURATION)

    assert [op.kind for op in program.ops] == ["trim"]
    assert program.output_duration == pytest.approx(5.0)


def test_written_numbers_are_understood():
    for prompt in ("give me a edit of six seconds", "make a 6 second edit"):
        assert parse_edit_program(prompt, DURATION).output_duration == pytest.approx(6.0)


def test_a_middle_edit_is_taken_from_the_middle():
    program = parse_edit_program("create a 10 second video from the middle", DURATION)
    trim = program.trim

    assert trim.start == pytest.approx((DURATION - 10) / 2)
    assert trim.end == pytest.approx((DURATION + 10) / 2)


def test_clauses_compose_into_several_operations():
    program = parse_edit_program(
        "cut the first 30 seconds, speed it up 1.5x and fade out over 2 seconds", DURATION
    )

    assert [op.kind for op in program.ops] == ["trim", "speed", "fade-out"]
    assert program.output_duration == pytest.approx((DURATION - 30) / 1.5)


def test_a_decimal_speed_survives_clause_splitting():
    program = parse_edit_program("speed it up 1.5x", DURATION)

    assert program.ops[0].kind == "speed"
    assert program.ops[0].value == pytest.approx(1.5)


def test_trimming_to_a_length_keeps_it_rather_than_dropping_it():
    kept = parse_edit_program("trim to the first 30 seconds", DURATION).trim
    dropped = parse_edit_program("cut the first 30 seconds", DURATION).trim

    assert (kept.start, kept.end) == (0.0, 30.0)
    assert dropped.start == pytest.approx(30.0)


def test_an_effect_on_a_window_does_not_shorten_the_video():
    program = parse_edit_program("make the first 10 seconds black and white", DURATION)

    assert [op.kind for op in program.ops] == ["grade"]
    assert program.output_duration == pytest.approx(DURATION)
    assert program.ops[0].end == pytest.approx(10.0)


def test_quoted_text_is_never_split_on_its_own_and():
    program = parse_edit_program('add text "Safety and quality" bottom right', DURATION)

    assert program.ops[0].kind == "text"
    assert program.ops[0].text == "Safety and quality"


def test_social_shapes_are_recognised():
    assert kinds("make it vertical for reels") == ["aspect"]
    assert parse_edit_program("make it square", DURATION).ops[0].name == "square"


def test_every_named_grade_and_aspect_can_be_planned():
    for name in GRADES:
        assert parse_edit_program(f"give it a {name} look", DURATION).ops
    for name in ASPECTS:
        assert parse_edit_program(f"make it {name}", DURATION).ops


def test_an_unsupported_request_explains_what_is_possible():
    with pytest.raises(ValueError, match="trim to a length"):
        parse_edit_program("summarise the meeting for me", DURATION)


def test_an_empty_instruction_is_refused():
    with pytest.raises(ValueError, match="Describe"):
        parse_edit_program("   ", DURATION)


def test_planning_never_emits_ffmpeg_fragments():
    program = parse_edit_program(
        'trim to 10 seconds, make it vertical, sepia and add text "Hi"', DURATION
    )
    rendered = " ".join(program.describe())

    assert "filter" not in rendered.lower()
    assert "-vf" not in rendered
    assert "ffmpeg" not in rendered.lower()


def test_render_refuses_nothing_and_builds_every_op(tmp_path, monkeypatch):
    """Every operation must reach the renderer without raising on the way."""
    captured = {}

    def fake_run(_bin, args, on_elapsed=None):
        captured["args"] = args

    monkeypatch.setattr("clipdesk.actions.editprogram.run_with_progress", fake_run)
    from clipdesk.config import RenderConfig

    program = parse_edit_program(
        'trim to 10 seconds, speed it up 2x, make it vertical, sepia, vignette, '
        'add text "Hi" center, fade in, fade out and set volume to 50%',
        DURATION,
    )
    render_program(
        tmp_path / "in.mp4",
        tmp_path / "out.mp4",
        program,
        has_audio=True,
        render=RenderConfig(),
        ffmpeg_bin="ffmpeg",
    )

    assert "-map" in captured["args"]
