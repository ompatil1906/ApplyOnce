"""Tests for mailer.py.

Standard library only. Nothing here can reach a real inbox: every test that
transmits points SmtpConfig at tests/fakesmtp.py, a throwaway server bound to
127.0.0.1 on an ephemeral port.

The tests concentrate on the things that are expensive to get wrong in
production and invisible in a demo:

  * A dry run must be the default, because `send` without --send must not
    transmit.
  * Screening happens *before* the network is touched, so a dry run reports
    exactly the set a real run would attempt.
  * The daily cap and the unsubscribes table are checked per recipient, so a
    refusal that lands mid-queue still stops what is behind it.
  * Claiming rows is atomic, so two concurrent runs cannot double-send.
  * Credentials are never accepted from a command-line flag, and never leave
    the process in the summary that gets printed.

Run with:

    python3 -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import time
import unittest
from email import message_from_string
from email.policy import default as DEFAULT_POLICY
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import db  # noqa: E402
import mailer  # noqa: E402
from fakesmtp import FakeSmtpServer  # noqa: E402


def config_for(server=None, **overrides) -> mailer.SmtpConfig:
    """A config that satisfies every validation rule and points at the fake."""
    cfg = mailer.SmtpConfig(
        host=server.host if server else "127.0.0.1",
        port=server.port if server else 2525,
        username="sender@example.com",
        password="app-password-not-a-real-one",
        sender="sender@example.com",
        sender_name="A Real Person",
        physical_address="1 Example Street, Springfield",
        daily_limit=50,
        delay=0.0,          # no waiting in tests
        retries=0,          # no retry sleeps in tests
        timeout=10.0,
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


class MailerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._prev = os.environ.get("APPLYONCE_DB")
        os.environ["APPLYONCE_DB"] = str(Path(self._tmp.name) / "mail.db")
        db.reset_connection()
        self.addCleanup(self._restore)
        # Screening reads env flags; clear them so the developer's shell cannot
        # change what these tests assert.
        for name in ("MAILER_ALLOW_CATCH_ALL", "MAILER_SELF_TEST"):
            self._prev_flag = os.environ.pop(name, None)
        self.addCleanup(self._restore_flags)

    def _restore(self):
        db.reset_connection()
        if self._prev is None:
            os.environ.pop("APPLYONCE_DB", None)
        else:
            os.environ["APPLYONCE_DB"] = self._prev

    def _restore_flags(self):
        for name in ("MAILER_ALLOW_CATCH_ALL", "MAILER_SELF_TEST"):
            os.environ.pop(name, None)
        if getattr(self, "_prev_flag", None) is not None:
            os.environ["MAILER_ALLOW_CATCH_ALL"] = self._prev_flag

    def queue(self, address="ada@acme.com", *, company="acme", subject="Quick question",
              body="Hi Ada,\n\nSaw your work.\n\nAda", approve=True):
        """Put one row through to 'approved', the only state send will take."""
        db.upsert_company({"id": company, "slug": company, "name": company.title(),
                           "domain": f"{company}.com", "batch": "Fall 2026"})
        db.enqueue([{"company_id": company, "email": address,
                     "founder_name": "Ada", "subject": subject, "body": body}])
        conn = db.connect()
        with db.write(conn):
            conn.execute("UPDATE queue SET status='approved', approved=1 "
                         "WHERE company_id=?", (company,))
        return company

    def rows(self, status=None):
        return db.list_queue(status or "")


class TestConfigValidation(MailerTestCase):
    def test_minimal_valid_config_has_no_problems(self):
        self.assertEqual(config_for().problems(), [])

    def test_missing_everything_is_reported_together(self):
        issues = mailer.SmtpConfig().problems()
        self.assertTrue(any("SMTP_HOST" in i for i in issues))
        self.assertTrue(any("SMTP_FROM" in i for i in issues))
        self.assertTrue(any("SMTP_PHYSICAL_ADDRESS" in i for i in issues))

    def test_account_password_with_no_app_password_is_a_problem(self):
        cfg = config_for(password="")
        self.assertTrue(any("app password" in i for i in cfg.problems()))

    def test_anonymous_from_is_a_problem(self):
        cfg = config_for(sender_name="")
        self.assertTrue(any("SMTP_FROM_NAME" in i for i in cfg.problems()))

    def test_plaintext_unsubscribe_url_is_rejected(self):
        cfg = config_for(unsubscribe_base="http://example.com/u")
        self.assertTrue(any("https" in i for i in cfg.problems()))

    def test_out_of_range_port_is_flagged(self):
        cfg = config_for(port=0)
        self.assertTrue(any("SMTP_PORT" in i for i in cfg.problems()))
        # 25 and 2525 are real relays; refusing them would be paternalistic
        self.assertEqual(config_for(port=2525).problems(), [])

    def test_public_summary_never_leaks_the_password(self):
        cfg = config_for()
        blob = str(cfg.public_summary())
        self.assertNotIn(cfg.password, blob)
        self.assertTrue(cfg.public_summary()["password_present"])

    def test_implicit_tls_only_on_465(self):
        self.assertTrue(config_for(port=465).use_implicit_tls)
        self.assertFalse(config_for(port=587).use_implicit_tls)

    def test_require_raises_when_not_ready(self):
        with self.assertRaises(mailer.MailerError):
            mailer.SmtpConfig().require()


class TestScreening(MailerTestCase):
    def test_unsubscribed_address_is_refused(self):
        db.unsubscribe("ada@acme.com")
        with self.assertRaises(mailer.Suppressed) as ctx:
            mailer.screen({"email": "ada@acme.com", "subject": "s", "body": "b"},
                          config_for())
        self.assertEqual(ctx.exception.reason, "unsubscribed")

    def test_unsubscribe_check_is_case_insensitive(self):
        db.unsubscribe("Ada@Acme.com")
        with self.assertRaises(mailer.Suppressed):
            mailer.screen({"email": "ada@ACME.com", "subject": "s", "body": "b"},
                          config_for())

    def test_role_addresses_are_refused(self):
        for address in ("info@acme.com", "support@acme.com", "noreply@acme.com"):
            with self.assertRaises(mailer.Suppressed) as ctx:
                mailer.screen({"email": address, "subject": "s", "body": "b"},
                              config_for())
            self.assertEqual(ctx.exception.reason, "role_address")

    def test_unrendered_placeholder_is_refused(self):
        # The failure mode that matters: "{first_name}" reaching a real inbox.
        with self.assertRaises(mailer.Suppressed) as ctx:
            mailer.screen({"email": "ada@acme.com", "subject": "{first_name}",
                           "body": "Hi {first_name}"}, config_for())
        self.assertEqual(ctx.exception.reason, "unrendered_placeholder")

    def test_malformed_address_is_refused(self):
        with self.assertRaises(mailer.Suppressed) as ctx:
            mailer.screen({"email": "not-an-address", "subject": "s", "body": "b"},
                          config_for())
        self.assertEqual(ctx.exception.reason, "malformed")

    def test_empty_subject_or_body_is_refused(self):
        for row, reason in (({"email": "a@b.com", "subject": "", "body": "b"}, "no_subject"),
                            ({"email": "a@b.com", "subject": "s", "body": "  "}, "no_body")):
            with self.assertRaises(mailer.Suppressed) as ctx:
                mailer.screen(row, config_for())
            self.assertEqual(ctx.exception.reason, reason)

    def test_healthy_row_passes(self):
        mailer.screen({"email": "ada@acme.com", "subject": "Hi", "body": "Hello Ada"},
                      config_for())  # must not raise

    def test_catch_all_domain_is_refused_unless_overridden(self):
        db.upsert_company({"id": "big", "slug": "big", "name": "Big",
                           "domain": "big.com", "batch": "Fall 2026"})
        db.upsert_email("big", "x@big.com", is_catch_all=True)
        row = {"email": "anyone@big.com", "subject": "s", "body": "b"}
        with self.assertRaises(mailer.Suppressed) as ctx:
            mailer.screen(row, config_for())
        self.assertEqual(ctx.exception.reason, "catch_all")
        os.environ["MAILER_ALLOW_CATCH_ALL"] = "1"
        mailer._CATCH_ALL_CACHE.clear()
        mailer.screen(row, config_for())  # must not raise when allowed


class TestMessageBuilding(MailerTestCase):
    def build(self, row=None, **overrides):
        row = row or {"email": "ada@acme.com", "subject": "Quick question",
                      "body": "Hi Ada", "company": "Acme"}
        return mailer.build_message(row, config_for(**overrides), queue_id=7)

    def test_required_headers(self):
        message = self.build()
        self.assertEqual(message["To"], "ada@acme.com")
        self.assertIn("A Real Person", message["From"])
        self.assertIn("sender@example.com", message["From"])
        self.assertTrue(message["Message-ID"])
        self.assertTrue(message["Date"])

    def test_reply_to_is_the_sender_not_a_bounce_address(self):
        self.assertEqual(self.build()["Reply-To"], "sender@example.com")

    def test_bulk_precedence_and_auto_submitted_are_declared(self):
        message = self.build()
        self.assertEqual(message["Precedence"], "bulk")
        self.assertEqual(message["Auto-Submitted"], "auto-generated")

    def test_footer_includes_the_physical_address(self):
        body = self.build().get_body(preferencelist=("plain",)).get_content()
        self.assertIn("1 Example Street", body)

    def test_footer_contains_an_unsubscribe_instruction(self):
        body = self.build().get_body(preferencelist=("plain",)).get_content()
        self.assertIn("unsubscribe", body.lower())

    def test_list_unsubscribe_header_present_for_http_base(self):
        message = self.build(unsubscribe_base="https://apply.example/u")
        self.assertIn("List-Unsubscribe", message)
        self.assertEqual(message["List-Unsubscribe-Post"],
                         "List-Unsubscribe=One-Click")

    def test_no_one_click_header_without_a_hosted_endpoint(self):
        # Claiming one-click with no endpoint to honour it would be a lie.
        message = self.build()
        self.assertIn("List-Unsubscribe", message)
        self.assertIsNone(message["List-Unsubscribe-Post"])

    def test_template_placeholder_is_substituted(self):
        message = self.build({"email": "ada@acme.com", "subject": "s",
                              "body": "stop me: {unsubscribe}"})
        body = message.get_body(preferencelist=("plain",)).get_content()
        self.assertNotIn("{unsubscribe}", body)
        self.assertIn("mailto:", body)

    def test_mailto_unsubscribe_link_is_properly_encoded(self):
        # An unencoded "subject=unsubscribe" becomes "subject=3Dunsubscribe"
        # and the mail client reads one malformed parameter instead of two.
        from urllib.parse import parse_qs, urlparse
        cfg = config_for(sender_name="A", physical_address="addr")
        url = mailer.unsubscribe_url("ada+tag@acme.com", cfg)
        self.assertNotIn("%3D", url)
        params = parse_qs(urlparse(url).query)
        self.assertEqual(params["subject"], ["unsubscribe"])
        self.assertEqual(params["body"], ["Unsubscribe ada+tag@acme.com"])

    def test_https_unsubscribe_url_is_not_encoded(self):
        cfg = config_for(unsubscribe_base="https://apply.example/u")
        url = mailer.unsubscribe_url("ada@acme.com", cfg)
        self.assertEqual(url, "https://apply.example/u/unsubscribe?email=ada@acme.com")

    def test_footer_is_not_doubled_when_the_template_has_one(self):
        row = {"email": "ada@acme.com", "subject": "s",
               "body": "Hi\n\n--\nYou are receiving this because you asked."}
        body = mailer.build_message(row, config_for()).get_body(
            preferencelist=("plain",)).get_content()
        self.assertEqual(body.lower().count("you are receiving this because"), 1)

    def test_message_id_is_stable_across_rebuilds(self):
        self.assertEqual(str(self.build()["Message-ID"]),
                         str(self.build()["Message-ID"]))

    def test_message_id_changes_when_the_draft_changes(self):
        row = {"email": "ada@acme.com", "subject": "s", "body": "original"}
        other = {"email": "ada@acme.com", "subject": "s", "body": "edited"}
        first = str(mailer.build_message(row, config_for(), queue_id=7)["Message-ID"])
        second = str(mailer.build_message(other, config_for(), queue_id=7)["Message-ID"])
        self.assertNotEqual(first, second)

    def test_body_is_plain_text_only(self):
        message = self.build()
        self.assertEqual(message.get_content_type(), "text/plain")


class TestDryRunAndCaps(MailerTestCase):
    def test_dry_run_is_the_default_and_sends_nothing(self):
        self.queue()
        with FakeSmtpServer() as server:
            summary = mailer.run(config=config_for(server), limit=10, dry_run=True)
            self.assertTrue(summary["dry_run"])
            self.assertEqual(server.messages, [])
        self.assertEqual(summary["results"][0]["status"], "would_send")
        self.assertEqual(self.rows("sent"), [])

    def test_dry_run_reports_the_same_set_a_real_run_would_use(self):
        self.queue("ada@acme.com", company="acme")
        db.upsert_company({"id": "beta", "slug": "beta", "name": "Beta",
                           "domain": "beta.com", "batch": "Fall 2026"})
        db.enqueue([{"company_id": "beta", "email": "info@beta.com",
                     "subject": "s", "body": "b"}])
        conn = db.connect()
        with db.write(conn):
            conn.execute("UPDATE queue SET status='approved'")
        with FakeSmtpServer() as server:
            dry = mailer.run(config=config_for(server), limit=10, dry_run=True)
        would = {r["email"] for r in dry["results"] if r["status"] == "would_send"}
        suppressed = {r["email"] for r in dry["results"] if r["status"] == "suppressed"}
        self.assertEqual(would, {"ada@acme.com"})
        self.assertEqual(suppressed, {"info@beta.com"})

    def test_only_approved_rows_are_eligible(self):
        self.queue()
        conn = db.connect()
        with db.write(conn):
            conn.execute("UPDATE queue SET status='draft'")
        with FakeSmtpServer() as server:
            summary = mailer.run(config=config_for(server), limit=10, dry_run=True)
        self.assertEqual(summary["considered"], 0)

    def test_daily_cap_stops_the_batch(self):
        self.queue("a1@acme.com", company="c1")
        self.queue("a2@acme.com", company="c2")
        with FakeSmtpServer() as server:
            summary = mailer.run(config=config_for(server, daily_limit=1),
                                 limit=10, dry_run=True)
        would = [r for r in summary["results"] if r["status"] == "would_send"]
        capped = [r for r in summary["results"] if r.get("reason") == "daily_cap"]
        self.assertEqual(len(would), 1)
        self.assertEqual(len(capped), 1)

    def test_daily_cap_counts_what_was_already_sent_today(self):
        self.queue("a1@acme.com", company="c1")
        conn = db.connect()
        with db.write(conn):
            conn.execute("UPDATE queue SET status='sent', sent_at=? WHERE company_id='c1'",
                         (int(time.time()),))
        self.queue("a2@acme.com", company="c2")
        with FakeSmtpServer() as server:
            summary = mailer.run(config=config_for(server, daily_limit=1),
                                 limit=10, dry_run=True)
        self.assertEqual(summary["already_sent_today"], 1)
        self.assertEqual([r for r in summary["results"]
                          if r["status"] == "would_send"], [])

    def test_suppressed_rows_are_marked_skipped_with_a_reason(self):
        db.unsubscribe("ada@acme.com")
        self.queue()
        with FakeSmtpServer() as server:
            mailer.run(config=config_for(server), limit=10, dry_run=True)
        row = self.rows("skipped")
        self.assertEqual(len(row), 1)
        self.assertIn("unsubscribed", row[0]["error"])


class TestRealDelivery(MailerTestCase):
    """End-to-end against the fake relay, over a real socket."""

    def test_sends_an_approved_row(self):
        self.queue()
        with FakeSmtpServer() as server:
            summary = mailer.run(config=config_for(server), limit=10, dry_run=False)
            self.assertEqual(summary["sent"], 1)
            self.assertEqual(len(server.messages), 1)
            received = server.messages[0]["data"]
        self.assertIn("Subject: Quick question", received)
        self.assertIn("1 Example Street", received)
        self.assertEqual(len(self.rows("sent")), 1)

    def test_envelope_recipient_is_the_queued_address(self):
        self.queue("ada@acme.com")
        with FakeSmtpServer() as server:
            mailer.run(config=config_for(server), limit=10, dry_run=False)
            self.assertIn("ada@acme.com", server.messages[0]["rcpt"][0])

    def test_message_id_is_recorded_against_the_row(self):
        self.queue()
        with FakeSmtpServer() as server:
            summary = mailer.run(config=config_for(server), limit=10, dry_run=False)
        row = self.rows("sent")[0]
        self.assertTrue(row["message_id"])
        self.assertEqual(row["message_id"], summary["results"][0]["message_id"])

    def test_delivery_is_logged(self):
        self.queue()
        with FakeSmtpServer() as server:
            mailer.run(config=config_for(server), limit=10, dry_run=False)
        events = [d["event"] for d in db.connect().execute(
            "SELECT event FROM deliveries ORDER BY id").fetchall()]
        self.assertIn("sent", events)

    def test_unsubscribed_row_is_never_transmitted(self):
        db.unsubscribe("ada@acme.com")
        self.queue()
        with FakeSmtpServer() as server:
            mailer.run(config=config_for(server), limit=10, dry_run=False)
            self.assertEqual(server.messages, [], "an opted-out address went out")

    def test_rejected_message_is_marked_failed_not_sent(self):
        self.queue()
        with FakeSmtpServer() as server:
            server.reject_next = 1
            summary = mailer.run(config=config_for(server), limit=10, dry_run=False)
            self.assertEqual(server.messages, [])
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(self.rows("sent"), [])
        self.assertEqual(len(self.rows("failed")), 1)

    def test_two_runs_cannot_send_the_same_row_twice(self):
        # The claim is the guarantee. Without it, a double-click sends twice.
        self.queue()
        with FakeSmtpServer() as server:
            first = mailer.run(config=config_for(server), limit=10, dry_run=False)
            second = mailer.run(config=config_for(server), limit=10, dry_run=False)
            delivered = len(server.messages)
        self.assertEqual(first["sent"], 1)
        self.assertEqual(second["sent"], 0)
        self.assertEqual(delivered, 1)

    def test_authentication_is_attempted_when_configured(self):
        self.queue()
        with FakeSmtpServer(require_auth=True) as server:
            summary = mailer.run(config=config_for(server), limit=10, dry_run=False)
            self.assertEqual(summary["sent"], 1)

    def test_bad_credentials_fail_the_batch_and_release_the_rows(self):
        self.queue()
        with FakeSmtpServer(require_auth=True) as server:
            server.fail_auth = True
            summary = mailer.run(config=config_for(server), limit=10, dry_run=False)
            self.assertEqual(server.messages, [])
        self.assertIn("error", summary)
        # Rows must not be left stranded in 'sending'.
        self.assertEqual(self.rows("approved"), [] if False else self.rows("approved"))
        self.assertEqual(len(self.rows("approved")), 1)

    def test_starttls_upgrade_is_used_when_advertised(self):
        with tempfile.TemporaryDirectory() as tmp:
            made = FakeSmtpServer.make_cert(tmp)
            if not made:
                self.skipTest("openssl not available to generate a test cert")
            cert, key = made
            self.queue()
            # Point the session at a client that trusts our throwaway cert.
            original = mailer.ssl.create_default_context
            mailer.ssl.create_default_context = lambda *a, **k: original(cafile=cert)
            self.addCleanup(lambda: setattr(mailer.ssl, "create_default_context", original))
            with FakeSmtpServer(advertise_starttls=True, certfile=cert, keyfile=key) as server:
                summary = mailer.run(config=config_for(server), limit=10, dry_run=False)
                self.assertEqual(summary["sent"], 1)
                self.assertEqual(len(server.messages), 1)


class TestQueueMechanics(MailerTestCase):
    def test_claim_is_exclusive(self):
        self.queue()
        ids = [r["id"] for r in self.rows("approved")]
        self.assertEqual(mailer.claim(ids), ids)
        self.assertEqual(mailer.claim(ids), [], "a claimed row must not be claimable again")

    def test_release_returns_rows_to_approved(self):
        self.queue()
        ids = [r["id"] for r in self.rows("approved")]
        mailer.claim(ids)
        self.assertEqual(mailer.release(ids), len(ids))
        self.assertEqual(len(self.rows("approved")), len(ids))

    def test_claim_ignores_rows_that_are_not_approved(self):
        self.queue()
        ids = [r["id"] for r in self.rows("approved")]
        conn = db.connect()
        with db.write(conn):
            conn.execute("UPDATE queue SET status='draft'")
        self.assertEqual(mailer.claim(ids), [])

    def test_reap_recovers_rows_stranded_in_sending(self):
        self.queue()
        ids = [r["id"] for r in self.rows("approved")]
        mailer.claim(ids)
        self.assertEqual(self.rows("approved"), [])
        # Backdate so the stuck-row window has elapsed.
        conn = db.connect()
        with db.write(conn):
            conn.execute("UPDATE queue SET created_at=? WHERE id=?",
                         (int(time.time()) - 7200, ids[0]))
        self.assertEqual(mailer.reap_stuck(), 1)
        self.assertEqual(len(self.rows("approved")), 1)

    def test_reap_leaves_a_fresh_sending_row_alone(self):
        self.queue()
        ids = [r["id"] for r in self.rows("approved")]
        mailer.claim(ids)
        self.assertEqual(mailer.reap_stuck(), 0)
        self.assertEqual(self.rows("approved"), [])

    def test_today_start_is_local_midnight(self):
        start = mailer.today_start()
        self.assertLessEqual(start, int(time.time()))
        self.assertLess(int(time.time()) - start, 86400)


class TestRetryClassification(MailerTestCase):
    def test_4xx_is_retryable(self):
        import smtplib
        self.assertTrue(mailer._is_retryable(
            smtplib.SMTPResponseException(451, b"try later")))
        self.assertTrue(mailer._is_retryable(smtplib.SMTPServerDisconnected("gone")))

    def test_5xx_is_not_retryable(self):
        import smtplib
        self.assertFalse(mailer._is_retryable(
            smtplib.SMTPResponseException(550, b"no such user")))

    def test_connection_errors_are_retryable(self):
        self.assertTrue(mailer._is_retryable(ConnectionResetError()))
        self.assertTrue(mailer._is_retryable(TimeoutError()))

    def test_deferred_450_is_retried_then_succeeds(self):
        self.queue()
        with FakeSmtpServer() as server:
            server.defer_reject = 1
            config = config_for(server, retries=1)
            original_sleep = mailer.time.sleep
            mailer.time.sleep = lambda _s: None
            self.addCleanup(lambda: setattr(mailer.time, "sleep", original_sleep))
            summary = mailer.run(config=config, limit=10, dry_run=False)
            self.assertEqual(summary["sent"], 1)
            self.assertEqual(len(server.messages), 1)

    def test_permanent_failure_does_not_retry(self):
        self.queue()
        with FakeSmtpServer() as server:
            server.reject_next = 5   # would fail every attempt if retried
            original_sleep = mailer.time.sleep
            mailer.time.sleep = lambda _s: None
            self.addCleanup(lambda: setattr(mailer.time, "sleep", original_sleep))
            config = config_for(server, retries=3)
            summary = mailer.run(config=config, limit=10, dry_run=False)
            self.assertEqual(summary["failed"], 1)


class TestCliSurface(MailerTestCase):
    """The CLI reads the environment, so these set it explicitly."""

    def setUp(self):
        super().setUp()
        import contextlib
        from fakesmtp import FakeSmtpServer as _S
        self.server = _S()
        self.addCleanup(self.server.close)
        self.env = {
            "SMTP_HOST": self.server.host,
            "SMTP_PORT": str(self.server.port),
            "SMTP_USERNAME": "sender@example.com",
            "SMTP_APP_PASSWORD": "app-password-not-a-real-one",
            "SMTP_FROM": "sender@example.com",
            "SMTP_FROM_NAME": "A Real Person",
            "SMTP_PHYSICAL_ADDRESS": "1 Example Street, Springfield",
            "SMTP_DELAY_SECONDS": "0",
            "SMTP_RETRIES": "0",
        }
        for key, value in self.env.items():
            self._saved = getattr(self, "_saved", {})
            self._saved[key] = os.environ.get(key)
            os.environ[key] = value

        def restore():
            for key, old in self._saved.items():
                if old is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old
        self.addCleanup(restore)

    def test_help_does_not_offer_a_password_flag(self):
        # A --password argument is the single easiest way to leak a credential
        # into shell history and ps. Assert it stays absent.
        source = (ROOT / "mailer.py").read_text(encoding="utf-8")
        self.assertNotIn('"--password"', source)
        self.assertNotIn("'--password'", source)

    def test_send_without_send_flag_is_a_dry_run(self):
        self.queue()
        import io
        import contextlib
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = mailer.main(["send", "--limit", "5"])
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("dry run", output.lower())
        self.assertEqual(self.rows("sent"), [])

    def test_check_reports_ready_when_environment_is_complete(self):
        import io
        import contextlib
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = mailer.main(["check"])
        output = buffer.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("ready", output)
        self.assertNotIn("app-password-not-a-real-one", output,
                         "the password must never be printed")

    def test_check_reports_missing_configuration(self):
        import io
        import contextlib
        saved = os.environ.pop("SMTP_HOST")
        self.addCleanup(lambda: os.environ.__setitem__("SMTP_HOST", saved))
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = mailer.main(["check"])
        self.assertEqual(code, 1)
        self.assertIn("SMTP_HOST", buffer.getvalue())

    def test_unknown_command_is_rejected_by_argparse(self):
        import contextlib
        import io
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            with self.assertRaises(SystemExit) as ctx:
                mailer.main(["frobnicate"])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()