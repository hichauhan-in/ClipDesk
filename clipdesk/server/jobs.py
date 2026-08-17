"""Background jobs: a queue, a bridge from worker threads to WebSockets, and
enough bookkeeping for the UI to show what is running and pick work back up.

Three things matter here.

**Work is queued, not piled on.** Transcription and ffmpeg saturate the machine,
so running two at once finishes both later than running them in sequence. Jobs go
into lanes with a fixed number of workers: one for anything that pins the CPU,
more for work that is only waiting on a network.

**Every job keeps a bounded ring of its events**, so a browser that connects late
(or reconnects after navigating away) replays the full history rather than
joining mid-stream.

**Finished results are kept.** A job whose whole point is to offer the user a
choice is useless if walking away loses it, so the result stays retrievable and
the UI can restore it when the user comes back.

**Jobs can wait for each other.** A step that edits the file an earlier step has
not written yet declares that step as a dependency and blocks until it lands.
Dependencies are always queued before their dependents, so a blocked worker is
always waiting on something that has already been picked up: the wait cannot
deadlock.
"""

from __future__ import annotations

import queue
import threading
import time
import traceback
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from clipdesk.events import EventBus, EventType, ProgressEvent

MAX_EVENTS = 800

#: How many jobs may run at once in each lane.
LANES: dict[str, int] = {
    # ffmpeg and Whisper already use every core they can. A second concurrent
    # job does not finish sooner, it just makes the first one slower.
    "media": 1,
    # Model calls wait on a network, but a burst of them is rude to a shared
    # endpoint and runs into rate limits.
    "model": 2,
    "network": 2,
}

#: Which lane each kind of work belongs in. Unknown kinds get the media lane,
#: because assuming something is expensive is the safe mistake.
KIND_LANE: dict[str, str] = {
    "analyze": "media",
    "cleanup": "media",
    "clips": "media",
    "bookend": "media",
    "intro": "media",
    "prompt-edit": "media",
    "download": "network",
    "asset-download": "network",
    "provision": "network",
    "sign-in": "network",
    "notes": "model",
    "article": "model",
    "clips-find": "model",
    "highlights-find": "model",
}

#: Kinds whose result is a set of options for the user to choose from. These are
#: the ones worth restoring when the user returns to the tab.
NEEDS_CHOICE = {"clips-find", "highlights-find"}

#: What each kind is called in the UI.
KIND_LABEL: dict[str, str] = {
    "analyze": "Analysing",
    "cleanup": "Clean cut",
    "clips": "Rendering clips",
    "bookend": "Intro and outro",
    "intro": "Building intro",
    "prompt-edit": "Applying video edit",
    "download": "Downloading",
    "asset-download": "Adding editor asset",
    "provision": "Installing",
    "notes": "Writing notes",
    "article": "Writing the article",
    "clips-find": "Finding clip options",
    "highlights-find": "Finding highlights",
}

#: Which project tab a job belongs to, so the UI can send the user back to it.
KIND_TAB: dict[str, str] = {
    "analyze": "overview",
    "notes": "transcript",
    "article": "transcript",
    "clips-find": "clip",
    "clips": "clip",
    "cleanup": "clip",
    "highlights-find": "clip",
    "bookend": "editor",
    "intro": "editor",
    "prompt-edit": "editor",
    "asset-download": "editor",
    "export": "outputs",
}

ACTIVE_STATUSES = {"queued", "running"}


def lane_for(kind: str) -> str:
    return KIND_LANE.get(kind, "media")


@dataclass
class Job:
    id: str
    kind: str
    project_id: str
    lane: str = "media"
    #: Which project tab owns this job, for resuming and for navigation.
    tab: str = ""
    label: str = ""
    status: str = "queued"  # queued | running | done | failed | cancelled
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    #: Free-form context the UI needs to rebuild its view, e.g. the search query.
    meta: dict[str, Any] = field(default_factory=dict)
    #: Jobs that must finish first. Always queued ahead of this one.
    depends_on: list[str] = field(default_factory=list)
    #: True when the result is a set of options the user still has to choose from.
    needs_choice: bool = False
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    #: Latest progress, mirrored out of the event stream so a poller can draw a
    #: bar without replaying every event.
    fraction: float | None = None
    message: str = ""
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    finished: threading.Event = field(default_factory=threading.Event)
    _listeners: list[Callable[[dict[str, Any]], None]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at

    def summary(self) -> dict[str, Any]:
        """Everything the UI needs to draw a job without replaying its events."""
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label or KIND_LABEL.get(self.kind, self.kind),
            "project_id": self.project_id,
            "lane": self.lane,
            "tab": self.tab,
            "status": self.status,
            "fraction": self.fraction,
            "message": self.message,
            "error": self.error,
            "meta": self.meta,
            "depends_on": list(self.depends_on),
            "needs_choice": self.needs_choice,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_s": round(self.elapsed(), 1),
            "result": self.result,
        }

    def snapshot(self) -> dict[str, Any]:
        return {**self.summary(), "events": list(self.events)}

    def emit(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.events.append(payload)
            # Mirror progress onto the job so pollers see it without the stream.
            kind = payload.get("type")
            if kind in ("progress", "stage_start"):
                if payload.get("message"):
                    self.message = str(payload["message"])
                self.fraction = payload.get("fraction")
            elif kind == "stage_end":
                self.fraction = 1.0
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(payload)
            except Exception:  # noqa: BLE001
                pass

    def add_listener(self, listener: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)
            backlog = list(self.events)
        for payload in backlog:
            try:
                listener(payload)
            except Exception:  # noqa: BLE001
                pass

        def remove() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return remove


class JobManager:
    """Queues jobs into lanes and keeps a bounded history of finished ones."""

    def __init__(self, history: int = 80, lanes: dict[str, int] | None = None) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: deque[str] = deque(maxlen=history)
        self._lock = threading.Lock()
        self._lanes = dict(lanes or LANES)
        self._queues: dict[str, queue.Queue[str]] = {}
        self._workers: dict[str, list[threading.Thread]] = {}
        self._work: dict[str, Callable[[EventBus], dict[str, Any] | None]] = {}

    # --- reading -----------------------------------------------------------
    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, project_id: str = "", *, active_only: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            jobs = [self._jobs[job_id] for job_id in self._order if job_id in self._jobs]
        chosen = [
            job
            for job in reversed(jobs)
            if (not project_id or job.project_id == project_id)
            and (not active_only or job.active)
        ]
        return [self._with_queue_position(job) for job in chosen]

    def active_count(self, project_id: str = "") -> int:
        with self._lock:
            return sum(
                1
                for job in self._jobs.values()
                if job.active and (not project_id or job.project_id == project_id)
            )

    def _with_queue_position(self, job: Job) -> dict[str, Any]:
        return {**job.summary(), "queue_position": self._queue_position(job)}

    def _queue_position(self, job: Job) -> int:
        """How many jobs are ahead of this one in its lane. 0 means it is next."""
        if job.status != "queued":
            return 0
        with self._lock:
            return sum(
                1
                for other in self._jobs.values()
                if other.lane == job.lane
                and other.status == "queued"
                and other.created_at < job.created_at
            )

    # --- writing -----------------------------------------------------------
    def start(
        self,
        kind: str,
        project_id: str,
        work: Callable[[EventBus], dict[str, Any] | None],
        *,
        label: str = "",
        tab: str = "",
        meta: dict[str, Any] | None = None,
        depends_on: list[str] | None = None,
    ) -> Job:
        lane = lane_for(kind)
        waiting = [job_id for job_id in (depends_on or []) if job_id]
        job = Job(
            id=uuid.uuid4().hex[:12],
            kind=kind,
            project_id=project_id,
            lane=lane,
            tab=tab or KIND_TAB.get(kind, ""),
            label=label or KIND_LABEL.get(kind, kind),
            meta=meta or {},
            depends_on=waiting,
            needs_choice=kind in NEEDS_CHOICE,
            message="Waiting for the step before it" if waiting else "Waiting for a free slot",
        )

        with self._lock:
            if len(self._order) == self._order.maxlen:
                evicted = self._order[0]
                self._jobs.pop(evicted, None)
                self._work.pop(evicted, None)
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._work[job.id] = work

        self._ensure_lane(lane)
        self._queues[lane].put(job.id)
        return job

    def cancel(self, job_id: str) -> bool:
        """Drop a job that has not started yet.

        A running job is left alone: killing ffmpeg mid-write or a model call
        mid-flight leaves debris, and saying so is better than pretending the
        stop worked.
        """
        job = self.get(job_id)
        if job is None or job.status != "queued":
            return False
        job.status = "cancelled"
        job.message = "Cancelled before it started"
        job.finished_at = time.time()
        job.finished.set()
        job.emit(ProgressEvent(EventType.LOG, job.kind, "Cancelled").to_dict())
        return True

    # --- workers -----------------------------------------------------------
    def _ensure_lane(self, lane: str) -> None:
        with self._lock:
            if lane in self._queues:
                return
            self._queues[lane] = queue.Queue()
            self._workers[lane] = [
                threading.Thread(
                    target=self._worker,
                    args=(lane,),
                    name=f"clipdesk-{lane}-{index}",
                    daemon=True,
                )
                for index in range(max(1, self._lanes.get(lane, 1)))
            ]
            threads = self._workers[lane]
        for thread in threads:
            thread.start()

    def _worker(self, lane: str) -> None:
        work_queue = self._queues[lane]
        while True:
            job_id = work_queue.get()
            try:
                self._run(job_id)
            finally:
                work_queue.task_done()

    def _await_dependencies(self, job: Job) -> Job | None:
        """Block until every dependency finishes. Returns the one that did not."""
        for dependency_id in job.depends_on:
            dependency = self.get(dependency_id)
            if dependency is None:
                continue
            if not dependency.finished.is_set():
                job.message = f"Waiting for “{dependency.label}”"
                job.emit(
                    ProgressEvent(EventType.LOG, job.kind, job.message).to_dict()
                )
                dependency.finished.wait()
            if dependency.status != "done":
                return dependency
        return None

    def _run(self, job_id: str) -> None:
        job = self.get(job_id)
        with self._lock:
            work = self._work.pop(job_id, None)
        if job is None or work is None or job.status == "cancelled":
            return

        blocker = self._await_dependencies(job)
        if blocker is not None:
            job.status = "cancelled"
            job.error = f"Skipped — “{blocker.label}” did not finish."
            job.message = job.error
            job.finished_at = time.time()
            job.finished.set()
            job.emit(ProgressEvent(EventType.ERROR, job.kind, job.error).to_dict())
            return

        job.status = "running"
        job.started_at = time.time()
        job.message = "Starting"
        bus = EventBus()
        bus.subscribe(lambda event: job.emit(event.to_dict()))
        job.emit(ProgressEvent(EventType.LOG, job.kind, f"Started {job.label}").to_dict())

        try:
            result = work(bus) or {}
            job.result = result
            job.status = "done"
            job.fraction = 1.0
            job.message = "Finished"
            job.emit(
                ProgressEvent(
                    EventType.DONE, job.kind, "Finished", 1.0, {"result": result}
                ).to_dict()
            )
        except Exception as exc:  # noqa: BLE001
            job.status = "failed"
            job.error = str(exc) or exc.__class__.__name__
            job.message = job.error
            job.emit(
                ProgressEvent(
                    EventType.ERROR,
                    job.kind,
                    job.error,
                    data={"traceback": traceback.format_exc(limit=6)},
                ).to_dict()
            )
        finally:
            job.finished_at = time.time()
            job.finished.set()
