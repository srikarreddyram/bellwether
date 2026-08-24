"""Atomic file I/O, the traffic weight, and the telemetry ring buffer.

These three cover the concurrency defects that made the old rollout silently
stall: a truncated weight file read as 0%, and a telemetry list mutated from
every request thread while being rewritten to disk on every request.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from bellwether.atomicio import (
    file_age_seconds,
    read_json,
    read_text,
    write_atomic,
    write_json_atomic,
)
from bellwether.errors import ValidationError
from bellwether.models import RequestSample
from bellwether.telemetry import TelemetryStore, load_snapshot
from bellwether.weights import TrafficWeightStore


class TestAtomicIO:
    def test_round_trip(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b" / "file.txt"
        write_atomic(target, "hello")
        assert read_text(target) == "hello"

    def test_missing_file_reads_as_none(self, tmp_path: Path) -> None:
        assert read_text(tmp_path / "nope") is None

    def test_no_temp_files_are_left_behind(self, tmp_path: Path) -> None:
        target = tmp_path / "f.json"
        write_json_atomic(target, {"a": 1})
        assert [p.name for p in tmp_path.iterdir()] == ["f.json"]

    def test_malformed_json_returns_default(self, tmp_path: Path) -> None:
        target = tmp_path / "bad.json"
        target.write_text("{not json")
        assert read_json(target, default=[]) == []

    def test_concurrent_writers_never_produce_a_partial_read(self, tmp_path: Path) -> None:
        """The core guarantee: a reader sees an old value or a new one, never junk."""
        target = tmp_path / "weight"
        write_atomic(target, "0")
        stop = threading.Event()
        corrupt: list = []

        def writer() -> None:
            i = 0
            while not stop.is_set():
                write_atomic(target, str(i % 101))
                i += 1

        def reader() -> None:
            while not stop.is_set():
                raw = read_text(target)
                if raw is None or not raw.strip().isdigit():
                    corrupt.append(raw)

        threads = [threading.Thread(target=writer) for _ in range(3)]
        threads += [threading.Thread(target=reader) for _ in range(3)]
        for thread in threads:
            thread.start()
        threading.Event().wait(0.6)
        stop.set()
        for thread in threads:
            thread.join(timeout=5)

        assert corrupt == [], f"observed torn reads: {corrupt[:5]}"

    def test_file_age(self, tmp_path: Path) -> None:
        target = tmp_path / "f"
        write_atomic(target, "x")
        assert 0 <= (file_age_seconds(target) or 0) < 5
        assert file_age_seconds(tmp_path / "missing") is None


class TestTrafficWeight:
    def test_set_and_get(self, tmp_path: Path) -> None:
        store = TrafficWeightStore(tmp_path / "w")
        assert store.set(50) == 50
        assert store.get() == 50

    def test_missing_file_defaults_to_zero(self, tmp_path: Path) -> None:
        assert TrafficWeightStore(tmp_path / "absent").get() == 0

    @pytest.mark.parametrize("bad", [-1, 101, 1000])
    def test_out_of_range_is_rejected(self, tmp_path: Path, bad: int) -> None:
        with pytest.raises(ValidationError, match="between"):
            TrafficWeightStore(tmp_path / "w").set(bad)

    @pytest.mark.parametrize("bad", ["50", 12.5, None, True])
    def test_non_integer_is_rejected(self, tmp_path: Path, bad: object) -> None:
        with pytest.raises(ValidationError, match="integer"):
            TrafficWeightStore(tmp_path / "w").set(bad)  # type: ignore[arg-type]

    def test_empty_file_falls_back_without_crashing(self, tmp_path: Path) -> None:
        """The old bare ``except`` hid this; now it is defined behaviour."""
        target = tmp_path / "w"
        target.write_text("")
        assert TrafficWeightStore(target).get() == 0

    def test_garbage_falls_back_to_default(self, tmp_path: Path) -> None:
        target = tmp_path / "w"
        target.write_text("not-a-number")
        assert TrafficWeightStore(target).get(default=7) == 7

    def test_out_of_range_on_disk_is_clamped(self, tmp_path: Path) -> None:
        target = tmp_path / "w"
        target.write_text("500")
        assert TrafficWeightStore(target).get() == 100

    def test_reset_is_the_rollback_primitive(self, tmp_path: Path) -> None:
        store = TrafficWeightStore(tmp_path / "w")
        store.set(100)
        assert store.reset() == 0
        assert store.get() == 0


class TestTelemetryStore:
    def sample(self, cohort: str = "canary", latency: float = 10.0) -> RequestSample:
        return RequestSample(cohort=cohort, latency_ms=latency, status_code=200, timestamp=1.0)

    def test_records_and_flushes(self, tmp_path: Path) -> None:
        store = TelemetryStore(tmp_path / "t.json", window=10, flush_interval_s=0.0)
        store.record(self.sample())
        assert len(load_snapshot(tmp_path / "t.json").samples) == 1

    def test_window_is_bounded(self, tmp_path: Path) -> None:
        store = TelemetryStore(tmp_path / "t.json", window=5, flush_interval_s=0.0)
        for i in range(50):
            store.record(self.sample(latency=float(i)))
        store.flush()
        kept = load_snapshot(tmp_path / "t.json").samples
        assert len(kept) == 5
        assert [s.latency_ms for s in kept] == [45, 46, 47, 48, 49]

    def test_concurrent_writers_lose_no_samples(self, tmp_path: Path) -> None:
        """A plain list mutated from many request threads drops entries."""
        store = TelemetryStore(tmp_path / "t.json", window=10_000, flush_interval_s=10.0)
        threads = [
            threading.Thread(target=lambda: [store.record(self.sample()) for _ in range(200)])
            for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert len(store.samples()) == 8 * 200

    def test_writes_are_coalesced_not_per_request(self, tmp_path: Path) -> None:
        """The old code rewrote the entire window to disk on every request.

        That is a full serialise-and-replace in the request path, against a
        stated budget of under 5ms of proxy overhead. Here 200 requests must
        cost a small constant number of writes, not 200.
        """
        target = tmp_path / "t.json"
        store = TelemetryStore(target, window=500, flush_interval_s=3600.0)

        writes = []
        original = store._write
        store._write = lambda payload: (writes.append(len(payload)), original(payload))[1]

        for _ in range(200):
            store.record(self.sample())

        # The first sample flushes eagerly so the file exists for readers; the
        # remaining 199 must coalesce behind the interval.
        assert len(writes) == 1, f"expected write coalescing, got {len(writes)} writes"

        store.flush()
        assert len(load_snapshot(target).samples) == 200

    def test_snapshot_skips_malformed_entries_individually(self, tmp_path: Path) -> None:
        target = tmp_path / "t.json"
        target.write_text(
            json.dumps(
                [
                    {"cohort": "canary", "latencyMs": 5, "statusCode": 200, "timestamp": 1},
                    {"garbage": True},
                    "not-an-object",
                    {"cohort": "stable", "latencyMs": 6, "statusCode": 200, "timestamp": 2},
                ]
            )
        )
        assert len(load_snapshot(target).samples) == 2

    def test_missing_file_is_reported_as_absent(self, tmp_path: Path) -> None:
        snapshot = load_snapshot(tmp_path / "absent.json")
        assert snapshot.is_empty
        assert not snapshot.exists

    def test_staleness(self, tmp_path: Path) -> None:
        store = TelemetryStore(tmp_path / "t.json", flush_interval_s=0.0)
        store.record(self.sample())
        snapshot = load_snapshot(tmp_path / "t.json")
        assert not snapshot.is_stale(max_age_s=120)
        assert snapshot.is_stale(max_age_s=0.0)
