"""HTTP API and websocket bridge.

Security posture, stated explicitly because the previous version had none:

* CORS is an explicit allowlist. ``/api/deploy`` clones and executes code named
  in the request body, so ``Access-Control-Allow-Origin: *`` meant any page the
  operator visited could trigger a deployment. Wildcards are rejected at config
  load.
* Mutating endpoints accept a bearer token when one is configured, and the
  server binds to loopback by default.
* The GitHub webhook requires a valid HMAC signature. Previously it was an
  unauthenticated endpoint that took a repository URL and ran ``git clone`` on
  it through a shell.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from typing import Any

from flask import Blueprint, Flask, Response, current_app, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO

from .config import Settings, load_settings
from .errors import (
    AuthenticationError,
    BellwetherError,
    ConflictError,
    ValidationError,
)
from .events import TOPIC_CHAOS
from .logging_setup import configure_logging, get_logger
from .security import verify_bearer_token, verify_webhook_signature
from .service import PlatformService

log = get_logger(__name__)

MAX_WEBHOOK_BYTES = 1024 * 1024


def _service() -> PlatformService:
    service: PlatformService = current_app.extensions["bellwether"]
    return service


def _settings() -> Settings:
    return _service().settings


def require_auth(view: Callable[..., Any]) -> Callable[..., Any]:
    """Enforce the bearer token on mutating endpoints, when one is configured."""

    @functools.wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        verify_bearer_token(_settings().api.auth_token, request.headers.get("Authorization"))
        return view(*args, **kwargs)

    return wrapper


api = Blueprint("api", __name__, url_prefix="/api")


# ── Read endpoints ────────────────────────────────────────────────────────────


@api.get("/health")
def health() -> Response:
    return jsonify({"status": "ok", "version": "3.0.0"})


@api.get("/config")
def platform_config() -> Response:
    return jsonify(_service().platform_config())


@api.get("/status")
def status() -> Response:
    return jsonify(_service().status())


@api.get("/history")
def history() -> Response:
    limit = request.args.get("limit", default=25, type=int)
    offset = request.args.get("offset", default=0, type=int)
    return jsonify(_service().history(limit=limit, offset=offset))


@api.get("/telemetry")
def telemetry() -> Response:
    return jsonify(_service().telemetry())


@api.get("/metrics")
def metrics() -> Response:
    limit = request.args.get("limit", type=int)
    return jsonify(_service().metrics(limit=limit))


@api.get("/console")
def console() -> Response:
    lines = _service().console()
    return jsonify({"lines": lines, "count": len(lines)})


# ── Mutating endpoints ────────────────────────────────────────────────────────


@api.post("/deploy")
@require_auth
def deploy() -> tuple[Response, int]:
    body = request.get_json(silent=True) or {}
    repo_url = body.get("repoUrl") or body.get("repo_url") or ""
    if not isinstance(repo_url, str) or not repo_url.strip():
        raise ValidationError("repoUrl is required")

    launch = body.get("launch")
    if launch is not None and not isinstance(launch, dict):
        raise ValidationError("launch must be an object")

    run = _service().start_deployment(repo_url.strip(), trigger="manual", launch=launch)
    return jsonify({"run": run.to_dict()}), 202


@api.post("/rollback")
@require_auth
def rollback() -> Response:
    body = request.get_json(silent=True) or {}
    reason = str(body.get("reason") or "operator requested rollback")[:500]
    return jsonify(_service().rollback(reason))


@api.post("/chaos")
@require_auth
def set_chaos() -> Response:
    body = request.get_json(silent=True) or {}
    if "enabled" not in body or not isinstance(body["enabled"], bool):
        raise ValidationError("body must contain a boolean 'enabled' field")
    enabled = _service().set_chaos(body["enabled"])
    _service().bus.publish(TOPIC_CHAOS, {"enabled": enabled})
    return jsonify({"enabled": enabled})


@api.get("/chaos")
def chaos_status() -> Response:
    service = _service()
    return jsonify(
        {"enabled": service.chaos_state(), "available": service.settings.proxy.chaos_enabled}
    )


@api.post("/webhook/github")
def github_webhook() -> tuple[Response, int]:
    raw = request.get_data(cache=False)
    if len(raw) > MAX_WEBHOOK_BYTES:
        raise ValidationError("webhook payload is too large")

    verify_webhook_signature(
        _settings().api.webhook_secret, raw, request.headers.get("X-Hub-Signature-256")
    )

    event = request.headers.get("X-GitHub-Event", "")
    if event == "ping":
        return jsonify({"status": "pong"}), 200
    if event != "push":
        return jsonify({"status": "ignored", "reason": f"event {event!r} is not handled"}), 200

    # Parse the bytes we just authenticated rather than trusting Flask's
    # content-type negotiation: GitHub can deliver a push as either
    # application/json or application/x-www-form-urlencoded, and the signature
    # covers the raw body either way.
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("webhook body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValidationError("webhook body must be a JSON object")

    ref = payload.get("ref")
    default_branch = (payload.get("repository") or {}).get("default_branch", "main")
    if ref != f"refs/heads/{default_branch}":
        return (
            jsonify({"status": "ignored", "reason": f"ref {ref!r} is not the default branch"}),
            200,
        )

    clone_url = (payload.get("repository") or {}).get("clone_url")
    if not clone_url:
        raise ValidationError("webhook payload has no repository.clone_url")

    run = _service().start_deployment(clone_url, trigger="webhook")
    return jsonify({"status": "triggered", "run": run.to_dict()}), 202


# ── Error handling ────────────────────────────────────────────────────────────


def _error_response(status: int, code: str, message: str) -> tuple[Response, int]:
    return jsonify({"error": {"code": code, "message": message}}), status


def register_error_handlers(app: Flask) -> None:
    """Map the exception hierarchy onto status codes, once, centrally."""

    @app.errorhandler(ValidationError)
    def _validation(exc: ValidationError) -> tuple[Response, int]:
        return _error_response(400, "validation_error", str(exc))

    @app.errorhandler(AuthenticationError)
    def _auth(exc: AuthenticationError) -> tuple[Response, int]:
        log.warning("rejected unauthenticated request", extra={"path": request.path})
        return _error_response(401, "unauthorized", str(exc))

    @app.errorhandler(ConflictError)
    def _conflict(exc: ConflictError) -> tuple[Response, int]:
        return _error_response(409, "conflict", str(exc))

    @app.errorhandler(BellwetherError)
    def _platform(exc: BellwetherError) -> tuple[Response, int]:
        log.exception("platform error", extra={"path": request.path})
        return _error_response(500, "platform_error", str(exc))

    @app.errorhandler(404)
    def _not_found(_exc: object) -> tuple[Response, int]:
        return _error_response(404, "not_found", f"no route for {request.path}")

    @app.errorhandler(405)
    def _not_allowed(_exc: object) -> tuple[Response, int]:
        return _error_response(405, "method_not_allowed", f"{request.method} is not allowed here")

    @app.errorhandler(Exception)
    def _unexpected(_exc: Exception) -> tuple[Response, int]:
        log.exception("unhandled error", extra={"path": request.path})
        # Never leak internals to the client; the detail is in the server log.
        return _error_response(500, "internal_error", "an unexpected error occurred")


# ── Websocket bridge ──────────────────────────────────────────────────────────


def _attach_socketio(service: PlatformService, socketio: SocketIO) -> None:
    """Forward every bus event to connected dashboards."""

    def forward(topic: str, payload: dict[str, Any]) -> None:
        try:
            socketio.emit(topic, payload)
        except Exception:  # noqa: BLE001 - a dead socket must not break the pipeline
            log.debug("socket emit failed", extra={"topic": topic})

    service.bus.subscribe(forward)

    @socketio.on("connect")
    def _on_connect() -> None:
        # Send a full snapshot so a dashboard that connects mid-run is not blank
        # until the next event happens to fire.
        socketio.emit(
            "snapshot",
            {
                "status": service.status(),
                "console": service.console(),
                "config": service.platform_config(),
            },
        )


# ── App factory ───────────────────────────────────────────────────────────────


def create_app(
    settings: Settings | None = None,
    *,
    service: PlatformService | None = None,
) -> tuple[Flask, SocketIO, PlatformService]:
    """Build the Flask app, the websocket server, and the service they share."""
    settings = settings or load_settings()
    configure_logging(settings.log_level, settings.log_format)

    app = Flask(__name__)
    app.config["JSON_SORT_KEYS"] = False
    app.config["MAX_CONTENT_LENGTH"] = MAX_WEBHOOK_BYTES
    app.url_map.strict_slashes = False

    service = service or PlatformService(settings)
    app.extensions["bellwether"] = service

    CORS(
        app,
        resources={r"/api/*": {"origins": list(settings.api.cors_origins)}},
        supports_credentials=False,
        allow_headers=["Content-Type", "Authorization"],
        methods=["GET", "POST", "OPTIONS"],
    )

    app.register_blueprint(api)
    register_error_handlers(app)

    socketio = SocketIO(
        app,
        cors_allowed_origins=list(settings.api.cors_origins),
        async_mode="threading",
        logger=False,
        engineio_logger=False,
    )
    _attach_socketio(service, socketio)

    log.info(
        "api ready",
        extra={
            "bind": f"{settings.api.host}:{settings.api.port}",
            "cors": list(settings.api.cors_origins),
            "auth": "token" if settings.api.auth_token else "none (loopback only)",
            "webhook": "hmac" if settings.api.webhook_secret else "disabled",
        },
    )
    return app, socketio, service


def serve(settings: Settings | None = None) -> None:  # pragma: no cover - process entry
    settings = settings or load_settings()
    app, socketio, service = create_app(settings)

    if settings.api.host not in ("127.0.0.1", "localhost", "::1") and not settings.api.auth_token:
        log.error(
            "REFUSING TO START: the API is bound to a non-loopback address without "
            "BELLWETHER_API_TOKEN set. /api/deploy executes code from the request body."
        )
        raise SystemExit(2)

    try:
        socketio.run(
            app,
            host=settings.api.host,
            port=settings.api.port,
            allow_unsafe_werkzeug=True,
        )
    finally:
        service.shutdown()
