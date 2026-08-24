"""The command line interface.

Each component is runnable on its own -- that is what makes the platform
debuggable when the dashboard is not the fastest way to an answer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bellwether.cli import build_parser, describe_settings, main
from bellwether.config import ApiSettings, PipelineSettings, Settings


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path: Path) -> None:
    """Point every CLI invocation at a throwaway state directory."""
    for name in list(__import__("os").environ):
        if name.startswith("BELLWETHER_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BELLWETHER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("BELLWETHER_TRACKING_ENABLED", "0")
    monkeypatch.setenv("BELLWETHER_LOG_LEVEL", "CRITICAL")


class TestParser:
    def test_requires_a_subcommand(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_rejects_an_unknown_subcommand(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(["teleport"])

    @pytest.mark.parametrize(
        "argv",
        [
            ["api"],
            ["proxy"],
            ["status"],
            ["config"],
            ["rollback"],
            ["risk"],
            ["weight"],
            ["weight", "50"],
            ["deploy", "url"],
            ["load"],
        ],
    )
    def test_every_documented_command_parses(self, argv: list[str]) -> None:
        assert build_parser().parse_args(argv).command == argv[0]

    def test_version_exits_zero(self, capsys) -> None:
        with pytest.raises(SystemExit) as excinfo:
            build_parser().parse_args(["--version"])
        assert excinfo.value.code == 0
        assert "bellwether" in capsys.readouterr().out


class TestWeight:
    def test_reads_zero_before_anything_is_set(self, capsys) -> None:
        assert main(["weight"]) == 0
        assert capsys.readouterr().out.strip() == "0"

    def test_sets_and_reads_back(self, capsys) -> None:
        assert main(["weight", "50"]) == 0
        capsys.readouterr()
        assert main(["weight"]) == 0
        assert capsys.readouterr().out.strip() == "50"

    def test_rejects_an_out_of_range_value(self, capsys) -> None:
        assert main(["weight", "150"]) == 1
        assert "between" in capsys.readouterr().err


class TestRisk:
    def test_exits_one_when_there_is_no_telemetry(self, capsys) -> None:
        """No evidence must not be reported as a pass."""
        assert main(["risk"]) == 1
        assert "ABORT" in capsys.readouterr().out

    def test_json_output_is_machine_readable(self, capsys) -> None:
        assert main(["risk", "--json"]) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["decision"] == "ABORT"
        assert payload["dataSource"] == "insufficient"

    def test_promotes_on_healthy_telemetry(self, capsys, monkeypatch, tmp_path: Path) -> None:
        import time

        from bellwether.atomicio import write_json_atomic

        monkeypatch.setenv("BELLWETHER_MIN_CANARY_SAMPLES", "3")
        telemetry = tmp_path / "state" / "proxy_telemetry.json"
        write_json_atomic(
            telemetry,
            [
                {"cohort": "canary", "latencyMs": 12.0, "statusCode": 200, "timestamp": time.time()}
                for _ in range(10)
            ],
        )
        assert main(["risk"]) == 0
        assert "PROMOTE" in capsys.readouterr().out


class TestRollbackAndStatus:
    def test_rollback_forces_the_weight_to_zero(self, capsys) -> None:
        main(["weight", "100"])
        capsys.readouterr()
        assert main(["rollback"]) == 0
        assert json.loads(capsys.readouterr().out)["trafficPct"] == 0

    def test_status_is_valid_json(self, capsys) -> None:
        assert main(["status"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert "stages" in payload
        assert payload["building"] is False


class TestConfigRedaction:
    def test_prints_valid_json(self, capsys) -> None:
        assert main(["config"]) == 0
        assert "proxy" in json.loads(capsys.readouterr().out)

    def test_secrets_never_appear_in_output(self, capsys, monkeypatch) -> None:
        """`bellwether config` gets pasted into issues and shown on screen shares."""
        monkeypatch.setenv("BELLWETHER_API_TOKEN", "tok-SHOULD-NOT-APPEAR")
        monkeypatch.setenv("BELLWETHER_WEBHOOK_SECRET", "hook-SHOULD-NOT-APPEAR")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp-SHOULD-NOT-APPEAR")

        assert main(["config"]) == 0
        out = capsys.readouterr().out
        assert "SHOULD-NOT-APPEAR" not in out
        assert out.count("***set***") == 3

    def test_unset_secrets_render_as_null_not_redacted(self) -> None:
        settings = Settings(
            paths=Settings.from_env().paths,
            proxy=Settings.from_env().proxy,
            risk=Settings.from_env().risk,
            api=ApiSettings(auth_token=None, webhook_secret=None),
            pipeline=PipelineSettings(github_token=None),
        )
        described = describe_settings(settings)
        assert described["api"]["auth_token"] is None
        assert described["pipeline"]["github_token"] is None


class TestErrorHandling:
    def test_an_invalid_repository_url_exits_one(self, capsys) -> None:
        assert main(["deploy", "https://evil.example.com/o/r.git"]) == 1
        assert "not allowlisted" in capsys.readouterr().err

    def test_a_configuration_error_exits_one(self, capsys, monkeypatch) -> None:
        monkeypatch.setenv("BELLWETHER_PROXY_PORT", "not-a-number")
        with pytest.raises(Exception) as excinfo:
            main(["status"])
        assert "must be an integer" in str(excinfo.value)
