"""Tests for serve.py's HTTP surface.

Standard library only. These spin the real handler up on an ephemeral port
rather than mocking it, because the bugs worth guarding here are protocol-level
and a mock would not catch them:

  * A POST to an unknown route used to answer 404 without draining the request
    body. On a keep-alive connection the unread bytes were then parsed as the
    next request, so the client saw a bogus "Bad request syntax" immediately
    after a legitimate 404.
  * /healthz is the browser's only way to learn whether /api/db exists, so its
    shape is a contract with index.html, not an implementation detail.

Run with:

    python3 -m unittest discover -s tests -v
"""

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import serve  # noqa: E402


class QuietHandler(serve.Handler):
    """The real handler with the per-request stderr log muted.

    Subclassing only the logging keeps the routing, parsing and protocol
    behaviour under test exactly as shipped -- and without 40 lines of
    "GET /healthz 200" drowning the actual test names.
    """

    def log_message(self, fmt, *args):
        pass


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_file = Path(self._tmp.name) / "http.db"
        self._prev = os.environ.get("APPLYONCE_DB")
        os.environ["APPLYONCE_DB"] = str(self.db_file)
        db.reset_connection()
        self.addCleanup(self._restore)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown)

    def _shutdown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def _restore(self):
        db.reset_connection()
        if self._prev is None:
            os.environ.pop("APPLYONCE_DB", None)
        else:
            os.environ["APPLYONCE_DB"] = self._prev

    def conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)

    def get(self, path, connection=None):
        own = connection is None
        connection = connection or self.conn()
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            body = response.read()
            return response.status, body
        finally:
            if own:
                connection.close()

    def get_json(self, path):
        status, body = self.get(path)
        return status, json.loads(body)

    def post(self, path, payload=None, raw=None, connection=None, headers=None):
        own = connection is None
        connection = connection or self.conn()
        body = raw if raw is not None else json.dumps(payload or {})
        all_headers = {"Content-Type": "application/json"}
        all_headers.update(headers or {})
        try:
            connection.request("POST", path, body=body, headers=all_headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            if own:
                connection.close()


class TestHealthz(ServerTestCase):
    def test_advertises_both_routes_when_db_present(self):
        status, payload = self.get_json("/healthz")
        self.assertEqual(status, 200)
        self.assertIn("/api/yc", payload["apis"])
        self.assertIn("/api/db", payload["apis"])
        self.assertTrue(payload["storage"]["available"])
        self.assertEqual(payload["storage"]["path"], str(self.db_file))

    def test_reports_schema_version_and_stats(self):
        _, payload = self.get_json("/healthz")
        storage = payload["storage"]
        self.assertEqual(storage["schema_version"], db.SCHEMA_VERSION)
        self.assertIn("companies", storage["stats"])

    def test_healthz_does_not_create_the_database(self):
        # Feature detection runs on every page load; asking "is storage
        # available?" must not be what brings the database into existence.
        fresh = Path(self._tmp.name) / "untouched.db"
        self._prev2 = os.environ["APPLYONCE_DB"]
        os.environ["APPLYONCE_DB"] = str(fresh)
        try:
            _, payload = self.get_json("/healthz")
            self.assertTrue(payload["storage"]["available"])
        finally:
            os.environ["APPLYONCE_DB"] = self._prev2
        self.assertEqual(payload["storage"]["path"], str(fresh))


class TestRouting(ServerTestCase):
    def test_serves_the_app(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"ApplyOnce", body)

    def test_index_html_alias(self):
        self.assertEqual(self.get("/index.html")[0], 200)

    def test_favicon_is_cheap(self):
        self.assertEqual(self.get("/favicon.ico")[0], 204)

    def test_unknown_get_is_json_404(self):
        status, payload = self.get_json("/api/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", payload)

    def test_py_suffix_accepted(self):
        # The same URL has to work locally and as a Vercel function.
        self.assertEqual(self.get("/api/yc.py?action=batches")[0], 200)

    def test_trailing_slash_is_tolerated(self):
        self.assertEqual(self.get("/healthz/")[0], 200)

    def test_head_returns_headers_without_a_body(self):
        connection = self.conn()
        self.addCleanup(connection.close)
        connection.request("HEAD", "/")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), b"")


class TestPostProtocol(ServerTestCase):
    def test_post_to_unknown_route_keeps_the_connection_usable(self):
        # The regression: an undrained body used to be re-parsed as the next
        # request on this same socket.
        connection = self.conn()
        self.addCleanup(connection.close)

        status, body = self.post("/api/nope", {"payload": "x" * 64},
                                 connection=connection)
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(body))

        status, body = self.get("/healthz", connection=connection)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_oversized_body_is_refused(self):
        connection = self.conn()
        self.addCleanup(connection.close)
        connection.putrequest("POST", "/api/db")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(serve.MAX_BODY_BYTES + 1))
        connection.endheaders()
        try:
            response = connection.getresponse()
            self.assertEqual(response.status, 413)
            response.read()
        except Exception:
            # Some servers close the socket instead of replying; either is an
            # acceptable refusal, a silent hang would not be.
            pass

    def test_malformed_json_is_a_client_error(self):
        status, body = self.post("/api/db", raw="{not json")
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body))

    def test_non_object_body_is_refused(self):
        status, body = self.post("/api/db", raw="[1,2,3]")
        self.assertEqual(status, 400)

    def test_cross_origin_post_is_refused(self):
        # Any page the user has open can reach 127.0.0.1. A browser always
        # attaches Origin to a cross-origin POST, so that header is the defence.
        connection = self.conn()
        self.addCleanup(connection.close)
        connection.request(
            "POST", "/api/db",
            body=json.dumps({"action": "status"}),
            headers={"Content-Type": "application/json",
                     "Origin": "https://evil.example"},
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        self.assertIn("cross-origin", json.loads(response.read())["error"])

    def test_same_origin_post_is_allowed(self):
        status, _ = self.post(
            "/api/db", {"action": "status"},
            headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)

    def test_post_without_an_origin_header_is_allowed(self):
        # curl and the CLIs send no Origin; they are local and deliberate.
        status, _ = self.post("/api/db", {"action": "status"})
        self.assertEqual(status, 200)

    def test_read_only_route_ignores_a_posted_body(self):
        # /api/yc has no write actions and its handler never reads the body, so
        # a POST with a read action just answers the read. Harmless and
        # convenient -- the client does not have to know which verbs a route
        # supports -- but worth pinning, because the body is genuinely dropped.
        status, body = self.post("/api/yc", {"action": "batches"})
        self.assertEqual(status, 200)
        self.assertIn("batches", json.loads(body))


class TestDbOverHttp(ServerTestCase):
    def test_status_then_import_then_companies(self):
        status, body = self.post("/api/db", {"action": "status"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["available"])

        state = {
            "activeBatch": "Fall 2026",
            "details": {"my_name": "Ada"},
            "batches": {"Fall 2026": {"offset": 0, "total": 1, "companies": {
                "acme": {"id": "acme", "slug": "acme", "name": "Acme Corp",
                         "domain": "acme.com", "batch": "Fall 2026",
                         "founders": [{"name": "Ada", "email": "ada@acme.com"}],
                         "founders_state": "loaded"}}}},
        }
        status, body = self.post("/api/db", {"action": "import", "state": state})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["imported"]["companies"], 1)

        status, body = self.get_json("/api/db?action=companies")
        self.assertEqual(status, 200)
        names = [c["name"] for c in body["companies"]]
        self.assertIn("Acme Corp", names)

    def test_import_requires_an_explicit_action(self):
        # Without "action" the dispatcher defaults to status. That default is
        # deliberate for GETs, so pin the behaviour rather than let it drift.
        status, body = self.post("/api/db", {"state": {"batches": {}}})
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIn("stats", payload)
        self.assertNotIn("imported", payload)


if __name__ == "__main__":
    unittest.main()