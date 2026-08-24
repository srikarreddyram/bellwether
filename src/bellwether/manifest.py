"""Explicit launch specifications.

Auto-detection covers the common cases, but no heuristic covers *every*
project. Without an escape hatch, "works on any repository" is a claim that
fails on the first monorepo with an unusual entrypoint.

A launch spec can come from either side:

* **The target repository**, as an optional ``.bellwether.yml`` at its root. Repos
  that want to opt in can declare exactly how they build and start.
* **The platform**, passed with the deploy request. This preserves the core
  promise that a target repository needs *no modification* -- an operator can
  describe someone else's repo without touching it.

The platform-side spec wins, so an operator can always override a repository's
own declaration.

Commands are stored as argument vectors and executed without a shell. A string
form is accepted for ergonomics and split with :func:`shlex.split`, which
applies quoting rules but never interprets ``;``, ``|`` or ``$(...)``.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from .errors import ValidationError
from .logging_setup import get_logger

log = get_logger(__name__)

MANIFEST_NAMES = (".bellwether.yml", ".bellwether.yaml", "bellwether.yml", ".bellwether.json")

CommandLike = str | Sequence[str]


def _to_argv(value: CommandLike, *, field_name: str) -> list[str]:
    """Normalise a command to an argument vector."""
    if isinstance(value, str):
        argv = shlex.split(value)
    elif isinstance(value, Sequence):
        argv = [str(part) for part in value]
    else:
        raise ValidationError(f"{field_name} must be a string or a list of strings")
    if not argv:
        raise ValidationError(f"{field_name} must not be empty")
    return argv


@dataclass(frozen=True)
class LaunchSpec:
    """How to build and run one repository, stated explicitly."""

    runtime: str | None = None
    build: list[list[str]] = field(default_factory=list)
    start: list[str] | None = None
    workdir: str | None = None
    health_path: str = "/"
    port_env: str = "PORT"
    env: dict[str, str] = field(default_factory=dict)
    container_port: int | None = None

    @property
    def is_empty(self) -> bool:
        return not self.build and self.start is None and self.runtime is None

    def merge(self, other: LaunchSpec | None) -> LaunchSpec:
        """Overlay ``other`` on top of this spec; ``other`` wins field by field."""
        if other is None:
            return self
        merged_env = dict(self.env)
        merged_env.update(other.env)
        return LaunchSpec(
            runtime=other.runtime or self.runtime,
            build=other.build or self.build,
            start=other.start or self.start,
            workdir=other.workdir or self.workdir,
            health_path=other.health_path if other.health_path != "/" else self.health_path,
            port_env=other.port_env if other.port_env != "PORT" else self.port_env,
            env=merged_env,
            container_port=other.container_port or self.container_port,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime": self.runtime,
            "build": [list(cmd) for cmd in self.build],
            "start": list(self.start) if self.start else None,
            "workdir": self.workdir,
            "healthPath": self.health_path,
            "portEnv": self.port_env,
            "env": dict(self.env),
            "containerPort": self.container_port,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> LaunchSpec:
        """Parse and validate a spec from a dictionary."""
        if not isinstance(raw, Mapping):
            raise ValidationError("launch spec must be a mapping")

        unknown = set(raw) - {
            "runtime",
            "build",
            "start",
            "workdir",
            "health_path",
            "healthPath",
            "port_env",
            "portEnv",
            "env",
            "container_port",
            "containerPort",
        }
        if unknown:
            raise ValidationError(f"unknown launch spec keys: {', '.join(sorted(unknown))}")

        build_raw = raw.get("build") or []
        if isinstance(build_raw, (str, bytes)):
            build_raw = [build_raw]
        build = [
            _to_argv(entry, field_name=f"build[{index}]") for index, entry in enumerate(build_raw)
        ]

        start_raw = raw.get("start")
        start = _to_argv(start_raw, field_name="start") if start_raw else None

        env_raw = raw.get("env") or {}
        if not isinstance(env_raw, Mapping):
            raise ValidationError("env must be a mapping of names to values")
        env = {str(key): str(value) for key, value in env_raw.items()}

        workdir = raw.get("workdir")
        if workdir is not None:
            workdir = str(workdir)
            if workdir.startswith("/") or ".." in Path(workdir).parts:
                raise ValidationError("workdir must be a relative path inside the repository")

        health_path = str(raw.get("health_path") or raw.get("healthPath") or "/")
        if not health_path.startswith("/"):
            raise ValidationError("health_path must start with '/'")

        container_port = raw.get("container_port") or raw.get("containerPort")
        if container_port is not None:
            try:
                container_port = int(container_port)
            except (TypeError, ValueError) as exc:
                raise ValidationError("container_port must be an integer") from exc
            if not 1 <= container_port <= 65535:
                raise ValidationError("container_port must be between 1 and 65535")

        runtime = raw.get("runtime")
        return cls(
            runtime=str(runtime).lower() if runtime else None,
            build=build,
            start=start,
            workdir=workdir,
            health_path=health_path,
            port_env=str(raw.get("port_env") or raw.get("portEnv") or "PORT"),
            env=env,
            container_port=container_port,
        )


def load_manifest(repo: Path) -> LaunchSpec | None:
    """Read ``.bellwether.yml`` (or a JSON equivalent) from a repository root."""
    for name in MANIFEST_NAMES:
        candidate = repo / name
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue

        try:
            data = _parse(text, is_json=candidate.suffix == ".json")
        except ValidationError:
            raise
        except Exception as exc:
            raise ValidationError(f"{name} could not be parsed: {exc}") from exc

        if data is None:
            return None
        log.info("using launch manifest from the repository", extra={"file": name})
        return LaunchSpec.from_mapping(data)
    return None


def _parse(text: str, *, is_json: bool) -> Mapping[str, Any] | None:
    import json

    if is_json:
        return cast("Mapping[str, Any] | None", json.loads(text))
    try:
        import yaml
    except ImportError:  # pragma: no cover - yaml is a declared dependency
        raise ValidationError(
            "a YAML manifest was found but PyYAML is not installed; "
            "install it or use .bellwether.json instead"
        ) from None
    return cast("Mapping[str, Any] | None", yaml.safe_load(text))
