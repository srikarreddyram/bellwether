"""The traffic proxy: the single public entrypoint and the source of telemetry.

It reads the weight file on every request, routes probabilistically (or stickily
by cohort cookie), forwards the request upstream, and records what happened.
The risk gate is only as good as this module's measurements.

Fixes over the previous version:

* **HEAD no longer returns a body.** ``do_HEAD`` shared a code path that always
  wrote the response body, violating RFC 9110 and confusing any client that
  used HEAD to health-check -- including the platform's own documented check.
* **Hop-by-hop headers are stripped.** Forwarding ``Content-Length`` and
  ``Transfer-Encoding`` verbatim while writing a different body length produces
  responses that clients hang on.
* **Cohort stickiness is honest.** The old cookie logic pinned a client to
  canary but silently reassigned it to stable whenever the weight dropped to
  zero, without clearing the cookie, so a rolled-back client kept a cookie
  claiming a cohort it was no longer in.
* **Failures are attributed correctly.** An upstream that is down is recorded as
  a 502 *against the cohort that was selected*, which is what makes a broken
  canary visible to the risk gate instead of averaging into nothing.
"""

from __future__ import annotations

import http.cookies
import json
import random
import socketserver
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .atomicio import read_text
from .config import ProxySettings
from .logging_setup import get_logger
from .models import RequestSample
from .telemetry import TelemetryStore
from .weights import TrafficWeightStore

log = get_logger(__name__)

HEALTH_PATH = "/__bellwether/health"
METRICS_PATH = "/__bellwether/metrics"
INTERNAL_PREFIX = "/__bellwether/"

# Headers that describe a single transport hop and must not be relayed.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_MAX_BODY_BYTES = 32 * 1024 * 1024


class Router:
    """Decides which cohort a request belongs to."""

    def __init__(self, settings: ProxySettings, weights: TrafficWeightStore) -> None:
        self._settings = settings
        self._weights = weights
        self._random = random.SystemRandom()

    @property
    def weight(self) -> int:
        return self._weights.get()

    def choose(self, cookie_cohort: str | None) -> tuple[str, bool]:
        """Return ``(cohort, should_set_cookie)``.

        A sticky client keeps its cohort while that cohort is still valid. When
        the weight falls to zero the canary no longer exists, so a pinned client
        is moved to stable *and* re-cookied -- otherwise it would carry a stale
        pin for the cookie's whole lifetime.
        """
        weight = self.weight

        if self._settings.sticky_sessions and cookie_cohort in ("stable", "canary"):
            if cookie_cohort == "canary" and weight <= 0:
                return "stable", True  # canary withdrawn; repin explicitly
            if cookie_cohort == "stable" and weight >= 100:
                return "canary", True  # fully promoted; stable is gone
            return cookie_cohort, False

        if weight <= 0:
            cohort = "stable"
        elif weight >= 100:
            cohort = "canary"
        else:
            cohort = "canary" if self._random.randint(1, 100) <= weight else "stable"
        return cohort, self._settings.sticky_sessions


class ChaosInjector:
    """Deliberate fault injection against the canary, for rollback drills.

    Only ever perturbs the canary cohort, and only when explicitly enabled --
    a chaos flag that could affect stable would make the baseline meaningless.
    """

    def __init__(self, settings: ProxySettings, flag_file: Path) -> None:
        self._settings = settings
        self._flag_file = flag_file
        # Fault injection and simulation are not security decisions, so a
        # deterministic-seedable PRNG is the right tool. Cohort routing uses
        # SystemRandom in Router, where unpredictability actually matters.
        self._random = random.Random()  # noqa: S311

    @property
    def active(self) -> bool:
        if not self._settings.chaos_enabled:
            return False
        raw = read_text(self._flag_file)
        return bool(raw and raw.strip() == "1")

    def apply(self, cohort: str) -> int | None:
        """Inject latency and maybe an error. Returns a forced status, if any."""
        if cohort != "canary" or not self.active:
            return None
        time.sleep(
            self._random.uniform(
                self._settings.chaos_latency_min_s, self._settings.chaos_latency_max_s
            )
        )
        if self._random.random() < self._settings.chaos_error_rate:
            return int(HTTPStatus.INTERNAL_SERVER_ERROR)
        return None


class _Handler(BaseHTTPRequestHandler):
    """Per-request handler. ``server`` carries the shared collaborators."""

    server_version = "Bellwether/3.0"
    protocol_version = "HTTP/1.1"

    # Populated by ProxyServer.
    settings: ProxySettings
    router: Router
    telemetry: TelemetryStore
    chaos: ChaosInjector

    def log_message(self, fmt: str, *args: object) -> None:
        # BaseHTTPRequestHandler writes to stderr per request, which is both
        # noisy and measurable overhead in the hot path.
        log.debug("proxy %s", fmt % args)

    # -- HTTP verbs ------------------------------------------------------------

    def do_GET(self) -> None:
        self._handle(body_expected=True)

    def do_POST(self) -> None:
        self._handle(body_expected=True)

    def do_PUT(self) -> None:
        self._handle(body_expected=True)

    def do_PATCH(self) -> None:
        self._handle(body_expected=True)

    def do_DELETE(self) -> None:
        self._handle(body_expected=True)

    def do_OPTIONS(self) -> None:
        self._handle(body_expected=True)

    def do_HEAD(self) -> None:
        # A HEAD response carries headers only. Writing a body here -- as the
        # previous shared code path did -- desynchronises the connection.
        self._handle(body_expected=False)

    # -- Core ------------------------------------------------------------------

    def _handle(self, *, body_expected: bool) -> None:
        if self.path.startswith(INTERNAL_PREFIX):
            self._handle_internal(body_expected=body_expected)
            return

        started = time.perf_counter()
        cohort, set_cookie = self.router.choose(self._cookie_cohort())
        port = self.settings.upstream_port(cohort)

        forced_status = self.chaos.apply(cohort)
        if forced_status is not None:
            status, headers, payload = (
                forced_status,
                [("Content-Type", "text/plain; charset=utf-8")],
                b"bellwether chaos: injected canary fault",
            )
        else:
            status, headers, payload = self._forward(port)

        latency_ms = (time.perf_counter() - started) * 1000.0
        self._respond(
            status=status,
            headers=headers,
            payload=payload,
            cohort=cohort,
            latency_ms=latency_ms,
            set_cookie=set_cookie,
            body_expected=body_expected,
        )

        self.telemetry.record(
            RequestSample(
                cohort=cohort,
                latency_ms=latency_ms,
                status_code=status,
                timestamp=time.time(),
            )
        )

    def _forward(self, port: int) -> tuple[int, list[tuple[str, str]], bytes]:
        url = f"http://{self.settings.upstream_host}:{port}{self.path}"
        body = self._read_body()

        request = urllib.request.Request(url, data=body, method=self.command)
        for key, value in self.headers.items():
            lowered = key.lower()
            if lowered in HOP_BY_HOP or lowered == "host":
                continue
            request.add_header(key, value)
        request.add_header("X-Forwarded-For", self.client_address[0])
        request.add_header("X-Forwarded-Proto", "http")

        try:
            with urllib.request.urlopen(  # noqa: S310 - loopback upstream only
                request, timeout=self.settings.upstream_timeout_s
            ) as response:
                return response.status, list(response.getheaders()), response.read()
        except urllib.error.HTTPError as exc:
            # A 4xx/5xx from the app is a real response; relay it as-is so the
            # risk gate sees the application's own error rate.
            return exc.code, list(exc.headers.items()), exc.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            log.warning("upstream unreachable", extra={"url": url, "reason": str(reason)})
            return (
                int(HTTPStatus.BAD_GATEWAY),
                [("Content-Type", "application/json")],
                json.dumps(
                    {"error": "bad_gateway", "upstream": url, "reason": str(reason)}
                ).encode(),
            )

    def _read_body(self) -> bytes | None:
        raw_length = self.headers.get("Content-Length")
        if not raw_length:
            return None
        try:
            length = int(raw_length)
        except ValueError:
            return None
        if length <= 0:
            return None
        if length > _MAX_BODY_BYTES:
            log.warning("request body exceeds limit; truncating", extra={"length": length})
            length = _MAX_BODY_BYTES
        return self.rfile.read(length)

    def _cookie_cohort(self) -> str | None:
        header = self.headers.get("Cookie")
        if not header:
            return None
        try:
            jar = http.cookies.SimpleCookie(header)
        except http.cookies.CookieError:
            return None
        morsel = jar.get(self.settings.cookie_name)
        return morsel.value if morsel else None

    def _respond(
        self,
        *,
        status: int,
        headers: list[tuple[str, str]],
        payload: bytes,
        cohort: str,
        latency_ms: float,
        set_cookie: bool,
        body_expected: bool,
    ) -> None:
        self.send_response(status)
        for key, value in headers:
            lowered = key.lower()
            # Content-Length is recomputed below; relaying the upstream's value
            # alongside a different body is how proxies hang their clients.
            if lowered in HOP_BY_HOP or lowered == "content-length":
                continue
            self.send_header(key, value)

        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Bellwether-Target", cohort)
        self.send_header("X-Bellwether-Latency", f"{latency_ms:.2f}")
        if set_cookie and self.settings.sticky_sessions:
            self.send_header(
                "Set-Cookie",
                f"{self.settings.cookie_name}={cohort}; Path=/; HttpOnly; SameSite=Lax; "
                f"Max-Age={self.settings.cookie_max_age_s}",
            )
        self.end_headers()

        if body_expected and payload:
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                log.debug("client disconnected before the response was written")

    def _handle_internal(self, *, body_expected: bool) -> None:
        """Serve the operator endpoints. Never proxied, never counted as traffic."""
        if self.path == HEALTH_PATH:
            payload = json.dumps(
                {
                    "status": "ok",
                    "canaryWeight": self.router.weight,
                    "stablePort": self.settings.stable_port,
                    "canaryPort": self.settings.canary_port,
                    "stickySessions": self.settings.sticky_sessions,
                    "chaosActive": self.chaos.active,
                }
            ).encode()
            status = int(HTTPStatus.OK)
        elif self.path == METRICS_PATH:
            samples = self.telemetry.samples()
            payload = json.dumps(
                {"count": len(samples), "samples": [s.to_dict() for s in samples]}
            ).encode()
            status = int(HTTPStatus.OK)
        else:
            payload = json.dumps({"error": "not_found", "path": self.path}).encode()
            status = int(HTTPStatus.NOT_FOUND)

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if body_expected:
            self.wfile.write(payload)


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    # Without this a restart within TIME_WAIT fails with "Address already in
    # use", which previously looked like a port conflict and sent operators
    # hunting for a process that had already exited.
    allow_reuse_address = True


class ProxyServer:
    """Owns the socket, the router, and the telemetry store."""

    def __init__(
        self,
        settings: ProxySettings,
        *,
        weight_file: Path,
        telemetry_file: Path,
        chaos_file: Path,
    ) -> None:
        self.settings = settings
        self.weights = TrafficWeightStore(weight_file)
        self.telemetry = TelemetryStore(
            telemetry_file,
            window=settings.telemetry_window,
            flush_interval_s=settings.telemetry_flush_interval_s,
        )
        self.router = Router(settings, self.weights)
        self.chaos = ChaosInjector(settings, chaos_file)

        handler = type(
            "BoundProxyHandler",
            (_Handler,),
            {
                "settings": settings,
                "router": self.router,
                "telemetry": self.telemetry,
                "chaos": self.chaos,
            },
        )
        self._server = _ThreadingHTTPServer((settings.host, settings.port), handler)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def serve_forever(self) -> None:
        log.info(
            "traffic proxy listening",
            extra={
                "address": f"{self.settings.host}:{self.port}",
                "stable": self.settings.stable_port,
                "canary": self.settings.canary_port,
            },
        )
        try:
            self._server.serve_forever(poll_interval=0.2)
        finally:
            self.telemetry.flush()

    def start_background(self) -> None:
        """Run the server on a daemon thread. Used by tests and the pipeline."""
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self.telemetry.flush()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        log.info("traffic proxy stopped")
