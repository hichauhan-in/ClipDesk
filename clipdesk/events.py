"""A tiny synchronous pub/sub bus for pipeline progress.

The pipeline runs on a worker thread and knows nothing about HTTP. It publishes
:class:`ProgressEvent`s; the job manager forwards them to a WebSocket, and tests
just collect them in a list.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    STAGE_START = "stage_start"
    PROGRESS = "progress"
    STAGE_END = "stage_end"
    LOG = "log"
    WARNING = "warning"
    ERROR = "error"
    DONE = "done"


@dataclass(slots=True)
class ProgressEvent:
    type: EventType
    stage: str = ""
    message: str = ""
    #: 0..1 within the current stage, or ``None`` when indeterminate.
    fraction: float | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "stage": self.stage,
            "message": self.message,
            "fraction": self.fraction,
            "data": self.data,
        }


Subscriber = Callable[[ProgressEvent], None]


class JobCancelled(RuntimeError):
    pass


class EventBus:
    def __init__(self, is_cancelled: Callable[[], bool] | None = None) -> None:
        self._subscribers: list[Subscriber] = []
        self._lock = threading.Lock()
        self._is_cancelled = is_cancelled or (lambda: False)

    def check_cancelled(self) -> None:
        if self._is_cancelled():
            raise JobCancelled("Cancelled by the user")

    def subscribe(self, subscriber: Subscriber) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(subscriber)

        def unsubscribe() -> None:
            with self._lock:
                if subscriber in self._subscribers:
                    self._subscribers.remove(subscriber)

        return unsubscribe

    def publish(self, event: ProgressEvent) -> None:
        self.check_cancelled()
        with self._lock:
            targets = list(self._subscribers)
        for subscriber in targets:
            # A broken subscriber must never take down the pipeline.
            try:
                subscriber(event)
            except Exception:  # noqa: BLE001
                pass

    # --- convenience -------------------------------------------------------
    def stage_start(self, stage: str, message: str = "") -> None:
        self.publish(ProgressEvent(EventType.STAGE_START, stage, message))

    def progress(self, stage: str, fraction: float | None, message: str = "") -> None:
        self.publish(ProgressEvent(EventType.PROGRESS, stage, message, fraction))

    def stage_end(self, stage: str, message: str = "", **data: Any) -> None:
        self.publish(ProgressEvent(EventType.STAGE_END, stage, message, 1.0, data))

    def log(self, message: str, stage: str = "") -> None:
        self.publish(ProgressEvent(EventType.LOG, stage, message))

    def warn(self, message: str, stage: str = "") -> None:
        self.publish(ProgressEvent(EventType.WARNING, stage, message))


class NullBus(EventBus):
    """An :class:`EventBus` that drops everything — handy in unit tests."""

    def publish(self, event: ProgressEvent) -> None:
        return
