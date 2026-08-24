"""Command construction per runtime.

Detection choosing the right runtime is worthless if the command it builds
binds the wrong port. These assert the actual argv for each runtime, stubbing
out the toolchains so the matrix runs anywhere.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from bellwether import launcher, processes
from bellwether.launcher import AppLauncher
from bellwether.manifest import LaunchSpec

PORT = 8123


@pytest.fixture()
def recorded(monkeypatch) -> list[list[str]]:
    """Record every command the launcher would run, without running any."""
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs: Any):
        calls.append([str(part) for part in argv])
        return processes.CommandResult(argv=list(argv), returncode=0, output="", duration_s=0.0)

    monkeypatch.setattr(processes, "run", fake_run)
    monkeypatch.setattr(launcher.processes, "run", fake_run)
    monkeypatch.setattr(launcher.processes, "require", lambda name: f"/usr/bin/{name}")
    return calls


@pytest.fixture()
def app_launcher(tmp_path: Path) -> AppLauncher:
    return AppLauncher(runtime_dir=tmp_path / "instances", log_dir=tmp_path / "logs")


def build(tmp_path: Path, files: dict[str, str], name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    for filename, content in files.items():
        target = repo / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return repo


def command_for(app_launcher: AppLauncher, repo: Path, **kwargs: Any):
    detection = launcher.detect(repo, override=kwargs.get("override"))
    spec = detection.spec or LaunchSpec()
    return app_launcher._build_command("canary", repo, PORT, detection, spec)


class TestPortBinding:
    """The port must reach the server, whatever the runtime."""

    def test_static_binds_the_assigned_port(self, app_launcher, tmp_path, recorded) -> None:
        repo = build(tmp_path, {"index.html": "<h1>hi</h1>"})
        argv, env, _ = command_for(app_launcher, repo)
        assert str(PORT) in argv
        assert "127.0.0.1" in argv
        assert env["PORT"] == str(PORT)

    def test_php_binds_the_assigned_port(self, app_launcher, tmp_path, recorded) -> None:
        repo = build(tmp_path, {"index.php": "<?php echo 1;"})
        argv, _, _ = command_for(app_launcher, repo)
        assert argv[0].endswith("php")
        assert f"127.0.0.1:{PORT}" in argv

    def test_every_runtime_receives_port_in_the_environment(
        self, app_launcher, tmp_path, recorded
    ) -> None:
        for index, files in enumerate(
            [
                {"index.html": "x"},
                {"index.php": "x"},
                {"script.py": "print(1)"},
            ]
        ):
            repo = build(tmp_path, files, name=f"r{index}")
            _, env, _ = command_for(app_launcher, repo)
            assert env["PORT"] == str(PORT)
            assert env["HOST"] == "127.0.0.1"


class TestPython:
    def test_flask_is_served_by_gunicorn_on_the_right_module(
        self, app_launcher, tmp_path, recorded, monkeypatch
    ) -> None:
        """FLASK_RUN_PORT is ignored by any app calling app.run() itself."""
        repo = build(tmp_path, {"app.py": "from flask import Flask\napp = Flask(__name__)\n"})
        monkeypatch.setattr(Path, "exists", lambda self: True)
        argv, _env, _ = command_for(app_launcher, repo)
        assert argv[0].endswith("gunicorn")
        assert "--bind" in argv
        assert argv[argv.index("--bind") + 1] == f"127.0.0.1:{PORT}"
        assert argv[-1] == "app:app"

    def test_fastapi_is_served_by_uvicorn(
        self, app_launcher, tmp_path, recorded, monkeypatch
    ) -> None:
        repo = build(tmp_path, {"main.py": "from fastapi import FastAPI\napp = FastAPI()\n"})
        monkeypatch.setattr(Path, "exists", lambda self: True)
        argv, _, _ = command_for(app_launcher, repo)
        assert argv[0].endswith("uvicorn")
        assert "--port" in argv
        assert argv[argv.index("--port") + 1] == str(PORT)

    def test_django_uses_manage_py_with_the_bind_address(
        self, app_launcher, tmp_path, recorded, monkeypatch
    ) -> None:
        repo = build(tmp_path, {"manage.py": "import django\n", "requirements.txt": "django\n"})
        monkeypatch.setattr(Path, "exists", lambda self: True)
        argv, _, _ = command_for(app_launcher, repo)
        assert "runserver" in argv
        assert f"127.0.0.1:{PORT}" in argv
        assert "--noreload" in argv

    def test_requirements_are_installed_into_the_isolated_venv(
        self, app_launcher, tmp_path, recorded, monkeypatch
    ) -> None:
        """Never into the platform's own interpreter."""
        repo = build(
            tmp_path,
            {
                "app.py": "from flask import Flask\napp = Flask(__name__)\n",
                "requirements.txt": "flask==3.0.0\n",
            },
        )
        monkeypatch.setattr(Path, "exists", lambda self: True)
        command_for(app_launcher, repo)

        installs = [c for c in recorded if "install" in c]
        assert installs, "requirements were never installed"
        for call in installs:
            assert str(tmp_path / "instances") in call[0], (
                f"pip ran outside the isolated venv: {call[0]}"
            )
            assert call[0] != sys.executable


class TestDocker:
    def test_builds_then_runs_with_the_port_mapped(
        self, app_launcher, tmp_path, recorded, monkeypatch
    ) -> None:
        monkeypatch.setattr(launcher.shutil, "which", lambda name: f"/usr/bin/{name}")
        repo = build(tmp_path, {"Dockerfile": "FROM python:3.12\nEXPOSE 9999\n"})

        argv, _, cleanup = command_for(app_launcher, repo)

        build_calls = [c for c in recorded if len(c) > 1 and c[1] == "build"]
        assert build_calls, "the image was never built"

        assert argv[1] == "run"
        assert "--rm" in argv
        assert f"127.0.0.1:{PORT}:9999" in argv, "EXPOSE port was not mapped to the host port"
        assert cleanup is not None and cleanup[1:3] == ["rm", "-f"], "no container cleanup recorded"

    def test_falls_back_to_the_host_port_without_an_expose(
        self, app_launcher, tmp_path, recorded, monkeypatch
    ) -> None:
        monkeypatch.setattr(launcher.shutil, "which", lambda name: f"/usr/bin/{name}")
        repo = build(tmp_path, {"Dockerfile": "FROM alpine\n"})
        argv, _, _ = command_for(app_launcher, repo)
        assert f"127.0.0.1:{PORT}:{PORT}" in argv

    def test_a_stale_container_is_removed_before_reuse(
        self, app_launcher, tmp_path, recorded, monkeypatch
    ) -> None:
        """Otherwise `docker run --name` fails with a conflict."""
        monkeypatch.setattr(launcher.shutil, "which", lambda name: f"/usr/bin/{name}")
        repo = build(tmp_path, {"Dockerfile": "FROM alpine\n"})
        command_for(app_launcher, repo)
        assert any(c[1:3] == ["rm", "-f"] for c in recorded if len(c) > 2)


class TestCompiled:
    def test_go_builds_a_binary_and_runs_it(self, app_launcher, tmp_path, recorded) -> None:
        repo = build(tmp_path, {"go.mod": "module x\n", "main.go": "package main"})
        argv, _, _ = command_for(app_launcher, repo)
        assert any(c[1] == "build" for c in recorded if len(c) > 1)
        assert argv == [str(tmp_path / "instances" / "canary" / "app")]

    def test_maven_failing_to_produce_a_jar_is_a_clear_error(
        self, app_launcher, tmp_path, recorded, monkeypatch
    ) -> None:
        monkeypatch.setattr(launcher.shutil, "which", lambda name: f"/usr/bin/{name}")
        repo = build(tmp_path, {"pom.xml": "<project/>"})
        with pytest.raises(launcher.LaunchError, match="no runnable jar"):
            command_for(app_launcher, repo)

    def test_maven_runs_the_built_jar_with_the_port(
        self, app_launcher, tmp_path, recorded, monkeypatch
    ) -> None:
        monkeypatch.setattr(launcher.shutil, "which", lambda name: f"/usr/bin/{name}")
        repo = build(tmp_path, {"pom.xml": "<project/>"})
        (repo / "target").mkdir()
        (repo / "target" / "app.jar").write_text("")
        argv, _, _ = command_for(app_launcher, repo)
        assert "-jar" in argv
        assert f"--server.port={PORT}" in argv

    def test_source_and_javadoc_jars_are_not_mistaken_for_the_artefact(
        self, app_launcher, tmp_path, recorded, monkeypatch
    ) -> None:
        monkeypatch.setattr(launcher.shutil, "which", lambda name: f"/usr/bin/{name}")
        repo = build(tmp_path, {"pom.xml": "<project/>"})
        (repo / "target").mkdir()
        for name in ("app-sources.jar", "app-javadoc.jar", "app.jar"):
            (repo / "target" / name).write_text("")
        argv, _, _ = command_for(app_launcher, repo)
        assert argv[argv.index("-jar") + 1].endswith("app.jar")


class TestManifestDriven:
    def test_an_explicit_start_command_is_used_verbatim(
        self, app_launcher, tmp_path, recorded
    ) -> None:
        repo = build(tmp_path, {"index.html": "x", ".bellwether.yml": "start: ./run.sh --serve\n"})
        argv, _, _ = command_for(app_launcher, repo)
        assert argv == ["./run.sh", "--serve"]

    def test_port_is_substituted_into_the_start_command(
        self, app_launcher, tmp_path, recorded
    ) -> None:
        repo = build(
            tmp_path, {"index.html": "x", ".bellwether.yml": "start: ./run.sh --port ${PORT}\n"}
        )
        argv, _, _ = command_for(app_launcher, repo)
        assert argv == ["./run.sh", "--port", str(PORT)]

    def test_declared_build_steps_run_in_order(self, app_launcher, tmp_path, recorded) -> None:
        repo = build(
            tmp_path,
            {
                "index.html": "x",
                ".bellwether.yml": "build:\n  - make deps\n  - make build\nstart: ./s\n",
            },
        )
        command_for(app_launcher, repo)
        assert recorded[:2] == [["make", "deps"], ["make", "build"]]

    def test_procfile_port_placeholder_is_substituted(
        self, app_launcher, tmp_path, recorded, monkeypatch
    ) -> None:
        monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
        repo = build(tmp_path, {"Procfile": "web: ./server -p $PORT\n"})
        argv, _, _ = command_for(app_launcher, repo)
        assert argv == ["./server", "-p", str(PORT)]

    def test_declared_env_reaches_the_process(self, app_launcher, tmp_path, recorded) -> None:
        repo = build(
            tmp_path,
            {"index.html": "x", ".bellwether.yml": "start: ./s\nenv:\n  APP_ENV: canary\n"},
        )
        _, env, _ = command_for(app_launcher, repo)
        assert env["APP_ENV"] == "canary"


class TestFallbackApp:
    def test_unknown_runtimes_get_the_bundled_stub(self, app_launcher, tmp_path, recorded) -> None:
        repo = build(tmp_path, {"README.md": "# nothing"})
        argv, _, _ = command_for(app_launcher, repo)
        assert argv[0] == sys.executable
        assert argv[1].endswith("fallback_app.py")
        assert Path(argv[1]).is_file(), "the stub is not shipped with the package"

    def test_the_stub_is_never_written_into_the_repository(
        self, app_launcher, tmp_path, recorded
    ) -> None:
        """v2 wrote a Flask app into the checkout, mutating the artefact."""
        repo = build(tmp_path, {"README.md": "# nothing"})
        command_for(app_launcher, repo)
        assert sorted(p.name for p in repo.iterdir()) == ["README.md"]

    @pytest.mark.integration
    def test_the_stub_actually_serves(self) -> None:
        import subprocess

        from conftest import free_port

        port = free_port()
        process = subprocess.Popen(
            [sys.executable, str(launcher._fallback_app_path()), str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            import time

            deadline = time.monotonic() + 10
            ok = False
            while time.monotonic() < deadline:
                ok, _ = launcher.probe(f"http://127.0.0.1:{port}/", timeout=1.0)
                if ok:
                    break
                time.sleep(0.2)
            assert ok, "the fallback stub never answered"
        finally:
            process.terminate()
            process.wait(timeout=5)
