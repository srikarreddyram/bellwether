"""Runtime detection.

The shell version only looked at repository-root ``*.py`` files, so a project
laid out as ``src/app.py`` silently fell through to the health stub -- and a
health stub passes every check and every risk threshold, so the pipeline went
green having deployed nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bellwether.errors import LaunchError
from bellwether.launcher import Runtime, detect, probe, tail


def build(tmp_path: Path, files: dict) -> Path:
    repo = tmp_path / "repo"
    for name, content in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    repo.mkdir(parents=True, exist_ok=True)
    return repo


class TestDetection:
    def test_flask_by_source(self, tmp_path: Path) -> None:
        repo = build(tmp_path, {"app.py": "from flask import Flask\napp = Flask(__name__)\n"})
        found = detect(repo)
        assert found.runtime is Runtime.FLASK
        assert found.entrypoint == "app:app"

    def test_flask_names_the_real_variable(self, tmp_path: Path) -> None:
        """``gunicorn module:app`` fails if the variable is not called 'app'."""
        repo = build(tmp_path, {"main.py": "from flask import Flask\nserver = Flask(__name__)\n"})
        assert detect(repo).entrypoint == "main:server"

    def test_flask_in_a_source_subdirectory(self, tmp_path: Path) -> None:
        repo = build(tmp_path, {"src/app.py": "from flask import Flask\napp = Flask(__name__)\n"})
        found = detect(repo)
        assert found.runtime is Runtime.FLASK
        assert found.entrypoint == "src.app:app"

    def test_flask_by_requirements_only(self, tmp_path: Path) -> None:
        repo = build(tmp_path, {"requirements.txt": "Flask==3.0.0\n"})
        assert detect(repo).runtime is Runtime.FLASK

    def test_fastapi_wins_over_flask_when_both_present(self, tmp_path: Path) -> None:
        repo = build(
            tmp_path,
            {
                "requirements.txt": "fastapi\nflask\n",
                "main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
            },
        )
        found = detect(repo)
        assert found.runtime is Runtime.FASTAPI
        assert found.entrypoint == "main:app"

    def test_django(self, tmp_path: Path) -> None:
        repo = build(tmp_path, {"manage.py": "import django\n", "requirements.txt": "django\n"})
        assert detect(repo).runtime is Runtime.DJANGO

    def test_node(self, tmp_path: Path) -> None:
        repo = build(tmp_path, {"package.json": '{"scripts":{"start":"node index.js"}}'})
        assert detect(repo).runtime is Runtime.NODE

    def test_static_html(self, tmp_path: Path) -> None:
        repo = build(tmp_path, {"index.html": "<h1>hi</h1>"})
        assert detect(repo).runtime is Runtime.STATIC

    def test_plain_python(self, tmp_path: Path) -> None:
        repo = build(tmp_path, {"script.py": "print('hi')\n"})
        assert detect(repo).runtime is Runtime.PYTHON

    def test_unknown_falls_back(self, tmp_path: Path) -> None:
        repo = build(tmp_path, {"README.md": "# nothing here"})
        assert detect(repo).runtime is Runtime.FALLBACK

    def test_entrypoint_preference_order(self, tmp_path: Path) -> None:
        """app.py should win over an alphabetically earlier helper module."""
        repo = build(
            tmp_path,
            {
                "aaa_helper.py": "from flask import Flask\napp = Flask(__name__)\n",
                "app.py": "from flask import Flask\napp = Flask(__name__)\n",
            },
        )
        assert detect(repo).entrypoint == "app:app"

    def test_missing_repo_raises(self, tmp_path: Path) -> None:
        with pytest.raises(LaunchError, match="does not exist"):
            detect(tmp_path / "nope")

    def test_detection_reason_is_populated(self, tmp_path: Path) -> None:
        repo = build(tmp_path, {"index.html": "<h1>hi</h1>"})
        assert detect(repo).reason


class TestUniversalRuntimes:
    """Detection must reach beyond Python and Node to be worth the name."""

    def test_dockerfile_wins_when_docker_is_available(self, tmp_path: Path, monkeypatch) -> None:
        """A Dockerfile already states exactly how to build and run, in the one
        format that works for every language. No heuristic beats that."""
        monkeypatch.setattr("bellwether.launcher.shutil.which", lambda name: "/usr/bin/docker")
        repo = build(
            tmp_path,
            {
                "Dockerfile": 'FROM python:3.12\nEXPOSE 8080\nCMD ["python", "app.py"]\n',
                "requirements.txt": "flask\n",
            },
        )
        found = detect(repo)
        assert found.runtime is Runtime.DOCKER
        assert "8080" in (found.entrypoint or "")

    def test_dockerfile_is_skipped_when_docker_is_absent(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("bellwether.launcher.shutil.which", lambda name: None)
        repo = build(tmp_path, {"Dockerfile": "FROM x\n", "requirements.txt": "flask\n"})
        assert detect(repo).runtime is Runtime.FLASK

    def test_procfile_web_process(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("bellwether.launcher.shutil.which", lambda name: None)
        repo = build(tmp_path, {"Procfile": "web: bundle exec puma -p $PORT\nworker: rake jobs\n"})
        found = detect(repo)
        assert found.runtime is Runtime.PROCFILE
        assert found.entrypoint == "bundle exec puma -p $PORT"

    def test_procfile_without_a_web_process_is_ignored(self, tmp_path: Path) -> None:
        repo = build(tmp_path, {"Procfile": "worker: rake jobs\n", "index.html": "<h1>x</h1>"})
        assert detect(repo).runtime is Runtime.STATIC

    def test_go(self, tmp_path: Path) -> None:
        repo = build(tmp_path, {"go.mod": "module example.com/app\n", "main.go": "package main"})
        assert detect(repo).runtime is Runtime.GO

    def test_java_maven(self, tmp_path: Path) -> None:
        repo = build(tmp_path, {"pom.xml": "<project/>"})
        assert detect(repo).runtime is Runtime.JAVA_MAVEN

    def test_java_gradle(self, tmp_path: Path) -> None:
        repo = build(tmp_path, {"build.gradle.kts": "plugins {}"})
        assert detect(repo).runtime is Runtime.JAVA_GRADLE

    def test_ruby(self, tmp_path: Path) -> None:
        repo = build(tmp_path, {"Gemfile": "source 'https://rubygems.org'", "config.ru": "run App"})
        assert detect(repo).runtime is Runtime.RUBY

    def test_rust(self, tmp_path: Path) -> None:
        repo = build(tmp_path, {"Cargo.toml": "[package]\nname = 'app'"})
        assert detect(repo).runtime is Runtime.RUST

    def test_php(self, tmp_path: Path) -> None:
        repo = build(tmp_path, {"composer.json": "{}"})
        assert detect(repo).runtime is Runtime.PHP

    def test_no_repository_is_left_undeployable(self, tmp_path: Path) -> None:
        """Every input resolves to some runtime; detection never raises on content."""
        for index, files in enumerate(
            [
                {"README.md": "# hi"},
                {"Makefile": "all:\n\techo hi"},
                {"data.csv": "a,b"},
            ]
        ):
            repo = build(tmp_path / str(index), files)
            assert detect(repo).runtime is Runtime.FALLBACK


class TestProbe:
    def test_unreachable_port_is_unhealthy(self) -> None:
        from conftest import free_port

        ok, reason = probe(f"http://127.0.0.1:{free_port()}/", timeout=1.0)
        assert ok is False
        assert reason

    def test_404_counts_as_healthy(self) -> None:
        """An app need not define a root route to be listening."""
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        from conftest import free_port

        class NotFound(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args: object) -> None:
                pass

        port = free_port()
        server = ThreadingHTTPServer(("127.0.0.1", port), NotFound)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            ok, reason = probe(f"http://127.0.0.1:{port}/", timeout=2.0)
            assert ok is True
            assert "404" in reason
        finally:
            server.shutdown()
            server.server_close()


class TestTail:
    def test_reads_the_last_lines(self, tmp_path: Path) -> None:
        target = tmp_path / "log"
        target.write_text("\n".join(str(i) for i in range(100)))
        assert tail(target, 3) == "97\n98\n99"

    def test_missing_file_is_reported(self, tmp_path: Path) -> None:
        assert "unavailable" in tail(tmp_path / "nope")
