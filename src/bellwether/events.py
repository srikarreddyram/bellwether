"""A tiny synchronous publish/subscribe bus.

The pipeline needs to stream stage transitions and console output to whoever is
watching, but it must not import Flask or Socket.IO -- that coupling is what
made the previous orchestrator impossible to unit test. Instead the pipeline
publishes plain events here, and the API layer subscribes and forwards them
over the websocket.

Subscribers are invoked on the publishing thread. Handlers must therefore be
cheap and must not raise; an exception in one subscriber is logged and does not
prevent the others from running or interrupt the pipeline.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from .logging_setup import get_logger

log = get_logger(__name__)

Listener = Callable[[str, dict[str, Any]], None]


class EventBus:
    """Thread-safe fan-out of ``(topic, payload)`` to registered listeners."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._listeners: list[Listener] = []

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Register ``listener``; returns a callable that unsubscribes it."""
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(topic, payload)
            except Exception:
                log.exception("event listener failed", extra={"topic": topic})

    def clear(self) -> None:
        with self._lock:
            self._listeners.clear()


# Topics, named once so producers and consumers cannot drift apart.
TOPIC_LOG = "log"
TOPIC_STAGE = "stage"
TOPIC_RUN = "run"
TOPIC_TRAFFIC = "traffic"
TOPIC_RISK = "risk"
TOPIC_CHAOS = "chaos"
