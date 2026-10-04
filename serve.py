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
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from api.yc import UpstreamError, handle_request  # noqa: E402

INDEX_HTML = ROOT / "index.html"

# db.py is local-only. It is absent from vercel.json because Vercel's
# filesystem is ephemeral and read-only, so a SQLite write could never work
# there. Import it defensively anyway: the research half of this app must keep
# working even if the storage half is broken or missing, so the browser can
# discover what is available via /healthz rather than by failing a request.
try:
    import db

    DB_AVAILABLE = True
except ImportError:  # pragma: no cover - defensive
    db = None  # type: ignore[assignment]
    DB_AVAILABLE = False

# Refuse to buffer an unbounded request body. Mirrors db.MAX_IMPORT_BYTES; the
# browser sends its whole state blob on import, which is the largest thing this
# server will ever receive.
MAX_BODY_BYTES = 16 * 1024 * 1024

# Path -> handler. Every handler has the same signature,
# (query, body) -> (status, payload), so adding a module is one dict entry.
API_ROUTES = {
    "/api/yc": lambda query, body: handle_request(query),
}
if DB_AVAILABLE:
    API_ROUTES["/api/db"] = db.handle_request

# What each route can do, so /healthz can answer without duplicating a list that
# would otherwise drift out of sync with the code.
YC_ACTIONS = ["batches", "companies", "company", "site_emails"]
DB_ACTIONS = [
    "status", "import", "export", "clear", "companies",
    "queue_list", "queue_add", "queue_approve", "queue_status",
    "unsubscribe", "settings_set",
]


class Handler(BaseHTTPRequestHandler):
    """Static file for / plus the JSON APIs under /api/*."""

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

    def finish(self) -> None:
        # db.connect() hands each thread its own SQLite connection, and this
        # server runs a thread per client connection. Without releasing it here
        # every connection that ever loaded a batch leaves an open handle
        # behind, so a long session slowly accumulates file descriptors.
        # finish() runs once per connection, not once per request, so a
        # keep-alive client still reuses its handle.
        if DB_AVAILABLE:
            try:
                db.reset_connection()
            except Exception:
                pass
        super().finish()

    def _route_for(self, path: str):
        """Resolve a URL path to (handler, actions) or (None, None).

        The `.py` suffix is accepted alongside the bare path so the same URL
        works whether it is hit by the local server or by a Vercel function.
        """
        base = path[:-3] if path.endswith(".py") else path
        handler = API_ROUTES.get(base)
        return handler, base

    def _read_json_body(self) -> dict:
        """Parse the request body as a JSON object, bounded.

        Raises UpstreamError rather than letting a malformed or oversized body
        surface as a 500 with a Python error message in it.
        """
        raw_length = self.headers.get("Content-Length", "")
        if not raw_length:
            return {}
        try:
            length = int(raw_length)
        except ValueError:
            raise UpstreamError("Content-Length is not an integer", 400) from None
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise UpstreamError(
                f"request body is {length} bytes, limit is {MAX_BODY_BYTES}", 413
            )

        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpstreamError(f"body is not valid JSON: {exc}", 400) from exc
        if not isinstance(parsed, dict):
            raise UpstreamError("body must be a JSON object", 400)
        return parsed

    # ------------------------------------------------------------------ verbs

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        handler, _ = self._route_for(path)
        if handler is not None:
            self._handle_api(handler, urllib.parse.parse_qs(parsed.query), {})
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
            self._healthz()
            return

        self._send_json(404, {"error": f"no route for {path}"})

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib naming
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if not self._origin_is_local():
            # This server listens on loopback and accepts writes, so any page you
            # happen to have open could POST to it. A browser always attaches
            # Origin to a cross-origin POST, so a missing or foreign one is the
            # signal. Non-browser clients (curl, the CLIs) send no Origin at all
            # and are left alone -- they are local and deliberate.
            self._send_json(403, {
                "error": "cross-origin POST refused; this API is local-only"
            })
            return

        # Read the body before routing, even for a path that will 404. This is
        # HTTP/1.1 keep-alive: any unread request bytes stay in the socket and
        # get parsed as the *next* request, which shows up as a bogus
        # "Bad request syntax" right after a legitimate 404. Draining first
        # keeps the connection usable either way.
        try:
            body = self._read_json_body()
        except UpstreamError as exc:
            self._send_json(exc.status, {"error": str(exc)})
            return

        handler, _ = self._route_for(path)
        if handler is None:
            self._send_json(404, {"error": f"no route for {path}"})
            return

        self._handle_api(handler, urllib.parse.parse_qs(parsed.query), body)

    def _origin_is_local(self) -> bool:
        """True unless a browser says this request came from somewhere else."""
        origin = self.headers.get("Origin") or self.headers.get("Referer")
        if not origin:
            return True
        parsed = urllib.parse.urlparse(origin)
        host = parsed.netloc or ""
        return host in ("", self.headers.get("Host", ""))

    def _handle_api(
        self, handler, query: dict[str, list[str]], body: dict
    ) -> None:
        try:
            status, payload = handler(query, body)
        except UpstreamError as exc:
            status = exc.status if exc.status >= 400 else 502
            payload = {"error": str(exc)}
        except Exception as exc:  # keep the server alive, hide the traceback
            status = 500
            payload = {"error": f"internal error: {exc}"}
        self._send_json(status, payload)

    def _healthz(self) -> None:
        """Advertise what this process can actually do.

        The browser checks this on boot to decide whether to show storage at
        all. On Vercel -- where only /api/yc exists -- the same page degrades to
        the research-only experience it always was, with no feature-detection
        code guessing from the URL.
        """
        payload: dict = {
            "ok": True,
            "apis": {"/api/yc": YC_ACTIONS},
            "storage": {"available": False},
            "vercel_compatible": True,
        }
        if DB_AVAILABLE:
            payload["apis"]["/api/db"] = DB_ACTIONS
            try:
                status = db.migrate()
                payload["storage"] = {
                    "available": True,
                    "path": str(db.db_path()),
                    "schema_version": status,
                    "stats": db.stats(),
                }
            except Exception as exc:
                payload["storage"] = {"available": False, "error": str(exc)}
        self._send_json(200, payload)

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
    parser.add_argument("--db", default="",
                        help="database file (default: $APPLYONCE_DB or ./applyonce.db)")
    args = parser.parse_args()

    # Set before db is used anywhere, and env wins over the flag's absence so
    # both entry points agree on the path.
    if args.db:
        os.environ["APPLYONCE_DB"] = args.db

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

    if DB_AVAILABLE:
        try:
            version = db.migrate()
            print(f"  db:   {db.db_path()} (schema v{version})")
        except Exception as exc:
            # Non-fatal: the research features still work off localStorage.
            print(f"  db:   unavailable ({exc}) -- continuing without storage")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    host = args.host if args.host != "0.0.0.0" else "localhost"
    routes = ", ".join(sorted(API_ROUTES))
    print(f"\n  ApplyOnce -> http://{host}:{args.port}\n"
          f"  API        -> {routes}\n"
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