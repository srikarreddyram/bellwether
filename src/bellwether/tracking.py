"""MLflow adapter for the deployment audit trail.

Every promote/abort decision is logged with its metrics, thresholds and
reasons, which is what makes the trail auditable after the fact.

MLflow is an *optional* dependency. If it is missing or the tracking store is
unwritable, deployments still run -- losing observability must never take the
deployment path down with it. Failures are logged loudly and the pipeline
continues.

Retention is handled through the MLflow client rather than the previous
``os.system("ls -1dt ./mlruns/0/* | tail -n +51 | xargs rm -rf")``, which
hardcoded experiment ``0``, raced the run it was executing inside, and would
happily delete anything matching the glob if the shell expanded it unexpectedly.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .config import RiskSettings
from .logging_setup import get_logger
from .models import RiskAssessment

log = get_logger(__name__)


class TrackingClient:
    """Thin, failure-tolerant wrapper over the MLflow tracking API."""

    def __init__(self, settings: RiskSettings) -> None:
        self._settings = settings
        self._mlflow: Any | None = None
        self._available: bool | None = None

    @property
    def enabled(self) -> bool:
        if not self._settings.tracking_enabled:
            return False
        if self._available is None:
            self._available = self._initialise()
        return self._available

    def _initialise(self) -> bool:
        try:
            import mlflow
        except ImportError:
            log.info("mlflow is not installed; deployment decisions will not be tracked")
            return False
        try:
            uri = self._settings.tracking_uri
            # A sqlite:/// URI names a file that MLflow will not create a
            # directory for; do it here so first run does not fail on a fresh
            # state directory.
            if uri.startswith("sqlite:///"):
                Path(uri[len("sqlite:///") :]).parent.mkdir(parents=True, exist_ok=True)
            mlflow.set_tracking_uri(uri)
            mlflow.set_experiment(self._settings.experiment_name)
        except Exception:
            log.exception("could not initialise mlflow tracking; continuing without it")
            return False
        self._mlflow = mlflow
        return True

    @contextlib.contextmanager
    def run(self, run_name: str, tags: dict[str, str] | None = None) -> Iterator[None]:
        """Context manager that starts an MLflow run, or does nothing if unavailable."""
        if not self.enabled or self._mlflow is None:
            yield
            return
        try:
            with self._mlflow.start_run(run_name=run_name, tags=tags or {}):
                yield
        except Exception:
            log.exception("mlflow run failed; deployment continues untracked")
            yield

    def log_assessment(
        self,
        assessment: RiskAssessment,
        *,
        repo_url: str,
        run_id: str,
    ) -> None:
        """Record one risk decision with the evidence that produced it."""
        if not self.enabled or self._mlflow is None:
            return
        mlflow = self._mlflow
        try:
            with mlflow.start_run(run_name=f"canary-{run_id}"):
                mlflow.set_tags(
                    {
                        "bellwether.repo": repo_url,
                        "bellwether.run_id": run_id,
                        "bellwether.decision": assessment.decision.value,
                        "bellwether.data_source": assessment.data_source,
                    }
                )
                mlflow.log_params(
                    {
                        "decision": assessment.decision.value,
                        "data_source": assessment.data_source,
                        "latency_p95_threshold_ms": assessment.latency_threshold_ms,
                        "error_rate_threshold": assessment.error_rate_threshold,
                        # MLflow truncates long param values; reasons are the
                        # human-readable audit record, so keep them bounded.
                        "reason": "; ".join(assessment.reasons)[:480] or "n/a",
                    }
                )
                mlflow.log_metrics(
                    {
                        "canary_latency_p95_ms": assessment.canary.latency_p95_ms,
                        "canary_latency_p50_ms": assessment.canary.latency_p50_ms,
                        "canary_error_rate": assessment.canary.error_rate,
                        "canary_requests": float(assessment.canary.count),
                        "stable_latency_p95_ms": assessment.stable.latency_p95_ms,
                        "stable_error_rate": assessment.stable.error_rate,
                        "stable_requests": float(assessment.stable.count),
                        "promoted": 1.0 if assessment.promoted else 0.0,
                    }
                )
        except Exception:
            log.exception("failed to log risk assessment to mlflow")

    def recent_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent runs, newest first, for the dashboard metrics view."""
        if not self.enabled or self._mlflow is None:
            return []
        mlflow = self._mlflow
        try:
            experiment = mlflow.get_experiment_by_name(self._settings.experiment_name)
            if experiment is None:
                return []
            frame = mlflow.search_runs(
                experiment_ids=[experiment.experiment_id],
                order_by=["attributes.start_time DESC"],
                max_results=limit,
            )
        except Exception:
            log.exception("failed to read mlflow runs")
            return []

        runs: list[dict[str, Any]] = []
        for record in frame.to_dict(orient="records"):
            runs.append(
                {
                    "runId": record.get("run_id"),
                    "startTime": _to_epoch_seconds(record.get("start_time")),
                    "decision": record.get("params.decision"),
                    "dataSource": record.get("params.data_source"),
                    "reason": record.get("params.reason"),
                    "canaryLatencyP95Ms": _to_float(record.get("metrics.canary_latency_p95_ms")),
                    "canaryErrorRate": _to_float(record.get("metrics.canary_error_rate")),
                    "canaryRequests": _to_float(record.get("metrics.canary_requests")),
                    "stableLatencyP95Ms": _to_float(record.get("metrics.stable_latency_p95_ms")),
                    "stableErrorRate": _to_float(record.get("metrics.stable_error_rate")),
                }
            )
        return runs

    def enforce_retention(self) -> int:
        """Delete runs beyond ``retention_runs``, newest kept. Returns count deleted."""
        if not self.enabled or self._mlflow is None:
            return 0
        mlflow = self._mlflow
        keep = self._settings.retention_runs
        try:
            experiment = mlflow.get_experiment_by_name(self._settings.experiment_name)
            if experiment is None:
                return 0
            client = mlflow.tracking.MlflowClient()
            runs = client.search_runs(
                experiment_ids=[experiment.experiment_id],
                order_by=["attributes.start_time DESC"],
                max_results=keep + 200,
            )
        except Exception:
            log.exception("failed to enumerate mlflow runs for retention")
            return 0

        deleted = 0
        for stale in runs[keep:]:
            try:
                client.delete_run(stale.info.run_id)
                deleted += 1
            except Exception:  # noqa: BLE001
                log.warning("could not delete mlflow run", extra={"run_id": stale.info.run_id})
        if deleted:
            log.info("applied mlflow retention", extra={"deleted": deleted, "kept": keep})
        return deleted


def _to_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if result != result else result  # drop NaN


def _to_epoch_seconds(value: Any) -> float | None:
    """MLflow returns pandas timestamps or epoch milliseconds depending on version."""
    if value is None:
        return None
    timestamp = getattr(value, "timestamp", None)
    if callable(timestamp):
        try:
            return float(timestamp())
        except (TypeError, ValueError, OSError):
            return None
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return None
