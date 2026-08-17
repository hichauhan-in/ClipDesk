"""Lining work up: the pending queue and the chaining it produces.

The queue holds the closure the endpoint already built, so a queued action is
the same action — the only difference is when it starts.
"""

from clipdesk.server.sequence import Sequence


def make(name):
    return lambda _bus: {"file": name}


def test_a_step_remembers_what_it_will_write():
    sequence = Sequence()
    step = sequence.add("p", "cleanup", make("clean.mp4"), label="Clean cut", produces="clean.mp4")

    assert step.produces == "clean.mp4"
    assert sequence.outputs("p") == ["clean.mp4"]


def test_queues_do_not_leak_between_projects():
    sequence = Sequence()
    sequence.add("a", "cleanup", make("x"), label="A", produces="x.mp4")

    assert sequence.list("b") == []
    assert sequence.outputs("b") == []


def test_steps_keep_the_order_they_were_added_in():
    sequence = Sequence()
    for name in ("clean.mp4", "final.mp4", "final-small.mp4"):
        sequence.add("p", "cleanup", make(name), label=name, produces=name)

    assert sequence.outputs("p") == ["clean.mp4", "final.mp4", "final-small.mp4"]


def test_a_step_can_be_moved_earlier():
    sequence = Sequence()
    first = sequence.add("p", "cleanup", make("a"), label="A", produces="a.mp4")
    second = sequence.add("p", "export", make("b"), label="B", produces="b.mp4")

    assert sequence.move("p", second.id, -1) is True
    assert sequence.outputs("p") == ["b.mp4", "a.mp4"]
    assert sequence.move("p", first.id, 1) is False  # already last


def test_a_step_cannot_be_moved_off_the_end():
    sequence = Sequence()
    only = sequence.add("p", "cleanup", make("a"), label="A")

    assert sequence.move("p", only.id, -1) is False
    assert sequence.move("p", only.id, 1) is False


def test_removing_a_step_takes_it_out_of_the_outputs():
    sequence = Sequence()
    step = sequence.add("p", "cleanup", make("a"), label="A", produces="a.mp4")
    sequence.add("p", "export", make("b"), label="B", produces="b.mp4")

    assert sequence.remove("p", step.id) is True
    assert sequence.outputs("p") == ["b.mp4"]
    assert sequence.remove("p", step.id) is False


def test_taking_the_queue_empties_it():
    sequence = Sequence()
    sequence.add("p", "cleanup", make("a"), label="A", produces="a.mp4")
    sequence.add("p", "export", make("b"), label="B", produces="b.mp4")

    taken = sequence.take("p")

    assert [step.label for step in taken] == ["A", "B"]
    assert sequence.list("p") == []


def test_a_step_without_an_output_is_not_offered_as_a_file():
    # A clip search produces options to choose from, not a file to point at.
    sequence = Sequence()
    sequence.add("p", "clips-find", make("none"), label="Finding clips")

    assert sequence.list("p") != []
    assert sequence.outputs("p") == []


def test_the_stored_work_is_the_work_that_runs():
    sequence = Sequence()
    sequence.add("p", "cleanup", lambda _bus: {"file": "clean.mp4"}, label="Clean cut")

    (step,) = sequence.take("p")

    assert step.work(None) == {"file": "clean.mp4"}
