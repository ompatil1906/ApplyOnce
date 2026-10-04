"""Contract tests between the three files that have to agree.

ApplyOnce splits across index.html (browser), serve.py (local server) and
db.py (storage), and the only thing keeping them in sync is a handful of string
literals. Each of those is a place where the code can drift and still import,
still compile, and still pass its own tests -- so pin them here:

  * Every action db.handle_request accepts must be advertised by
    serve.DB_ACTIONS, because /healthz is how the browser discovers what the
    server can do. An unadvertised action is invisible to the UI.
  * Every action serve.DB_ACTIONS advertises must actually be dispatched,
    otherwise /healthz promises a capability that 400s.
  * index.html must keep the element ids its own script reaches for, and must
    call the endpoints that exist.

Run with:

    python3 -m unittest discover -s tests -v
"""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import serve  # noqa: E402

INDEX = (ROOT / "index.html").read_text(encoding="utf-8")

# Actions the dispatcher branches on. Matched from source rather than by calling
# handle_request, because probing it would mean inventing valid bodies for every
# action and would execute real writes as a side effect of a wiring test.
_DB_SOURCE = (ROOT / "db.py").read_text(encoding="utf-8")
DISPATCHED = set(re.findall(r'action\s*==\s*"([a-z_]+)"', _DB_SOURCE))


class TestActionAdvertisements(unittest.TestCase):
    def test_every_dispatched_action_is_advertised(self):
        missing = sorted(DISPATCHED - set(serve.DB_ACTIONS))
        self.assertEqual(missing, [], f"unadvertised actions: {missing}")

    def test_every_advertised_action_is_dispatched(self):
        phantom = sorted(set(serve.DB_ACTIONS) - DISPATCHED)
        self.assertEqual(phantom, [], f"advertised but not implemented: {phantom}")

    def test_yc_actions_match_the_handler(self):
        # Same drift risk on the research route, which Vercel also serves.
        yc_source = (ROOT / "api" / "yc.py").read_text(encoding="utf-8")
        implemented = set(re.findall(r'action\s*==\s*"([a-z_]+)"', yc_source))
        missing = sorted(implemented - set(serve.YC_ACTIONS))
        self.assertEqual(missing, [], f"unadvertised yc actions: {missing}")


class TestBrowserWiring(unittest.TestCase):
    def test_storage_elements_exist(self):
        # The ids renderStorage() and the button handlers look up. A rename in
        # the markup alone would leave the script querying nothing.
        for element_id in ("storageCard", "storageBox", "storageNote",
                           "btnDbSave", "btnDbRestore", "btnDbRefresh"):
            self.assertIn(f'id="{element_id}"', INDEX,
                          f"index.html is missing #{element_id}")

    def test_script_references_the_endpoints_it_calls(self):
        self.assertIn("/api/db", INDEX)
        self.assertIn("/healthz", INDEX)
        self.assertIn("/api/yc", INDEX)

    def test_browser_asks_healthz_before_offering_storage(self):
        # Feature detection, not URL sniffing: the page must consult /healthz
        # so the same file works on Vercel, where /api/db does not exist.
        self.assertRegex(INDEX, r'dbGet\(\s*"status"|action:\s*"status"')

    def test_apify_token_is_never_sent_to_the_database(self):
        # The browser keeps the token local and talks to Apify directly. If a
        # future change ever pushed it at /api/db, db.py would have to reject
        # it, so make sure the JS has no such call.
        self.assertNotRegex(INDEX, r"dbPost\([^)]*apify")

    def test_no_external_script_or_font(self):
        # The whole point is one HTML file with no CDN.
        self.assertNotRegex(INDEX, r'<script[^>]+\ssrc\s*=')
        self.assertNotIn("fonts.googleapis.com", INDEX)


class TestSendQueueWiring(unittest.TestCase):
    """The review queue is the one place where a typo is invisible until a user
    clicks a button, so the element ids and the action names are pinned."""

    def test_queue_elements_exist(self):
        for element_id in ("queueCard", "queueBox", "queueNote", "queueActions",
                           "queueList", "queueCounts", "btnQueueApproveAll",
                           "btnQueueRefresh"):
            self.assertIn(f'id="{element_id}"', INDEX,
                          f"index.html is missing #{element_id}")

    def test_queue_actions_are_real_db_actions(self):
        # dbGet/dbPost string literals are the only thing binding the buttons to
        # the dispatcher; a typo here is a button that 400s at click time.
        called = set(re.findall(r'db(?:Get|Post)\(\s*"(queue_[a-z_]+)"', INDEX))
        self.assertTrue(called, "no queue action is called from index.html")
        unknown = sorted(called - DISPATCHED)
        self.assertEqual(unknown, [], f"index.html calls unknown actions: {unknown}")

    def test_queue_actions_are_advertised(self):
        called = set(re.findall(r'db(?:Get|Post)\(\s*"(queue_[a-z_]+)"', INDEX))
        missing = sorted(called - set(serve.DB_ACTIONS))
        self.assertEqual(missing, [], f"unadvertised queue actions: {missing}")

    def test_queue_row_fields_the_ui_reads_exist(self):
        # renderQueue() reads status/email/company_id/founder_name/subject/error
        # off each row. queue_list hands back raw rows, so every one of those has
        # to actually come back, or the review list renders blank cells.
        import os
        import tempfile
        previous = os.environ.get("APPLYONCE_DB")
        os.environ["APPLYONCE_DB"] = os.path.join(tempfile.mkdtemp(), "wiring.db")
        try:
            db.upsert_company({"id": "acme", "slug": "acme",
                               "name": "Acme Corp", "domain": "acme.com"})
            db.enqueue([{"company_id": "acme", "email": "ada@acme.com",
                         "founder_name": "Ada", "subject": "hi", "body": "b"}])
            row = db.list_queue()[0]
        finally:
            if previous is None:
                os.environ.pop("APPLYONCE_DB", None)
            else:
                os.environ["APPLYONCE_DB"] = previous
            db.reset_connection()
        for field in ("id", "status", "email", "company_id", "company_name",
                      "founder_name", "subject", "error"):
            self.assertIn(field, row, f"queue rows have no '{field}'")
        # The review list shows the name, not the slug: "Acme Corp" reads as a
        # person you are about to email, "acme" reads as a database row.
        self.assertEqual(row["company_name"], "Acme Corp")

    def test_renaming_a_company_updates_its_queued_drafts(self):
        # The name is joined, not copied, so it cannot go stale in the queue.
        import os
        import tempfile
        previous = os.environ.get("APPLYONCE_DB")
        os.environ["APPLYONCE_DB"] = os.path.join(tempfile.mkdtemp(), "wiring.db")
        try:
            db.upsert_company({"id": "acme", "slug": "acme",
                               "name": "Old Name", "domain": "acme.com"})
            db.enqueue([{"company_id": "acme", "email": "ada@acme.com",
                         "subject": "hi", "body": "b"}])
            db.upsert_company({"id": "acme", "slug": "acme",
                               "name": "New Name", "domain": "acme.com"})
            self.assertEqual(db.list_queue()[0]["company_name"], "New Name")
        finally:
            if previous is None:
                os.environ.pop("APPLYONCE_DB", None)
            else:
                os.environ["APPLYONCE_DB"] = previous
            db.reset_connection()

    def test_a_queued_row_survives_its_company_being_deleted(self):
        # LEFT JOIN, not INNER: a draft you have already written is worth more
        # than the company record it points at.
        import os
        import tempfile
        previous = os.environ.get("APPLYONCE_DB")
        os.environ["APPLYONCE_DB"] = os.path.join(tempfile.mkdtemp(), "wiring.db")
        try:
            db.upsert_company({"id": "acme", "slug": "acme",
                               "name": "Acme Corp", "domain": "acme.com"})
            db.enqueue([{"company_id": "acme", "email": "ada@acme.com",
                         "subject": "hi", "body": "b"}])
            conn = db.connect()
            with db.write(conn):
                conn.execute("DELETE FROM companies WHERE id='acme'")
            row = db.list_queue()[0]
            self.assertEqual(row["email"], "ada@acme.com")
            self.assertIsNone(row["company_name"])
        finally:
            if previous is None:
                os.environ.pop("APPLYONCE_DB", None)
            else:
                os.environ["APPLYONCE_DB"] = previous
            db.reset_connection()

    def test_the_page_cannot_transmit(self):
        # There is deliberately no send action reachable from the browser.
        # Delivery needs terminal-held credentials and a typed confirmation, and
        # serve.py listens on loopback where any open page could reach it.
        self.assertNotRegex(INDEX, r'dbPost\(\s*"send')
        self.assertNotRegex(INDEX, r'dbGet\(\s*"send')
        self.assertNotIn("/api/mail", INDEX)
        self.assertNotIn("/api/send", INDEX)

    def test_the_page_tells_you_sending_is_a_terminal_action(self):
        # Otherwise the absence of a Send button reads as a missing feature.
        self.assertRegex(INDEX, r'mailer\.py send[^\n]*--send')


if __name__ == "__main__":
    unittest.main()