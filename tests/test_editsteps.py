"""Running a subset of the planned steps."""

import pytest

from clipdesk.actions.editprogram import parse_edit_program

PROMPT = "trim to 20 seconds, speed it up 2x and make it black and white"


def test_all_steps_run_when_none_are_singled_out():
    program = parse_edit_program(PROMPT, 600.0)

    assert program.keeping(None) is program
    assert len(program.ops) == 3


def test_only_the_chosen_steps_survive():
    program = parse_edit_program(PROMPT, 600.0)

    kept = program.keeping([0, 2])

    assert [op.kind for op in kept.ops] == ["trim", "grade"]
    # Dropping the speed change restores the trimmed runtime.
    assert kept.output_duration == pytest.approx(20.0)
    assert program.output_duration == pytest.approx(10.0)


def test_out_of_range_choices_are_ignored():
    program = parse_edit_program(PROMPT, 600.0)

    assert len(program.keeping([0, 99]).ops) == 1


def test_choosing_nothing_is_refused():
    program = parse_edit_program(PROMPT, 600.0)

    with pytest.raises(ValueError, match="at least one step"):
        program.keeping([])
