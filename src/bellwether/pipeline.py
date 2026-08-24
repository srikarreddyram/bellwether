"""The rollout pipeline.

The stage order here is the correction that matters most. The previous
orchestrator launched the canary first, never started the proxy at all, and
only launched the stable baseline during the *final* promotion step. The
consequences compounded:

* With no proxy on port 9000, the load generator's requests were all connection
  refused and swallowed by a bare ``except``. No telemetry was ever written.
* With no telemetry, the risk gate always took its fallback path and compared a
  ``random.uniform()`` draw against the threshold. Every "risk decision" the
  platform ever made was a random number.
* With no stable instance during the 10% and 50% phases, the 90% and 50% of
  traffic routed to "stable" hit a closed port.

A canary needs a baseline to be a canary. The order is therefore: baseline
first, then the proxy that can route to it, then the canary, and only then any
traffic shift.

The second correction is that rollback is guaranteed. Every exit path -- stage
failure, operator abort, risk gate, or an unexpected exception -- runs through
``_rollback`` in a ``finally`` block. Previously an abort simply returned
``False``, leaving the canary live and still receiving its share of traffic.
"""

from __future__ import annotations

import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import launcher, processes
from .config import Settings, child_env
from .errors import BellwetherError, PipelineAborted, StageFailed, ValidationError
from .events import TOPIC_LOG, TOPIC_RISK, TOPIC_RUN, TOPIC_STAGE, TOPIC_TRAFFIC, EventBus
from .launcher import AppLauncher, Instance, stop_instance
from .logging_setup import get_logger
from .manifest import LaunchSpec
from .models import Decision, DeploymentRun, RiskAssessment, RunStatus, Stage, StageStatus
from .risk import assess
from .security import validate_repo_url
from .store import DeploymentStore
from .telemetry import load_snapshot
from .tracking import TrackingClient
from .weights import TrafficWeightStore

log = get_logger(__name__)

STABLE = "stable"
CANARY = "canary"


# ── Stage catalogue ───────────────────────────────────────────────────────────
# The single source of truth for what a rollout consists of. The API serves this
# to the dashboard, so the UI renders whatever the backend actually runs. The
# previous design duplicated the list as a hardcoded STAGE_MAP in the frontend
# with a warning that the strings "must match exactly, including case" -- a
# comment that is really a description of a bug waiting to happen.


@dataclass(frozen=True)
class StageDefinition:
    """One declared stage. Typed rather than a dict, so a typo is a type error."""

    key: str
    title: str
    traffic: int
    description: str

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "title": self.title,
            "traffic": self.traffic,
            "description": self.description,
        }


STAGE_DEFINITIONS: list[StageDefinition] = [
    StageDefinition(
        "checkout",
        "Checkout",
        0,
        "Validate the repository URL and clone it into an isolated workspace.",
    ),
    StageDefinition(
        "verify",
        "Verify",
        0,
        "Detect the runtime and run pre-flight checks before anything binds a port.",
    ),
    StageDefinition(
        "baseline",
        "Baseline",
        0,
        "Start the stable instance. A canary is meaningless without it.",
    ),
    StageDefinition(
        "proxy",
        "Proxy",
        0,
        "Ensure the traffic proxy is live and routing 100% to stable.",
    ),
    StageDefinition(
        "canary",
        "Canary",
        0,
        "Start the candidate instance alongside stable, still receiving no traffic.",
    ),
    StageDefinition(
        "canary_10",
        "Canary 10%",
        10,
        "Shift 10% of live traffic to the candidate and soak under real load.",
    ),
    StageDefinition(
        "risk",
        "Risk Gate",
        10,
        "Score observed latency and error rate against thresholds.",
    ),
    StageDefinition(
        "promote_50",
        "Promote 50%",
        50,
        "Halve the split and re-score before committing further.",
    ),
    StageDefinition(
        "promote_100",
        "Promote 100%",
        100,
        "Send all traffic to the candidate and retire the previous baseline.",
    ),
]


def build_stages() -> list[Stage]:
    return [
        Stage(
            key=definition.key,
            title=definition.title,
            description=definition.description,
            traffic_pct=definition.traffic,
        )
        for definition in STAGE_DEFINITIONS
    ]


@dataclass
class PipelineResult:
    run: DeploymentRun
    assessment: RiskAssessment | None = None
    rolled_back: bool = False
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.run.status is RunStatus.SUCCEEDED


class AbortSignal:
    """Cooperative cancellation, checkable from any stage.

    Backed by both an in-process event (fast, used by the API) and a file (so an
    operator can abort from a separate CLI invocation).
    """

    def __init__(self, flag_file: Path) -> None:
        self._event = threading.Event()
        self._flag_file = flag_file
        self._reason = "operator requested rollback"

    def request(self, reason: str = "operator requested rollback") -> None:
        self._reason = reason
        self._event.set()
        try:
            self._flag_file.parent.mkdir(parents=True, exist_ok=True)
            self._flag_file.write_text(reason, encoding="utf-8")
        except OSError:
            log.warning("could not write abort flag", extra={"path": str(self._flag_file)})

    def clear(self) -> None:
        self._event.clear()
        self._flag_file.unlink(missing_ok=True)

    @property
    def reason(self) -> str:
        return self._reason

    def is_set(self) -> bool:
        if self._event.is_set():
            return True
        if self._flag_file.exists():
            self._event.set()
            return True
        return False

    def raise_if_set(self) -> None:
        if self.is_set():
            raise PipelineAborted(self._reason)

    def wait(self, seconds: float, *, poll: float = 0.25) -> None:
        """Sleep, but wake early and raise if an abort arrives.

        The previous ``time.sleep(6)`` inside the soak meant an operator's
        rollback click sat unacknowledged for up to six seconds.
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.raise_if_set()
            time.sleep(min(poll, max(0.0, deadline - time.monotonic())))
        self.raise_if_set()


class LoadGenerator:
    """Drives real traffic through the proxy so the risk gate has evidence."""

    def __init__(self, target: str, *, workers: int, interval_s: float) -> None:
        self._target = target
        self._workers = workers
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self.sent = 0
        self.failed = 0

    def __enter__(self) -> LoadGenerator:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def start(self) -> None:
        if self._workers <= 0:
            return
        self._stop.clear()
        for index in range(self._workers):
            thread = threading.Thread(target=self._worker, name=f"load-{index}", daemon=True)
            thread.start()
            self._threads.append(thread)
        log.info("load generator started", extra={"workers": self._workers})

    def _worker(self) -> None:
        # No cookie jar: each request must be routed independently, otherwise
        # sticky sessions would pin the whole generator to one cohort and the
        # canary would receive no measured traffic at all.
        opener = urllib.request.build_opener()
        while not self._stop.is_set():
            try:
                with opener.open(self._target, timeout=5.0):
                    pass
                self._bump(ok=True)
            except (urllib.error.URLError, TimeoutError, OSError):
                self._bump(ok=False)
            self._stop.wait(self._interval_s)

    def _bump(self, *, ok: bool) -> None:
        with self._lock:
            self.sent += 1
            if not ok:
                self.failed += 1

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=3.0)
        self._threads.clear()


class ProxySupervisor:
    """Keeps the data plane running independently of the control plane.

    The proxy is a separate process on purpose: restarting the API to deploy a
    fix should not drop live traffic.
    """

    def __init__(self, settings: Settings, emit: Callable[[str], None]) -> None:
        self._settings = settings
        self._emit = emit

    @property
    def health_url(self) -> str:
        proxy = self._settings.proxy
        return f"http://{proxy.host}:{proxy.port}/__bellwether/health"

    @property
    def base_url(self) -> str:
        proxy = self._settings.proxy
        return f"http://{proxy.host}:{proxy.port}/"

    def is_healthy(self, *, timeout: float = 2.0) -> bool:
        try:
            with urllib.request.urlopen(self.health_url, timeout=timeout) as response:  # noqa: S310
                return bool(200 <= response.status < 300)
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def ensure_running(self, *, timeout_s: float = 20.0) -> None:
        if self.is_healthy():
            self._emit(f"traffic proxy already healthy on port {self._settings.proxy.port}")
            return

        pid_file = self._settings.paths.pid_file("proxy")
        processes.stop_pid_file(pid_file)
        processes.free_port(self._settings.proxy.port, host=self._settings.proxy.host)

        argv = [sys.executable, "-m", "bellwether", "proxy"]
        log_path = self._settings.paths.log_file("proxy")
        self._emit(f"starting traffic proxy on port {self._settings.proxy.port}")
        pid = processes.spawn(
            argv, cwd=Path.cwd(), log_path=log_path, env=child_env(self._settings)
        )
        processes.write_pid_file(pid_file, pid)

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.is_healthy():
                self._emit("traffic proxy is healthy")
                return
            if not processes.is_running(pid):
                raise StageFailed(
                    "proxy",
                    "the traffic proxy exited during startup:\n" + launcher.tail(log_path, 20),
                )
            time.sleep(0.3)
        raise StageFailed(
            "proxy",
            f"the traffic proxy did not become healthy within {timeout_s:.0f}s:\n"
            + launcher.tail(log_path, 20),
        )


class Pipeline:
    """Executes one rollout, start to finish, with guaranteed cleanup."""

    def __init__(
        self,
        settings: Settings,
        *,
        store: DeploymentStore,
        bus: EventBus,
        tracking: TrackingClient | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._bus = bus
        self._tracking = tracking or TrackingClient(settings.risk)
        self._weights = TrafficWeightStore(settings.paths.weight_file)
        self.abort = AbortSignal(settings.paths.state_dir / "abort_requested")

        self.stages: list[Stage] = build_stages()
        self.run: DeploymentRun | None = None
        self.launch_override: LaunchSpec | None = None
        # Retained so a dashboard that connects after a run finished can still
        # show the verdict, rather than reporting it as never scored.
        self.last_assessment: RiskAssessment | None = None
        self._instances: dict[str, Instance] = {}
        self._proxy = ProxySupervisor(settings, self.emit)
        self._lock = threading.RLock()

    # ── Observability ─────────────────────────────────────────────────────────

    def emit(self, message: str) -> None:
        log.info(message)
        self._bus.publish(TOPIC_LOG, {"line": message, "ts": time.time()})

    def _set_stage(self, key: str, status: StageStatus, detail: str | None = None) -> None:
        with self._lock:
            for stage in self.stages:
                if stage.key != key:
                    continue
                stage.status = status
                stage.detail = detail
                if status is StageStatus.RUNNING and stage.started_at is None:
                    stage.started_at = time.time()
                if status in (
                    StageStatus.SUCCEEDED,
                    StageStatus.FAILED,
                    StageStatus.SKIPPED,
                    StageStatus.ROLLED_BACK,
                ):
                    stage.finished_at = time.time()
                self._bus.publish(TOPIC_STAGE, stage.to_dict())
                return

    def _set_traffic(self, weight: int) -> None:
        self._weights.set(weight)
        if self.run is not None:
            self.run.traffic_pct = weight
            self._store.update(self.run.id, traffic_pct=weight)
        self._bus.publish(TOPIC_TRAFFIC, {"weight": weight, "ts": time.time()})
        self.emit(f"traffic weight set to {weight}% canary")

    def snapshot(self) -> dict[str, object]:
        """Current pipeline state, as served to the dashboard."""
        with self._lock:
            return {
                "run": self.run.to_dict() if self.run else None,
                "stages": [stage.to_dict() for stage in self.stages],
                "trafficPct": self._weights.get(),
                "risk": self.last_assessment.to_dict() if self.last_assessment else None,
                "instances": {
                    name: instance.to_dict() for name, instance in self._instances.items()
                },
                "proxyHealthy": self._proxy.is_healthy(timeout=0.5),
            }

    # ── Entry point ───────────────────────────────────────────────────────────

    def execute(
        self, run: DeploymentRun, *, launch_override: LaunchSpec | None = None
    ) -> PipelineResult:
        """Run the full rollout. Always leaves traffic in a defined state."""
        self.abort.clear()
        self.stages = build_stages()
        self.run = run
        self.launch_override = launch_override
        self.last_assessment = None
        self._instances.clear()

        settings = self._settings
        settings.paths.ensure()

        run.status = RunStatus.RUNNING
        self._store.update(run.id, status=RunStatus.RUNNING)
        self._bus.publish(TOPIC_RUN, run.to_dict())
        self.emit(f"=== run {run.number} :: {run.repo_url} ===")

        result = PipelineResult(run=run)
        try:
            self._set_traffic(0)
            repo = self._stage_checkout(run.repo_url)
            self._stage_verify(repo)
            self._stage_baseline(repo)
            self._stage_proxy()
            self._stage_canary(repo)
            self._stage_canary_10()
            result.assessment = self._stage_risk("risk")
            self._stage_promote_50()
            self._stage_promote_100()

            run.status = RunStatus.SUCCEEDED
            run.decision = Decision.PROMOTE.value
            self.emit(f"=== run {run.number} promoted to 100% ===")

        except PipelineAborted as exc:
            result.error = str(exc)
            result.rolled_back = True
            run.status = RunStatus.ROLLED_BACK
            run.decision = Decision.ABORT.value
            self.emit(f"ABORTED: {exc}")
            self._mark_incomplete(StageStatus.ROLLED_BACK, str(exc))

        except StageFailed as exc:
            result.error = exc.message
            result.rolled_back = True
            run.status = RunStatus.FAILED
            run.decision = Decision.ABORT.value
            self.emit(f"FAILED at stage {exc.stage}: {exc.message}")
            self._set_stage(exc.stage, StageStatus.FAILED, exc.message)
            self._mark_incomplete(StageStatus.SKIPPED, "not reached")

        except BellwetherError as exc:
            result.error = str(exc)
            result.rolled_back = True
            run.status = RunStatus.FAILED
            run.decision = Decision.ABORT.value
            self.emit(f"FAILED: {exc}")
            self._mark_incomplete(StageStatus.SKIPPED, "not reached")

        except Exception as exc:
            log.exception("unexpected pipeline error")
            result.error = f"unexpected error: {exc}"
            result.rolled_back = True
            run.status = RunStatus.FAILED
            run.decision = Decision.ABORT.value
            self.emit(f"FAILED with an unexpected error: {exc}")
            self._mark_incomplete(StageStatus.SKIPPED, "not reached")

        finally:
            if run.status is not RunStatus.SUCCEEDED:
                self._rollback(result.error or "pipeline did not complete")
            run.finished_at = time.time()
            self._store.update(
                run.id,
                status=run.status,
                decision=run.decision,
                detail=result.error,
                traffic_pct=self._weights.get(),
                finished=True,
            )
            self._bus.publish(TOPIC_RUN, run.to_dict())
            self.abort.clear()

        return result

    def _mark_incomplete(self, status: StageStatus, detail: str) -> None:
        with self._lock:
            for stage in self.stages:
                if stage.status in (StageStatus.PENDING, StageStatus.RUNNING):
                    stage.status = status
                    stage.detail = detail
                    stage.finished_at = time.time()
                    self._bus.publish(TOPIC_STAGE, stage.to_dict())

    # ── Stages ────────────────────────────────────────────────────────────────

    def _stage_checkout(self, repo_url: str) -> Path:
        key = "checkout"
        self._set_stage(key, StageStatus.RUNNING)
        self.abort.raise_if_set()

        try:
            ref = validate_repo_url(
                repo_url,
                allowed_hosts=self._settings.pipeline.allowed_repo_hosts,
                allow_local=self._settings.pipeline.allow_local_repos,
            )
        except ValidationError as exc:
            raise StageFailed(key, str(exc)) from exc

        workspace = self._settings.paths.workspace_dir / ref.name
        self.emit(f"cloning {ref.url} into {workspace}")
        shutil.rmtree(workspace, ignore_errors=True)
        workspace.parent.mkdir(parents=True, exist_ok=True)

        argv = ["git", "clone"]
        if self._settings.pipeline.clone_depth > 0:
            argv += ["--depth", str(self._settings.pipeline.clone_depth)]
        # ``--`` terminates option parsing, so a URL can never be read as a flag.
        argv += ["--", ref.url, str(workspace)]

        try:
            processes.run(
                argv,
                timeout=self._settings.pipeline.clone_timeout_s,
                on_line=self.emit,
                env={"GIT_TERMINAL_PROMPT": "0"},
            )
        except BellwetherError as exc:
            raise StageFailed(key, f"clone failed: {exc}") from exc

        self._set_stage(key, StageStatus.SUCCEEDED, f"cloned {ref.slug}")
        return workspace

    def _stage_verify(self, repo: Path) -> None:
        key = "verify"
        self._set_stage(key, StageStatus.RUNNING)
        self.abort.raise_if_set()

        detection = launcher.detect(repo, override=self.launch_override)
        self.emit(f"runtime: {detection.runtime.value} ({detection.reason})")

        if detection.runtime is launcher.Runtime.FALLBACK:
            # Explicit and visible rather than silently deploying a stub that
            # trivially passes every health check and every risk threshold.
            self.emit(
                "WARNING: no runtime was recognised. A health stub will be deployed, "
                "which means the risk gate will not be exercising your application."
            )

        self._tracking.enforce_retention()
        self._set_stage(key, StageStatus.SUCCEEDED, detection.runtime.value)

    def _launcher(self) -> AppLauncher:
        return AppLauncher(
            runtime_dir=self._settings.paths.runtime_dir,
            log_dir=self._settings.paths.log_dir,
            install_timeout_s=self._settings.pipeline.dependency_install_timeout_s,
            launch_timeout_s=self._settings.pipeline.launch_timeout_s,
            on_line=self.emit,
        )

    def _stage_baseline(self, repo: Path) -> None:
        key = "baseline"
        self._set_stage(key, StageStatus.RUNNING)
        self.abort.raise_if_set()

        if self._settings.pipeline.stable_ref:
            self.emit(f"checking out stable ref {self._settings.pipeline.stable_ref}")
            processes.run(
                ["git", "-C", str(repo), "checkout", "--", self._settings.pipeline.stable_ref],
                on_line=self.emit,
                check=False,
            )

        try:
            instance = self._launcher().launch(
                name=STABLE,
                repo=repo,
                port=self._settings.proxy.stable_port,
                pid_file=self._settings.paths.pid_file(STABLE),
                override=self.launch_override,
            )
        except BellwetherError as exc:
            raise StageFailed(key, str(exc)) from exc

        self._instances[STABLE] = instance
        self._set_stage(key, StageStatus.SUCCEEDED, f"stable on :{instance.port}")

    def _stage_proxy(self) -> None:
        key = "proxy"
        self._set_stage(key, StageStatus.RUNNING)
        self.abort.raise_if_set()
        self._proxy.ensure_running()
        self._set_stage(key, StageStatus.SUCCEEDED, f"proxy on :{self._settings.proxy.port}")

    def _stage_canary(self, repo: Path) -> None:
        key = "canary"
        self._set_stage(key, StageStatus.RUNNING)
        self.abort.raise_if_set()

        try:
            instance = self._launcher().launch(
                name=CANARY,
                repo=repo,
                port=self._settings.proxy.canary_port,
                pid_file=self._settings.paths.pid_file(CANARY),
                override=self.launch_override,
            )
        except BellwetherError as exc:
            raise StageFailed(key, str(exc)) from exc

        self._instances[CANARY] = instance
        self._set_stage(key, StageStatus.SUCCEEDED, f"canary on :{instance.port}")

    def _soak(self, weight: int, seconds: float, label: str) -> None:
        """Shift traffic and hold, driving load so the shift is measurable."""
        self._set_traffic(weight)
        self.emit(f"{label}: soaking for {seconds:.0f}s under generated load")
        with LoadGenerator(
            self._proxy.base_url,
            workers=self._settings.pipeline.load_workers,
            interval_s=self._settings.pipeline.load_interval_s,
        ) as load:
            self.abort.wait(seconds)
            self.emit(f"{label}: {load.sent} requests generated ({load.failed} transport failures)")

    def _stage_canary_10(self) -> None:
        key = "canary_10"
        self._set_stage(key, StageStatus.RUNNING)
        self._soak(10, self._settings.pipeline.canary_soak_s, "canary 10%")
        self._set_stage(key, StageStatus.SUCCEEDED)

    def _stage_risk(self, key: str) -> RiskAssessment:
        self._set_stage(key, StageStatus.RUNNING)
        self.abort.raise_if_set()

        snapshot = load_snapshot(self._settings.paths.telemetry_file)
        assessment = assess(snapshot, self._settings.risk)

        self.last_assessment = assessment
        self.emit(f"risk gate: {assessment.summary()}")
        self._bus.publish(TOPIC_RISK, assessment.to_dict())
        if self.run is not None:
            self._tracking.log_assessment(
                assessment, repo_url=self.run.repo_url, run_id=self.run.id
            )

        if not assessment.promoted:
            self._set_stage(key, StageStatus.FAILED, "; ".join(assessment.reasons))
            raise StageFailed(key, "risk gate refused promotion: " + "; ".join(assessment.reasons))

        self._set_stage(key, StageStatus.SUCCEEDED, assessment.summary())
        return assessment

    def _stage_promote_50(self) -> None:
        key = "promote_50"
        self._set_stage(key, StageStatus.RUNNING)
        self._soak(50, self._settings.pipeline.promote_soak_s, "promote 50%")

        # Re-score at the higher weight. Load-dependent regressions only surface
        # once the canary is actually carrying meaningful traffic, so promoting
        # to 100% off a single 10% measurement would be promoting on stale
        # evidence.
        snapshot = load_snapshot(self._settings.paths.telemetry_file)
        assessment = assess(snapshot, self._settings.risk)
        self.last_assessment = assessment
        self.emit(f"re-scored at 50%: {assessment.summary()}")
        self._bus.publish(TOPIC_RISK, assessment.to_dict())
        if self.run is not None:
            self._tracking.log_assessment(
                assessment, repo_url=self.run.repo_url, run_id=f"{self.run.id}-50"
            )
        if not assessment.promoted:
            raise StageFailed(key, "risk gate refused at 50%: " + "; ".join(assessment.reasons))

        self._set_stage(key, StageStatus.SUCCEEDED, assessment.summary())

    def _stage_promote_100(self) -> None:
        key = "promote_100"
        self._set_stage(key, StageStatus.RUNNING)
        self.abort.raise_if_set()

        self._set_traffic(100)

        # Prove the canary answers on its own before retiring the only instance
        # that could serve traffic if it does not.
        canary = self._instances.get(CANARY)
        if canary is not None:
            healthy, detail = launcher.probe(canary.base_url, timeout=5.0)
            if not healthy:
                raise StageFailed(key, f"canary smoke test failed before retiring stable: {detail}")
            self.emit(f"canary smoke test passed ({detail})")

        self.emit("retiring the previous stable instance")
        stop_instance(self._settings.paths.pid_file(STABLE))
        self._instances.pop(STABLE, None)

        self._set_stage(key, StageStatus.SUCCEEDED, "canary serving 100% of traffic")

    # ── Rollback ──────────────────────────────────────────────────────────────

    def _rollback(self, reason: str) -> None:
        """Return the system to the stable baseline. Must not raise."""
        self.emit(f"rolling back: {reason}")
        try:
            self._weights.reset()
            self._bus.publish(TOPIC_TRAFFIC, {"weight": 0, "ts": time.time()})
            self.emit("traffic weight reset to 0% -- all traffic on stable")
        except Exception:
            log.exception("failed to reset traffic weight during rollback")

        try:
            stop_instance(self._settings.paths.pid_file(CANARY))
            self._instances.pop(CANARY, None)
            self.emit("canary instance stopped")
        except Exception:
            log.exception("failed to stop the canary during rollback")

        # If we failed *after* retiring stable, nothing is serving. Say so
        # loudly rather than reporting a clean rollback that did not happen.
        if STABLE not in self._instances and not self._proxy.is_healthy(timeout=1.0):
            self.emit(
                "WARNING: rollback completed but no healthy stable instance is running. "
                "Re-run the pipeline to restore service."
            )

    def request_abort(self, reason: str = "operator requested rollback") -> None:
        self.abort.request(reason)
        self.emit(f"abort requested: {reason}")

    def emergency_rollback(self, reason: str = "operator requested rollback") -> dict[str, object]:
        """Roll back immediately, whether or not a pipeline is running.

        The previous ``/api/rollback`` only wrote a flag file that the pipeline
        polled between stages -- so with no pipeline running it did nothing at
        all, while the canary happily kept serving its share of traffic.
        """
        self.request_abort(reason)
        self._rollback(reason)
        return {
            "trafficPct": self._weights.get(),
            "canaryStopped": True,
            "reason": reason,
        }
