"""Request telemetry collected by the traffic proxy.

Design constraints, both of which the previous implementation violated:

* **Thread safety.** The proxy serves requests on many threads. The old code
  appended to and trimmed a plain list from all of them concurrently, which can
  drop or duplicate samples and, under CPython, can raise from ``list.pop`` on
  an empty list mid-race.
* **Bounded write amplification.** The old code serialised the entire window to
  disk on *every* request. That is an fsync-free full rewrite in the request
  path, against a stated budget of under 5 ms of proxy overhead. Here writes are
  coalesced behind a configurable interval and performed atomically.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .atomicio import file_age_seconds, read_json, write_json_atomic
from .logging_setup import get_logger
from .models import RequestSample

log = get_logger(__name__)


@dataclass(frozen=True)
class TelemetrySnapshot:
    """Samples read back from disk, with the freshness needed to trust them."""

    samples: list[RequestSample]
    age_s: float | None
    exists: bool

    @property
    def is_empty(self) -> bool:
        return not self.samples

    def is_stale(self, max_age_s: float) -> bool:
        if self.age_s is None:
            return True
        return self.age_s > max_age_s

    def for_cohort(self, cohort: str) -> list[RequestSample]:
        return [sample for sample in self.samples if sample.cohort == cohort]


class TelemetryStore:
    """A bounded, thread-safe ring buffer of request samples with atomic flush."""

    def __init__(
        self,
        path: Path,
        *,
        window: int = 500,
        flush_interval_s: float = 0.25,
    ) -> None:
        self._path = path
        self._window = window
        self._flush_interval_s = flush_interval_s
        self._lock = threading.Lock()
        self._samples: deque[RequestSample] = deque(maxlen=window)
        self._last_flush = 0.0
        self._dirty = False

    @property
    def path(self) -> Path:
        return self._path

    def record(self, sample: RequestSample) -> None:
        """Append ``sample`` and flush if the coalescing interval has elapsed."""
        with self._lock:
            self._samples.append(sample)
            self._dirty = True
            due = (time.monotonic() - self._last_flush) >= self._flush_interval_s
            payload = [item.to_dict() for item in self._samples] if due else None
            if due:
                self._last_flush = time.monotonic()
                self._dirty = False

        # Write outside the lock: request threads should never block on disk I/O
        # held by another request thread.
        if payload is not None:
            self._write(payload)

    def flush(self) -> None:
        """Force a write of any buffered samples."""
        with self._lock:
            if not self._dirty:
                return
            payload = [item.to_dict() for item in self._samples]
            self._last_flush = time.monotonic()
            self._dirty = False
        self._write(payload)

    def _write(self, payload: list[dict[str, Any]]) -> None:
        try:
            write_json_atomic(self._path, payload)
        except OSError:
            log.exception("failed to persist telemetry", extra={"path": str(self._path)})

    def samples(self) -> list[RequestSample]:
        with self._lock:
            return list(self._samples)

    def clear(self) -> None:
        with self._lock:
            self._samples.clear()
            self._dirty = True
        self.flush()


def load_snapshot(path: Path, *, now: float | None = None) -> TelemetrySnapshot:
    """Read a telemetry snapshot written by the proxy.

    Malformed or partially written entries are skipped individually rather than
    discarding the whole file, so one bad record cannot force the risk engine
    into its fallback path.
    """
    raw = read_json(path, default=None)
    if raw is None:
        return TelemetrySnapshot(samples=[], age_s=None, exists=path.exists())

    samples: list[RequestSample] = []
    if isinstance(raw, Iterable):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            try:
                samples.append(RequestSample.from_dict(entry))
            except (KeyError, TypeError, ValueError):
                continue

    return TelemetrySnapshot(
        samples=samples,
        age_s=file_age_seconds(path, now=now),
        exists=True,
    )
