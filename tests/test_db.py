"""Tests for db.py -- the local SQLite store.

Standard library only, like everything else here. Run them with:

    python3 -m unittest discover -s tests -v

These lean heavily on the bugs found while building the thing, because those
are the ones a future refactor would silently reintroduce:

  * set_flag() must update sent_at and hidden independently. It used to coerce
    an omitted argument to 0 in the INSERT, which made the COALESCE guard in
    the ON CONFLICT clause unreachable -- so hiding a company erased the record
    that you had already emailed them.
  * export_state() must return a usable activeBatch. Returning "" made a
    faithful restore look like total data loss, because the UI only ever draws
    the active batch.
  * connect() must apply the schema itself. It used to assume serve.py had
    already called migrate(), so `import db; db.upsert_company(...)` died with
    "no such table" on any fresh database.
  * import_state() must be idempotent, so a retried save cannot duplicate rows.
"""

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402


def company(cid, name="Acme", **extra):
    """A minimal company blob shaped like the browser sends it."""
    blob = {
        "id": cid,
        "slug": cid,
        "name": name,
        "domain": f"{cid}.com",
        "batch": "Fall 2026",
        "one_liner": "does a thing",
        "founders": [],
        "founders_state": "loaded",
    }
    blob.update(extra)
    return blob


def key(cid):
    """The row id upsert_company will assign to this blob."""
    return db.company_key("yc", company(cid))


class DbTestCase(unittest.TestCase):
    """Points the module at a throwaway database for each test.

    db_path() re-reads APPLYONCE_DB on every call, so setting it here is enough
    -- but the thread-local connection has to be dropped as well, or the next
    test inherits the previous file's handle.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_file = Path(self._tmp.name) / "test.db"
        self._prev = os.environ.get("APPLYONCE_DB")
        os.environ["APPLYONCE_DB"] = str(self.db_file)
        db.reset_connection()
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        db.reset_connection()
        if self._prev is None:
            os.environ.pop("APPLYONCE_DB", None)
        else:
            os.environ["APPLYONCE_DB"] = self._prev

    def fresh(self, cid, name="Acme", **extra):
        return db.upsert_company(company(cid, name, **extra))


class TestSchemaBootstrap(DbTestCase):
    def test_connect_creates_schema_without_explicit_migrate(self):
        # No migrate() call anywhere in this test on purpose.
        self.fresh("acme")
        self.assertEqual(db.stats()["companies"], 1)

    def test_second_process_sees_the_same_data(self):
        self.fresh("acme")
        db.reset_connection()
        self.assertEqual(db.stats()["companies"], 1)

    def test_migrate_is_idempotent_and_reports_version(self):
        self.assertEqual(db.migrate(), db.SCHEMA_VERSION)
        self.assertEqual(db.migrate(), db.SCHEMA_VERSION)

    def test_foreign_keys_cascade_child_rows(self):
        self.fresh("acme")
        db.upsert_founder(key("acme"), {"name": "Ada", "email": ""})
        self.assertEqual(db.stats()["founders"], 1)
        db.clear()
        self.assertEqual(db.stats()["founders"], 0)
        self.assertEqual(db.stats()["companies"], 0)


class TestFlags(DbTestCase):
    """The regression that motivated this file."""

    def test_setting_hidden_preserves_sent_at(self):
        db.set_flag("c1", sent_at=1700000000)
        db.set_flag("c1", hidden=True)
        flags = db.get_flags()["c1"]
        self.assertEqual(flags["sent_at"], 1700000000)
        self.assertEqual(flags["hidden"], 1)

    def test_setting_sent_at_preserves_hidden(self):
        db.set_flag("c1", hidden=True)
        db.set_flag("c1", sent_at=1700000000)
        flags = db.get_flags()["c1"]
        self.assertEqual(flags["sent_at"], 1700000000)
        self.assertEqual(flags["hidden"], 1)

    def test_sent_at_can_be_cleared_without_clearing_hidden(self):
        db.set_flag("c1", sent_at=1700000000, hidden=True)
        db.set_flag("c1", sent_at=0)
        flags = db.get_flags()["c1"]
        self.assertEqual(flags["sent_at"], 0)
        self.assertEqual(flags["hidden"], 1)

    def test_hidden_can_be_cleared_without_clearing_sent_at(self):
        db.set_flag("c1", sent_at=1700000000, hidden=True)
        db.set_flag("c1", hidden=False)
        flags = db.get_flags()["c1"]
        self.assertEqual(flags["sent_at"], 1700000000)
        self.assertEqual(flags["hidden"], 0)

    def test_no_op_update_changes_nothing(self):
        db.set_flag("c1", sent_at=1700000000, hidden=True)
        db.set_flag("c1")
        flags = db.get_flags()["c1"]
        self.assertEqual(flags["sent_at"], 1700000000)
        self.assertEqual(flags["hidden"], 1)

    def test_flags_survive_for_unknown_company(self):
        # Hiding a card must work even if the company row never got imported.
        db.set_flag("ghost", hidden=True)
        self.assertEqual(db.get_flags()["ghost"]["hidden"], 1)


class TestExportImport(DbTestCase):
    def test_active_batch_is_exported_and_never_blank(self):
        # The UI renders only state.activeBatch, so "" here looks like an empty
        # database even when every company is present.
        for name in ("Fall 2026", "Spring 2026"):
            self.fresh(name.split()[0].lower(), name)
        blob = db.export_state()
        self.assertIn(blob["activeBatch"], blob["batches"])
        self.assertNotEqual(blob["activeBatch"], "")

    def test_round_trip_into_a_separate_database(self):
        self.fresh("acme", "Acme Corp")
        self.fresh("beta", "Beta Labs")
        db.upsert_founder(key("acme"), {"name": "Ada Lovelace"})
        db.set_flag("acme", sent_at=1700000000)
        db.set_flag("beta", hidden=True)
        blob = db.export_state()

        other = Path(self._tmp.name) / "other.db"
        os.environ["APPLYONCE_DB"] = str(other)
        db.reset_connection()
        counters = db.import_state(blob)

        self.assertEqual(counters["companies"], 2)
        self.assertEqual(counters["skipped"], 0)
        restored = db.export_state()
        self.assertEqual(restored["sent"], blob["sent"])
        self.assertEqual(restored["hidden"], blob["hidden"])
        self.assertEqual(restored["activeBatch"], blob["activeBatch"])

    def test_import_is_idempotent(self):
        self.fresh("acme")
        self.fresh("beta")
        blob = db.export_state()
        for _ in range(3):
            db.import_state(blob)
        self.assertEqual(db.stats()["companies"], 2)

    def test_replace_wipes_previous_companies(self):
        self.fresh("acme")
        db.import_state(db.export_state(), replace=True)
        self.assertEqual(db.stats()["companies"], 1)

    def test_unicode_names_survive(self):
        self.fresh("rs", "Pavlović & Sons")
        self.assertEqual(db.export_state()["batches"]["Fall 2026"]["companies"]
                         ["rs"]["name"], "Pavlović & Sons")

    def test_apify_token_is_never_persisted(self):
        blob = db.export_state()
        self.assertEqual(blob["apify"], {})
        db.import_state({
            "details": {"my_name": "Ada"},
            "apify": {"token": "apify_supersecret"},
            "batches": {},
        })
        self.assertEqual(db.all_settings().get("apify.token"), None)
        self.assertNotIn("apify_supersecret", json.dumps(db.export_state()))

    def test_import_rejects_non_object_state(self):
        with self.assertRaises(db.UpstreamError):
            db.import_state(["not", "a", "dict"])

    def test_import_rejects_missing_batches(self):
        with self.assertRaises(db.UpstreamError):
            db.import_state({"details": {}})


class TestQueueAndUnsubscribes(DbTestCase):
    def test_enqueue_is_idempotent_per_company_and_address(self):
        self.fresh("acme")
        cid = key("acme")
        item = {"company_id": cid, "email": "ada@acme.com",
                "founder_name": "Ada", "subject": "hi", "body": "there"}
        self.assertEqual(db.enqueue([item]), 1)
        self.assertEqual(db.enqueue([item]), 0, "double click must not double send")
        self.assertEqual(db.stats()["queue"], 1)

    def test_unsubscribe_blocks_pending_queue_items(self):
        self.fresh("acme")
        cid = key("acme")
        db.enqueue([{"company_id": cid, "email": "Ada@Acme.com",
                     "subject": "s", "body": "b"}])
        self.assertTrue(db.unsubscribe("ada@acme.com"))
        self.assertTrue(db.is_unsubscribed("ADA@acme.com"))
        pending = [q for q in db.list_queue() if q["status"] == "draft"]
        self.assertEqual(pending, [], "unsubscribe must cancel what was waiting")

    def test_unsubscribe_is_case_insensitive_and_repeatable(self):
        self.assertTrue(db.unsubscribe("Ada@Acme.com"))
        self.assertTrue(db.unsubscribe("ada@ACME.com"))
        self.assertEqual(len(db.list_unsubscribed()), 1)

    def test_unsubscribe_rejects_empty_address(self):
        self.assertFalse(db.unsubscribe("   "))


class TestConcurrency(DbTestCase):
    def test_parallel_writers_do_not_lose_rows(self):
        errors = []

        def worker(n):
            try:
                for i in range(10):
                    db.upsert_company(company(f"c{n}-{i}", f"Co {n}-{i}"))
            except Exception as exc:  # pragma: no cover - only on failure
                errors.append(exc)
            finally:
                # Each thread gets its own connection, and only the thread that
                # opened it can close it. serve.py does this in finish(); any
                # other caller has to do the same or leak a handle per thread.
                db.reset_connection()

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(db.stats()["companies"], 40)

    def test_a_worker_thread_does_not_disturb_the_main_thread(self):
        # reset_connection() must only touch the calling thread's handle.
        self.fresh("acme")

        def worker():
            db.upsert_company(company("from-worker"))
            db.reset_connection()

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertEqual(db.stats()["companies"], 2)


class TestFootprintReporting(DbTestCase):
    def test_footprint_counts_the_wal(self):
        # In WAL mode the main file can stay one page while every row lives in
        # the sidecar, so reporting only the main file makes a populated
        # database look empty.
        self.fresh("acme")
        foot = db._footprint(db.db_path())
        self.assertTrue(foot["exists"])
        self.assertGreater(foot["total_bytes"], 0)
        self.assertEqual(
            foot["total_bytes"], foot["file_bytes"] + foot["wal_bytes"])

    def test_footprint_of_missing_file_is_all_zero(self):
        foot = db._footprint(Path(self._tmp.name) / "nope.db")
        self.assertFalse(foot["exists"])
        self.assertEqual(foot["total_bytes"], 0)


class TestQueueApi(DbTestCase):
    """The endpoints index.html drives. A mismatch here is a broken button,
    so these are pinned rather than left to the mailer's own tests."""

    def seed(self, n=3):
        for i in range(n):
            db.upsert_company(company(f"c{i}", f"Co {i}"))
            db.enqueue([{"company_id": f"c{i}", "email": f"a{i}@co{i}.com",
                         "subject": f"s{i}", "body": f"b{i}"}])

    def get(self, query):
        return db.handle_request(query, {})

    def post(self, body):
        return db.handle_request({}, body)

    def test_queue_list_is_wrapped_and_defaults_newest_first(self):
        self.seed(3)
        status, payload = self.get({"action": "queue_list"})
        self.assertEqual(status, 200)
        rows = payload["queue"]
        self.assertEqual(len(rows), 3)
        self.assertGreater(rows[0]["id"], rows[-1]["id"])

    def test_queue_list_honours_limit_and_status(self):
        self.seed(4)
        db.approve([r["id"] for r in db.list_queue("draft")[:2]])
        status, payload = self.get({"action": "queue_list", "status": "draft",
                                    "limit": "1"})
        self.assertEqual(status, 200)
        rows = payload["queue"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "draft")

    def test_queue_list_oldest_first(self):
        self.seed(3)
        _, newest = self.get({"action": "queue_list"})
        _, oldest = self.get({"action": "queue_list", "oldest_first": "1"})
        self.assertEqual([r["id"] for r in oldest["queue"]],
                         list(reversed([r["id"] for r in newest["queue"]])))

    def test_queue_status_accepts_a_batch_of_ids(self):
        # The "approve all drafts" button sends the whole list in one request,
        # so a partial application would be a real bug, not a cosmetic one.
        self.seed(3)
        ids = [r["id"] for r in db.list_queue("draft")]
        status, payload = self.post({"action": "queue_status", "ids": ids,
                                     "status": "approved"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["changed"], 3)
        self.assertEqual(len(db.list_queue("approved")), 3)

    def test_queue_status_batch_records_the_skip_reason(self):
        self.seed(1)
        queue_id = db.list_queue("draft")[0]["id"]
        self.post({"action": "queue_status", "ids": [queue_id],
                   "status": "skipped", "error": "not a real founder"})
        row = db.list_queue("skipped")[0]
        self.assertEqual(row["error"], "not a real founder")

    def test_queue_status_still_takes_a_single_id(self):
        self.seed(1)
        queue_id = db.list_queue("draft")[0]["id"]
        status, payload = self.post({"action": "queue_status", "id": queue_id,
                                     "status": "approved"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["changed"], 1)

    def test_queue_status_without_an_id_is_a_client_error(self):
        with self.assertRaises(db.UpstreamError):
            self.post({"action": "queue_status", "status": "approved"})

    def test_queue_status_rejects_an_unknown_status(self):
        self.seed(1)
        queue_id = db.list_queue("draft")[0]["id"]
        with self.assertRaises(db.UpstreamError) as ctx:
            self.post({"action": "queue_status", "ids": [queue_id],
                       "status": "yolo"})
        self.assertEqual(ctx.exception.status, 400)

    def test_an_empty_id_batch_is_a_no_op(self):
        status, payload = self.post({"action": "queue_status", "ids": [],
                                     "status": "approved"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["changed"], 0)


class TestHandleRequest(DbTestCase):
    def post(self, body):
        return db.handle_request({}, body)

    def test_status_reports_capability_and_stats(self):
        self.fresh("acme")
        status, payload = self.post({"action": "status"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["stats"]["companies"], 1)
        self.assertIn("total_bytes", payload)

    def test_import_reports_counters(self):
        self.fresh("acme")
        status, payload = self.post({
            "action": "import",
            "state": {"activeBatch": "Fall 2026",
                      "batches": {"Fall 2026": {"companies": {"x": company("zed")}}}}})
        self.assertEqual(status, 200)
        self.assertEqual(payload["imported"]["companies"], 1)

    def test_unknown_action_is_rejected(self):
        # Raises rather than returning a tuple: serve.py is what turns this
        # into an HTTP status, so db.py has no opinion about status codes.
        with self.assertRaises(db.UpstreamError) as ctx:
            self.post({"action": "drop_tables"})
        self.assertEqual(ctx.exception.status, 400)

    def test_import_without_state_is_a_client_error(self):
        with self.assertRaises(db.UpstreamError) as ctx:
            self.post({"action": "import"})
        self.assertEqual(ctx.exception.status, 400)

    def test_clear_keeps_settings_and_templates(self):
        # The sender identity is expensive to retype and is not research output.
        db.set_setting("sender.my_name", "Ada")
        self.fresh("acme")
        status, _ = self.post({"action": "clear"})
        self.assertEqual(status, 200)
        self.assertEqual(db.stats()["companies"], 0)
        self.assertEqual(db.get_setting("sender.my_name"), "Ada")


if __name__ == "__main__":
    unittest.main()