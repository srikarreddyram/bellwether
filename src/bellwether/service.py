"""Application service layer.

Holds the objects that outlive a single HTTP request -- the deployment store,
the event bus, the pipeline, the tracking client -- and enforces the platform's
one hard concurrency rule: exactly one rollout at a time.

That rule used to be ``if pipeline_state["building"]: return 400`` followed by a
separate assignment. Two requests arriving together both read ``False`` and both
started a pipeline, and two pipelines fighting over ports 8001/8002 and one
weight file is not a recoverable state. It is now a single atomic transition
under a lock.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Mapping
from typing import Any

from .config import Settings
from .errors import ConflictError
from .events import TOPIC_LOG, EventBus
from .logging_setup import get_logger
from .manifest import LaunchSpec
from .models import DeploymentRun, RunStatus
from .pipeline import Pipeline, PipelineResult
from .security import validate_repo_url
from .store import DeploymentStore
from .telemetry import load_snapshot
from .tracking import TrackingClient
from .weights import TrafficWeightStore

log = get_logger(__name__)


class PlatformService:
    """Coordinates deployments and exposes read models for the API."""

    def __init__(self, settings: Settings) -> None:
        settings.paths.ensure()
        self.settings = settings
        self.bus = EventBus()
        self.store = DeploymentStore(settings.paths.database_file)
        self.tracking = TrackingClient(settings.risk)
        self.weights = TrafficWeightStore(settings.paths.weight_file)
        self.pipeline = Pipeline(settings, store=self.store, bus=self.bus, tracking=self.tracking)

        self._run_lock = threading.Lock()
        self._active_run_id: str | None = None
        self._worker: threading.Thread | None = None
        self._console: deque[str] = deque(maxlen=settings.api.console_buffer_lines)
        self._console_lock = threading.Lock()

        self.bus.subscribe(self._capture_console)
        orphans = self.store.reconcile_orphans()
        if orphans:
            log.warning("reconciled interrupted runs at startup", extra={"count": orphans})

    # ── Console buffer ────────────────────────────────────────────────────────

    def _capture_console(self, topic: str, payload: dict[str, Any]) -> None:
        if topic != TOPIC_LOG:
            return
        line = str(payload.get("line", ""))
        with self._console_lock:
            self._console.append(line)

    def console(self) -> list[str]:
        with self._console_lock:
            return list(self._console)

    # ── Deployments ───────────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        with self._run_lock:
            return self._active_run_id is not None

    def start_deployment(
        self,
        repo_url: str,
        *,
        trigger: str = "manual",
        launch: Mapping[str, Any] | None = None,
    ) -> DeploymentRun:
        """Validate, record, and start a rollout. Raises on a concurrent run.

        ``launch`` is an optional platform-side launch spec. It lets an
        operator describe how to build and run a repository they do not
        control, which is what keeps the "no modification to the target
        repository" promise true when auto-detection guesses wrong.
        """
        ref = validate_repo_url(
            repo_url,
            allowed_hosts=self.settings.pipeline.allowed_repo_hosts,
            allow_local=self.settings.pipeline.allow_local_repos,
        )
        override = LaunchSpec.from_mapping(launch) if launch else None

        with self._run_lock:
            if self._active_run_id is not None:
                raise ConflictError(
                    f"deployment {self._active_run_id} is already running; "
                    "wait for it to finish or roll it back first"
                )
            run = DeploymentRun(
                repo_url=ref.url,
                number=self.store.next_number(),
                trigger=trigger,
                status=RunStatus.QUEUED,
            )
            self.store.create(run)
            self._active_run_id = run.id

            with self._console_lock:
                self._console.clear()

            self._worker = threading.Thread(
                target=self._execute,
                args=(run, override),
                name=f"pipeline-{run.number}",
                daemon=True,
            )
            self._worker.start()

        return run

    def _execute(self, run: DeploymentRun, override: LaunchSpec | None = None) -> None:
        try:
            result: PipelineResult = self.pipeline.execute(run, launch_override=override)
            log.info(
                "run finished",
                extra={"run_id": run.id, "status": result.run.status.value},
            )
        except Exception:
            log.exception("pipeline worker crashed", extra={"run_id": run.id})
            self.store.update(
                run.id,
                status=RunStatus.FAILED,
                detail="pipeline worker crashed; see platform logs",
                finished=True,
            )
        finally:
            with self._run_lock:
                self._active_run_id = None

    def rollback(self, reason: str = "operator requested rollback") -> dict[str, Any]:
        """Roll back immediately, running or not."""
        result = self.pipeline.emergency_rollback(reason)
        active = self._active_run_id
        if active:
            self.store.update(active, status=RunStatus.ROLLED_BACK, detail=reason)
        return result

    # ── Read models ───────────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        snapshot = self.pipeline.snapshot()
        snapshot["building"] = self.is_running
        snapshot["activeRunId"] = self._active_run_id
        return snapshot

    def history(self, limit: int = 25, offset: int = 0) -> dict[str, Any]:
        return {
            "runs": [run.to_dict() for run in self.store.recent(limit=limit, offset=offset)],
            "stats": self.store.stats(),
        }

    def telemetry(self) -> dict[str, Any]:
        """Live proxy telemetry, aggregated per cohort.

        This is the real request stream, not MLflow history. The old dashboard
        charted one point per MLflow *run* under the label "Canary Latency",
        which is a different quantity entirely and made a 20-build history look
        like a 20-second latency trace.
        """
        from .risk import summarise

        snapshot = load_snapshot(self.settings.paths.telemetry_file)
        return {
            "ageS": snapshot.age_s,
            "sampleCount": len(snapshot.samples),
            "cohorts": {
                "canary": summarise(snapshot.samples, "canary").to_dict(),
                "stable": summarise(snapshot.samples, "stable").to_dict(),
            },
            "samples": [sample.to_dict() for sample in snapshot.samples[-200:]],
            "thresholds": {
                "latencyP95Ms": self.settings.risk.latency_p95_threshold_ms,
                "errorRate": self.settings.risk.error_rate_threshold,
            },
        }

    def metrics(self, limit: int | None = None) -> dict[str, Any]:
        limit = limit or self.settings.api.metrics_page_size
        return {
            "trackingEnabled": self.tracking.enabled,
            "runs": self.tracking.recent_runs(limit=limit),
        }

    def platform_config(self) -> dict[str, Any]:
        """Everything the dashboard needs to render itself.

        Serving the stage catalogue means the UI cannot drift from what the
        backend actually executes.
        """
        from .pipeline import STAGE_DEFINITIONS

        return {
            "version": "3.0.0",
            "stages": [definition.to_dict() for definition in STAGE_DEFINITIONS],
            "proxy": {
                "url": f"http://{self.settings.proxy.host}:{self.settings.proxy.port}",
                "stablePort": self.settings.proxy.stable_port,
                "canaryPort": self.settings.proxy.canary_port,
                "stickySessions": self.settings.proxy.sticky_sessions,
            },
            "risk": {
                "latencyP95Ms": self.settings.risk.latency_p95_threshold_ms,
                "errorRate": self.settings.risk.error_rate_threshold,
                "minCanarySamples": self.settings.risk.min_canary_samples,
                "insufficientDataPolicy": self.settings.risk.insufficient_data_policy,
            },
            "chaosAvailable": self.settings.proxy.chaos_enabled,
            "allowedRepoHosts": list(self.settings.pipeline.allowed_repo_hosts),
        }

    # ── Chaos ─────────────────────────────────────────────────────────────────

    def chaos_state(self) -> bool:
        from .atomicio import read_text

        raw = read_text(self.settings.paths.chaos_file)
        return bool(raw and raw.strip() == "1")

    def set_chaos(self, enabled: bool) -> bool:
        from .atomicio import write_atomic

        if not self.settings.proxy.chaos_enabled:
            raise ConflictError(
                "chaos injection is disabled; start the platform with "
                "BELLWETHER_ENABLE_CHAOS=1 to allow it"
            )
        write_atomic(self.settings.paths.chaos_file, "1" if enabled else "0")
        log.warning("chaos injection toggled", extra={"enabled": enabled})
        return enabled

    def shutdown(self) -> None:
        self.store.close()
        self.bus.clear()


def _now() -> float:
    return time.time()
