"""Typed, environment-driven settings.

Every tunable in the platform is read here exactly once and validated at
startup, so a typo in an environment variable fails immediately with a clear
message instead of silently degrading behaviour at 3am.

Nothing in this module reads a hardcoded ``/tmp`` path: the runtime state
directory is configurable, which is what lets the test suite run a full
pipeline against a temporary directory without touching a developer's machine.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigurationError

# ── Environment parsing helpers ───────────────────────────────────────────────

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off", ""})


def _get(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _get_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = int(raw.strip())
        except ValueError as exc:
            raise ConfigurationError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ConfigurationError(f"{name} must be <= {maximum}, got {value}")
    return value


def _get_float(
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = float(raw.strip())
        except ValueError as exc:
            raise ConfigurationError(f"{name} must be a number, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ConfigurationError(f"{name} must be <= {maximum}, got {value}")
    return value


def _get_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    lowered = raw.strip().lower()
    if lowered in _TRUTHY:
        return True
    if lowered in _FALSY:
        return False
    raise ConfigurationError(f"{name} must be a boolean, got {raw!r}")


def _get_list(name: str, default: Sequence[str]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return tuple(default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _get_choice(name: str, default: str, allowed: Sequence[str]) -> str:
    value = _get(name, default).lower()
    if value not in allowed:
        raise ConfigurationError(f"{name} must be one of {sorted(allowed)}, got {value!r}")
    return value


def _default_state_dir() -> Path:
    configured = os.environ.get("BELLWETHER_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path(tempfile.gettempdir()) / "bellwether"


# ── Settings groups ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Paths:
    """Every file the platform reads or writes at runtime.

    Grouping them here means a test can point the whole platform at a tmpdir,
    and an operator can see the complete on-disk footprint in one place.
    """

    state_dir: Path

    @property
    def weight_file(self) -> Path:
        return self.state_dir / "traffic_weight"

    @property
    def telemetry_file(self) -> Path:
        return self.state_dir / "proxy_telemetry.json"

    @property
    def chaos_file(self) -> Path:
        return self.state_dir / "chaos_mode"

    @property
    def workspace_dir(self) -> Path:
        return self.state_dir / "workspace"

    @property
    def runtime_dir(self) -> Path:
        """Per-instance virtualenvs and checkouts for launched target apps."""
        return self.state_dir / "instances"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def database_file(self) -> Path:
        override = os.environ.get("BELLWETHER_DATABASE")
        return Path(override) if override else self.state_dir / "deployments.db"

    def pid_file(self, name: str) -> Path:
        return self.state_dir / f"{name}.pid"

    def log_file(self, name: str) -> Path:
        return self.log_dir / f"{name}.log"

    def ensure(self) -> None:
        for directory in (self.state_dir, self.workspace_dir, self.runtime_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class ProxySettings:
    host: str = "127.0.0.1"
    port: int = 9000
    upstream_host: str = "127.0.0.1"
    stable_port: int = 8001
    canary_port: int = 8002
    upstream_timeout_s: float = 10.0
    sticky_sessions: bool = True
    cookie_name: str = "bellwether_cohort"
    cookie_max_age_s: int = 600
    telemetry_window: int = 500
    telemetry_flush_interval_s: float = 0.25
    chaos_enabled: bool = False
    chaos_latency_min_s: float = 0.3
    chaos_latency_max_s: float = 0.8
    chaos_error_rate: float = 0.15

    def upstream_port(self, cohort: str) -> int:
        return self.canary_port if cohort == "canary" else self.stable_port


@dataclass(frozen=True)
class RiskSettings:
    latency_p95_threshold_ms: float = 500.0
    error_rate_threshold: float = 0.05
    min_canary_samples: int = 5
    max_telemetry_age_s: float = 120.0
    # What to do when there is not enough real traffic to judge the canary:
    #   abort    -- fail closed. The default: never promote on absent evidence.
    #   simulate -- synthesise plausible metrics. Demo only; flagged in output.
    #   promote  -- fail open. Only where the gate is explicitly advisory.
    insufficient_data_policy: str = "abort"
    tracking_enabled: bool = True
    # MLflow's filesystem store was deprecated in February 2026; SQLite is the
    # supported local backend and gives us real queries for retention and for
    # the dashboard's history view.
    tracking_uri: str = ""
    experiment_name: str = "bellwether-canary"
    retention_runs: int = 50


@dataclass(frozen=True)
class ApiSettings:
    host: str = "127.0.0.1"
    port: int = 5001
    cors_origins: tuple[str, ...] = ("http://localhost:5173", "http://127.0.0.1:5173")
    auth_token: str | None = None
    webhook_secret: str | None = None
    console_buffer_lines: int = 500
    metrics_page_size: int = 50


@dataclass(frozen=True)
class PipelineSettings:
    allowed_repo_hosts: tuple[str, ...] = ("github.com", "gitlab.com", "bitbucket.org")
    allow_local_repos: bool = False
    clone_depth: int = 1
    clone_timeout_s: float = 300.0
    launch_timeout_s: float = 90.0
    dependency_install_timeout_s: float = 600.0
    canary_soak_s: float = 20.0
    promote_soak_s: float = 10.0
    load_workers: int = 8
    load_interval_s: float = 0.05
    stable_ref: str | None = None
    cloud_ci_enabled: bool = False
    cloud_ci_repo: str = ""
    cloud_ci_workflow: str = "cloud_ci.yml"
    cloud_ci_ref: str = "main"
    cloud_ci_timeout_s: float = 600.0
    github_token: str | None = None


@dataclass(frozen=True)
class Settings:
    paths: Paths
    proxy: ProxySettings
    risk: RiskSettings
    api: ApiSettings
    pipeline: PipelineSettings
    log_level: str = "INFO"
    log_format: str = "text"

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from the process environment, validating as we go."""
        paths = Paths(state_dir=_default_state_dir())

        proxy = ProxySettings(
            host=_get("BELLWETHER_PROXY_HOST", "127.0.0.1"),
            port=_get_int("BELLWETHER_PROXY_PORT", 9000, minimum=1, maximum=65535),
            upstream_host=_get("BELLWETHER_UPSTREAM_HOST", "127.0.0.1"),
            stable_port=_get_int("BELLWETHER_STABLE_PORT", 8001, minimum=1, maximum=65535),
            canary_port=_get_int("BELLWETHER_CANARY_PORT", 8002, minimum=1, maximum=65535),
            upstream_timeout_s=_get_float("BELLWETHER_UPSTREAM_TIMEOUT_S", 10.0, minimum=0.1),
            sticky_sessions=_get_bool("BELLWETHER_STICKY_SESSIONS", True),
            cookie_max_age_s=_get_int("BELLWETHER_COHORT_TTL_S", 600, minimum=1),
            telemetry_window=_get_int("BELLWETHER_TELEMETRY_WINDOW", 500, minimum=10),
            telemetry_flush_interval_s=_get_float(
                "BELLWETHER_TELEMETRY_FLUSH_S", 0.25, minimum=0.0
            ),
            chaos_enabled=_get_bool("BELLWETHER_ENABLE_CHAOS", False),
            chaos_error_rate=_get_float(
                "BELLWETHER_CHAOS_ERROR_RATE", 0.15, minimum=0.0, maximum=1.0
            ),
        )
        if proxy.stable_port == proxy.canary_port:
            raise ConfigurationError(
                "BELLWETHER_STABLE_PORT and BELLWETHER_CANARY_PORT must differ"
            )
        if proxy.port in (proxy.stable_port, proxy.canary_port):
            raise ConfigurationError("BELLWETHER_PROXY_PORT collides with an upstream app port")

        risk = RiskSettings(
            latency_p95_threshold_ms=_get_float(
                "BELLWETHER_LATENCY_P95_THRESHOLD_MS", 500.0, minimum=0.0
            ),
            error_rate_threshold=_get_float(
                "BELLWETHER_ERROR_RATE_THRESHOLD", 0.05, minimum=0.0, maximum=1.0
            ),
            min_canary_samples=_get_int("BELLWETHER_MIN_CANARY_SAMPLES", 5, minimum=1),
            max_telemetry_age_s=_get_float("BELLWETHER_MAX_TELEMETRY_AGE_S", 120.0, minimum=1.0),
            insufficient_data_policy=_get_choice(
                "BELLWETHER_INSUFFICIENT_DATA_POLICY", "abort", ("abort", "simulate", "promote")
            ),
            tracking_enabled=_get_bool("BELLWETHER_TRACKING_ENABLED", True),
            tracking_uri=_get("MLFLOW_TRACKING_URI", f"sqlite:///{paths.state_dir / 'mlflow.db'}"),
            experiment_name=_get("BELLWETHER_MLFLOW_EXPERIMENT", "bellwether-canary"),
            retention_runs=_get_int("BELLWETHER_MLFLOW_RETENTION_RUNS", 50, minimum=1),
        )

        api = ApiSettings(
            host=_get("BELLWETHER_API_HOST", "127.0.0.1"),
            port=_get_int("BELLWETHER_API_PORT", 5001, minimum=1, maximum=65535),
            cors_origins=_get_list(
                "BELLWETHER_CORS_ORIGINS", ("http://localhost:5173", "http://127.0.0.1:5173")
            ),
            auth_token=os.environ.get("BELLWETHER_API_TOKEN") or None,
            webhook_secret=os.environ.get("BELLWETHER_WEBHOOK_SECRET") or None,
            console_buffer_lines=_get_int("BELLWETHER_CONSOLE_LINES", 500, minimum=50),
        )
        if "*" in api.cors_origins:
            raise ConfigurationError(
                "BELLWETHER_CORS_ORIGINS may not be '*': /api/deploy executes code from the "
                "request body, so a wildcard origin would let any website you visit trigger "
                "a deployment. List explicit origins instead."
            )

        pipeline = PipelineSettings(
            allowed_repo_hosts=_get_list(
                "BELLWETHER_ALLOWED_REPO_HOSTS", ("github.com", "gitlab.com", "bitbucket.org")
            ),
            allow_local_repos=_get_bool("BELLWETHER_ALLOW_LOCAL_REPOS", False),
            clone_depth=_get_int("BELLWETHER_CLONE_DEPTH", 1, minimum=0),
            launch_timeout_s=_get_float("BELLWETHER_LAUNCH_TIMEOUT_S", 90.0, minimum=1.0),
            canary_soak_s=_get_float("BELLWETHER_CANARY_SOAK_S", 20.0, minimum=0.0),
            promote_soak_s=_get_float("BELLWETHER_PROMOTE_SOAK_S", 10.0, minimum=0.0),
            load_workers=_get_int("BELLWETHER_LOAD_WORKERS", 8, minimum=0),
            stable_ref=os.environ.get("BELLWETHER_STABLE_REF") or None,
            cloud_ci_enabled=_get_bool("BELLWETHER_CLOUD_CI_ENABLED", False),
            cloud_ci_repo=_get("BELLWETHER_CLOUD_CI_REPO", ""),
            cloud_ci_workflow=_get("BELLWETHER_CLOUD_CI_WORKFLOW", "cloud_ci.yml"),
            cloud_ci_ref=_get("BELLWETHER_CLOUD_CI_REF", "main"),
            cloud_ci_timeout_s=_get_float("BELLWETHER_CLOUD_CI_TIMEOUT_S", 600.0, minimum=10.0),
            github_token=os.environ.get("GITHUB_TOKEN") or None,
        )
        if pipeline.cloud_ci_enabled and not pipeline.cloud_ci_repo:
            raise ConfigurationError(
                "BELLWETHER_CLOUD_CI_ENABLED is set but BELLWETHER_CLOUD_CI_REPO is empty"
            )
        if pipeline.cloud_ci_enabled and not pipeline.github_token:
            raise ConfigurationError(
                "BELLWETHER_CLOUD_CI_ENABLED is set but GITHUB_TOKEN is not available"
            )

        return cls(
            paths=paths,
            proxy=proxy,
            risk=risk,
            api=api,
            pipeline=pipeline,
            log_level=_get("BELLWETHER_LOG_LEVEL", "INFO").upper(),
            log_format=_get_choice("BELLWETHER_LOG_FORMAT", "text", ("text", "json")),
        )


def child_env(settings: Settings) -> dict[str, str]:
    """Environment that reproduces ``settings`` in a child process.

    The proxy runs as its own process so restarting the control plane does not
    drop live traffic. It rebuilds its configuration from the environment, so a
    parent running on non-default ports must hand those ports down explicitly --
    otherwise the child silently binds the defaults and the pipeline routes
    traffic to a proxy that is listening somewhere else.
    """
    return {
        "BELLWETHER_STATE_DIR": str(settings.paths.state_dir),
        "BELLWETHER_PROXY_HOST": settings.proxy.host,
        "BELLWETHER_PROXY_PORT": str(settings.proxy.port),
        "BELLWETHER_UPSTREAM_HOST": settings.proxy.upstream_host,
        "BELLWETHER_STABLE_PORT": str(settings.proxy.stable_port),
        "BELLWETHER_CANARY_PORT": str(settings.proxy.canary_port),
        "BELLWETHER_UPSTREAM_TIMEOUT_S": str(settings.proxy.upstream_timeout_s),
        "BELLWETHER_STICKY_SESSIONS": "1" if settings.proxy.sticky_sessions else "0",
        "BELLWETHER_COHORT_TTL_S": str(settings.proxy.cookie_max_age_s),
        "BELLWETHER_TELEMETRY_WINDOW": str(settings.proxy.telemetry_window),
        "BELLWETHER_TELEMETRY_FLUSH_S": str(settings.proxy.telemetry_flush_interval_s),
        "BELLWETHER_ENABLE_CHAOS": "1" if settings.proxy.chaos_enabled else "0",
        "BELLWETHER_CHAOS_ERROR_RATE": str(settings.proxy.chaos_error_rate),
        "BELLWETHER_LOG_LEVEL": settings.log_level,
        "BELLWETHER_LOG_FORMAT": settings.log_format,
        "PYTHONUNBUFFERED": "1",
    }


def load_settings(*, dotenv: bool = True) -> Settings:
    """Load ``.env`` (if present) and build validated settings."""
    if dotenv:
        try:
            from dotenv import load_dotenv
        except ImportError:  # pragma: no cover - optional at runtime
            pass
        else:
            load_dotenv(override=False)
    return Settings.from_env()
