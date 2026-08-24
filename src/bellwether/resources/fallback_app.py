"""Minimal health-check server used when a repository's runtime is unknown.

Deliberately dependency-free -- it must start even when nothing could be
installed. The previous implementation wrote a Flask app *into the cloned
repository* and then pip-installed Flask to run it, which mutated the artefact
under test and failed on any machine without network access.

Usage: python3 fallback_app.py <port>
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    server_version = "BellwetherFallback/1.0"

    def do_GET(self) -> None:
        body = json.dumps(
            {
                "status": "ok",
                "service": "bellwether-fallback",
                "note": "repository runtime was not recognised; serving a health stub",
                "path": self.path,
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("fallback %s\n" % (fmt % args))


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8001
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    sys.stderr.write(f"bellwether fallback listening on 127.0.0.1:{port}\n")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
