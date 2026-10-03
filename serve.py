#!/usr/bin/env python3
"""
Local development server for ApplyOnce.

Serves the single-page app and mounts the very same request handler that runs
in production on Vercel, so there is exactly one code path to reason about:

    python3 serve.py                 # http://127.0.0.1:8000
    python3 serve.py --port 5500
    python3 serve.py --host 0.0.0.0  # reachable from your phone on the LAN

Standard library only, like everything else in this project.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from api.yc import UpstreamError, handle_request  # noqa: E402

INDEX_HTML = ROOT / "index.html"

# Routes the browser is allowed to reach. Anything else 404s rather than being
# served off the filesystem.
API_PATH = "/api/yc"


class Handler(BaseHTTPRequestHandler):
    """Static file for / plus the JSON API under /api/yc."""

    protocol_version = "HTTP/1.1"
    server_version = "ApplyOnce/dev"

    # ---------------------------------------------------------------- helpers

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Local dev must never serve a stale page while iterating.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("  %s\n" % (fmt % args))

    # ------------------------------------------------------------------ verbs

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == API_PATH or path == f"{API_PATH}.py":
            self._handle_api(urllib.parse.parse_qs(parsed.query))
            return

        if path in ("/", "/index.html"):
            self._serve_index()
            return

        if path == "/favicon.ico":
            # Inline SVG favicon via a redirect-free empty response; the page
            # also declares its own <link rel="icon"> as a data URI.
            self._send(204, b"", "image/x-icon")
            return

        if path == "/healthz":
            self._send_json(200, {"ok": True, "actions": [
                "batches", "companies", "company", "site_emails"]})
            return

        self._send_json(404, {"error": f"no route for {path}"})

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib naming
        self.do_GET()

    def _handle_api(self, query: dict[str, list[str]]) -> None:
        try:
            status, payload = handle_request(query)
        except UpstreamError as exc:
            status = exc.status if exc.status >= 400 else 502
            payload = {"error": str(exc)}
        except Exception as exc:  # keep the server alive, hide the traceback
            status = 500
            payload = {"error": f"internal error: {exc}"}
        self._send_json(status, payload)

    def _serve_index(self) -> None:
        if not INDEX_HTML.exists():
            self._send_json(500, {
                "error": "index.html is missing from the project root"
            })
            return
        self._send(200, INDEX_HTML.read_bytes(), "text/html; charset=utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run ApplyOnce locally.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000,
                        help="bind port (default: 8000)")
    args = parser.parse_args()

    # Warm the caches before announcing readiness, so the first click in the
    # browser is not the one that pays for the Algolia round trip.
    def warm() -> None:
        try:
            batches = handle_request({"action": "batches"})[1]["batches"]
            print(f"  warm: {len(batches)} batches, latest {batches[0]['name']}"
                  if batches else "  warm: no batches returned")
        except Exception as exc:
            print(f"  warm failed (the app will retry on demand): {exc}")

    print("warming caches...")
    warm()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    host = args.host if args.host != "0.0.0.0" else "localhost"
    print(f"\n  ApplyOnce -> http://{host}:{args.port}\n"
          f"  API        -> http://{host}:{args.port}/api/yc?action=batches\n"
          f"  Ctrl-C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())