"""Runtime detection and application launch.

This replaces ``launch_app.sh``. Moving it into Python buys four things the
shell version could not offer:

* **Reach.** Detection covers Docker, Procfile, Python (Flask/FastAPI/Django),
  Node, Go, Java (Maven/Gradle), Ruby, PHP, Rust, and static sites, plus an
  explicit manifest for anything else. The shell version handled four cases and
  fell through to a health stub -- which passes every check and every risk
  threshold, so the pipeline went green having deployed nothing.
* **Isolation.** Each instance gets its own virtualenv (or container). The shell
  version ran ``pip3 install -r requirements.txt`` against the *platform's own*
  interpreter, so a target repository could overwrite the platform's Flask, and
  stable and canary -- which exist precisely to run two versions of one app --
  shared a single set of site-packages.
* **Correct process ownership.** ``PORT=x npm start & echo $!`` records the npm
  wrapper's PID, not the server holding the port. Killing it orphans the server
  and the next deployment cannot bind.
* **Testability.** Detection is a pure function over a directory tree, so the
  whole runtime matrix is covered by unit tests instead of by hand.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from . import processes
from .errors import LaunchError
from .logging_setup import get_logger
from .manifest import LaunchSpec, load_manifest

log = get_logger(__name__)

HEALTH_POLL_INTERVAL_S = 0.5
_MAX_SCAN_BYTES = 256 * 1024
_PY_SEARCH_DIRS = ("", "src", "app", "api", "backend", "server")


class Runtime(str, Enum):
    DOCKER = "docker"
    PROCFILE = "procfile"
    MANIFEST = "manifest"
    FLASK = "flask"
    FASTAPI = "fastapi"
    DJANGO = "django"
    PYTHON = "python"
    NODE = "node"
    GO = "go"
    JAVA_MAVEN = "java-maven"
    JAVA_GRADLE = "java-gradle"
    RUBY = "ruby"
    PHP = "php"
    RUST = "rust"
    STATIC = "static"
    FALLBACK = "fallback"

    @property
    def is_python(self) -> bool:
        return self in (Runtime.FLASK, Runtime.FASTAPI, Runtime.DJANGO, Runtime.PYTHON)


@dataclass(frozen=True)
class Detection:
    """What we think this repository is, and why."""

    runtime: Runtime
    reason: str
    entrypoint: str | None = None
    spec: LaunchSpec | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "runtime": self.runtime.value,
            "reason": self.reason,
            "entrypoint": self.entrypoint,
            "spec": self.spec.to_dict() if self.spec else None,
        }


@dataclass
class Instance:
    """A launched application process (or container)."""

    name: str
    port: int
    pid: int
    detection: Detection
    log_path: Path
    health_path: str = "/"
    cleanup_argv: list[str] | None = None
    started_at: float = field(default_factory=time.time)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def health_url(self) -> str:
        return f"http://127.0.0.1:{self.port}{self.health_path}"

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "port": self.port,
            "pid": self.pid,
            "detection": self.detection.to_dict(),
            "logPath": str(self.log_path),
            "healthPath": self.health_path,
            "startedAt": self.started_at,
        }


# ── Detection ─────────────────────────────────────────────────────────────────

_FLASK_APP = re.compile(r"^\s*(\w+)\s*=\s*Flask\s*\(", re.MULTILINE)
_FASTAPI_APP = re.compile(r"^\s*(\w+)\s*=\s*FastAPI\s*\(", re.MULTILINE)
_EXPOSE = re.compile(r"^\s*EXPOSE\s+(\d+)", re.MULTILINE | re.IGNORECASE)
_PROCFILE_WEB = re.compile(r"^\s*web\s*:\s*(.+)$", re.MULTILINE)


def detect(repo: Path, *, override: LaunchSpec | None = None) -> Detection:
    """Identify how to run the repository at ``repo``.

    Precedence, most explicit first. An operator override beats the
    repository's own manifest, which beats every heuristic -- so a wrong guess
    is always correctable without editing the target repository.
    """
    if not repo.is_dir():
        raise LaunchError(f"repository path does not exist: {repo}")

    manifest = load_manifest(repo)
    spec = (manifest or LaunchSpec()).merge(override)

    if not spec.is_empty:
        source = "operator override" if override and not override.is_empty else ".bellwether.yml"
        if spec.start:
            return Detection(Runtime.MANIFEST, f"explicit launch spec from {source}", spec=spec)
        if spec.runtime:
            forced = _coerce_runtime(spec.runtime)
            detected = _detect_heuristically(repo)
            return Detection(
                forced,
                f"runtime forced to {forced.value} by {source}",
                entrypoint=detected.entrypoint,
                spec=spec,
            )

    detected = _detect_heuristically(repo)
    return Detection(detected.runtime, detected.reason, detected.entrypoint, spec=spec or None)


def _coerce_runtime(name: str) -> Runtime:
    try:
        return Runtime(name.lower())
    except ValueError as exc:
        valid = ", ".join(runtime.value for runtime in Runtime)
        raise LaunchError(f"unknown runtime {name!r}; valid options are: {valid}") from exc


def _detect_heuristically(repo: Path) -> Detection:
    """Guess the runtime from the repository's contents."""
    # Docker first: if a project ships a Dockerfile it has already told us
    # precisely how it builds and runs, in the one format that works for every
    # language. Nothing we can infer beats that.
    if (repo / "Dockerfile").is_file() and shutil.which("docker"):
        port = _dockerfile_exposed_port(repo / "Dockerfile")
        return Detection(
            Runtime.DOCKER,
            "Dockerfile present and docker is available",
            entrypoint=f"EXPOSE {port}" if port else None,
        )

    procfile = repo / "Procfile"
    if procfile.is_file():
        command = _procfile_web_command(procfile)
        if command:
            return Detection(Runtime.PROCFILE, "Procfile declares a web process", command)

    requirements = _read_dependency_manifests(repo)

    django_entry = _find_file(repo, "manage.py")
    if django_entry is not None and ("django" in requirements or _has_import(repo, "django")):
        return Detection(
            Runtime.DJANGO,
            "manage.py with a django dependency",
            str(django_entry.relative_to(repo)),
        )

    fastapi_entry = _find_app_module(repo, _FASTAPI_APP)
    if fastapi_entry is not None or "fastapi" in requirements:
        return Detection(
            Runtime.FASTAPI,
            "FastAPI() instance found" if fastapi_entry else "fastapi in dependencies",
            fastapi_entry or "main:app",
        )

    flask_entry = _find_app_module(repo, _FLASK_APP)
    if flask_entry is not None or "flask" in requirements:
        return Detection(
            Runtime.FLASK,
            "Flask() instance found" if flask_entry else "flask in dependencies",
            flask_entry or "app:app",
        )

    if (repo / "package.json").is_file():
        return Detection(Runtime.NODE, "package.json present", "package.json")
    if (repo / "go.mod").is_file():
        return Detection(Runtime.GO, "go.mod present", "go.mod")
    if (repo / "pom.xml").is_file():
        return Detection(Runtime.JAVA_MAVEN, "pom.xml present", "pom.xml")
    if (repo / "build.gradle").is_file() or (repo / "build.gradle.kts").is_file():
        return Detection(Runtime.JAVA_GRADLE, "build.gradle present", "build.gradle")
    if (repo / "Gemfile").is_file():
        return Detection(Runtime.RUBY, "Gemfile present", "Gemfile")
    if (repo / "Cargo.toml").is_file():
        return Detection(Runtime.RUST, "Cargo.toml present", "Cargo.toml")
    if (repo / "composer.json").is_file() or (repo / "index.php").is_file():
        return Detection(Runtime.PHP, "composer.json or index.php present")

    if requirements or any(repo.glob("*.py")):
        return Detection(Runtime.PYTHON, "python sources without a recognised web framework")
    if (repo / "index.html").is_file():
        return Detection(Runtime.STATIC, "index.html at repository root", "index.html")

    return Detection(Runtime.FALLBACK, "no recognised runtime; serving a health stub")


def _dockerfile_exposed_port(path: Path) -> int | None:
    try:
        match = _EXPOSE.search(path.read_text(encoding="utf-8", errors="replace")[:_MAX_SCAN_BYTES])
    except OSError:
        return None
    return int(match.group(1)) if match else None


def _procfile_web_command(path: Path) -> str | None:
    try:
        match = _PROCFILE_WEB.search(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return match.group(1).strip() if match else None


def _read_dependency_manifests(repo: Path) -> str:
    parts: list[str] = []
    for name in (
        "requirements.txt",
        "requirements/base.txt",
        "pyproject.toml",
        "Pipfile",
        "setup.py",
        "setup.cfg",
    ):
        candidate = repo / name
        if candidate.is_file():
            try:
                parts.append(
                    candidate.read_text(encoding="utf-8", errors="replace")[:_MAX_SCAN_BYTES]
                )
            except OSError:
                continue
    return "\n".join(parts).lower()


def _find_file(repo: Path, name: str) -> Path | None:
    for prefix in _PY_SEARCH_DIRS:
        candidate = (repo / prefix / name) if prefix else (repo / name)
        if candidate.is_file():
            return candidate
    return None


def _has_import(repo: Path, module: str) -> bool:
    pattern = re.compile(rf"^\s*(import|from)\s+{re.escape(module)}\b", re.MULTILINE)
    return any(pattern.search(text) for text in _iter_python_sources(repo))


def _iter_python_sources(repo: Path) -> Iterator[str]:
    for prefix in _PY_SEARCH_DIRS:
        directory = (repo / prefix) if prefix else repo
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.py")):
            try:
                yield path.read_text(encoding="utf-8", errors="replace")[:_MAX_SCAN_BYTES]
            except OSError:
                continue


def _find_app_module(repo: Path, pattern: re.Pattern[str]) -> str | None:
    """Return a ``module:attribute`` target for the first matching source file."""
    for prefix in _PY_SEARCH_DIRS:
        directory = (repo / prefix) if prefix else repo
        if not directory.is_dir():
            continue
        # Conventional entrypoint names first, so a helper module that also
        # constructs an app does not win over app.py.
        candidates = sorted(
            directory.glob("*.py"),
            key=lambda p: (p.stem not in ("app", "main", "server", "wsgi", "asgi"), p.stem),
        )
        for path in candidates:
            try:
                source = path.read_text(encoding="utf-8", errors="replace")[:_MAX_SCAN_BYTES]
            except OSError:
                continue
            match = pattern.search(source)
            if match:
                module = path.stem if not prefix else f"{prefix}.{path.stem}"
                return f"{module}:{match.group(1)}"
    return None


# ── Launching ─────────────────────────────────────────────────────────────────


class AppLauncher:
    """Builds isolated environments and starts target applications."""

    def __init__(
        self,
        *,
        runtime_dir: Path,
        log_dir: Path,
        install_timeout_s: float = 600.0,
        launch_timeout_s: float = 90.0,
        on_line: processes.LineSink | None = None,
    ) -> None:
        self._runtime_dir = runtime_dir
        self._log_dir = log_dir
        self._install_timeout_s = install_timeout_s
        self._launch_timeout_s = launch_timeout_s
        self._on_line = on_line

    def _emit(self, message: str) -> None:
        log.info(message)
        if self._on_line is not None:
            self._on_line(message)

    def launch(
        self,
        *,
        name: str,
        repo: Path,
        port: int,
        pid_file: Path,
        override: LaunchSpec | None = None,
    ) -> Instance:
        """Prepare and start ``repo`` on ``port``, returning once it is healthy."""
        detection = detect(repo, override=override)
        self._emit(f"[{name}] detected {detection.runtime.value} -- {detection.reason}")

        spec = detection.spec or LaunchSpec()
        workdir = repo / spec.workdir if spec.workdir else repo
        if not workdir.is_dir():
            raise LaunchError(f"workdir {spec.workdir!r} does not exist in the repository")

        # Reclaim the port before binding: a previous run may have left
        # something behind, and a bind failure is indistinguishable from a
        # broken application.
        stop_instance(pid_file)
        processes.free_port(port)

        argv, env, cleanup = self._build_command(name, workdir, port, detection, spec)

        log_path = self._log_dir / f"{name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("", encoding="utf-8")  # this run's log, not the last one's

        self._emit(f"[{name}] starting: {' '.join(argv)}")
        pid = processes.spawn(argv, cwd=workdir, log_path=log_path, env=env)

        instance = Instance(
            name=name,
            port=port,
            pid=pid,
            detection=detection,
            log_path=log_path,
            health_path=spec.health_path,
            cleanup_argv=cleanup,
        )
        _write_instance_record(pid_file, instance)

        try:
            self._await_health(instance)
        except LaunchError:
            stop_instance(pid_file)
            raise
        self._emit(f"[{name}] healthy on port {port} (pid {pid})")
        return instance

    # -- Command construction --------------------------------------------------

    def _build_command(
        self, name: str, repo: Path, port: int, detection: Detection, spec: LaunchSpec
    ) -> tuple[list[str], dict[str, str], list[str] | None]:
        env = {
            spec.port_env: str(port),
            "PORT": str(port),
            "HOST": "127.0.0.1",
            "PYTHONUNBUFFERED": "1",
            # Frameworks that hardcode one of these instead of reading PORT.
            "FLASK_RUN_PORT": str(port),
            "UVICORN_PORT": str(port),
            "SERVER_PORT": str(port),
            "BELLWETHER_INSTANCE": name,
        }
        env.update({key: value.replace("${PORT}", str(port)) for key, value in spec.env.items()})

        runtime = detection.runtime

        # Repo- or operator-declared build steps run before anything else.
        for index, command in enumerate(spec.build):
            self._emit(f"[{name}] build step {index + 1}/{len(spec.build)}: {' '.join(command)}")
            processes.run(
                command, cwd=repo, env=env, timeout=self._install_timeout_s, on_line=self._on_line
            )

        if runtime is Runtime.DOCKER:
            return self._docker_command(name, repo, port, env)

        if spec.start:
            return [part.replace("${PORT}", str(port)) for part in spec.start], env, None

        if runtime is Runtime.MANIFEST:
            raise LaunchError("launch spec selected but no start command was provided")

        if runtime is Runtime.PROCFILE:
            import shlex

            command = shlex.split(detection.entrypoint or "")
            if not command:
                raise LaunchError("Procfile web process has no command")
            return [part.replace("$PORT", str(port)) for part in command], env, None

        if runtime.is_python:
            venv = self._prepare_python_env(name, repo, runtime)
            env["VIRTUAL_ENV"] = str(venv.root)
            env["PATH"] = f"{venv.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
            return self._python_command(venv, repo, port, detection), env, None

        if runtime is Runtime.NODE:
            return self._node_command(repo, env), env, None
        if runtime is Runtime.GO:
            return self._go_command(name, repo), env, None
        if runtime is Runtime.JAVA_MAVEN:
            return self._maven_command(repo, port), env, None
        if runtime is Runtime.JAVA_GRADLE:
            return self._gradle_command(repo, port), env, None
        if runtime is Runtime.RUBY:
            return self._ruby_command(repo, port), env, None
        if runtime is Runtime.RUST:
            return self._rust_command(repo), env, None
        if runtime is Runtime.PHP:
            return (
                [processes.require("php"), "-S", f"127.0.0.1:{port}", "-t", str(repo)],
                env,
                None,
            )
        if runtime is Runtime.STATIC:
            return (
                [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                env,
                None,
            )

        return [sys.executable, str(_fallback_app_path()), str(port)], env, None

    # -- Docker ----------------------------------------------------------------

    def _docker_command(
        self, name: str, repo: Path, port: int, env: dict[str, str]
    ) -> tuple[list[str], dict[str, str], list[str]]:
        """Build the image and run it with the host port mapped in.

        Containers give the strongest isolation available here -- stable and
        canary cannot share a filesystem, a dependency tree, or a global
        interpreter -- and they make the platform genuinely language-agnostic.
        """
        docker = processes.require("docker")
        image = f"bellwether/{_slug(repo.name)}:{name}"
        container = f"bellwether-{_slug(repo.name)}-{name}"

        self._emit(f"[{name}] building container image {image}")
        processes.run(
            [docker, "build", "-t", image, "-f", str(repo / "Dockerfile"), str(repo)],
            cwd=repo,
            timeout=self._install_timeout_s,
            on_line=self._on_line,
        )

        # Remove any container left over from a previous run before reusing the
        # name, otherwise ``docker run`` fails with a name conflict.
        processes.run([docker, "rm", "-f", container], check=False, timeout=60)

        container_port = _dockerfile_exposed_port(repo / "Dockerfile") or port
        argv = [
            docker,
            "run",
            "--rm",
            "--name",
            container,
            "-p",
            f"127.0.0.1:{port}:{container_port}",
            "-e",
            f"PORT={container_port}",
            "-e",
            "HOST=0.0.0.0",
        ]
        for key, value in env.items():
            if key in ("PORT", "HOST", "PATH", "VIRTUAL_ENV"):
                continue
            argv += ["-e", f"{key}={value}"]
        argv.append(image)

        self._emit(f"[{name}] container port {container_port} mapped to host port {port}")
        return argv, env, [docker, "rm", "-f", container]

    # -- Python ----------------------------------------------------------------

    def _prepare_python_env(self, name: str, repo: Path, runtime: Runtime) -> _Venv:
        venv = _Venv(self._runtime_dir / name / "venv")
        if not venv.python.exists():
            self._emit(f"[{name}] creating an isolated virtualenv at {venv.root}")
            venv.root.parent.mkdir(parents=True, exist_ok=True)
            processes.run(
                [sys.executable, "-m", "venv", str(venv.root)],
                timeout=self._install_timeout_s,
                on_line=self._on_line,
            )

        pip_base = [
            str(venv.python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
        ]
        requirements = repo / "requirements.txt"
        if requirements.is_file():
            self._emit(f"[{name}] installing requirements.txt into the isolated environment")
            processes.run(
                [*pip_base, "-r", str(requirements)],
                cwd=repo,
                timeout=self._install_timeout_s,
                on_line=self._on_line,
            )
        elif (repo / "pyproject.toml").is_file():
            self._emit(f"[{name}] installing the project from pyproject.toml")
            processes.run(
                [*pip_base, "."],
                cwd=repo,
                timeout=self._install_timeout_s,
                on_line=self._on_line,
                check=False,
            )

        # The server that binds the port is ours, not the app's, so install it
        # rather than assuming the target repository ships one.
        server = {
            Runtime.FLASK: "gunicorn",
            Runtime.DJANGO: "gunicorn",
            Runtime.FASTAPI: "uvicorn",
        }.get(runtime)
        if server and not (venv.bin_dir / server).exists():
            self._emit(f"[{name}] installing {server} to serve the application")
            processes.run(
                [*pip_base, server],
                timeout=self._install_timeout_s,
                on_line=self._on_line,
                check=False,
            )
        return venv

    def _python_command(
        self, venv: _Venv, repo: Path, port: int, detection: Detection
    ) -> list[str]:
        bind = f"127.0.0.1:{port}"

        if detection.runtime is Runtime.FLASK:
            target = detection.entrypoint or "app:app"
            gunicorn = venv.bin_dir / "gunicorn"
            if gunicorn.exists():
                # An explicit WSGI server is the only reliable way to control
                # the bind address: FLASK_RUN_PORT is ignored by any app that
                # calls app.run(port=...) itself, which is most of them.
                return [
                    str(gunicorn),
                    "--bind",
                    bind,
                    "--workers",
                    "2",
                    "--timeout",
                    "60",
                    "--access-logfile",
                    "-",
                    target,
                ]
            return [
                str(venv.python),
                "-m",
                "flask",
                "--app",
                target.split(":")[0],
                "run",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ]

        if detection.runtime is Runtime.FASTAPI:
            target = detection.entrypoint or "main:app"
            uvicorn = venv.bin_dir / "uvicorn"
            binary = str(uvicorn) if uvicorn.exists() else None
            if binary:
                return [binary, target, "--host", "127.0.0.1", "--port", str(port)]
            return [
                str(venv.python),
                "-m",
                "uvicorn",
                target,
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ]

        if detection.runtime is Runtime.DJANGO:
            manage = repo / (detection.entrypoint or "manage.py")
            return [str(venv.python), str(manage), "runserver", bind, "--noreload"]

        main_module = _first_existing(repo, ("main.py", "app.py", "server.py", "run.py"))
        if main_module is not None:
            return [str(venv.python), str(main_module)]
        return [str(venv.python), "-m", "http.server", str(port), "--bind", "127.0.0.1"]

    # -- Node ------------------------------------------------------------------

    def _node_command(self, repo: Path, env: dict[str, str]) -> list[str]:
        npm = processes.require("npm")
        install_argv = [npm, "ci"] if (repo / "package-lock.json").is_file() else [npm, "install"]
        self._emit(f"[node] {' '.join(install_argv[1:])} (this can take a while)")
        processes.run(
            install_argv,
            cwd=repo,
            timeout=self._install_timeout_s,
            on_line=self._on_line,
            env={"npm_config_fund": "false", "npm_config_audit": "false"},
        )

        try:
            manifest = json.loads((repo / "package.json").read_text(encoding="utf-8"))
            scripts = manifest.get("scripts") or {}
        except (OSError, ValueError):
            scripts = {}

        if "build" in scripts:
            self._emit("[node] running the build script")
            processes.run(
                [npm, "run", "build"],
                cwd=repo,
                timeout=self._install_timeout_s,
                on_line=self._on_line,
                check=False,
                env=env,
            )

        if "start" in scripts:
            return [npm, "start"]
        entry = _first_existing(
            repo, ("server.js", "index.js", "app.js", "src/index.js", "dist/index.js")
        )
        if entry is not None:
            return [processes.require("node"), str(entry)]
        raise LaunchError(
            "package.json has no 'start' script and no conventional entrypoint was found. "
            "Add a start script, or declare one in .bellwether.yml."
        )

    # -- Compiled and scripted runtimes ---------------------------------------

    def _go_command(self, name: str, repo: Path) -> list[str]:
        go = processes.require("go")
        binary = self._runtime_dir / name / "app"
        binary.parent.mkdir(parents=True, exist_ok=True)
        self._emit("[go] building")
        processes.run(
            [go, "build", "-o", str(binary), "./..."],
            cwd=repo,
            timeout=self._install_timeout_s,
            on_line=self._on_line,
        )
        return [str(binary)]

    def _maven_command(self, repo: Path, port: int) -> list[str]:
        mvn = shutil.which("mvn") or str(repo / "mvnw")
        if not Path(mvn).exists() and not shutil.which("mvn"):
            raise LaunchError("pom.xml found but neither 'mvn' nor './mvnw' is available")
        self._emit("[java] mvn package -DskipTests (this can take several minutes)")
        processes.run(
            [mvn, "-B", "package", "-DskipTests"],
            cwd=repo,
            timeout=self._install_timeout_s,
            on_line=self._on_line,
        )
        jar = _newest_jar(repo / "target")
        if jar is None:
            raise LaunchError("maven build produced no runnable jar in target/")
        return [processes.require("java"), "-jar", str(jar), f"--server.port={port}"]

    def _gradle_command(self, repo: Path, port: int) -> list[str]:
        gradle = str(repo / "gradlew") if (repo / "gradlew").is_file() else shutil.which("gradle")
        if not gradle:
            raise LaunchError(
                "build.gradle found but neither './gradlew' nor 'gradle' is available"
            )
        self._emit("[java] gradle build -x test (this can take several minutes)")
        processes.run(
            [gradle, "build", "-x", "test"],
            cwd=repo,
            timeout=self._install_timeout_s,
            on_line=self._on_line,
        )
        jar = _newest_jar(repo / "build" / "libs")
        if jar is None:
            raise LaunchError("gradle build produced no runnable jar in build/libs/")
        return [processes.require("java"), "-jar", str(jar), f"--server.port={port}"]

    def _ruby_command(self, repo: Path, port: int) -> list[str]:
        bundle = processes.require("bundle")
        self._emit("[ruby] bundle install")
        processes.run(
            [bundle, "install"],
            cwd=repo,
            timeout=self._install_timeout_s,
            on_line=self._on_line,
            check=False,
        )
        if (repo / "config.ru").is_file():
            return [bundle, "exec", "rackup", "-o", "127.0.0.1", "-p", str(port)]
        return [bundle, "exec", "rails", "server", "-b", "127.0.0.1", "-p", str(port)]

    def _rust_command(self, repo: Path) -> list[str]:
        cargo = processes.require("cargo")
        self._emit("[rust] cargo build --release (this can take several minutes)")
        processes.run(
            [cargo, "build", "--release"],
            cwd=repo,
            timeout=self._install_timeout_s,
            on_line=self._on_line,
        )
        return [cargo, "run", "--release", "--quiet"]

    # -- Health ----------------------------------------------------------------

    def _await_health(self, instance: Instance) -> None:
        """Block until the app answers, it dies, or the timeout expires."""
        deadline = time.monotonic() + self._launch_timeout_s
        last_error = "no response"

        while time.monotonic() < deadline:
            if not processes.is_running(instance.pid):
                raise LaunchError(
                    f"{instance.name} exited during startup. Last log lines:\n"
                    + tail(instance.log_path, 25)
                )
            ok, last_error = probe(instance.health_url, timeout=3.0)
            if ok:
                return
            time.sleep(HEALTH_POLL_INTERVAL_S)

        raise LaunchError(
            f"{instance.name} did not answer on port {instance.port} within "
            f"{self._launch_timeout_s:.0f}s (last error: {last_error}). Log tail:\n"
            + tail(instance.log_path, 25)
        )


# ── Instance lifecycle ────────────────────────────────────────────────────────


def _write_instance_record(pid_file: Path, instance: Instance) -> None:
    """Persist the pid plus any cleanup command the runtime needs.

    Containers outlive the ``docker run`` client under some signals, so the
    cleanup command is recorded next to the pid and replayed on stop.
    """
    from .atomicio import write_json_atomic

    processes.write_pid_file(pid_file, instance.pid)
    write_json_atomic(
        pid_file.with_suffix(".json"),
        {"pid": instance.pid, "port": instance.port, "cleanup": instance.cleanup_argv},
    )


def stop_instance(pid_file: Path, *, grace_period_s: float = 5.0) -> bool:
    """Stop the process recorded in ``pid_file`` and run any cleanup command."""
    from .atomicio import read_json

    record = read_json(pid_file.with_suffix(".json"), default={}) or {}
    stopped = processes.stop_pid_file(pid_file, grace_period_s=grace_period_s)

    cleanup = record.get("cleanup")
    if isinstance(cleanup, list) and cleanup:
        log.info("running instance cleanup", extra={"argv": cleanup})
        processes.run([str(part) for part in cleanup], check=False, timeout=60)

    pid_file.with_suffix(".json").unlink(missing_ok=True)
    return stopped


def probe(url: str, *, timeout: float = 3.0) -> tuple[bool, str]:
    """Whether ``url`` is answering HTTP at all.

    Any status code counts as healthy, including 404: a target application need
    not define a root route to be up. Only a transport-level failure means it is
    not listening.
    """
    request = urllib.request.Request(url, method="GET")  # noqa: S310 - loopback only
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return True, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return True, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return False, str(exc.reason)
    except (TimeoutError, OSError) as exc:
        return False, str(exc)


def tail(path: Path, lines: int = 25) -> str:
    """Last ``lines`` of ``path``, or a placeholder if unreadable."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(log unavailable)"
    tail_lines = content.splitlines()[-lines:]
    return "\n".join(tail_lines) if tail_lines else "(log empty)"


def _first_existing(repo: Path, names: Sequence[str]) -> Path | None:
    for name in names:
        candidate = repo / name
        if candidate.is_file():
            return candidate
    return None


def _newest_jar(directory: Path) -> Path | None:
    if not directory.is_dir():
        return None
    jars = [
        jar
        for jar in directory.glob("*.jar")
        if not jar.name.endswith(("-sources.jar", "-javadoc.jar", "-plain.jar"))
    ]
    return max(jars, key=lambda p: p.stat().st_mtime) if jars else None


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "-", value.lower()).strip("-") or "app"


def _fallback_app_path() -> Path:
    return Path(__file__).resolve().parent / "resources" / "fallback_app.py"


@dataclass(frozen=True)
class _Venv:
    root: Path

    @property
    def bin_dir(self) -> Path:
        return self.root / ("Scripts" if os.name == "nt" else "bin")

    @property
    def python(self) -> Path:
        return self.bin_dir / ("python.exe" if os.name == "nt" else "python")


def purge_instance_dir(runtime_dir: Path, name: str) -> None:
    """Remove a cached virtualenv or build output for ``name``."""
    shutil.rmtree(runtime_dir / name, ignore_errors=True)
