"""Explicit launch specifications.

Auto-detection is a heuristic. These cover the escape hatch that makes
"runs on any repository" true rather than aspirational.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bellwether.errors import ValidationError
from bellwether.launcher import Runtime, detect
from bellwether.manifest import LaunchSpec, load_manifest


class TestParsing:
    def test_string_commands_are_split_into_argv(self) -> None:
        spec = LaunchSpec.from_mapping({"start": "node server.js --port 3000"})
        assert spec.start == ["node", "server.js", "--port", "3000"]

    def test_quoting_is_respected(self) -> None:
        spec = LaunchSpec.from_mapping({"start": 'python -c "import x; x.run()"'})
        assert spec.start == ["python", "-c", "import x; x.run()"]

    def test_shell_metacharacters_stay_literal(self) -> None:
        """shlex applies quoting rules but never interprets ';' or '|'.

        The argv is executed without a shell, so a semicolon is just a
        character in an argument, not a command separator.
        """
        spec = LaunchSpec.from_mapping({"start": "app.sh; rm -rf /"})
        assert spec.start == ["app.sh;", "rm", "-rf", "/"]

    def test_list_form_is_accepted_verbatim(self) -> None:
        spec = LaunchSpec.from_mapping({"start": ["python", "-m", "app"]})
        assert spec.start == ["python", "-m", "app"]

    def test_build_accepts_a_single_string(self) -> None:
        assert LaunchSpec.from_mapping({"build": "make all"}).build == [["make", "all"]]

    def test_build_accepts_a_list_of_steps(self) -> None:
        spec = LaunchSpec.from_mapping({"build": ["npm ci", "npm run build"]})
        assert spec.build == [["npm", "ci"], ["npm", "run", "build"]]

    def test_unknown_keys_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match="unknown launch spec keys"):
            LaunchSpec.from_mapping({"strat": "node x.js"})

    def test_empty_command_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            LaunchSpec.from_mapping({"start": "   "})

    def test_absolute_workdir_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="relative path"):
            LaunchSpec.from_mapping({"workdir": "/etc"})

    def test_workdir_traversal_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="relative path"):
            LaunchSpec.from_mapping({"workdir": "../../etc"})

    def test_health_path_must_be_absolute(self) -> None:
        with pytest.raises(ValidationError, match="must start with"):
            LaunchSpec.from_mapping({"health_path": "healthz"})

    def test_container_port_is_validated(self) -> None:
        with pytest.raises(ValidationError, match="between 1 and 65535"):
            LaunchSpec.from_mapping({"container_port": 99999})

    def test_camel_and_snake_case_both_work(self) -> None:
        spec = LaunchSpec.from_mapping({"healthPath": "/up", "portEnv": "APP_PORT"})
        assert spec.health_path == "/up"
        assert spec.port_env == "APP_PORT"


class TestMerge:
    def test_override_wins_field_by_field(self) -> None:
        base = LaunchSpec.from_mapping({"start": "a", "health_path": "/base"})
        override = LaunchSpec.from_mapping({"start": "b"})
        merged = base.merge(override)
        assert merged.start == ["b"]
        assert merged.health_path == "/base", "unset override fields must not clobber"

    def test_env_is_merged_not_replaced(self) -> None:
        base = LaunchSpec.from_mapping({"env": {"A": "1", "B": "2"}})
        merged = base.merge(LaunchSpec.from_mapping({"env": {"B": "9"}}))
        assert merged.env == {"A": "1", "B": "9"}

    def test_merging_none_is_a_noop(self) -> None:
        base = LaunchSpec.from_mapping({"start": "a"})
        assert base.merge(None) is base


class TestManifestFile:
    def test_yaml_manifest_is_read(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".bellwether.yml").write_text(
            "runtime: node\nbuild:\n  - npm ci\nstart: node server.js\nhealth_path: /healthz\n"
        )
        spec = load_manifest(repo)
        assert spec is not None
        assert spec.start == ["node", "server.js"]
        assert spec.health_path == "/healthz"

    def test_json_manifest_is_read(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".bellwether.json").write_text('{"start": "./run.sh"}')
        spec = load_manifest(repo)
        assert spec is not None
        assert spec.start == ["./run.sh"]

    def test_absent_manifest_returns_none(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        assert load_manifest(repo) is None

    def test_malformed_manifest_raises_clearly(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".bellwether.yml").write_text("start: [unclosed\n")
        with pytest.raises(ValidationError, match="could not be parsed"):
            load_manifest(repo)


class TestDetectionPrecedence:
    def test_manifest_beats_heuristics(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "index.html").write_text("<h1>static</h1>")
        (repo / ".bellwether.yml").write_text("start: ./custom-server\n")
        assert detect(repo).runtime is Runtime.MANIFEST

    def test_operator_override_beats_the_repository_manifest(self, tmp_path: Path) -> None:
        """A repo cannot dictate how the platform runs it."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".bellwether.yml").write_text("start: ./repo-choice\n")
        override = LaunchSpec.from_mapping({"start": "./operator-choice"})
        found = detect(repo, override=override)
        assert found.spec is not None
        assert found.spec.start == ["./operator-choice"]

    def test_runtime_can_be_forced_without_a_start_command(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "index.html").write_text("<h1>hi</h1>")
        found = detect(repo, override=LaunchSpec.from_mapping({"runtime": "static"}))
        assert found.runtime is Runtime.STATIC
        assert "forced" in found.reason

    def test_unknown_forced_runtime_is_rejected(self, tmp_path: Path) -> None:
        from bellwether.errors import LaunchError

        repo = tmp_path / "repo"
        repo.mkdir()
        with pytest.raises(LaunchError, match="unknown runtime"):
            detect(repo, override=LaunchSpec.from_mapping({"runtime": "cobol"}))
