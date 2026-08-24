"""Traffic proxy: routing decisions and real end-to-end forwarding."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from bellwether.config import ProxySettings
from bellwether.proxy import ChaosInjector, ProxyServer, Router
from bellwether.weights import TrafficWeightStore
from conftest import free_port

# ── Routing (pure) ────────────────────────────────────────────────────────────


@pytest.fixture()
def router(tmp_path: Path) -> Router:
    weights = TrafficWeightStore(tmp_path / "w")
    weights.set(0)
    return Router(ProxySettings(sticky_sessions=True), weights)


class TestRouting:
    def test_zero_weight_sends_everything_to_stable(self, router: Router) -> None:
        assert all(router.choose(None)[0] == "stable" for _ in range(200))

    def test_full_weight_sends_everything_to_canary(self, router: Router) -> None:
        router._weights.set(100)
        assert all(router.choose(None)[0] == "canary" for _ in range(200))

    def test_split_is_approximately_the_weight(self, router: Router) -> None:
        router._weights.set(50)
        canary = sum(router.choose(None)[0] == "canary" for _ in range(4000))
        assert 1700 < canary < 2300, f"50% split produced {canary}/4000 canary"

    def test_ten_percent_split(self, router: Router) -> None:
        router._weights.set(10)
        canary = sum(router.choose(None)[0] == "canary" for _ in range(4000))
        assert 250 < canary < 550, f"10% split produced {canary}/4000 canary"


class TestStickiness:
    def test_a_pinned_client_keeps_its_cohort(self, router: Router) -> None:
        router._weights.set(50)
        assert all(router.choose("canary") == ("canary", False) for _ in range(50))

    def test_withdrawn_canary_repins_the_client(self, router: Router) -> None:
        """After rollback the canary is gone.

        The old logic routed the client to stable but left its cookie claiming
        'canary', so it carried a stale pin for the cookie's whole lifetime.
        """
        router._weights.set(0)
        cohort, set_cookie = router.choose("canary")
        assert cohort == "stable"
        assert set_cookie is True

    def test_full_promotion_repins_stable_clients(self, router: Router) -> None:
        router._weights.set(100)
        assert router.choose("stable") == ("canary", True)

    def test_unknown_cookie_value_is_ignored(self, router: Router) -> None:
        router._weights.set(0)
        cohort, _ = router.choose("nonsense")
        assert cohort == "stable"

    def test_stickiness_can_be_disabled(self, tmp_path: Path) -> None:
        weights = TrafficWeightStore(tmp_path / "w")
        weights.set(100)
        router = Router(ProxySettings(sticky_sessions=False), weights)
        cohort, set_cookie = router.choose("stable")
        assert cohort == "canary"
        assert set_cookie is False


class TestChaos:
    def test_disabled_by_default(self, tmp_path: Path) -> None:
        flag = tmp_path / "chaos"
        flag.write_text("1")
        injector = ChaosInjector(ProxySettings(chaos_enabled=False), flag)
        assert injector.active is False
        assert injector.apply("canary") is None

    def test_never_touches_stable(self, tmp_path: Path) -> None:
        """Perturbing the baseline would make the comparison meaningless."""
        flag = tmp_path / "chaos"
        flag.write_text("1")
        injector = ChaosInjector(
            ProxySettings(
                chaos_enabled=True,
                chaos_error_rate=1.0,
                chaos_latency_min_s=0.0,
                chaos_latency_max_s=0.0,
            ),
            flag,
        )
        assert injector.apply("stable") is None
        assert injector.apply("canary") == 500


# ── End to end ────────────────────────────────────────────────────────────────


class _Upstream(BaseHTTPRequestHandler):
    identity = "unset"
    status = 200

    def _send(self, body: bytes) -> None:
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Upstream", self.identity)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._send(json.dumps({"who": self.identity, "path": self.path}).encode())

    def do_HEAD(self) -> None:
        body = json.dumps({"who": self.identity}).encode()
        self.send_response(self.status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        payload = self.rfile.read(length) if length else b""
        self._send(json.dumps({"who": self.identity, "echo": payload.decode()}).encode())

    def log_message(self, *args: object) -> None:
        pass


def _start_upstream(port: int, identity: str, status: int = 200) -> ThreadingHTTPServer:
    handler = type(f"H{identity}", (_Upstream,), {"identity": identity, "status": status})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    ).start()
    return server


@pytest.fixture()
def live(tmp_path: Path) -> Iterator[tuple]:
    settings = ProxySettings(
        host="127.0.0.1",
        port=free_port(),
        stable_port=free_port(),
        canary_port=free_port(),
        telemetry_flush_interval_s=0.0,
        upstream_timeout_s=3.0,
        sticky_sessions=False,
    )
    stable = _start_upstream(settings.stable_port, "stable")
    canary = _start_upstream(settings.canary_port, "canary")
    proxy = ProxyServer(
        settings,
        weight_file=tmp_path / "w",
        telemetry_file=tmp_path / "t.json",
        chaos_file=tmp_path / "c",
    )
    proxy.weights.set(0)
    proxy.start_background()
    base = f"http://127.0.0.1:{settings.port}"
    try:
        yield proxy, base, settings
    finally:
        proxy.shutdown()
        for server in (stable, canary):
            server.shutdown()
            server.server_close()


def fetch(url: str, *, method: str = "GET", data: bytes | None = None):
    request = urllib.request.Request(url, method=method, data=data)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    return urllib.request.urlopen(request, timeout=5)


@pytest.mark.integration
class TestEndToEnd:
    def test_routes_to_stable_at_zero_weight(self, live) -> None:
        _, base, _ = live
        with fetch(f"{base}/hello") as response:
            assert response.headers["X-Bellwether-Target"] == "stable"
            assert json.loads(response.read())["who"] == "stable"

    def test_routes_to_canary_at_full_weight(self, live) -> None:
        proxy, base, _ = live
        proxy.weights.set(100)
        with fetch(base + "/") as response:
            assert json.loads(response.read())["who"] == "canary"

    def test_preserves_the_request_path(self, live) -> None:
        _, base, _ = live
        with fetch(f"{base}/deep/path?q=1") as response:
            assert json.loads(response.read())["path"] == "/deep/path?q=1"

    def test_forwards_a_post_body(self, live) -> None:
        _, base, _ = live
        with fetch(base + "/", method="POST", data=b'{"x":1}') as response:
            assert json.loads(response.read())["echo"] == '{"x":1}'

    def test_head_returns_no_body(self, live) -> None:
        """The old handler wrote a body on HEAD, desynchronising the connection."""
        _, base, _ = live
        with fetch(base + "/", method="HEAD") as response:
            assert response.read() == b""
            assert response.headers["X-Bellwether-Target"] == "stable"

    def test_adds_observability_headers(self, live) -> None:
        _, base, _ = live
        with fetch(base + "/") as response:
            assert response.headers["X-Bellwether-Target"] in ("stable", "canary")
            assert float(response.headers["X-Bellwether-Latency"]) >= 0

    def test_content_length_matches_the_body(self, live) -> None:
        """Relaying the upstream's Content-Length beside a different body hangs clients."""
        _, base, _ = live
        with fetch(base + "/") as response:
            body = response.read()
            assert int(response.headers["Content-Length"]) == len(body)

    def test_health_endpoint_is_not_proxied(self, live) -> None:
        proxy, base, _ = live
        proxy.weights.set(25)
        with fetch(f"{base}/__bellwether/health") as response:
            payload = json.loads(response.read())
        assert payload["status"] == "ok"
        assert payload["canaryWeight"] == 25

    def test_internal_traffic_is_excluded_from_telemetry(self, live) -> None:
        """Health polls must not dilute the sample the risk gate scores."""
        proxy, base, _ = live
        for _ in range(5):
            fetch(f"{base}/__bellwether/health").close()
        assert proxy.telemetry.samples() == []

    def test_dead_upstream_yields_502_attributed_to_its_cohort(self, live) -> None:
        proxy, base, settings = live
        proxy.weights.set(100)
        # Nothing is listening on this port, so the canary is effectively down.
        proxy.settings = settings  # type: ignore[misc]
        object.__setattr__(proxy.settings, "canary_port", free_port())

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            fetch(base + "/")
        assert excinfo.value.code == 502

        samples = proxy.telemetry.samples()
        assert samples[-1].cohort == "canary"
        assert samples[-1].is_error, "a down canary must be visible to the risk gate"

    def test_telemetry_records_each_proxied_request(self, live) -> None:
        proxy, base, _ = live
        proxy.weights.set(0)
        for _ in range(12):
            fetch(base + "/").close()
        samples = proxy.telemetry.samples()
        assert len(samples) == 12
        assert all(s.cohort == "stable" for s in samples)
        assert all(s.latency_ms > 0 for s in samples)

    def test_sticky_cookie_is_set_when_enabled(self, tmp_path: Path) -> None:
        settings = ProxySettings(
            host="127.0.0.1",
            port=free_port(),
            stable_port=free_port(),
            canary_port=free_port(),
            sticky_sessions=True,
            telemetry_flush_interval_s=0.0,
        )
        stable = _start_upstream(settings.stable_port, "stable")
        proxy = ProxyServer(
            settings,
            weight_file=tmp_path / "w",
            telemetry_file=tmp_path / "t.json",
            chaos_file=tmp_path / "c",
        )
        proxy.weights.set(0)
        proxy.start_background()
        try:
            with fetch(f"http://127.0.0.1:{settings.port}/") as response:
                cookie = response.headers["Set-Cookie"]
            assert cookie.startswith("bellwether_cohort=stable")
            assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
        finally:
            proxy.shutdown()
            stable.shutdown()
            stable.server_close()
