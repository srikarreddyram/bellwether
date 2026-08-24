"""The MLflow audit trail.

Tracking is observability, not the deployment path. The contract these enforce
is that losing it degrades gracefully and never takes a rollout down.
"""

from __future__ import annotations

import sys

import pytest

from bellwether.config import RiskSettings
from bellwether.models import Decision, RiskAssessment
from bellwether.risk import summarise
from bellwether.tracking import TrackingClient


def assessment(decision: Decision = Decision.PROMOTE) -> RiskAssessment:
    return RiskAssessment(
        decision=decision,
        reasons=["a reason"],
        canary=summarise([], "canary"),
        stable=summarise([], "stable"),
        latency_threshold_ms=500.0,
        error_rate_threshold=0.05,
        data_source="telemetry",
    )


class TestDisabled:
    def test_reports_itself_disabled(self, tmp_path) -> None:
        client = TrackingClient(RiskSettings(tracking_enabled=False))
        assert client.enabled is False

    def test_logging_is_a_silent_no_op(self) -> None:
        client = TrackingClient(RiskSettings(tracking_enabled=False))
        client.log_assessment(assessment(), repo_url="r", run_id="1")

    def test_reads_return_empty(self) -> None:
        client = TrackingClient(RiskSettings(tracking_enabled=False))
        assert client.recent_runs() == []
        assert client.enforce_retention() == 0

    def test_the_run_context_still_yields(self) -> None:
        client = TrackingClient(RiskSettings(tracking_enabled=False))
        entered = False
        with client.run("x"):
            entered = True
        assert entered


class TestUnavailable:
    def test_a_missing_mlflow_disables_tracking_rather_than_raising(self, monkeypatch) -> None:
        """A deployment tool must not require its observability library."""
        monkeypatch.setitem(sys.modules, "mlflow", None)
        client = TrackingClient(RiskSettings(tracking_enabled=True))
        assert client.enabled is False
        client.log_assessment(assessment(), repo_url="r", run_id="1")  # must not raise

    def test_an_unwritable_store_disables_tracking(self, monkeypatch) -> None:
        class Broken:
            @staticmethod
            def set_tracking_uri(_uri: str) -> None:
                raise OSError("read-only filesystem")

        monkeypatch.setitem(sys.modules, "mlflow", Broken())
        client = TrackingClient(RiskSettings(tracking_enabled=True))
        assert client.enabled is False


class TestEnabled:
    @pytest.fixture()
    def settings(self, tmp_path) -> RiskSettings:
        return RiskSettings(
            tracking_enabled=True,
            # Exercise the real default backend, not the deprecated file store.
            tracking_uri=f"sqlite:///{tmp_path / 'mlflow.db'}",
            experiment_name="bellwether-tests",
            retention_runs=3,
        )

    def test_an_assessment_is_recorded_and_readable(self, settings: RiskSettings) -> None:
        pytest.importorskip("mlflow")
        client = TrackingClient(settings)
        if not client.enabled:
            pytest.skip("mlflow could not initialise in this environment")

        client.log_assessment(assessment(Decision.ABORT), repo_url="https://x/y", run_id="abc")
        runs = client.recent_runs(limit=5)
        assert runs, "the decision was not recorded"
        assert runs[0]["decision"] == "ABORT"

    def test_retention_keeps_only_the_newest_runs(self, settings: RiskSettings) -> None:
        """The old implementation shelled out to `ls -1dt | tail | xargs rm -rf`."""
        pytest.importorskip("mlflow")
        client = TrackingClient(settings)
        if not client.enabled:
            pytest.skip("mlflow could not initialise in this environment")

        for index in range(6):
            client.log_assessment(assessment(), repo_url="https://x/y", run_id=str(index))

        deleted = client.enforce_retention()
        assert deleted >= 1
        assert len(client.recent_runs(limit=50)) <= settings.retention_runs
