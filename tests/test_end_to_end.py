"""A real deployment, end to end.

This is the test the previous implementation could never have passed. It runs
the actual pipeline against a real git repository, launches two real instances,
starts the real proxy as a real subprocess, drives real traffic through it, and
requires the risk gate to reach its decision from telemetry the proxy genuinely
recorded -- ``data_source == "telemetry"``, never ``"simulated"``.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest

from bellwether.config import Settings
from bellwether.events import EventBus
from bellwether.models import DeploymentRun, RunStatus
from bellwether.pipeline import Pipeline
from bellwether.processes import run as run_command
from bellwether.store import DeploymentStore
from bellwether.telemetry import load_snapshot

pytestmark = pytest.mark.integration


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """A minimal static site, as a real git repository."""
    repo = tmp_path / "sample-app"
    repo.mkdir()
    (repo / "index.html").write_text("<h1>bellwether sample</h1>")
    run_command(["git", "init", "-q", "-b", "main"], cwd=repo)
    run_command(["git", "config", "user.email", "test@bellwether.local"], cwd=repo)
    run_command(["git", "config", "user.name", "bellwether tests"], cwd=repo)
    run_command(["git", "add", "-A"], cwd=repo)
    run_command(["git", "commit", "-q", "-m", "initial"], cwd=repo)
    return repo


@pytest.fixture()
def e2e_settings(settings: Settings) -> Settings:
    """Enough soak time for the load generator to produce a real sample."""
    object.__setattr__(settings.pipeline, "canary_soak_s", 4.0)
    object.__setattr__(settings.pipeline, "promote_soak_s", 3.0)
    object.__setattr__(settings.pipeline, "load_workers", 6)
    object.__setattr__(settings.risk, "min_canary_samples", 3)
    return settings


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read())


class TestFullRollout:
    def test_a_healthy_repository_reaches_100_percent(
        self, e2e_settings: Settings, git_repo: Path, tmp_path: Path
    ) -> None:
        store = DeploymentStore(tmp_path / "d.db")
        pipeline = Pipeline(e2e_settings, store=store, bus=EventBus())
        run = DeploymentRun(repo_url=str(git_repo), number=1)
        store.create(run)

        try:
            result = pipeline.execute(run)

            assert result.succeeded, f"pipeline failed: {result.error}"
            assert run.status is RunStatus.SUCCEEDED
            assert pipeline._weights.get() == 100

            # Every stage ran and succeeded.
            statuses = {stage.key: stage.status.value for stage in pipeline.stages}
            assert set(statuses.values()) == {"SUCCEEDED"}, statuses

            # The decision came from measured traffic, not a random draw.
            assert result.assessment is not None
            assert result.assessment.data_source == "telemetry"
            assert result.assessment.canary.count >= 3
            assert result.assessment.promoted

            # The proxy really did record both cohorts.
            snapshot = load_snapshot(e2e_settings.paths.telemetry_file)
            cohorts = {sample.cohort for sample in snapshot.samples}
            assert "canary" in cohorts, "the canary never received measured traffic"

        finally:
            pipeline.emergency_rollback("test teardown")
            from bellwether.launcher import stop_instance
            from bellwether.processes import stop_pid_file

            for name in ("stable", "canary"):
                stop_instance(e2e_settings.paths.pid_file(name))
            stop_pid_file(e2e_settings.paths.pid_file("proxy"))
            store.close()

    def test_traffic_actually_reaches_both_instances(
        self, e2e_settings: Settings, git_repo: Path, tmp_path: Path
    ) -> None:
        """The claim the old implementation could not back up.

        With the previous stage order there was no proxy and no stable
        instance during the canary phase, so 'traffic splitting' split nothing.
        """
        store = DeploymentStore(tmp_path / "d.db")
        pipeline = Pipeline(e2e_settings, store=store, bus=EventBus())
        run = DeploymentRun(repo_url=str(git_repo), number=1)
        store.create(run)

        try:
            repo = pipeline._stage_checkout(str(git_repo))
            pipeline._stage_verify(repo)
            pipeline._stage_baseline(repo)
            pipeline._stage_proxy()
            pipeline._stage_canary(repo)

            proxy_url = pipeline._proxy.base_url

            # 0% canary: everything must land on stable.
            pipeline._set_traffic(0)
            targets = {_target(proxy_url) for _ in range(20)}
            assert targets == {"stable"}

            # 100% canary: everything must land on canary.
            pipeline._set_traffic(100)
            targets = {_target(proxy_url) for _ in range(20)}
            assert targets == {"canary"}

            # 50%: both must be exercised.
            pipeline._set_traffic(50)
            observed = [_target(proxy_url) for _ in range(80)]
            assert set(observed) == {"stable", "canary"}, "the split reached only one cohort"

            health = fetch_json(pipeline._proxy.health_url)
            assert health["canaryWeight"] == 50

        finally:
            pipeline.emergency_rollback("test teardown")
            from bellwether.launcher import stop_instance
            from bellwether.processes import stop_pid_file

            for name in ("stable", "canary"):
                stop_instance(e2e_settings.paths.pid_file(name))
            stop_pid_file(e2e_settings.paths.pid_file("proxy"))
            store.close()


def _target(proxy_url: str) -> str:
    """Which cohort served one request, per the proxy's own header."""
    request = urllib.request.Request(proxy_url)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.headers["X-Bellwether-Target"]
