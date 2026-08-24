"""Domain types shared across the platform.

These are plain dataclasses with explicit ``to_dict`` methods rather than ad-hoc
dictionaries. The API serialises them directly, which means the dashboard's
contract is defined in one place and a field rename cannot silently produce a
half-populated payload.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StageStatus(str, Enum):
    """Lifecycle of a single pipeline stage."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    ROLLED_BACK = "ROLLED_BACK"


class RunStatus(str, Enum):
    """Lifecycle of a whole deployment run.

    The previous implementation used ``"FAILED"`` on the backend and compared
    against ``"FAILURE"`` on the frontend, so failed runs never rendered as
    failed. A shared enum makes that class of drift impossible.
    """

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"

    @property
    def is_terminal(self) -> bool:
        return self in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.ROLLED_BACK)


class Cohort(str, Enum):
    STABLE = "stable"
    CANARY = "canary"


class Decision(str, Enum):
    PROMOTE = "PROMOTE"
    ABORT = "ABORT"


@dataclass
class Stage:
    """One step of the rollout, as reported to the dashboard."""

    key: str
    title: str
    description: str
    traffic_pct: int = 0
    status: StageStatus = StageStatus.PENDING
    started_at: float | None = None
    finished_at: float | None = None
    detail: str | None = None

    @property
    def duration_s(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at if self.finished_at is not None else time.time()
        return round(end - self.started_at, 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "description": self.description,
            "trafficPct": self.traffic_pct,
            "status": self.status.value,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "durationS": self.duration_s,
            "detail": self.detail,
        }


@dataclass
class DeploymentRun:
    """A single execution of the rollout pipeline."""

    repo_url: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    number: int = 0
    status: RunStatus = RunStatus.QUEUED
    trigger: str = "manual"
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    traffic_pct: int = 0
    decision: str | None = None
    detail: str | None = None

    @property
    def duration_s(self) -> float | None:
        if self.finished_at is None:
            return None
        return round(self.finished_at - self.created_at, 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "number": self.number,
            "repoUrl": self.repo_url,
            "status": self.status.value,
            "trigger": self.trigger,
            "createdAt": self.created_at,
            "finishedAt": self.finished_at,
            "durationS": self.duration_s,
            "trafficPct": self.traffic_pct,
            "decision": self.decision,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class RequestSample:
    """One proxied request, as observed by the traffic proxy."""

    cohort: str
    latency_ms: float
    status_code: int
    timestamp: float

    @property
    def is_error(self) -> bool:
        """5xx counts as an error; 4xx is the client's fault, not the build's."""
        return self.status_code >= 500

    def to_dict(self) -> dict[str, Any]:
        return {
            "cohort": self.cohort,
            "latencyMs": round(self.latency_ms, 3),
            "statusCode": self.status_code,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RequestSample:
        return cls(
            cohort=str(raw["cohort"]),
            latency_ms=float(raw["latencyMs"]),
            status_code=int(raw["statusCode"]),
            timestamp=float(raw["timestamp"]),
        )


@dataclass(frozen=True)
class CohortStats:
    """Aggregated health of one cohort over the telemetry window."""

    cohort: str
    count: int
    error_count: int
    error_rate: float
    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    latency_max_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "cohort": self.cohort,
            "count": self.count,
            "errorCount": self.error_count,
            "errorRate": round(self.error_rate, 6),
            "latencyP50Ms": round(self.latency_p50_ms, 3),
            "latencyP95Ms": round(self.latency_p95_ms, 3),
            "latencyP99Ms": round(self.latency_p99_ms, 3),
            "latencyMaxMs": round(self.latency_max_ms, 3),
        }

    @classmethod
    def empty(cls, cohort: str) -> CohortStats:
        return cls(cohort, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0)


@dataclass(frozen=True)
class RiskAssessment:
    """The output of the risk engine: a decision plus the evidence behind it."""

    decision: Decision
    reasons: list[str]
    canary: CohortStats
    stable: CohortStats
    latency_threshold_ms: float
    error_rate_threshold: float
    data_source: str  # "telemetry" | "simulated" | "insufficient"
    evaluated_at: float = field(default_factory=time.time)

    @property
    def promoted(self) -> bool:
        return self.decision is Decision.PROMOTE

    @property
    def exit_code(self) -> int:
        """0 promotes, 1 aborts -- the contract the pipeline and CLI rely on."""
        return 0 if self.promoted else 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "reasons": list(self.reasons),
            "canary": self.canary.to_dict(),
            "stable": self.stable.to_dict(),
            "thresholds": {
                "latencyP95Ms": self.latency_threshold_ms,
                "errorRate": self.error_rate_threshold,
            },
            "dataSource": self.data_source,
            "evaluatedAt": self.evaluated_at,
        }

    def summary(self) -> str:
        head = (
            f"{self.decision.value} "
            f"(source={self.data_source}, canary n={self.canary.count}, "
            f"p95={self.canary.latency_p95_ms:.1f}ms, "
            f"errors={self.canary.error_rate:.1%})"
        )
        if not self.reasons:
            return head
        return head + " -- " + "; ".join(self.reasons)
