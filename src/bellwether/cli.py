"""Command line interface.

Every component is runnable on its own, which is what makes the platform
debuggable: an operator can start just the proxy, score risk against whatever
telemetry exists right now, or force traffic to 0% without going near the
dashboard.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from . import __version__
from .config import Settings, load_settings
from .errors import BellwetherError
from .logging_setup import configure_logging, get_logger

log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bellwether",
        description="Risk-gated progressive delivery for unmodified repositories.",
    )
    parser.add_argument("--version", action="version", version=f"bellwether {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("api", help="Run the dashboard API and websocket server.")
    sub.add_parser("proxy", help="Run the traffic proxy in the foreground.")

    deploy = sub.add_parser("deploy", help="Run one rollout to completion, in this process.")
    deploy.add_argument("repo_url", help="Repository to deploy.")

    risk = sub.add_parser("risk", help="Score the current proxy telemetry and exit 0/1.")
    risk.add_argument("--json", action="store_true", help="Emit the full assessment as JSON.")

    weight = sub.add_parser("weight", help="Read or set the canary traffic weight.")
    weight.add_argument("value", nargs="?", type=int, help="0-100. Omit to read.")

    sub.add_parser("rollback", help="Force traffic to 0%% and stop the canary.")
    sub.add_parser("status", help="Print the current platform state as JSON.")
    sub.add_parser("config", help="Print effective settings as JSON.")

    load = sub.add_parser("load", help="Generate traffic through the proxy.")
    load.add_argument("--seconds", type=float, default=30.0)
    load.add_argument("--workers", type=int, default=8)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    configure_logging(settings.log_level, settings.log_format)

    try:
        return _dispatch(args, settings)
    except BellwetherError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


def _dispatch(args: argparse.Namespace, settings: Settings) -> int:
    settings.paths.ensure()

    if args.command == "api":
        from .api import serve

        serve(settings)
        return 0

    if args.command == "proxy":
        return _run_proxy(settings)

    if args.command == "deploy":
        return _run_deploy(settings, args.repo_url)

    if args.command == "risk":
        return _run_risk(settings, as_json=args.json)

    if args.command == "weight":
        return _run_weight(settings, args.value)

    if args.command == "rollback":
        return _run_rollback(settings)

    if args.command == "status":
        return _run_status(settings)

    if args.command == "config":
        return _run_config(settings)

    if args.command == "load":
        return _run_load(settings, seconds=args.seconds, workers=args.workers)

    raise BellwetherError(f"unknown command {args.command!r}")


def _run_proxy(settings: Settings) -> int:
    from .proxy import ProxyServer

    server = ProxyServer(
        settings.proxy,
        weight_file=settings.paths.weight_file,
        telemetry_file=settings.paths.telemetry_file,
        chaos_file=settings.paths.chaos_file,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


def _run_deploy(settings: Settings, repo_url: str) -> int:
    from .service import PlatformService

    service = PlatformService(settings)
    try:
        run = service.start_deployment(repo_url, trigger="cli")
        print(f"started run {run.number} ({run.id})", file=sys.stderr)
        worker = service._worker
        if worker is not None:
            worker.join()
        final = service.store.get(run.id)
        if final is None:
            return 1
        print(json.dumps(final.to_dict(), indent=2))
        return 0 if final.status.value == "SUCCEEDED" else 1
    finally:
        service.shutdown()


def _run_risk(settings: Settings, *, as_json: bool) -> int:
    from .risk import assess
    from .telemetry import load_snapshot
    from .tracking import TrackingClient

    snapshot = load_snapshot(settings.paths.telemetry_file)
    assessment = assess(snapshot, settings.risk)

    TrackingClient(settings.risk).log_assessment(assessment, repo_url="(cli)", run_id="cli")

    if as_json:
        print(json.dumps(assessment.to_dict(), indent=2))
    else:
        print(assessment.summary())
        for reason in assessment.reasons:
            print(f"  - {reason}")
    return assessment.exit_code


def _run_weight(settings: Settings, value: int | None) -> int:
    from .weights import TrafficWeightStore

    store = TrafficWeightStore(settings.paths.weight_file)
    if value is None:
        print(store.get())
        return 0
    print(store.set(value))
    return 0


def _run_rollback(settings: Settings) -> int:
    from .service import PlatformService

    service = PlatformService(settings)
    try:
        print(json.dumps(service.rollback("cli rollback"), indent=2))
    finally:
        service.shutdown()
    return 0


def _run_status(settings: Settings) -> int:
    from .service import PlatformService

    service = PlatformService(settings)
    try:
        print(json.dumps(service.status(), indent=2, default=str))
    finally:
        service.shutdown()
    return 0


def _run_config(settings: Settings) -> int:
    print(json.dumps(describe_settings(settings), indent=2))
    return 0


def describe_settings(settings: Settings) -> dict[str, Any]:
    """Settings as a JSON-safe tree, with every secret redacted.

    Redaction is unconditional: `bellwether config` gets pasted into issues and
    run on screen-shared terminals.
    """
    import dataclasses

    SECRETS = {"auth_token", "webhook_secret", "github_token"}

    def encode(value: Any, *, redact: bool = False) -> Any:
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {
                field.name: encode(getattr(value, field.name), redact=field.name in SECRETS)
                for field in dataclasses.fields(value)
            }
        if redact:
            return "***set***" if value else None
        if isinstance(value, (list, tuple)):
            return [encode(item) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    result: dict[str, Any] = encode(settings)
    return result


def _run_load(settings: Settings, *, seconds: float, workers: int) -> int:
    from .pipeline import LoadGenerator

    target = f"http://{settings.proxy.host}:{settings.proxy.port}/"
    print(f"driving {workers} workers at {target} for {seconds:.0f}s", file=sys.stderr)
    with LoadGenerator(
        target, workers=workers, interval_s=settings.pipeline.load_interval_s
    ) as gen:
        import time as _time

        _time.sleep(seconds)
    print(f"sent {gen.sent} requests ({gen.failed} transport failures)", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
