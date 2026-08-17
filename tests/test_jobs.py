"""The job queue: lanes, ordering, and the bookkeeping the UI reads.

Work is queued rather than piled on because ffmpeg and Whisper already use every
core; two at once finishes both later than one after the other.
"""

import threading
import time

import pytest

from clipdesk.server.jobs import JobManager, lane_for


def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def manager():
    return JobManager()


# --- lanes -------------------------------------------------------------------
@pytest.mark.parametrize(
    ("kind", "lane"),
    [
        ("analyze", "media"),
        ("cleanup", "media"),
        ("clips", "media"),
        ("download", "network"),
        ("notes", "model"),
        ("clips-find", "model"),
    ],
)
def test_work_is_sorted_into_the_right_lane(kind, lane):
    assert lane_for(kind) == lane


def test_an_unknown_kind_is_treated_as_expensive():
    # Guessing "cheap" would let an unknown job run alongside a render.
    assert lane_for("something-new") == "media"


# --- queueing ----------------------------------------------------------------
def test_media_work_runs_one_at_a_time():
    manager = JobManager(lanes={"media": 1})
    running = []
    peak = []
    release = threading.Event()

    def work(_bus):
        running.append(1)
        peak.append(len(running))
        release.wait(3)
        running.pop()
        return {}

    jobs = [manager.start("analyze", "p", work) for _ in range(3)]
    assert wait_for(lambda: len(peak) >= 1)
    release.set()
    for job in jobs:
        assert job.finished.wait(5)

    assert max(peak) == 1


def test_a_queued_job_says_it_is_waiting():
    manager = JobManager(lanes={"media": 1})
    release = threading.Event()
    manager.start("analyze", "p", lambda _bus: release.wait(3) or {})
    second = manager.start("analyze", "p", lambda _bus: {})

    assert wait_for(lambda: second.status == "queued")
    assert second.message == "Waiting for a free slot"
    release.set()


def test_the_queue_reports_how_many_are_ahead():
    manager = JobManager(lanes={"media": 1})
    release = threading.Event()
    first = manager.start("analyze", "p", lambda _bus: release.wait(3) or {})
    manager.start("analyze", "p", lambda _bus: {})
    third = manager.start("analyze", "p", lambda _bus: {})

    assert wait_for(lambda: first.status == "running")
    listed = {job["id"]: job for job in manager.list("p")}
    assert listed[third.id]["queue_position"] == 1
    release.set()


def test_a_slow_render_does_not_hold_up_a_download():
    # Different lanes: a download is waiting on a network and has no reason to
    # queue behind something that is pinning the CPU.
    manager = JobManager(lanes={"media": 1, "network": 2})
    release = threading.Event()
    manager.start("analyze", "p", lambda _bus: release.wait(3) or {})
    download = manager.start("download", "p", lambda _bus: {"done": True})

    assert download.finished.wait(3)
    assert download.status == "done"
    release.set()


# --- dependencies ------------------------------------------------------------
def test_a_dependent_job_waits_for_the_one_before_it():
    # Two workers, so nothing forces the order except the dependency itself.
    manager = JobManager(lanes={"model": 2})
    order = []
    release = threading.Event()

    def first(_bus):
        release.wait(3)
        order.append("first")
        return {}

    one = manager.start("notes", "p", first)
    two = manager.start("notes", "p", lambda _bus: order.append("second") or {}, depends_on=[one.id])

    assert wait_for(lambda: two.status == "queued" or two.message.startswith("Waiting"))
    assert order == []
    release.set()

    assert two.finished.wait(3)
    assert order == ["first", "second"]


def test_a_dependent_job_is_skipped_when_the_step_before_it_fails():
    manager = JobManager(lanes={"media": 1})
    ran = []

    def boom(_bus):
        raise RuntimeError("no source file")

    one = manager.start("cleanup", "p", boom)
    two = manager.start("bookend", "p", lambda _bus: ran.append(1) or {}, depends_on=[one.id])

    assert two.finished.wait(3)
    assert ran == []
    assert two.status == "cancelled"
    assert "did not finish" in two.error


def test_a_chain_of_three_runs_in_order():
    manager = JobManager(lanes={"media": 1})
    order = []
    previous = []
    for name in ("cleanup", "bookend", "export"):
        job = manager.start(
            name, "p", lambda _bus, n=name: order.append(n) or {}, depends_on=previous
        )
        previous = [job.id]

    assert wait_for(lambda: len(order) == 3)
    assert order == ["cleanup", "bookend", "export"]


def test_a_dependency_is_reported_to_the_ui(manager):
    one = manager.start("cleanup", "p", lambda _bus: {})
    two = manager.start("export", "p", lambda _bus: {}, depends_on=[one.id])
    assert two.summary()["depends_on"] == [one.id]


# --- cancelling --------------------------------------------------------------
def test_a_queued_job_can_be_cancelled():
    manager = JobManager(lanes={"media": 1})
    release = threading.Event()
    ran = []
    manager.start("analyze", "p", lambda _bus: release.wait(3) or {})
    second = manager.start("analyze", "p", lambda _bus: ran.append(1) or {})

    assert wait_for(lambda: second.status == "queued")
    assert manager.cancel(second.id) is True
    release.set()

    assert second.status == "cancelled"
    assert wait_for(lambda: not manager.active_count("p"))
    assert ran == []


def test_a_running_job_is_not_cancelled():
    # Stopping ffmpeg midway leaves a half-written file, so the honest answer is
    # that it has to finish.
    manager = JobManager(lanes={"media": 1})
    release = threading.Event()
    job = manager.start("analyze", "p", lambda _bus: release.wait(3) or {})

    assert wait_for(lambda: job.status == "running")
    assert manager.cancel(job.id) is False
    release.set()


# --- what the UI reads -------------------------------------------------------
def test_a_finished_job_keeps_its_result(manager):
    job = manager.start("clips-find", "p", lambda _bus: {"candidates": [1, 2, 3]})

    assert job.finished.wait(5)
    assert job.summary()["result"] == {"candidates": [1, 2, 3]}


def test_a_search_is_marked_as_needing_a_choice(manager):
    job = manager.start("clips-find", "p", lambda _bus: {})

    assert job.needs_choice is True


def test_a_render_is_not_marked_as_needing_a_choice(manager):
    job = manager.start("clips", "p", lambda _bus: {})

    assert job.needs_choice is False


def test_a_job_knows_which_tab_it_belongs_to(manager):
    # The clean cut lives inside the clip tab, and notes inside the transcript tab.
    assert manager.start("cleanup", "p", lambda _bus: {}).tab == "clip"
    assert manager.start("notes", "p", lambda _bus: {}).tab == "transcript"
    assert manager.start("intro", "p", lambda _bus: {}).tab == "editor"


def test_progress_is_mirrored_onto_the_job(manager):
    def work(bus):
        bus.progress("stage", 0.5, "Halfway")
        return {}

    job = manager.start("analyze", "p", work)

    assert job.finished.wait(5)
    assert "Halfway" in [event.get("message") for event in job.events]


def test_a_failure_is_recorded_rather_than_raised(manager):
    def work(_bus):
        raise RuntimeError("the model refused")

    job = manager.start("notes", "p", work)

    assert job.finished.wait(5)
    assert job.status == "failed"
    assert job.error == "the model refused"


def test_jobs_can_be_filtered_by_project(manager):
    manager.start("notes", "alpha", lambda _bus: {}).finished.wait(5)
    manager.start("notes", "beta", lambda _bus: {}).finished.wait(5)

    assert {job["project_id"] for job in manager.list("alpha")} == {"alpha"}


def test_elapsed_time_is_reported(manager):
    job = manager.start("notes", "p", lambda _bus: time.sleep(0.05) or {})

    assert job.finished.wait(5)
    assert job.summary()["elapsed_s"] > 0
