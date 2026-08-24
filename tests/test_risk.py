"""The risk gate.

The previous engine was never exercised against real telemetry -- the proxy was
never started -- so every decision came from ``random.uniform()``. These tests
pin the arithmetic and the fail-closed behaviour that replaced it.
"""

from __future__ import annotations

import random

import pytest

from bellwether.config import RiskSettings
from bellwether.models import Decision, RequestSample
from bellwether.risk import assess, evaluate, percentile, summarise
from bellwether.telemetry import TelemetrySnapshot


def samples(cohort: str, latencies, statuses=None):
    statuses = statuses or [200] * len(latencies)
    return [
        RequestSample(cohort=cohort, latency_ms=lat, status_code=code, timestamp=1000.0 + i)
        for i, (lat, code) in enumerate(zip(latencies, statuses, strict=False))
    ]


class TestPercentile:
    def test_empty(self) -> None:
        assert percentile([], 0.95) == 0.0

    def test_single_value(self) -> None:
        assert percentile([42.0], 0.95) == 42.0

    def test_median(self) -> None:
        assert percentile([1, 2, 3, 4, 5], 0.5) == 3.0

    def test_interpolates_between_ranks(self) -> None:
        # position = 0.95 * 9 = 8.55 -> between 9 and 10, 55% of the way.
        assert percentile(list(range(1, 11)), 0.95) == pytest.approx(9.55)

    def test_p95_of_twenty_is_not_the_maximum(self) -> None:
        """``int(len(v) * 0.95)`` returned index 19 -- the max -- for n=20.

        That systematically overstated latency and made the gate abort on a
        single slow outlier.
        """
        values = [10.0] * 19 + [10_000.0]
        assert percentile(values, 0.95) < 1000.0
        assert max(values) == 10_000.0

    def test_ordering_is_independent_of_input_order(self) -> None:
        shuffled = [5, 1, 4, 2, 3]
        assert percentile(shuffled, 0.5) == percentile(sorted(shuffled), 0.5)

    def test_rejects_out_of_range_q(self) -> None:
        with pytest.raises(ValueError, match=r"\[0, 1\]"):
            percentile([1.0], 1.5)


class TestSummarise:
    def test_empty_cohort(self) -> None:
        stats = summarise([], "canary")
        assert stats.count == 0
        assert stats.error_rate == 0.0

    def test_only_counts_its_own_cohort(self) -> None:
        mixed = samples("canary", [10, 20]) + samples("stable", [1000, 2000])
        assert summarise(mixed, "canary").count == 2
        assert summarise(mixed, "canary").latency_max_ms == 20

    def test_5xx_counts_as_error_but_4xx_does_not(self) -> None:
        # A 404 is the client's fault; counting it against the build would abort
        # rollouts because someone probed a nonexistent path.
        stats = summarise(samples("canary", [1, 1, 1, 1], [200, 404, 500, 503]), "canary")
        assert stats.error_count == 2
        assert stats.error_rate == 0.5


class TestEvaluate:
    settings = RiskSettings(latency_p95_threshold_ms=500.0, error_rate_threshold=0.05)

    def test_healthy_canary_promotes(self) -> None:
        result = evaluate(samples("canary", [50] * 20), self.settings)
        assert result.decision is Decision.PROMOTE
        assert result.exit_code == 0

    def test_slow_canary_aborts(self) -> None:
        result = evaluate(samples("canary", [900] * 20), self.settings)
        assert result.decision is Decision.ABORT
        assert result.exit_code == 1
        assert any("latency" in reason for reason in result.reasons)

    def test_erroring_canary_aborts(self) -> None:
        statuses = [500] * 3 + [200] * 17
        result = evaluate(samples("canary", [50] * 20, statuses), self.settings)
        assert result.decision is Decision.ABORT
        assert any("error rate" in reason for reason in result.reasons)

    def test_stable_cohort_health_does_not_gate_promotion(self) -> None:
        """A struggling baseline is not a reason to block a healthy candidate."""
        mixed = samples("canary", [40] * 20) + samples("stable", [5000] * 20, [500] * 20)
        result = evaluate(mixed, self.settings)
        assert result.decision is Decision.PROMOTE
        assert result.stable.count == 20

    def test_reasons_are_populated_on_promotion_too(self) -> None:
        result = evaluate(samples("canary", [50] * 10), self.settings)
        assert result.reasons, "a promote decision must still be explainable"


class TestInsufficientData:
    base = RiskSettings(min_canary_samples=5, tracking_enabled=False)

    def snapshot(self, canary_count: int, age: float = 1.0) -> TelemetrySnapshot:
        return TelemetrySnapshot(
            samples=samples("canary", [50] * canary_count), age_s=age, exists=True
        )

    def test_defaults_to_abort(self) -> None:
        """Absent evidence is not evidence of safety."""
        result = assess(self.snapshot(2), self.base)
        assert result.decision is Decision.ABORT
        assert result.data_source == "insufficient"

    def test_missing_telemetry_file_aborts_and_says_why(self) -> None:
        empty = TelemetrySnapshot(samples=[], age_s=None, exists=False)
        result = assess(empty, self.base)
        assert result.decision is Decision.ABORT
        assert "proxy" in result.reasons[0]

    def test_stale_telemetry_aborts(self) -> None:
        stale = self.snapshot(50, age=99_999.0)
        result = assess(stale, self.base)
        assert result.decision is Decision.ABORT
        assert "stale" in result.reasons[0]

    def test_promote_policy_fails_open(self) -> None:
        settings = RiskSettings(min_canary_samples=5, insufficient_data_policy="promote")
        assert assess(self.snapshot(1), settings).decision is Decision.PROMOTE

    def test_simulate_policy_is_labelled_as_simulated(self) -> None:
        settings = RiskSettings(min_canary_samples=5, insufficient_data_policy="simulate")
        result = assess(self.snapshot(0), settings, rng=random.Random(0))
        assert result.data_source == "simulated"
        assert any("SIMULATED" in reason for reason in result.reasons)

    def test_sufficient_data_uses_real_telemetry(self) -> None:
        result = assess(self.snapshot(20), self.base)
        assert result.data_source == "telemetry"
        assert result.canary.count == 20
