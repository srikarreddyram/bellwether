"""Deployment history persistence."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from bellwether.models import DeploymentRun, RunStatus
from bellwether.store import DeploymentStore


@pytest.fixture()
def store(tmp_path: Path) -> DeploymentStore:
    return DeploymentStore(tmp_path / "deployments.db")


def make(store: DeploymentStore, url: str = "https://github.com/o/r.git") -> DeploymentRun:
    return store.create(DeploymentRun(repo_url=url, number=store.next_number()))


class TestPersistence:
    def test_create_and_read_back(self, store: DeploymentStore) -> None:
        run = make(store)
        fetched = store.get(run.id)
        assert fetched is not None
        assert fetched.repo_url == run.repo_url
        assert fetched.status is RunStatus.QUEUED

    def test_survives_reopen(self, tmp_path: Path) -> None:
        path = tmp_path / "d.db"
        run = make(DeploymentStore(path))
        assert DeploymentStore(path).get(run.id) is not None

    def test_ids_are_unique_within_the_same_second(self, store: DeploymentStore) -> None:
        """``str(int(time.time()))`` collided and raised IntegrityError."""
        ids = {make(store).id for _ in range(50)}
        assert len(ids) == 50

    def test_numbers_increment(self, store: DeploymentStore) -> None:
        assert [make(store).number for _ in range(3)] == [1, 2, 3]

    def test_ordering_is_insertion_order_not_lexicographic(self, store: DeploymentStore) -> None:
        """``ORDER BY id DESC`` on a TEXT epoch reorders as the epoch gains digits."""
        runs = [make(store) for _ in range(5)]
        recent = store.recent(limit=10)
        assert [r.id for r in recent] == [r.id for r in reversed(runs)]

    def test_pagination(self, store: DeploymentStore) -> None:
        for _ in range(10):
            make(store)
        assert len(store.recent(limit=3)) == 3
        assert len(store.recent(limit=3, offset=9)) == 1

    def test_limit_is_clamped(self, store: DeploymentStore) -> None:
        make(store)
        assert len(store.recent(limit=100_000)) == 1


class TestUpdates:
    def test_status_transition(self, store: DeploymentStore) -> None:
        run = make(store)
        store.update(run.id, status=RunStatus.SUCCEEDED, traffic_pct=100, finished=True)
        fetched = store.get(run.id)
        assert fetched is not None
        assert fetched.status is RunStatus.SUCCEEDED
        assert fetched.traffic_pct == 100
        assert fetched.duration_s is not None

    def test_empty_update_is_a_noop(self, store: DeploymentStore) -> None:
        run = make(store)
        store.update(run.id)
        assert store.get(run.id) is not None

    def test_orphans_are_reconciled_at_startup(self, tmp_path: Path) -> None:
        """A crash used to leave phantom RUNNING rows in history forever."""
        path = tmp_path / "d.db"
        first = DeploymentStore(path)
        run = make(first)
        first.update(run.id, status=RunStatus.RUNNING)
        first.close()

        second = DeploymentStore(path)
        assert second.reconcile_orphans() == 1
        fetched = second.get(run.id)
        assert fetched is not None
        assert fetched.status is RunStatus.FAILED
        assert "interrupted" in (fetched.detail or "")

    def test_reconcile_is_idempotent(self, store: DeploymentStore) -> None:
        run = make(store)
        store.update(run.id, status=RunStatus.SUCCEEDED)
        assert store.reconcile_orphans() == 0


class TestStats:
    def test_empty(self, store: DeploymentStore) -> None:
        assert store.stats()["total"] == 0
        assert store.stats()["successRate"] is None

    def test_success_rate(self, store: DeploymentStore) -> None:
        for status in (RunStatus.SUCCEEDED, RunStatus.SUCCEEDED, RunStatus.FAILED):
            run = make(store)
            store.update(run.id, status=status, finished=True)
        stats = store.stats()
        assert stats["total"] == 3
        assert stats["successRate"] == pytest.approx(2 / 3)
        assert stats["avgDurationS"] is not None


class TestConcurrency:
    def test_writes_from_many_threads(self, store: DeploymentStore) -> None:
        """SQLite connections are not shareable across threads; the API is multithreaded."""
        errors: list = []

        def worker() -> None:
            try:
                for _ in range(10):
                    run = make(store)
                    store.update(run.id, status=RunStatus.SUCCEEDED, finished=True)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert errors == []
        assert store.stats()["total"] == 40


class TestSchema:
    def test_refuses_a_newer_schema(self, tmp_path: Path) -> None:
        path = tmp_path / "d.db"
        DeploymentStore(path).close()
        import sqlite3

        connection = sqlite3.connect(path)
        connection.execute("PRAGMA user_version=999")
        connection.commit()
        connection.close()

        with pytest.raises(RuntimeError, match="newer than this build"):
            DeploymentStore(path)
