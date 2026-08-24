"""The traffic weight: the single source of truth for the rollout.

The proxy reads this on every request and the pipeline writes it at each
promotion step. Because those are separate processes, the file is the contract
between them -- and it is written atomically, so the proxy can never observe a
truncated value.

That mattered: the previous ``echo $TRAFFIC > file`` plus ``int(f.read())``
pairing had a window where the reader saw an empty file, hit a bare ``except``,
and silently fell back to 0% canary. The rollout would appear to stall at 0%
with nothing in any log to explain it.
"""

from __future__ import annotations

import threading
from pathlib import Path

from .atomicio import read_text, write_atomic
from .errors import ValidationError
from .logging_setup import get_logger

log = get_logger(__name__)

MIN_WEIGHT = 0
MAX_WEIGHT = 100


class TrafficWeightStore:
    """Read/write the canary traffic weight as a percentage in [0, 100]."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def set(self, weight: int) -> int:
        """Persist ``weight``, returning the value written."""
        weight = self.validate(weight)
        with self._lock:
            write_atomic(self._path, str(weight))
        log.info("traffic weight set", extra={"weight": weight})
        return weight

    def get(self, *, default: int = 0) -> int:
        """Read the current weight.

        Falls back to ``default`` when the file is missing or unparseable, and
        says so at WARNING level rather than swallowing the condition, so a
        stuck rollout is diagnosable from the logs.
        """
        raw = read_text(self._path)
        if raw is None:
            return default
        stripped = raw.strip()
        if not stripped:
            return default
        try:
            value = int(stripped)
        except ValueError:
            log.warning(
                "traffic weight file is not an integer; using default",
                extra={"path": str(self._path), "raw": stripped[:32], "default": default},
            )
            return default
        if not MIN_WEIGHT <= value <= MAX_WEIGHT:
            log.warning(
                "traffic weight out of range; clamping",
                extra={"path": str(self._path), "value": value},
            )
            return max(MIN_WEIGHT, min(MAX_WEIGHT, value))
        return value

    def reset(self) -> int:
        """Route all traffic to stable. The rollback primitive."""
        return self.set(MIN_WEIGHT)

    @staticmethod
    def validate(weight: object) -> int:
        if isinstance(weight, bool) or not isinstance(weight, int):
            raise ValidationError(f"traffic weight must be an integer, got {weight!r}")
        if not MIN_WEIGHT <= weight <= MAX_WEIGHT:
            raise ValidationError(
                f"traffic weight must be between {MIN_WEIGHT} and {MAX_WEIGHT}, got {weight}"
            )
        return weight
