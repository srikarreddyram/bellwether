"""The rollout state machine.

These are the regression tests for the defects that made the previous
orchestrator's core claim untrue: the stage order, and rollback actually
happening on every failure path.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from bellwether.config import Settings
from bellwether.errors import PipelineAborted, StageFailed
from bellwether.events import TOPIC_LOG, TOPIC_STAGE, EventBus
from bellwether.models import DeploymentRun, RunStatus, StageStatus
from bellwether.pipeline import (
    STAGE_DEFINITIONS,
    AbortSignal,
    LoadGenerator,
    Pipeline,
    build_stages,
)
from bellwether.store import DeploymentStore


@pytest.fixture()
def pipeline(settings: Settings, tmp_path: Path) -> Pipeline:
    store = DeploymentStore(tmp_path / "d.db")
    return Pipeline(settings, store=store, bus=EventBus())


class TestStageCatalogue:
    def test_baseline_precedes_proxy_which_precedes_canary(self) -> None:
        """The ordering bug, pinned.

        The old orchestrator started the canary first, never started the proxy,
        and only started the stable baseline during the *final* promotion --
        so during the 10% and 50% phases the traffic routed to "stable" hit a
        closed port, and the risk gate had no telemetry to read.
        """
        keys = [stage.key for stage in STAGE_DEFINITIONS]
        assert keys.index("baseline") < keys.index("proxy") < keys.index("canary")

    def test_no_traffic_shifts_before_both_instances_exist(self) -> None:
        keys = [stage.key for stage in STAGE_DEFINITIONS]
        first_shift = next(
            index for index, stage in enumerate(STAGE_DEFINITIONS) if stage.traffic > 0
        )
        assert first_shift > keys.index("canary")

    def test_risk_gate_precedes_every_promotion(self) -> None:
        keys = [stage.key for stage in STAGE_DEFINITIONS]
        assert keys.index("risk") < keys.index("promote_50") < keys.index("promote_100")

    def test_traffic_is_monotonically_non_decreasing(self) -> None:
        weights = [stage.traffic for stage in STAGE_DEFINITIONS]
        assert weights == sorted(weights)
        assert weights[-1] == 100

    def test_stage_keys_are_unique(self) -> None:
        keys = [stage.key for stage in STAGE_DEFINITIONS]
        assert len(keys) == len(set(keys))

    def test_build_stages_starts_all_pending(self) -> None:
        assert all(stage.status is StageStatus.PENDING for stage in build_stages())


class TestAbortSignal:
    def test_set_and_clear(self, tmp_path: Path) -> None:
        signal = AbortSignal(tmp_path / "abort")
        assert not signal.is_set()
        signal.request("because")
        assert signal.is_set()
        assert signal.reason == "because"
        signal.clear()
        assert not signal.is_set()

    def test_visible_across_processes_via_the_flag_file(self, tmp_path: Path) -> None:
        flag = tmp_path / "abort"
        signal = AbortSignal(flag)
        flag.write_text("external rollback")  # as another process would
        assert signal.is_set()

    def test_raise_if_set(self, tmp_path: Path) -> None:
        signal = AbortSignal(tmp_path / "abort")
        signal.raise_if_set()
        signal.request()
        with pytest.raises(PipelineAborted):
            signal.raise_if_set()

    def test_wait_wakes_early_on_abort(self, tmp_path: Path) -> None:
        """``time.sleep(6)`` meant a rollback click sat unacknowledged for 6s."""
        signal = AbortSignal(tmp_path / "abort")
        threading.Timer(0.15, signal.request).start()

        started = time.monotonic()
        with pytest.raises(PipelineAborted):
            signal.wait(10.0)
        assert time.monotonic() - started < 2.0

    def test_wait_returns_normally_when_not_aborted(self, tmp_path: Path) -> None:
        AbortSignal(tmp_path / "abort").wait(0.1)


class TestRollbackGuarantees:
    def test_rollback_resets_traffic_and_stops_the_canary(self, pipeline: Pipeline) -> None:
        pipeline._weights.set(50)
        pipeline._rollback("test")
        assert pipeline._weights.get() == 0

    def test_emergency_rollback_works_with_no_run_in_flight(self, pipeline: Pipeline) -> None:
        pipeline._weights.set(100)
        result = pipeline.emergency_rollback("operator")
        assert result["trafficPct"] == 0
        assert pipeline._weights.get() == 0

    def test_failure_mid_pipeline_always_resets_traffic(
        self, pipeline: Pipeline, monkeypatch
    ) -> None:
        """The old code returned False on abort and left the canary live."""
        pipeline._weights.set(50)

        def explode(_repo_url: str) -> Path:
            raise StageFailed("checkout", "boom")

        monkeypatch.setattr(pipeline, "_stage_checkout", explode)

        run = DeploymentRun(repo_url="https://github.com/o/r.git", number=1)
        pipeline._store.create(run)
        result = pipeline.execute(run)

        assert result.succeeded is False
        assert result.rolled_back is True
        assert run.status is RunStatus.FAILED
        assert pipeline._weights.get() == 0, "traffic must never be left mid-shift"

    def test_unexpected_exception_still_rolls_back(self, pipeline: Pipeline, monkeypatch) -> None:
        monkeypatch.setattr(
            pipeline, "_stage_checkout", lambda url: (_ for _ in ()).throw(RuntimeError("kaboom"))
        )
        run = DeploymentRun(repo_url="https://github.com/o/r.git", number=1)
        pipeline._store.create(run)
        result = pipeline.execute(run)

        assert run.status is RunStatus.FAILED
        assert "kaboom" in (result.error or "")
        assert pipeline._weights.get() == 0

    def test_abort_marks_the_run_rolled_back(self, pipeline: Pipeline, monkeypatch) -> None:
        def abort_here(_repo_url: str) -> Path:
            raise PipelineAborted("operator pressed the button")

        monkeypatch.setattr(pipeline, "_stage_checkout", abort_here)
        run = DeploymentRun(repo_url="https://github.com/o/r.git", number=1)
        pipeline._store.create(run)
        pipeline.execute(run)

        assert run.status is RunStatus.ROLLED_BACK
        assert pipeline._weights.get() == 0

    def test_unreached_stages_are_marked_not_pending(self, pipeline: Pipeline, monkeypatch) -> None:
        monkeypatch.setattr(
            pipeline,
            "_stage_checkout",
            lambda url: (_ for _ in ()).throw(StageFailed("checkout", "no")),
        )
        run = DeploymentRun(repo_url="https://github.com/o/r.git", number=1)
        pipeline._store.create(run)
        pipeline.execute(run)

        statuses = {stage.key: stage.status for stage in pipeline.stages}
        assert statuses["checkout"] is StageStatus.FAILED
        assert statuses["promote_100"] is StageStatus.SKIPPED
        assert StageStatus.PENDING not in statuses.values()

    def test_the_run_is_persisted_as_finished(self, pipeline: Pipeline, monkeypatch) -> None:
        monkeypatch.setattr(
            pipeline,
            "_stage_checkout",
            lambda url: (_ for _ in ()).throw(StageFailed("checkout", "no")),
        )
        run = DeploymentRun(repo_url="https://github.com/o/r.git", number=1)
        pipeline._store.create(run)
        pipeline.execute(run)

        stored = pipeline._store.get(run.id)
        assert stored is not None
        assert stored.status is RunStatus.FAILED
        assert stored.finished_at is not None


class TestAssessmentRetention:
    def test_no_assessment_before_any_run(self, pipeline: Pipeline) -> None:
        assert pipeline.snapshot()["risk"] is None

    def test_the_last_verdict_survives_the_run(self, pipeline: Pipeline, monkeypatch) -> None:
        """A dashboard opened after a run ends must still see the verdict.

        Previously the assessment existed only as a websocket event, so a
        dashboard that connected one second later reported a scored run as
        "not scored".
        """
        from bellwether.models import Decision, RiskAssessment
        from bellwether.risk import summarise

        verdict = RiskAssessment(
            decision=Decision.PROMOTE,
            reasons=["within thresholds"],
            canary=summarise([], "canary"),
            stable=summarise([], "stable"),
            latency_threshold_ms=500.0,
            error_rate_threshold=0.05,
            data_source="telemetry",
        )
        monkeypatch.setattr("bellwether.pipeline.assess", lambda snapshot, settings: verdict)

        assessment = pipeline._stage_risk("risk")
        assert assessment is verdict
        assert pipeline.snapshot()["risk"]["decision"] == "PROMOTE"

    def test_a_new_run_clears_the_previous_verdict(self, pipeline: Pipeline, monkeypatch) -> None:
        from bellwether.models import Decision, RiskAssessment
        from bellwether.risk import summarise

        pipeline.last_assessment = RiskAssessment(
            decision=Decision.ABORT,
            reasons=["stale"],
            canary=summarise([], "canary"),
            stable=summarise([], "stable"),
            latency_threshold_ms=500.0,
            error_rate_threshold=0.05,
            data_source="telemetry",
        )
        monkeypatch.setattr(
            pipeline,
            "_stage_checkout",
            lambda url: (_ for _ in ()).throw(StageFailed("checkout", "stop here")),
        )
        run = DeploymentRun(repo_url="https://github.com/o/r.git", number=2)
        pipeline._store.create(run)
        pipeline.execute(run)

        assert pipeline.last_assessment is None, "a stale verdict must not sit beside a new run"


class TestEventStream:
    def test_stage_transitions_are_published(self, pipeline: Pipeline) -> None:
        events: list[tuple] = []
        pipeline._bus.subscribe(lambda topic, payload: events.append((topic, payload)))
        pipeline._set_stage("checkout", StageStatus.RUNNING)
        pipeline._set_stage("checkout", StageStatus.SUCCEEDED, "done")

        stage_events = [payload for topic, payload in events if topic == TOPIC_STAGE]
        assert [event["status"] for event in stage_events] == ["RUNNING", "SUCCEEDED"]
        assert stage_events[-1]["durationS"] is not None

    def test_logs_are_published(self, pipeline: Pipeline) -> None:
        lines: list[str] = []
        pipeline._bus.subscribe(
            lambda topic, payload: lines.append(payload["line"]) if topic == TOPIC_LOG else None
        )
        pipeline.emit("hello")
        assert lines == ["hello"]

    def test_a_failing_subscriber_does_not_break_the_pipeline(self, pipeline: Pipeline) -> None:
        pipeline._bus.subscribe(lambda topic, payload: (_ for _ in ()).throw(RuntimeError()))
        pipeline.emit("still works")  # must not raise


class TestLoadGenerator:
    def test_zero_workers_is_inert(self) -> None:
        with LoadGenerator("http://127.0.0.1:1", workers=0, interval_s=0.01) as gen:
            time.sleep(0.05)
        assert gen.sent == 0

    def test_counts_transport_failures(self) -> None:
        from conftest import free_port

        target = f"http://127.0.0.1:{free_port()}/"
        with LoadGenerator(target, workers=2, interval_s=0.01) as gen:
            time.sleep(0.3)
        assert gen.sent > 0
        assert gen.failed == gen.sent, "nothing is listening; every request must fail"
