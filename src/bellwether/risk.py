"""The risk gate: decide whether the canary earns more traffic.

This module is deliberately pure. It takes samples in and returns a decision
out, touching no files, no clock beyond a timestamp, and no MLflow. That is
what makes the gate testable -- and testability is the whole point, because the
previous version was untestable and, as a direct consequence, wrong: it never
started the proxy, so no telemetry was ever produced, so every single
"evaluation" fell through to ``random.uniform(50.0, 150.0)`` and compared a
random number against the threshold.

Two corrections to the maths, beyond wiring up real data:

* ``int(len(values) * 0.95)`` is not the 95th percentile. For n=20 it returns
  index 19 -- the maximum. :func:`percentile` uses linear interpolation between
  closest ranks, which is what "P95" is normally taken to mean.
* Absent evidence is not evidence of safety. The default policy on too few
  samples is now ABORT rather than a coin flip.
"""

from __future__ import annotations

import random
from collections.abc import Sequence

from .config import RiskSettings
from .logging_setup import get_logger
from .models import CohortStats, Decision, RequestSample, RiskAssessment
from .telemetry import TelemetrySnapshot

log = get_logger(__name__)

STABLE = "stable"
CANARY = "canary"


def percentile(values: Sequence[float], q: float) -> float:
    """The ``q``-th percentile of ``values`` (``q`` in [0, 1]).

    Linear interpolation between closest ranks -- the same definition as
    ``numpy.percentile`` with the default method, so a reviewer comparing
    against a notebook gets matching numbers.
    """
    if not values:
        return 0.0
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"q must be in [0, 1], got {q}")

    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])

    position = q * (len(ordered) - 1)
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    weight = position - lower_index
    return float(ordered[lower_index] * (1.0 - weight) + ordered[upper_index] * weight)


def summarise(samples: Sequence[RequestSample], cohort: str) -> CohortStats:
    """Aggregate the samples belonging to ``cohort``."""
    subset = [sample for sample in samples if sample.cohort == cohort]
    if not subset:
        return CohortStats.empty(cohort)

    latencies = [sample.latency_ms for sample in subset]
    errors = sum(1 for sample in subset if sample.is_error)

    return CohortStats(
        cohort=cohort,
        count=len(subset),
        error_count=errors,
        error_rate=errors / len(subset),
        latency_p50_ms=percentile(latencies, 0.50),
        latency_p95_ms=percentile(latencies, 0.95),
        latency_p99_ms=percentile(latencies, 0.99),
        latency_max_ms=max(latencies),
    )


def evaluate(
    samples: Sequence[RequestSample],
    settings: RiskSettings,
    *,
    data_source: str = "telemetry",
) -> RiskAssessment:
    """Apply the thresholds to real samples and return a decision with reasons."""
    canary = summarise(samples, CANARY)
    stable = summarise(samples, STABLE)
    reasons: list[str] = []

    if canary.latency_p95_ms > settings.latency_p95_threshold_ms:
        reasons.append(
            f"canary latency P95 {canary.latency_p95_ms:.1f}ms exceeds "
            f"threshold {settings.latency_p95_threshold_ms:.1f}ms"
        )
    if canary.error_rate > settings.error_rate_threshold:
        reasons.append(
            f"canary error rate {canary.error_rate:.2%} exceeds "
            f"threshold {settings.error_rate_threshold:.2%}"
        )

    decision = Decision.ABORT if reasons else Decision.PROMOTE
    if decision is Decision.PROMOTE:
        reasons.append(
            f"canary within thresholds over {canary.count} requests "
            f"(P95 {canary.latency_p95_ms:.1f}ms, errors {canary.error_rate:.2%})"
        )
        if stable.count:
            reasons.append(
                f"stable baseline over {stable.count} requests: "
                f"P95 {stable.latency_p95_ms:.1f}ms, errors {stable.error_rate:.2%}"
            )

    return RiskAssessment(
        decision=decision,
        reasons=reasons,
        canary=canary,
        stable=stable,
        latency_threshold_ms=settings.latency_p95_threshold_ms,
        error_rate_threshold=settings.error_rate_threshold,
        data_source=data_source,
    )


def assess(
    snapshot: TelemetrySnapshot,
    settings: RiskSettings,
    *,
    rng: random.Random | None = None,
) -> RiskAssessment:
    """Turn a telemetry snapshot into a decision, applying the data policy.

    When there is not enough fresh canary traffic to judge the build,
    ``settings.insufficient_data_policy`` decides what happens. The default,
    ``abort``, fails closed. ``simulate`` exists only so the dashboard demo can
    run without live traffic, and it labels its output ``data_source
    ="simulated"`` so a simulated pass can never be mistaken for a real one.
    """
    canary_samples = snapshot.for_cohort(CANARY)
    shortfall = _describe_shortfall(snapshot, canary_samples, settings)

    if shortfall is None:
        return evaluate(snapshot.samples, settings, data_source="telemetry")

    policy = settings.insufficient_data_policy
    log.warning("insufficient canary telemetry", extra={"reason": shortfall, "policy": policy})

    if policy == "simulate":
        return _simulated(settings, shortfall, rng or random.Random())  # noqa: S311

    empty_canary = summarise(canary_samples, CANARY)
    stable = summarise(snapshot.samples, STABLE)
    decision = Decision.PROMOTE if policy == "promote" else Decision.ABORT
    verb = "promoting" if decision is Decision.PROMOTE else "aborting"
    return RiskAssessment(
        decision=decision,
        reasons=[f"{shortfall}; {verb} per insufficient-data policy {policy!r}"],
        canary=empty_canary,
        stable=stable,
        latency_threshold_ms=settings.latency_p95_threshold_ms,
        error_rate_threshold=settings.error_rate_threshold,
        data_source="insufficient",
    )


def _describe_shortfall(
    snapshot: TelemetrySnapshot,
    canary_samples: Sequence[RequestSample],
    settings: RiskSettings,
) -> str | None:
    """Why the telemetry cannot be trusted, or ``None`` if it can."""
    if not snapshot.exists:
        return "proxy telemetry file does not exist -- was the traffic proxy started?"
    if snapshot.is_stale(settings.max_telemetry_age_s):
        age = "unknown" if snapshot.age_s is None else f"{snapshot.age_s:.0f}s"
        return f"proxy telemetry is stale (last written {age} ago)"
    if len(canary_samples) < settings.min_canary_samples:
        return (
            f"only {len(canary_samples)} canary requests observed, "
            f"need at least {settings.min_canary_samples}"
        )
    return None


def _simulated(settings: RiskSettings, shortfall: str, rng: random.Random) -> RiskAssessment:
    """Fabricate plausible metrics for demos. Never trusted as evidence."""
    count = settings.min_canary_samples
    latency = rng.uniform(40.0, 180.0)
    error_rate = rng.uniform(0.0, settings.error_rate_threshold)
    canary = CohortStats(
        cohort=CANARY,
        count=count,
        error_count=round(error_rate * count),
        error_rate=error_rate,
        latency_p50_ms=latency * 0.6,
        latency_p95_ms=latency,
        latency_p99_ms=latency * 1.1,
        latency_max_ms=latency * 1.2,
    )
    breached = (
        latency > settings.latency_p95_threshold_ms or error_rate > settings.error_rate_threshold
    )
    return RiskAssessment(
        decision=Decision.ABORT if breached else Decision.PROMOTE,
        reasons=[
            f"{shortfall}; metrics SIMULATED under policy 'simulate' -- "
            "this decision is not based on real traffic"
        ],
        canary=canary,
        stable=CohortStats.empty(STABLE),
        latency_threshold_ms=settings.latency_p95_threshold_ms,
        error_rate_threshold=settings.error_rate_threshold,
        data_source="simulated",
    )
