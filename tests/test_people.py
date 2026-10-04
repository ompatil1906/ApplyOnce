"""Tests for people.py.

The judgement rules are the whole point of the module, so they are pinned
individually rather than tested through a single end-to-end score. A scoring
change that silently flips a band should fail a named test, not quietly reorder
a table.

Two properties matter more than any particular number, and get the most tests:

  * It never promotes a guess into a person. A pattern-matched address must not
    raise a score, and must not be presented as evidence anyone exists.
  * It never invents confidence, and never lowers or overwrites a score that
    came from stronger evidence elsewhere.

Run with:

    python3 -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
import people  # noqa: E402


class PeopleTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._previous = os.environ.get("APPLYONCE_DB")
        os.environ["APPLYONCE_DB"] = os.path.join(self._dir.name, "people.db")
        db.reset_connection()
        self.addCleanup(self._restore)
        # Most tests build on the default company; without it upsert_founder
        # trips the foreign key.
        self.company()

    def _restore(self):
        db.reset_connection()
        if self._previous is None:
            os.environ.pop("APPLYONCE_DB", None)
        else:
            os.environ["APPLYONCE_DB"] = self._previous
        self._dir.cleanup()

    def company(self, cid="acme", name="Acme Corp", domain="acme.com",
                batch="Fall 2026"):
        db.upsert_company({"id": cid, "slug": cid, "name": name,
                           "domain": domain, "batch": batch})
        return cid

    def founder(self, cid="acme", name="Ada Lovelace", title="CEO", raw=None):
        payload = {"name": name, "title": title}
        if raw is not None:
            payload["raw"] = raw
        return db.upsert_founder(cid, payload)

    def email(self, address, cid="acme", founder_id=None, **kwargs):
        db.upsert_email(cid, address, founder_id=founder_id, **kwargs)


class TestNameJudgement(PeopleTestCase):
    def test_a_plain_name_scores_positive(self):
        result = people.score_person(
            {"name": "Ada Lovelace", "first_name": "Ada",
             "last_name": "Lovelace", "title": "CEO"})
        codes = [r["code"] for r in result["reasons"]]
        self.assertIn("name_usable", codes)

    def test_a_department_label_is_not_a_person(self):
        # This is the shape a team-page scrape produces, and it is the single
        # most common way a "founder" stops being a person.
        for label in ("Team", "Founders", "Staff", "Hiring Team", "Support"):
            with self.subTest(name=label):
                result = people.score_person({"name": label, "title": ""})
                codes = {r["code"] for r in result["reasons"]}
                self.assertIn("name_not_a_person", codes)
                self.assertEqual(result["band"], people.HOLD)

    def test_a_single_token_name_cannot_produce_an_address(self):
        result = people.score_person({"name": "Cher", "title": "Singer"})
        codes = {r["code"] for r in result["reasons"]}
        self.assertIn("name_single_token", codes)

    def test_initials_are_penalised(self):
        # "J. Doe" is a directory artefact, not a person's name.
        result = people.score_person({"name": "J.D.", "title": "CEO"})
        codes = {r["code"] for r in result["reasons"]}
        self.assertIn("name_is_initials", codes)

    def test_digits_in_a_name_are_penalised(self):
        result = people.score_person({"name": "Founder2", "title": "CEO"})
        codes = {r["code"] for r in result["reasons"]}
        self.assertIn("name_has_digits", codes)

    def test_an_absurdly_long_name_is_rejected(self):
        result = people.score_person({"name": "A" * 80, "title": ""})
        codes = {r["code"] for r in result["reasons"]}
        self.assertIn("name_too_long", codes)


class TestTitleJudgement(PeopleTestCase):
    def test_decision_titles_help(self):
        for title in ("CEO", "Co-Founder & CTO", "Founder and President",
                      "Head of Product", "VP Engineering"):
            with self.subTest(title=title):
                result = people.score_person(
                    {"name": "Ada Lovelace", "title": title})
                codes = {r["code"] for r in result["reasons"]}
                self.assertIn("title_decision_maker", codes)

    def test_passive_titles_hurt(self):
        for title in ("Advisor", "Intern", "Board Member", "Angel Investor",
                      "Student"):
            with self.subTest(title=title):
                result = people.score_person(
                    {"name": "Ada Lovelace", "title": title})
                codes = {r["code"] for r in result["reasons"]}
                self.assertIn("title_passive", codes)

    def test_passive_beats_decision_when_a_title_contains_both(self):
        # "Founders' Office Manager" contains "founder" but is not a founder.
        # Letting the substring win would score a tea person above a CEO.
        result = people.score_person(
            {"name": "Ada Lovelace", "title": "Founders' Office Manager"})
        codes = {r["code"] for r in result["reasons"]}
        self.assertIn("title_passive", codes)
        self.assertNotIn("title_decision_maker", codes)

    def test_no_title_is_neutral(self):
        result = people.score_person({"name": "Ada Lovelace", "title": ""})
        passive = [r for r in result["reasons"]
                   if r["code"] in ("title_passive", "title_decision_maker")]
        self.assertEqual(passive, [])


class TestFootprint(PeopleTestCase):
    def test_a_public_profile_is_positive_evidence(self):
        result = people.score_person(
            {"name": "Ada Lovelace", "title": "CEO",
             "raw": {"linkedin": "https://linkedin.com/in/ada"}})
        reasons = {r["code"]: r for r in result["reasons"]}
        self.assertIn("public_profile", reasons)
        self.assertGreater(reasons["public_profile"]["points"], 0)

    def test_the_profile_is_read_out_of_a_serialised_raw_column(self):
        # db stores raw as JSON text, so a dict in memory would not survive a
        # round trip through the database.
        self.founder(raw={"twitter": "https://twitter.com/ada"})
        loaded = people.load_people()
        self.assertEqual(len(loaded), 1)
        self.assertIsInstance(loaded[0]["raw"], str)
        result = people.score_person(loaded[0])
        codes = {r["code"] for r in result["reasons"]}
        self.assertIn("public_profile", codes)

    def test_a_profile_at_the_top_level_is_also_found(self):
        # This is the shape the browser sends: _founder_from_payload puts
        # linkedin/twitter on the founder object itself, which is what ends up
        # in the raw column.
        self.founder(name="Ada Lovelace", title="CEO")
        conn = db.connect()
        conn.execute(
            "UPDATE founders SET raw=? WHERE id=1",
            (people.json.dumps({"name": "Ada Lovelace", "title": "CEO",
                                "linkedin": "https://linkedin.com/in/ada"}),))
        conn.commit()
        result = people.score_person(people.load_people()[0])
        codes = {r["code"] for r in result["reasons"]}
        self.assertIn("public_profile", codes)

    def test_corrupt_raw_json_does_not_crash(self):
        founder_id = self.founder(raw="{not json at all")
        loaded = people.load_people()
        result = people.score_person(loaded[0])
        self.assertIn("no_public_profile",
                      {r["code"] for r in result["reasons"]})


class TestAddressJudgement(PeopleTestCase):
    def test_a_verified_address_is_the_strongest_signal(self):
        founder_id = self.founder(name="Ada Lovelace", title="CEO")
        self.email("ada@acme.com", founder_id=founder_id, source="verified",
                   verified=1)
        result = people.score_person(people.load_people()[0])
        codes = {r["code"] for r in result["reasons"]}
        self.assertIn("address_verified", codes)
        self.assertEqual(result["band"], people.WRITE)

    def test_a_name_matching_address_is_weaker_than_verification(self):
        founder_id = self.founder(name="Ada Lovelace", title="CEO")
        self.email("ada@acme.com", founder_id=founder_id, source="guess")
        result = people.score_person(people.load_people()[0])
        codes = {r["code"] for r in result["reasons"]}
        self.assertIn("address_matches_name", codes)
        self.assertNotIn("address_verified", codes)

    def test_personal_webmail_is_positive_not_negative(self):
        # Counterintuitive but deliberate: somebody who arranged to be
        # personally reachable on webmail is a better signal for cold email
        # than the corporate pattern everyone is forced onto.
        founder_id = self.founder(name="Ada Lovelace", title="CEO")
        self.email("ada.lovelace@gmail.com", founder_id=founder_id,
                   source="unverified")
        result = people.score_person(people.load_people()[0])
        reasons = {r["code"]: r for r in result["reasons"]}
        self.assertIn("address_personal_webmail", reasons)
        self.assertGreater(reasons["address_personal_webmail"]["points"], 0)

    def test_a_catch_all_domain_discounts_a_name_matching_address(self):
        # zeta.cloud accepts every address, so "barbara@zeta.cloud" carrying her
        # name is not evidence that Barbara has a mailbox there.
        self.company("zeta", "Zeta Cloud", "zeta.cloud")
        founder_id = self.founder("zeta", "Barbara Liskov", "CEO")
        self.email("barbara@zeta.cloud", cid="zeta", founder_id=founder_id,
                   source="guess", is_catch_all=1)
        result = people.score_person(people.load_people()[0])
        codes = {r["code"] for r in result["reasons"]}
        self.assertIn("address_catch_all", codes)
        self.assertNotIn("address_matches_name", codes)
        self.assertFalse(result["contactable"])

    def test_a_catch_all_reason_is_always_visible_when_it_decides(self):
        # The bug this pins: the score was correctly capped by the catch-all
        # check but the catch-all never appeared in the reasons, so the printed
        # explanation named the wrong cause.
        self.company("zeta", "Zeta Cloud", "zeta.cloud")
        founder_id = self.founder("zeta", "Barbara Liskov", "CEO")
        self.email("barbara@zeta.cloud", cid="zeta", founder_id=founder_id,
                   is_catch_all=1)
        result = people.score_person(people.load_people()[0])
        self.assertEqual(result["band"], people.HOLD)
        self.assertIn("catch-all", result["summary"])
        self.assertIn("address_catch_all", {r["code"] for r in result["reasons"]})

    def test_a_role_only_mailbox_is_not_a_person(self):
        founder_id = self.founder(name="Ada Lovelace", title="CEO")
        self.email("info@acme.com", founder_id=founder_id, source="site_role",
                   is_role=1)
        result = people.score_person(people.load_people()[0])
        codes = {r["code"] for r in result["reasons"]}
        self.assertIn("address_role_only", codes)
        self.assertNotIn("address_matches_name", codes)

    def test_no_address_at_all_is_negative(self):
        result = people.score_person({"name": "Ada Lovelace", "title": "CEO"})
        codes = {r["code"] for r in result["reasons"]}
        self.assertIn("address_none", codes)


class TestGuessesAreNeverEvidence(PeopleTestCase):
    """The central safety property, so it gets its own class."""

    def test_candidates_are_offered_but_carry_zero_points(self):
        self.founder(name="Ada Lovelace", title="CEO")
        result = people.score_person(people.load_people()[0])
        self.assertTrue(result["candidates"])
        candidate_reasons = [r for r in result["reasons"]
                             if r["code"] == "candidates_available"]
        self.assertEqual(len(candidate_reasons), 1)
        self.assertEqual(candidate_reasons[0]["points"], 0.0)

    def test_generating_candidates_adds_no_points(self):
        # The property that matters: the ladder contributes exactly zero to the
        # arithmetic. Compare the points, not the final score -- having a domain
        # to guess against also makes someone contactable, which lifts the cap
        # and is a separate, intended effect.
        bare = people.score_person(
            {"name": "Ada Lovelace", "first_name": "Ada",
             "last_name": "Lovelace", "title": "CEO"})
        with_guesses = people.score_person(
            {"name": "Ada Lovelace", "first_name": "Ada",
             "last_name": "Lovelace", "title": "CEO",
             "company_domain": "acme.com"})
        self.assertTrue(with_guesses["candidates"])
        self.assertEqual(sum(r["points"] for r in bare["reasons"]),
                         sum(r["points"] for r in with_guesses["reasons"]))

    def test_candidates_do_not_move_someone_into_the_write_band(self):
        # Nobody reaches "write" on the strength of a string template.
        result = people.score_person(
            {"name": "Ada Lovelace", "first_name": "Ada",
             "last_name": "Lovelace", "title": "",
             "company_domain": "acme.com"})
        self.assertTrue(result["candidates"])
        self.assertNotEqual(result["band"], people.WRITE)

    def test_a_department_label_gets_no_candidates(self):
        # "Team" splits cleanly into a single token, so the ladder would happily
        # emit team@, founders@, hello@... Five role mailboxes for a department
        # is worse than no suggestions, since those are the addresses the
        # mailer refuses to send to.
        self.founder(name="Team", title="Engineering")
        result = people.score_person(people.load_people()[0])
        self.assertEqual(result["candidates"], [])

    def test_a_single_token_name_gets_no_candidates(self):
        # No surname means no person-shaped pattern to build.
        self.founder(name="Cher", title="Singer")
        result = people.score_person(people.load_people()[0])
        self.assertEqual(result["candidates"], [])

    def test_an_existing_address_is_not_offered_again(self):
        founder_id = self.founder(name="Ada Lovelace", title="CEO")
        self.email("ada@acme.com", founder_id=founder_id, source="site")
        result = people.score_person(people.load_people()[0])
        self.assertNotIn("ada@acme.com",
                         [c["email"] for c in result["candidates"]])

    def test_candidates_are_capped(self):
        result = people.score_person(
            {"name": "Ada Lovelace", "first_name": "Ada",
             "last_name": "Lovelace", "title": "CEO",
             "company_domain": "acme.com"})
        self.assertLessEqual(len(result["candidates"]), 5)


class TestReachabilityCap(PeopleTestCase):
    def test_someone_unreachable_cannot_score_high(self):
        # A perfect decision-maker at a real company is still not someone you
        # can write to if there is no way to reach them: no address, and no
        # domain to build a candidate from.
        result = people.score_person(
            {"name": "Ada Lovelace", "first_name": "Ada",
             "last_name": "Lovelace", "title": "Co-Founder & CEO",
             "company_domain": "", "raw": {"linkedin": "x"}},
            emails=[])
        self.assertFalse(result["contactable"])
        self.assertLessEqual(result["score"], 0.35)
        self.assertEqual(result["band"], people.HOLD)

    def test_a_person_shaped_candidate_counts_as_a_route(self):
        # The guess ladder is how this project reaches people at all, so a
        # person-shaped candidate is a genuine route even though it is not
        # counted as evidence that the person exists.
        result = people.score_person(
            {"name": "Ada Lovelace", "first_name": "Ada",
             "last_name": "Lovelace", "title": "CEO",
             "company_domain": "acme.com"}, emails=[])
        self.assertTrue(result["contactable"])

    def test_role_shaped_candidates_alone_do_not_count_as_a_route(self):
        result = people.score_person(
            {"name": "Founders", "title": "Company", "company_domain": "a.com"},
            emails=[])
        self.assertEqual(result["candidates"], [])

    def test_a_role_mailbox_is_never_mistaken_for_this_person(self):
        founder_id = self.founder(name="Ada Lovelace", title="CEO")
        self.email("sales@acme.com", founder_id=founder_id, is_role=1)
        result = people.score_person(people.load_people()[0])
        codes = {r["code"] for r in result["reasons"]}
        self.assertIn("address_role_only", codes)
        self.assertNotIn("address_matches_name", codes)
        self.assertNotIn("address_verified", codes)

    def test_a_role_mailbox_with_nothing_else_is_not_a_route(self):
        # No surname, so the ladder cannot build a person-shaped pattern either.
        self.company("zeta", "Zeta", "zeta.com")
        founder_id = self.founder("zeta", "Cher", "Singer")
        self.email("sales@zeta.com", cid="zeta", founder_id=founder_id,
                   is_role=1)
        result = people.score_person(people.load_people()[0])
        self.assertFalse(result["contactable"])


class TestSuppression(PeopleTestCase):
    """An unsubscribed person is not worth less. They are off-limits."""

    def test_an_unsubscribed_address_blocks_contact(self):
        founder_id = self.founder(name="Ada Lovelace", title="CEO")
        self.email("ada@acme.com", founder_id=founder_id, source="verified",
                   verified=1)
        db.unsubscribe("ada@acme.com")
        result = people.score_person(
            people.load_people()[0],
            unsubscribed={"ada@acme.com"})
        self.assertTrue(result["blocked"])
        self.assertEqual(result["band"], people.HOLD)
        # Still scores high: suppression is not a judgement about the person.
        self.assertGreater(result["score"], 0.7)

    def test_a_hidden_company_blocks_contact(self):
        founder_id = self.founder(name="Ada Lovelace", title="CEO")
        self.email("ada@acme.com", founder_id=founder_id, verified=1)
        result = people.score_person(
            people.load_people()[0],
            flags={"acme": {"hidden": 1}})
        self.assertTrue(result["blocked"])
        self.assertIn("hid this company", result["summary"])

    def test_an_already_contacted_company_blocks_contact(self):
        founder_id = self.founder(name="Ada Lovelace", title="CEO")
        self.email("ada@acme.com", founder_id=founder_id, verified=1)
        result = people.score_person(
            people.load_people()[0],
            flags={"acme": {"sent_at": 12345}})
        self.assertTrue(result["blocked"])

    def test_suppression_reasons_are_not_numeric_penalties(self):
        # If these were negative points, a strong person on the unsubscribes
        # list could be averaged back above a threshold by enough other
        # positives, and mailed again.
        result = people.score_person(
            {"name": "Ada Lovelace", "first_name": "Ada",
             "last_name": "Lovelace", "title": "CEO"},
            unsubscribed={"ada@acme.com"})
        for reason in result["reasons"]:
            if reason["code"] in ("unsubscribed", "company_hidden",
                                  "already_contacted"):
                self.assertEqual(reason["points"], 0.0)


class TestOrdering(PeopleTestCase):
    def test_blocked_people_sort_last_even_when_they_score_highest(self):
        self.company("blocked-co", "Blocked Co", "blocked.com")
        blocked = self.founder("blocked-co", "Zoe Blocked", "CEO")
        self.email("zoe@blocked.com", cid="blocked-co", founder_id=blocked,
                   verified=1)
        db.unsubscribe("zoe@blocked.com")

        self.company("ok-co", "Ok Co", "ok.com")
        ok = self.founder("ok-co", "Adam Ok", "CEO")
        self.email("adam@ok.com", cid="ok-co", founder_id=ok, verified=1)

        results = people.score_all()
        self.assertEqual(results[0]["name"], "Adam Ok")
        self.assertEqual(results[-1]["name"], "Zoe Blocked")

    def test_the_table_is_sorted_by_descending_score_within_a_tier(self):
        self.company("a-co", "A Co", "a.com")
        self.founder("a-co", "Low Person", "CEO")
        self.company("b-co", "B Co", "b.com")
        high = self.founder("b-co", "High Person", "CEO")
        self.email("high@b.com", cid="b-co", founder_id=high, verified=1)

        results = people.score_all()
        scores = [r["score"] for r in results]
        self.assertEqual(scores, sorted(scores, reverse=True))


class TestApplyScores(PeopleTestCase):
    def set_confidence(self, founder_id, value):
        conn = db.connect()
        conn.execute("UPDATE founders SET confidence=? WHERE id=?",
                     (value, founder_id))
        conn.commit()

    def test_a_higher_score_is_persisted(self):
        founder_id = self.founder(name="Ada Lovelace", title="CEO")
        self.email("ada@acme.com", founder_id=founder_id, source="unverified")
        self.set_confidence(founder_id, 0.0)

        results = people.score_all()
        summary = people.apply_scores(results)
        self.assertEqual(summary["raised"], 1)
        stored = db.connect().execute(
            "SELECT confidence FROM founders WHERE id=?",
            (founder_id,)).fetchone()["confidence"]
        self.assertGreater(stored, 0.0)

    def test_a_verified_address_is_never_overwritten(self):
        # db._founder_confidence derives 0.95 from a verified mailbox, which is
        # stronger evidence than anything inferred from a title or a profile
        # URL. Overwriting it with a guess-driven score would be a regression.
        founder_id = self.founder(name="Ada Lovelace", title="CEO")
        self.email("ada@acme.com", founder_id=founder_id, source="verified",
                   verified=1)
        # The 0.95 an import would have derived from the verified mailbox.
        self.set_confidence(founder_id, 0.95)

        results = people.score_all()
        summary = people.apply_scores(results)
        self.assertEqual(summary["raised"], 0)
        self.assertEqual(summary["skipped_verified"], 1)
        self.assertEqual(self.confidence(founder_id), 0.95)

    def confidence(self, founder_id):
        return db.connect().execute(
            "SELECT confidence FROM founders WHERE id=?",
            (founder_id,)).fetchone()["confidence"]

    def test_a_verified_mailbox_outranks_the_here_score(self):
        # Even with a title and a profile, a guess-driven score must not beat
        # the number a real mailbox check produced.
        founder_id = self.founder(
            name="Ada Lovelace", title="Co-Founder & CEO",
            raw={"linkedin": "https://linkedin.com/in/ada"})
        self.email("ada@acme.com", founder_id=founder_id, source="verified",
                   verified=1)
        self.set_confidence(founder_id, 0.95)
        result = people.score_all()[0]
        self.assertLess(result["score"], 0.95)

    def test_a_lower_score_is_never_written(self):
        self.founder(name="Team", title="")
        db.connect().execute("UPDATE founders SET confidence=0.9")
        db.connect().commit()
        results = people.score_all()
        summary = people.apply_scores(results)
        self.assertEqual(summary["raised"], 0)
        stored = db.connect().execute(
            "SELECT confidence FROM founders").fetchone()["confidence"]
        self.assertEqual(stored, 0.9)

    def test_blocked_people_are_never_written(self):
        self.founder(name="Ada Lovelace", title="CEO")
        self.email("ada@acme.com", founder_id=1, verified=1)
        db.unsubscribe("ada@acme.com")
        results = people.score_all()
        summary = people.apply_scores(results)
        self.assertEqual(summary["skipped_blocked"], 1)
        self.assertEqual(summary["raised"], 0)

    def test_apply_is_idempotent(self):
        self.founder(name="Ada Lovelace", title="CEO")
        self.email("ada@acme.com", founder_id=1, source="unverified")
        first = people.apply_scores(people.score_all())
        second = people.apply_scores(people.score_all())
        self.assertEqual(first["raised"], 1)
        self.assertEqual(second["raised"], 0)


class TestLoadPeople(PeopleTestCase):
    def test_a_founder_gets_its_addresses_attached(self):
        founder_id = self.founder(name="Ada Lovelace", title="CEO")
        self.email("ada@acme.com", founder_id=founder_id, source="verified",
                   verified=1)
        self.email("info@acme.com", founder_id=founder_id, source="site_role")
        loaded = people.load_people()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(len(loaded[0]["emails"]), 2)
        self.assertEqual(len(loaded[0]["addresses"]), 2)

    def test_verified_addresses_are_listed_first(self):
        founder_id = self.founder(name="Ada Lovelace", title="CEO")
        self.email("ada@acme.com", founder_id=founder_id, source="guess")
        self.email("verified@acme.com", founder_id=founder_id, source="verified",
                   verified=1)
        loaded = people.load_people()
        self.assertTrue(loaded[0]["emails"][0]["verified"])

    def test_the_company_domain_is_attached_for_the_guess_ladder(self):
        self.founder(name="Ada Lovelace", title="CEO")
        loaded = people.load_people()
        self.assertEqual(loaded[0]["company_domain"], "acme.com")

    def test_batch_filter(self):
        self.company("a", "A Co", "a.com", batch="Fall 2026")
        self.founder("a", "Fall Person", "CEO")
        self.company("b", "B Co", "b.com", batch="Spring 2027")
        self.founder("b", "Spring Person", "CEO")
        loaded = people.load_people(batch="Fall 2026")
        self.assertEqual([f["name"] for f in loaded], ["Fall Person"])

    def test_limit_is_applied(self):
        for i in range(5):
            self.company(f"c{i}", f"Co {i}", f"c{i}.com")
            self.founder(f"c{i}", f"Person {i}", "CEO")
        self.assertEqual(len(people.load_people(limit=2)), 2)

    def test_an_empty_database_is_not_an_error(self):
        self.assertEqual(people.load_people(), [])
        self.assertEqual(people.score_all(), [])

    def test_no_founder_can_receive_another_founders_addresses(self):
        # The whole load is one query keyed on founder_id. If the IN list were
        # built wrong, everyone would inherit everyone else's addresses.
        a = self.founder(name="Ada Lovelace", title="CEO")
        self.company("b-co", "B Co", "b.com")
        b = self.founder("b-co", "Grace Hopper", "CTO")
        self.email("ada@acme.com", founder_id=a, verified=1)
        self.email("grace@b.com", cid="b-co", founder_id=b, verified=1)
        loaded = {f["name"]: f for f in people.load_people()}
        self.assertEqual(loaded["Ada Lovelace"]["addresses"], ["ada@acme.com"])
        self.assertEqual(loaded["Grace Hopper"]["addresses"], ["grace@b.com"])


class TestCli(PeopleTestCase):
    def run_cli(self, *args):
        import io
        import contextlib
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = people.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_score_on_an_empty_database_says_so(self):
        code, out, _ = self.run_cli("score")
        self.assertEqual(code, 0)
        self.assertIn("no founders stored", out)

    def test_score_json_is_valid_and_carries_reasons(self):
        import json
        self.founder(name="Ada Lovelace", title="CEO")
        code, out, _ = self.run_cli("score", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(len(payload["people"]), 1)
        self.assertTrue(payload["people"][0]["reasons"])

    def test_contactable_filter_excludes_the_unreachable(self):
        self.founder(name="Ada Lovelace", title="CEO")
        self.company("zeta", "Zeta Cloud", "zeta.cloud")
        z = self.founder("zeta", "Barbara Liskov", "CEO")
        self.email("barbara@zeta.cloud", cid="zeta", founder_id=z,
                   is_catch_all=1)
        import json
        code, out, _ = self.run_cli("score", "--contactable", "--json")
        names = [p["name"] for p in json.loads(out)["people"]]
        self.assertIn("Ada Lovelace", names)

    def test_hold_filter_shows_only_people_to_avoid(self):
        self.founder(name="Team", title="")
        self.founder("acme", "Ada Lovelace", "CEO")
        import json
        code, out, _ = self.run_cli("score", "--hold", "--json")
        names = [p["name"] for p in json.loads(out)["people"]]
        self.assertIn("Team", names)
        self.assertNotIn("Ada Lovelace", names)

    def test_min_score_filter(self):
        self.founder(name="Team", title="")
        self.founder("acme", "Ada Lovelace", "CEO")
        self.email("ada@acme.com", founder_id=2, verified=1)
        import json
        code, out, _ = self.run_cli("score", "--min-score", "0.5", "--json")
        for person in json.loads(out)["people"]:
            self.assertGreaterEqual(person["score"], 0.5)

    def test_why_on_a_missing_id_exits_nonzero(self):
        code, _, err = self.run_cli("why", "9999")
        self.assertEqual(code, 1)
        self.assertIn("no founder with id", err)

    def test_why_prints_every_reason(self):
        founder_id = self.founder(name="Ada Lovelace", title="CEO")
        self.email("ada@acme.com", founder_id=founder_id, verified=1)
        code, out, _ = self.run_cli("why", str(founder_id))
        self.assertEqual(code, 0)
        self.assertIn("Ada Lovelace", out)
        self.assertIn("reasons, strongest first", out)
        self.assertIn("address_verified", out)

    def test_why_json(self):
        import json
        founder_id = self.founder(name="Ada Lovelace", title="CEO")
        code, out, _ = self.run_cli("why", str(founder_id), "--json")
        payload = json.loads(out)
        self.assertEqual(payload["name"], "Ada Lovelace")

    def test_apply_reports_what_it_did(self):
        self.founder(name="Ada Lovelace", title="CEO")
        self.email("ada@acme.com", founder_id=1, source="unverified")
        code, out, _ = self.run_cli("score", "--apply")
        self.assertEqual(code, 0)
        self.assertIn("persisted:", out)

    def test_json_mode_does_not_write(self):
        # --apply is easy to combine with --json by accident; refusing is safer
        # than writing while streaming machine-readable output.
        self.founder(name="Ada Lovelace", title="CEO")
        code, out, _ = self.run_cli("score", "--apply", "--json")
        self.assertEqual(code, 0)
        self.assertNotIn("persisted", out)


class TestInvariants(PeopleTestCase):
    """Properties that must hold for every input, whatever the weights."""

    def people_matrix(self):
        names = ["Ada Lovelace", "Cher", "J. Doe", "Team", "Founder2", ""]
        titles = ["CEO", "Advisor", "", "Intern", "Co-Founder & CTO"]
        domains = ["acme.com", "", "zeta.cloud"]
        addresses = [
            [],
            [{"email": "ada@acme.com", "verified": 1}],
            [{"email": "ada@acme.com", "verified": 0}],
            [{"email": "info@acme.com", "is_role": 1}],
            [{"email": "ada@acme.com", "is_catch_all": 1}],
            [{"email": "ada@gmail.com"}],
        ]
        for name in names:
            for title in titles:
                for domain in domains:
                    for address in addresses:
                        yield {
                            "name": name, "first_name": "Ada",
                            "last_name": "Lovelace", "title": title,
                            "company_domain": domain,
                        }, address

    def test_scores_are_always_between_zero_and_one(self):
        for founder, emails in self.people_matrix():
            result = people.score_person(founder, emails)
            self.assertGreaterEqual(result["score"], 0.0)
            self.assertLessEqual(result["score"], 1.0)

    def test_every_result_has_a_valid_band(self):
        for founder, emails in self.people_matrix():
            result = people.score_person(founder, emails)
            self.assertIn(result["band"],
                          (people.WRITE, people.CAREFUL, people.HOLD))

    def test_every_reason_has_a_code_a_number_and_an_explanation(self):
        for founder, emails in self.people_matrix():
            result = people.score_person(founder, emails)
            self.assertTrue(result["reasons"], "a score with no reasons")
            for reason in result["reasons"]:
                self.assertTrue(reason["code"])
                self.assertIsInstance(reason["points"], float)
                self.assertTrue(reason["detail"],
                                f"{reason['code']} has no explanation")

    def test_the_score_equals_the_sum_of_its_reasons(self):
        # Otherwise the printed reasons are decoration, and a reader who adds
        # them up gets a different answer from the one in the table.
        for founder, emails in self.people_matrix():
            result = people.score_person(founder, emails)
            total = sum(r["points"] for r in result["reasons"])
            expected = max(0.0, min(1.0, 0.5 + total / 2.0)) if total > 0 \
                else max(0.0, min(1.0, 0.5 + total))
            if not result["contactable"]:
                expected = min(expected, 0.35)
            self.assertAlmostEqual(result["score"], round(expected, 3), places=3)

    def test_an_empty_input_does_not_crash(self):
        result = people.score_person({}, [])
        self.assertEqual(result["score"], 0.0)
        self.assertEqual(result["band"], people.HOLD)
        self.assertTrue(result["reasons"])

    def test_no_result_ever_claims_a_verified_address(self):
        # Verification is a fact about a mailbox check, not something a score
        # can confer. Nothing here may set verified.
        for founder, emails in self.people_matrix():
            result = people.score_person(founder, emails)
            self.assertNotIn("verified", result)
            for candidate in result["candidates"]:
                self.assertNotIn("verified", candidate)
                self.assertNotIn("confidence", candidate)


if __name__ == "__main__":
    unittest.main()
