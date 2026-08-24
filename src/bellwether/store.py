"""Durable deployment history.

Improvements over the previous single-table-no-schema approach:

* **Real primary keys.** ``str(int(time.time()))`` collides whenever two
  deployments start in the same second, and ``ORDER BY id DESC`` on that TEXT
  column sorts lexicographically -- so history reorders itself the moment the
  epoch gains a digit. Rows now use a random id and are ordered by an
  autoincrementing sequence.
* **Schema versioning.** ``user_version`` drives forward migrations, so an
  upgrade does not silently run against an old table shape.
* **WAL + per-thread connections.** SQLite objects are not shareable across
  threads; the API serves requests on many.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .logging_setup import get_logger
from .models import DeploymentRun, RunStatus

log = get_logger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS deployments (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    id          TEXT    NOT NULL UNIQUE,
    number      INTEGER NOT NULL,
    repo_url    TEXT    NOT NULL,
    status      TEXT    NOT NULL,
    trigger     TEXT    NOT NULL DEFAULT 'manual',
    created_at  REAL    NOT NULL,
    finished_at REAL,
    traffic_pct INTEGER NOT NULL DEFAULT 0,
    decision    TEXT,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_deployments_created_at ON deployments (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_deployments_status     ON deployments (status);
"""


class DeploymentStore:
    """SQLite-backed repository of deployment runs."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._local = threading.local()
        self._write_lock = threading.Lock()
        # SQLite connections cannot be shared across threads, so each thread
        # opens its own. They are also registered here: without a registry,
        # close() only releases the calling thread's connection and every
        # request thread leaks one for the lifetime of the process.
        self._connections: list[sqlite3.Connection] = []
        self._registry_lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    @property
    def path(self) -> Path:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(str(self._path), timeout=10.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA foreign_keys=ON")
            self._local.connection = connection
            with self._registry_lock:
                self._connections.append(connection)
        return connection

    def _migrate(self) -> None:
        connection = self._connect()
        with self._write_lock, connection:
            current = connection.execute("PRAGMA user_version").fetchone()[0]
            if current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema version {current} is newer than this build supports "
                    f"({SCHEMA_VERSION}); upgrade bellwether or point BELLWETHER_DATABASE elsewhere"
                )
            if current < SCHEMA_VERSION:
                connection.executescript(_SCHEMA)
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                log.info(
                    "migrated deployment database",
                    extra={"from": current, "to": SCHEMA_VERSION, "path": str(self._path)},
                )

    # ── Writes ────────────────────────────────────────────────────────────────

    def next_number(self) -> int:
        """Monotonic, human-friendly build number."""
        connection = self._connect()
        row = connection.execute("SELECT COALESCE(MAX(number), 0) AS n FROM deployments").fetchone()
        return int(row["n"]) + 1

    def create(self, run: DeploymentRun) -> DeploymentRun:
        connection = self._connect()
        with self._write_lock, connection:
            connection.execute(
                """
                INSERT INTO deployments
                    (id, number, repo_url, status, trigger, created_at,
                     finished_at, traffic_pct, decision, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.id,
                    run.number,
                    run.repo_url,
                    run.status.value,
                    run.trigger,
                    run.created_at,
                    run.finished_at,
                    run.traffic_pct,
                    run.decision,
                    run.detail,
                ),
            )
        log.info("recorded deployment", extra={"run_id": run.id, "number": run.number})
        return run

    def update(
        self,
        run_id: str,
        *,
        status: RunStatus | None = None,
        traffic_pct: int | None = None,
        decision: str | None = None,
        detail: str | None = None,
        finished: bool = False,
    ) -> None:
        assignments: list[str] = []
        values: list[Any] = []
        if status is not None:
            assignments.append("status = ?")
            values.append(status.value)
        if traffic_pct is not None:
            assignments.append("traffic_pct = ?")
            values.append(traffic_pct)
        if decision is not None:
            assignments.append("decision = ?")
            values.append(decision)
        if detail is not None:
            assignments.append("detail = ?")
            values.append(detail)
        if finished:
            assignments.append("finished_at = ?")
            values.append(time.time())
        if not assignments:
            return

        values.append(run_id)
        connection = self._connect()
        with self._write_lock, connection:
            connection.execute(
                f"UPDATE deployments SET {', '.join(assignments)} WHERE id = ?",  # noqa: S608
                values,
            )

    def reconcile_orphans(self) -> int:
        """Mark runs left ``RUNNING`` by a crashed process as failed.

        Called at startup. Without this, a hard restart leaves phantom
        in-progress deployments in the history forever.
        """
        connection = self._connect()
        with self._write_lock, connection:
            cursor = connection.execute(
                """
                UPDATE deployments
                   SET status = ?, finished_at = ?, detail = ?
                 WHERE status IN (?, ?)
                """,
                (
                    RunStatus.FAILED.value,
                    time.time(),
                    "interrupted: the platform restarted while this run was in progress",
                    RunStatus.RUNNING.value,
                    RunStatus.QUEUED.value,
                ),
            )
            count = cursor.rowcount or 0
        if count:
            log.warning("marked interrupted deployments as failed", extra={"count": count})
        return count

    # ── Reads ─────────────────────────────────────────────────────────────────

    def get(self, run_id: str) -> DeploymentRun | None:
        connection = self._connect()
        row = connection.execute("SELECT * FROM deployments WHERE id = ?", (run_id,)).fetchone()
        return _row_to_run(row) if row else None

    def recent(self, limit: int = 25, offset: int = 0) -> list[DeploymentRun]:
        connection = self._connect()
        rows = connection.execute(
            "SELECT * FROM deployments ORDER BY seq DESC LIMIT ? OFFSET ?",
            (max(1, min(limit, 200)), max(0, offset)),
        ).fetchall()
        return [_row_to_run(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        """Aggregate counters for the dashboard summary tiles."""
        connection = self._connect()
        rows = connection.execute(
            "SELECT status, COUNT(*) AS n FROM deployments GROUP BY status"
        ).fetchall()
        by_status = {row["status"]: int(row["n"]) for row in rows}
        total = sum(by_status.values())
        succeeded = by_status.get(RunStatus.SUCCEEDED.value, 0)
        duration_row = connection.execute(
            """
            SELECT AVG(finished_at - created_at) AS avg_duration
              FROM deployments
             WHERE finished_at IS NOT NULL
            """
        ).fetchone()
        return {
            "total": total,
            "byStatus": by_status,
            "successRate": (succeeded / total) if total else None,
            "avgDurationS": (
                round(float(duration_row["avg_duration"]), 2)
                if duration_row and duration_row["avg_duration"] is not None
                else None
            ),
        }

    def close(self) -> None:
        """Close every connection this store opened, on any thread."""
        with self._registry_lock:
            connections, self._connections = self._connections, []
        for connection in connections:
            with contextlib.suppress(sqlite3.Error):  # already-closed handles
                connection.close()
        self._local.connection = None


def _row_to_run(row: sqlite3.Row) -> DeploymentRun:
    return DeploymentRun(
        id=row["id"],
        number=int(row["number"]),
        repo_url=row["repo_url"],
        status=RunStatus(row["status"]),
        trigger=row["trigger"],
        created_at=float(row["created_at"]),
        finished_at=float(row["finished_at"]) if row["finished_at"] is not None else None,
        traffic_pct=int(row["traffic_pct"]),
        decision=row["decision"],
        detail=row["detail"],
    )
