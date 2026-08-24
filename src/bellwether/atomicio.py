"""Crash-safe file primitives.

The proxy writes telemetry while the risk engine reads it, and the pipeline
writes traffic weights while the proxy reads them on every request. A plain
``open(path, "w")`` truncates the file before the new bytes land, so a reader
that arrives mid-write sees an empty or half-written file. Under the previous
implementation that surfaced as ``int("")`` in the proxy -- silently falling
back to 0% canary weight -- and as a ``JSONDecodeError`` in the risk engine,
which silently fell back to simulated metrics.

Writing to a sibling temporary file and then ``os.replace``-ing it is atomic on
POSIX and Windows, so a reader observes either the old file or the new one and
never an intermediate state.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_atomic(path: Path, data: str, *, encoding: str = "utf-8") -> None:
    """Atomically replace ``path`` with ``data``.

    The temporary file is created in the destination directory because
    ``os.replace`` is only atomic within a single filesystem.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_json_atomic(path: Path, payload: Any) -> None:
    """Serialise ``payload`` as JSON and atomically write it to ``path``."""
    write_atomic(path, json.dumps(payload, separators=(",", ":")))


def read_text(path: Path, *, encoding: str = "utf-8") -> str | None:
    """Return the contents of ``path``, or ``None`` if it does not exist."""
    try:
        return path.read_text(encoding=encoding)
    except FileNotFoundError:
        return None
    except OSError:
        return None


def read_json(path: Path, *, default: Any = None) -> Any:
    """Return parsed JSON from ``path``, or ``default`` if absent or malformed."""
    raw = read_text(path)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def file_age_seconds(path: Path, *, now: float | None = None) -> float | None:
    """Seconds since ``path`` was last modified, or ``None`` if it does not exist."""
    import time

    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return (time.time() if now is None else now) - mtime
