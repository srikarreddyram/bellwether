"""Shared fixtures.

Every fixture points the platform at a temporary state directory, so the suite
never touches a developer's real ``/tmp/bellwether`` or their MLflow store.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from bellwether.config import (
    ApiSettings,
    Paths,
    PipelineSettings,
    ProxySettings,
    RiskSettings,
    Settings,
)


def free_port() -> int:
    """Reserve an ephemeral port and release it for the caller to bind."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def paths(tmp_path: Path) -> Paths:
    created = Paths(state_dir=tmp_path / "state")
    created.ensure()
    return created


@pytest.fixture()
def settings(paths: Paths) -> Settings:
    return Settings(
        paths=paths,
        proxy=ProxySettings(
            port=free_port(),
            stable_port=free_port(),
            canary_port=free_port(),
            telemetry_flush_interval_s=0.0,
            upstream_timeout_s=2.0,
        ),
        risk=RiskSettings(tracking_enabled=False, min_canary_samples=5),
        api=ApiSettings(auth_token=None, webhook_secret="test-secret"),
        pipeline=PipelineSettings(
            allow_local_repos=True,
            canary_soak_s=0.2,
            promote_soak_s=0.2,
            load_workers=0,
            launch_timeout_s=15.0,
        ),
        log_level="WARNING",
    )
