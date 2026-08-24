"""Bellwether -- risk-gated progressive delivery for unmodified repositories.

The platform clones a target repository twice, runs the two copies side by
side as *stable* and *canary*, routes real traffic between them through a
measuring proxy, and promotes or rolls back based on what that traffic actually
did.

Layout:

    config      typed settings, validated once at startup
    security    repository URL validation and request authentication
    processes   subprocess and process-group management (never a shell)
    launcher    runtime detection and isolated application launch
    proxy       the weighted traffic proxy and telemetry source
    telemetry   thread-safe, atomically persisted request samples
    risk        the pure scoring engine
    tracking    MLflow audit trail (optional, failure-tolerant)
    store       SQLite deployment history
    pipeline    the rollout state machine
    service     coordination and concurrency control
    api         HTTP + websocket surface
"""

from __future__ import annotations

__version__ = "3.0.0"
__all__ = ["__version__"]
