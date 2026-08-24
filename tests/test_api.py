"""HTTP API: contracts, authentication, and concurrency guards."""

from __future__ import annotations

import hmac
import json
from collections.abc import Iterator
from hashlib import sha256

import pytest

from bellwether.api import create_app
from bellwether.config import ApiSettings, Settings
from bellwether.service import PlatformService


@pytest.fixture()
def app_bundle(settings: Settings) -> Iterator[tuple]:
    service = PlatformService(settings)
    app, _socketio, _ = create_app(settings, service=service)
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client, service, settings
    service.shutdown()


@pytest.fixture()
def client(app_bundle):
    return app_bundle[0]


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, sha256).hexdigest()


class TestReadEndpoints:
    def test_health(self, client) -> None:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.get_json()["status"] == "ok"

    def test_config_serves_the_stage_catalogue(self, client) -> None:
        """The dashboard renders backend-declared stages, not a hardcoded copy."""
        payload = client.get("/api/config").get_json()
        keys = [stage["key"] for stage in payload["stages"]]
        assert keys[:3] == ["checkout", "verify", "baseline"]
        assert "risk" in keys

    def test_status_shape(self, client) -> None:
        payload = client.get("/api/status").get_json()
        assert payload["building"] is False
        assert isinstance(payload["stages"], list)
        assert payload["trafficPct"] == 0

    def test_history_is_empty_initially(self, client) -> None:
        payload = client.get("/api/history").get_json()
        assert payload["runs"] == []
        assert payload["stats"]["total"] == 0

    def test_telemetry_shape(self, client) -> None:
        payload = client.get("/api/telemetry").get_json()
        assert set(payload["cohorts"]) == {"canary", "stable"}
        assert payload["thresholds"]["latencyP95Ms"] == 500.0

    def test_console(self, client) -> None:
        assert client.get("/api/console").get_json()["lines"] == []

    def test_unknown_route_returns_structured_json(self, client) -> None:
        response = client.get("/api/nope")
        assert response.status_code == 404
        assert response.get_json()["error"]["code"] == "not_found"


class TestDeployValidation:
    def test_missing_url_is_rejected(self, client) -> None:
        response = client.post("/api/deploy", json={})
        assert response.status_code == 400
        assert response.get_json()["error"]["code"] == "validation_error"

    def test_command_injection_is_rejected(self, client) -> None:
        """This exact payload was remote code execution in the previous version."""
        response = client.post(
            "/api/deploy", json={"repoUrl": "https://github.com/o/r.git; touch /tmp/pwned"}
        )
        assert response.status_code == 400

    def test_unlisted_host_is_rejected(self, client) -> None:
        response = client.post("/api/deploy", json={"repoUrl": "https://evil.com/o/r.git"})
        assert response.status_code == 400
        assert "allowlisted" in response.get_json()["error"]["message"]

    def test_accepts_both_camel_and_snake_case(self, client) -> None:
        for key in ("repoUrl", "repo_url"):
            response = client.post("/api/deploy", json={key: "not a url at all"})
            assert response.status_code == 400, key


class TestConcurrencyGuard:
    def test_a_second_deployment_is_rejected_with_409(self, app_bundle, monkeypatch) -> None:
        """Two pipelines fighting over one weight file is unrecoverable."""
        client, service, _ = app_bundle
        monkeypatch.setattr(service.pipeline, "execute", lambda run: __import__("time").sleep(2))
        first = client.post("/api/deploy", json={"repoUrl": "https://github.com/o/r.git"})
        assert first.status_code == 202

        second = client.post("/api/deploy", json={"repoUrl": "https://github.com/o/r.git"})
        assert second.status_code == 409
        assert second.get_json()["error"]["code"] == "conflict"


class TestAuth:
    def test_mutations_require_a_token_when_configured(self, settings: Settings) -> None:
        secured = Settings(
            paths=settings.paths,
            proxy=settings.proxy,
            risk=settings.risk,
            api=ApiSettings(auth_token="s3cret", webhook_secret="w"),
            pipeline=settings.pipeline,
        )
        service = PlatformService(secured)
        app, _, _ = create_app(secured, service=service)
        app.config["TESTING"] = True
        try:
            with app.test_client() as client:
                assert client.get("/api/health").status_code == 200
                assert client.post("/api/rollback", json={}).status_code == 401
                assert (
                    client.post(
                        "/api/rollback", json={}, headers={"Authorization": "Bearer wrong"}
                    ).status_code
                    == 401
                )
                assert (
                    client.post(
                        "/api/rollback", json={}, headers={"Authorization": "Bearer s3cret"}
                    ).status_code
                    == 200
                )
        finally:
            service.shutdown()


class TestWebhook:
    URL = "/api/webhook/github"

    def push_body(self, branch: str = "main") -> bytes:
        return json.dumps(
            {
                "ref": f"refs/heads/{branch}",
                "repository": {
                    "clone_url": "https://github.com/o/r.git",
                    "default_branch": "main",
                },
            }
        ).encode()

    def test_unsigned_request_is_rejected(self, client) -> None:
        """Previously this endpoint cloned and executed whatever it was sent."""
        response = client.post(self.URL, data=self.push_body(), headers={"X-GitHub-Event": "push"})
        assert response.status_code == 401

    def test_bad_signature_is_rejected(self, client) -> None:
        response = client.post(
            self.URL,
            data=self.push_body(),
            headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": "sha256=deadbeef"},
        )
        assert response.status_code == 401

    def test_tampered_body_is_rejected(self, client) -> None:
        signature = sign("test-secret", self.push_body())
        response = client.post(
            self.URL,
            data=self.push_body(branch="attacker"),
            headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": signature},
        )
        assert response.status_code == 401

    def test_ping_is_acknowledged(self, client) -> None:
        body = b"{}"
        response = client.post(
            self.URL,
            data=body,
            headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": sign("test-secret", body)},
        )
        assert response.status_code == 200
        assert response.get_json()["status"] == "pong"

    def test_non_default_branch_is_ignored(self, client) -> None:
        body = self.push_body(branch="feature/x")
        response = client.post(
            self.URL,
            data=body,
            headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": sign("test-secret", body)},
        )
        assert response.status_code == 200
        assert response.get_json()["status"] == "ignored"

    def test_malformed_body_is_rejected_after_signature_check(self, client) -> None:
        body = b"not json"
        response = client.post(
            self.URL,
            data=body,
            headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": sign("test-secret", body)},
        )
        assert response.status_code == 400

    def test_signed_push_triggers_a_run(self, app_bundle, monkeypatch) -> None:
        client, service, _ = app_bundle
        monkeypatch.setattr(service.pipeline, "execute", lambda run: None)
        body = self.push_body()
        response = client.post(
            self.URL,
            data=body,
            headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": sign("test-secret", body)},
        )
        assert response.status_code == 202
        assert response.get_json()["run"]["trigger"] == "webhook"


class TestChaos:
    def test_disabled_by_default(self, client) -> None:
        payload = client.get("/api/chaos").get_json()
        assert payload["available"] is False

    def test_enabling_is_refused_when_not_permitted(self, client) -> None:
        response = client.post("/api/chaos", json={"enabled": True})
        assert response.status_code == 409

    def test_non_boolean_is_rejected(self, client) -> None:
        assert client.post("/api/chaos", json={"enabled": "yes"}).status_code == 400


class TestRollback:
    def test_rollback_works_with_no_pipeline_running(self, app_bundle) -> None:
        """The old endpoint only wrote a flag the pipeline polled between stages.

        With nothing running it did nothing at all, while the canary kept
        serving its share of traffic.
        """
        client, service, _ = app_bundle
        service.weights.set(50)
        response = client.post("/api/rollback", json={"reason": "test"})
        assert response.status_code == 200
        assert response.get_json()["trafficPct"] == 0
        assert service.weights.get() == 0


class TestCors:
    def test_wildcard_origin_is_refused_at_config_load(self, monkeypatch) -> None:
        from bellwether.config import Settings as S
        from bellwether.errors import ConfigurationError

        monkeypatch.setenv("BELLWETHER_CORS_ORIGINS", "*")
        with pytest.raises(ConfigurationError, match="may not be"):
            S.from_env()

    def test_allowlisted_origin_is_echoed(self, client) -> None:
        response = client.get("/api/health", headers={"Origin": "http://localhost:5173"})
        assert response.headers.get("Access-Control-Allow-Origin") == "http://localhost:5173"

    def test_unlisted_origin_is_not_echoed(self, client) -> None:
        response = client.get("/api/health", headers={"Origin": "https://evil.example"})
        assert response.headers.get("Access-Control-Allow-Origin") != "https://evil.example"
