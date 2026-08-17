"""A staging area for work the user has lined up but not started.

Rendering is slow, so the useful unit of work is rarely one action: it is "clean
this up, then top and tail the result, then export it". Doing that by hand means
waiting at each step to start the next one.

A queued step is the *closure the endpoint already built*, held back rather than
handed to the job manager. Nothing about the action changes by being queued, and
there is no second code path that could drift from the first.

Two consequences follow from holding closures rather than a serialised recipe:

* The queue lives for as long as the server does and no longer. That is honest
  for a tool that is started, used, and closed.
* A step can name a file an earlier step has not written yet. The name is only
  resolved when the step runs, by which time the file is there.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from clipdesk.events import EventBus


@dataclass(slots=True)
class PendingStep:
    id: str
    kind: str
    label: str
    #: The file this step will write, so later steps can name it before it exists.
    produces: str
    tab: str
    work: Callable[[EventBus], dict[str, Any] | None]
    meta: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "produces": self.produces,
            "tab": self.tab,
            "meta": self.meta,
        }


class Sequence:
    """Per-project ordered lists of steps waiting to be run."""

    def __init__(self) -> None:
        self._steps: dict[str, list[PendingStep]] = {}
        self._lock = threading.Lock()

    def add(
        self,
        project_id: str,
        kind: str,
        work: Callable[[EventBus], dict[str, Any] | None],
        *,
        label: str,
        produces: str = "",
        tab: str = "",
        meta: dict[str, Any] | None = None,
    ) -> PendingStep:
        step = PendingStep(
            id=uuid.uuid4().hex[:10],
            kind=kind,
            label=label,
            produces=produces,
            tab=tab,
            work=work,
            meta=meta or {},
        )
        with self._lock:
            self._steps.setdefault(project_id, []).append(step)
        return step

    def list(self, project_id: str) -> list[PendingStep]:
        with self._lock:
            return list(self._steps.get(project_id, ()))

    def outputs(self, project_id: str) -> list[str]:
        """Names the queue will produce, in the order they will appear."""
        return [step.produces for step in self.list(project_id) if step.produces]

    def remove(self, project_id: str, step_id: str) -> bool:
        with self._lock:
            steps = self._steps.get(project_id)
            if not steps:
                return False
            for index, step in enumerate(steps):
                if step.id == step_id:
                    del steps[index]
                    return True
        return False

    def move(self, project_id: str, step_id: str, offset: int) -> bool:
        """Shift one step earlier or later. Refuses to move it out of the list."""
        with self._lock:
            steps = self._steps.get(project_id) or []
            index = next((i for i, step in enumerate(steps) if step.id == step_id), -1)
            target = index + offset
            if index < 0 or not 0 <= target < len(steps):
                return False
            steps[index], steps[target] = steps[target], steps[index]
            return True

    def take(self, project_id: str) -> list[PendingStep]:
        """Empty the queue and hand back what was in it."""
        with self._lock:
            return self._steps.pop(project_id, [])

    def clear(self, project_id: str) -> int:
        return len(self.take(project_id))
