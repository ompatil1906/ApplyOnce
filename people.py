#!/usr/bin/env python3
"""
People: decide which of the names you collected are worth writing to.

``db._founder_confidence`` scores a founder partly from their email address, and
that is the wrong shape for the hard case. A founder with a verified personal
address is trivially a real, reachable human. But the set that actually needs
judgement is everyone else: a name scraped off a team page, a founder whose
address ladder came back empty, someone who is technically reachable but not
actually the person you meant. Those people sit at confidence 0 and no amount of
email guessing will move them, because the question is not "what is their
address" but "is this a person, and is this the right one".

So this module scores *people* rather than addresses. Every judgement is a
named reason with a number attached, so a score is never a bare float you have
to trust: ``python3 people.py why 42`` prints the whole argument, and every row
in a table carries the reason that decided it.

Three things it deliberately will not do:

  * It never invents confidence. A founder with no address and no other signal
    stays near zero, because zero is the honest answer.
  * It never promotes a guess to a person. A pattern-matched address is reported
    as a *candidate*, with the pattern that produced it, and is never silently
    treated as evidence the person exists.
  * It never contacts anyone. There is no network code in this file. This is a
    judgement layer over data you already have.

Personal webmail deserves a note, because it cuts against the usual advice.
A founder using ``ada@gmail.com`` rather than ``ada@acme.com`` is, for an email
you did not ask for, a *better* signal than the corporate pattern: it is
somebody who arranged to be personally reachable. Role mailboxes and catch-all
domains are the opposite -- they are reachable and go nowhere.

Examples
--------
    # who is worth writing to, best first, with the reason for each
    python3 people.py score

    # only the ones I would actually contact
    python3 people.py score --contactable

    # the full argument for one person
    python3 people.py why 42

    # just one batch
    python3 people.py score --batch "Fall 2026"

    # machine-readable
    python3 people.py score --json

    # persist raised scores (never lowers, never touches a verified address)
    python3 people.py score --apply

Standard library only, like everything else in this project.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import db  # noqa: E402
from api.yc import (  # noqa: E402
    guess_emails,
    is_role_email,
    name_matches_email,
    normalize_domain,
    split_name,
)

# --------------------------------------------------------------------------- #
# Judgement vocabulary
# --------------------------------------------------------------------------- #

# Free webmail providers. Deliberately a judgement input rather than a filter:
# see the module docstring for why a founder's gmail is a good sign here.
WEBMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "msn.com", "yahoo.com", "yahoo.co.uk", "icloud.com", "me.com", "mac.com",
    "protonmail.com", "proton.me", "pm.me", "fastmail.com", "aol.com",
    "gmx.com", "gmx.de", "mail.com", "zoho.com", "yandex.com", "hey.com",
})

# Titles that mean "this person decides things here". Matched as substrings of
# the lowercased title, so "Co-Founder & CEO" matches both.
DECISION_TITLES = (
    "founder", "co-founder", "cofounder", "ceo", "cto", "cmo", "cfo",
    "coo", "cpo", "chief", "president", "owner", "partner", "vp",
    "vice president", "head of", "managing director", "principal",
)

# Titles that mean "present on the page, but not the person you would email
# about the product". Negative, because a page listing an intern next to a CEO
# is the normal case and should not flatten both to the same score.
PASSIVE_TITLES = (
    "intern", "advisor", "adviser", "mentor", "student", "apprentice",
    "contributor", "board", "investor", "vc", "angel", "part-time",
    "contractor", "freelance", "assistant", "coordinator", "manager",
    "office", "receptionist", "operations",
)

# Name shapes that mean we cannot derive a useful address or cannot be confident
# the field is a person at all.
_NON_PERSON_NAMES = frozenset({
    "team", "founders", "founder", "staff", "team members", "employees",
    "everyone", "hiring", "sales", "support", "contact", "admin", "office",
    "general", "unknown", "n a", "na", "none", "tbd", "-", "--", "?",
})

# Department words. A name made *entirely* of these is a label, not a person:
# "Hiring Team" is as much a scrape artefact as "Team". One of these words next
# to a real name is fine -- "Grace Team" is just a person called Team, and we
# have no way to know, so we do not guess either way.
_DEPARTMENT_WORDS = frozenset({
    "team", "founders", "founder", "staff", "hiring", "sales", "support",
    "contact", "admin", "office", "general", "everyone", "employees",
    "partners", "advisors", "advisers", "board", "management", "leadership",
    "team?", "our", "the", "us",
})

# Recommendation bands, highest first.
WRITE, CAREFUL, HOLD = "write", "write_carefully", "hold"


class Reason:
    """One named input to a score.

    Kept as a class rather than a tuple so a reason always carries its own
    explanation. A score you cannot explain is a score you should not act on.
    """

    __slots__ = ("code", "points", "detail")

    def __init__(self, code: str, points: float, detail: str = "") -> None:
        self.code = code
        self.points = points
        self.detail = detail

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "points": round(self.points, 3),
                "detail": self.detail}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


# --------------------------------------------------------------------------- #
# The judgement itself
# --------------------------------------------------------------------------- #

def _name_reasons(founder: dict[str, Any]) -> list[Reason]:
    """Can we tell this is a person, and can we derive an address for them?"""
    reasons: list[Reason] = []
    name = str(founder.get("name") or "").strip()
    lowered = name.lower()

    if not name:
        reasons.append(Reason("name_missing", -0.5, "no name at all"))
        return reasons

    if lowered in _NON_PERSON_NAMES:
        reasons.append(Reason(
            "name_not_a_person", -0.6,
            f"{name!r} is a department label, not a person"))
        return reasons

    tokens = [t for t in name.replace(".", " ").split() if t]
    if tokens and all(t.lower() in _DEPARTMENT_WORDS for t in tokens):
        reasons.append(Reason(
            "name_not_a_person", -0.6,
            f"{name!r} is made only of department words, not a person"))
        return reasons

    first = str(founder.get("first_name") or "").strip()
    last = str(founder.get("last_name") or "").strip()
    if not first:
        first, last = split_name(name)

    # A one-word name gives us nothing to build an address from. Worth more if
    # they also have no title, because then it is not a person record at all.
    if not last:
        reasons.append(Reason(
            "name_single_token", -0.15,
            f"only {name!r}: no surname, so no address can be derived"))

    # Genuine initials -- "JD", "J.D.", "A B" -- are a directory artefact.
    # "J. Doe" is not: a surname is there to pattern-match with.
    letters = [t for t in re.findall(r"[a-z]+", name.lower()) if t]
    if letters and all(len(t) == 1 for t in letters):
        reasons.append(Reason(
            "name_is_initials", -0.2,
            f"{name!r} is initials rather than a full name"))

    # Digits in a name field usually mean a handle or a vandal edit.
    if any(ch.isdigit() for ch in name):
        reasons.append(Reason(
            "name_has_digits", -0.15, f"{name!r} contains digits"))

    # A very long "name" is a paragraph that got scraped into the wrong field.
    if len(name) > 60:
        reasons.append(Reason(
            "name_too_long", -0.2,
            f"{len(name)} characters: probably not a name"))

    if not reasons:
        reasons.append(Reason("name_usable", 0.05,
                              f"{name!r} is a usable person name"))
    return reasons


def _title_reasons(founder: dict[str, Any]) -> list[Reason]:
    title = str(founder.get("title") or "").strip().lower()
    if not title:
        return [Reason("title_unknown", 0.0,
                       "no title on record, so no signal either way")]

    decision = any(token in title for token in DECISION_TITLES)
    passive = any(token in title for token in PASSIVE_TITLES)

    # Passive wins when both match: "Founders' Office Manager" contains
    # "founder" but the person manages an office.
    if passive:
        return [Reason("title_passive", -0.15,
                       f"{title!r} is present but not a decision-maker")]
    if decision:
        return [Reason("title_decision_maker", 0.2,
                       f"{title!r} decides things here")]
    return [Reason("title_other", 0.0, f"{title!r} does not read as a decision-maker")]


def _footprint_reasons(founder: dict[str, Any]) -> list[Reason]:
    """A public profile is evidence a person exists, not just a name in a table.

    Read from the founder's stored payload, which is where ``_founder_from_payload``
    puts linkedin/twitter/yc_profile. Those fields are already in the database;
    this only asks whether they were ever populated.

    The lookup checks two levels because the column holds the whole founder
    object. When the browser sends an enriched founder, the profile URLs sit at
    the top level; when a caller passes ``raw=`` explicitly, they end up one
    level down. Missing both is normal and means only "no profile on record".
    """
    raw = founder.get("raw")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = {}
    if not isinstance(raw, dict):
        raw = {}

    nested = raw.get("raw")
    sources = [raw, nested if isinstance(nested, dict) else {}]

    present = [key for key in ("linkedin", "twitter", "yc_profile", "github")
               for source in sources
               if str(source.get(key) or "").strip()]
    # Preserve order while removing the duplicate a nested dict would produce.
    seen: list[str] = []
    for key in present:
        if key not in seen:
            seen.append(key)

    if seen:
        return [Reason("public_profile", 0.1,
                       f"has {', '.join(seen)}: a real public footprint")]
    return [Reason("no_public_profile", 0.0,
                   "no linkedin/twitter/yc profile on record")]


def _address_reasons(
    founder: dict[str, Any],
    emails: list[dict[str, Any]],
) -> tuple[list[Reason], list[dict[str, Any]]]:
    """Score the addresses we hold, and derive candidates we do not.

    Returns ``(reasons, candidates)``. Candidates are guesses: they are returned
    so a human can look at them, never counted as evidence the person is real.
    """
    reasons: list[Reason] = []
    candidates: list[dict[str, Any]] = []
    company_domain = normalize_domain(str(founder.get("company_domain") or ""))

    known = {str(e.get("email") or "").strip().lower(): e for e in emails}

    # A catch-all resolves for every address, so a name-shaped local part on such
    # a domain proves nothing about who owns it. Judge it first and discount
    # everything below: without this, a catch-all can outscore a real mailbox,
    # and the reason that decided the score never shows up in the output at all.
    catch_all_only = bool(
        [e for e in known.values() if e.get("is_catch_all")]
    ) and not [e for e in known.values()
               if normalize_domain(str(e.get("email") or "")) not in WEBMAIL_DOMAINS
               and not e.get("is_catch_all")]

    # 1. Addresses that demonstrably belong to this person.
    mine = [e for e in known.values()
            if name_matches_email(str(e.get("email") or ""),
                                  str(founder.get("first_name") or ""),
                                  str(founder.get("last_name") or ""))]
    verified = [e for e in mine if e.get("verified")]

    if verified and not catch_all_only:
        reasons.append(Reason("address_verified", 0.5,
                              f"{verified[0]['email']} is verified: a real mailbox"))
    elif mine and not catch_all_only:
        reasons.append(Reason("address_matches_name", 0.25,
                              f"{mine[0]['email']} carries their name"))

    # 2. Personal webmail is a positive signal here, not a warning.
    personal = [e for e in known.values()
                if normalize_domain(str(e.get("email") or "")) in WEBMAIL_DOMAINS]
    if personal:
        reasons.append(Reason("address_personal_webmail", 0.15,
                              f"{personal[0]['email']}: arranged to be personally "
                              "reachable"))

    # 3. Reachability that does not identify this person.
    role_only = [e for e in known.values() if is_role_email(str(e.get("email") or ""))]
    if role_only and not mine:
        reasons.append(Reason("address_role_only", -0.1,
                              f"only a role mailbox ({role_only[0]['email']}), "
                              "which reaches a department, not a person"))
    if catch_all_only:
        reasons.append(Reason("address_catch_all", -0.25,
                              "catch-all domain: every address resolves, so the "
                              "name-shaped part proves nothing about who owns it"))

    # 4. Nothing at all: offer the ladder as candidates, explicitly uncounted.
    #
    # Only for a name we believe is a person. "Team" and "Founders" produce a
    # clean surname-less split and would otherwise generate role mailboxes, which
    # is precisely the address we refuse to send to -- five confident-looking
    # suggestions for a department label is worse than none.
    first = str(founder.get("first_name") or "").strip()
    last = str(founder.get("last_name") or "").strip()
    if not first:
        first, last = split_name(str(founder.get("name") or ""))

    name_is_person = str(founder.get("name") or "").strip().lower() not in _NON_PERSON_NAMES
    if (not mine and not verified and first and last and company_domain
            and name_is_person):
        for index, candidate in enumerate(guess_emails(first, last, company_domain)):
            if candidate in known:
                continue
            candidates.append({
                "email": candidate,
                "is_role": is_role_email(candidate),
                "rank": index,
            })
            if len(candidates) >= 5:
                break
        if candidates:
            reasons.append(Reason(
                "candidates_available", 0.0,
                f"{len(candidates)} unverified candidate(s) from the pattern "
                "ladder; not counted as evidence"))

    if not mine and not role_only and not catch_all_only:
        reasons.append(Reason("address_none", -0.2,
                              "no address of any kind for this person"))

    return reasons, candidates


def _suppression_reasons(
    founder: dict[str, Any],
    unsubscribed: set[str],
    flags: dict[str, dict[str, int]],
) -> list[Reason]:
    """Reasons this person must not be contacted regardless of score.

    These are not penalties. A person on the unsubscribes list is not worth less,
    they are simply off-limits, and averaging that into a float would be a way of
    eventually mailing them anyway.
    """
    reasons: list[Reason] = []
    company_id = str(founder.get("company_id") or "")

    if str(founder.get("email") or "").strip().lower() in unsubscribed:
        reasons.append(Reason("unsubscribed", 0.0,
                              "this address opted out; never contact again"))
    for address in (founder.get("addresses") or []):
        if str(address).strip().lower() in unsubscribed:
            reasons.append(Reason("unsubscribed", 0.0,
                                  f"{address} opted out; never contact again"))
            break

    flag = flags.get(company_id) or {}
    if flag.get("hidden"):
        reasons.append(Reason("company_hidden", 0.0,
                              "you hid this company"))
    if flag.get("sent_at"):
        reasons.append(Reason("already_contacted", 0.0,
                              "you have already emailed this company"))

    return reasons


def score_person(
    founder: dict[str, Any],
    emails: list[dict[str, Any]] | None = None,
    *,
    unsubscribed: set[str] | None = None,
    flags: dict[str, dict[str, int]] | None = None,
) -> dict[str, Any]:
    """Score one founder as a person, and explain every point.

    ``emails`` defaults to whatever ``load_people`` attached under the founder's
    own ``emails`` key, so the natural call ``score_person(load_people()[0])``
    works. It is a separate parameter only so a caller can score one founder
    against a hypothetical set of addresses.

    Returns a dict with ``score`` (0..1), ``band``, ``blocked``, ``reasons``,
    ``candidates`` and ``summary``. Nothing here reaches the network or writes to
    the database.
    """
    if emails is None:
        emails = founder.get("emails") or []
    emails = emails or []
    unsubscribed = unsubscribed or set()
    flags = flags or {}

    reasons: list[Reason] = []
    reasons += _name_reasons(founder)
    reasons += _title_reasons(founder)
    reasons += _footprint_reasons(founder)
    address_reasons, candidates = _address_reasons(founder, emails)
    reasons += address_reasons
    suppressions = _suppression_reasons(founder, unsubscribed, flags)

    raw = sum(r.points for r in reasons)
    score = _clamp(0.5 + raw / 2.0) if raw > 0 else _clamp(0.5 + raw)

    # A person we have no way to reach cannot be contacted however real they
    # are, so reachability caps the score rather than adding to it.
    contactable = bool(
        [e for e in emails
         if not is_role_email(str(e.get("email") or ""))
         and not e.get("is_catch_all")]
    ) or bool(candidates and not all(c["is_role"] for c in candidates))

    if not contactable:
        score = min(score, 0.35)
        reasons.append(Reason("not_reachable", 0.0,
                              "no personal route to this person, so the score "
                              "is capped"))

    if suppressions:
        band = HOLD
    elif score >= 0.7:
        band = WRITE
    elif score >= 0.45:
        band = CAREFUL
    else:
        band = HOLD

    return {
        "founder_id": founder.get("id"),
        "name": founder.get("name"),
        "company_id": founder.get("company_id"),
        "company_name": founder.get("company_name"),
        "title": founder.get("title"),
        "score": round(score, 3),
        "band": band,
        "contactable": contactable,
        "blocked": bool(suppressions),
        "reasons": [r.as_dict() for r in reasons + suppressions],
        "candidates": candidates,
        "summary": _summary(band, reasons, suppressions, candidates),
    }


def _summary(
    band: str,
    reasons: list[Reason],
    suppressions: list[Reason],
    candidates: list[dict[str, Any]],
) -> str:
    """One line that names the reason that actually decided it."""
    if suppressions:
        return suppressions[0].detail
    # The highest-magnitude judgement is the one that decided the outcome.
    decisive = max(reasons, key=lambda r: abs(r.points), default=None)
    if band == WRITE and candidates and decisive and decisive.points < 0.2:
        person_like = [c for c in candidates if not c["is_role"]]
        if person_like:
            return f"looks like a person; try {person_like[0]['email']}"
    if decisive is None:
        return "no signal either way"
    return decisive.detail or decisive.code


# --------------------------------------------------------------------------- #
# Reading people out of the database
# --------------------------------------------------------------------------- #

def load_people(
    *,
    batch: str = "",
    limit: int = 0,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Every founder with their addresses, newest companies first.

    One query rather than N: a batch of 200 companies with 400 founders would
    otherwise be 400 round trips, which is the difference between a preview and
    a hang.
    """
    conn = conn or db.connect()

    where = ""
    params: list[Any] = []
    if batch:
        where = "WHERE c.batch = ?"
        params.append(batch)

    founders = conn.execute(
        "SELECT f.*, c.name AS company_name, c.domain AS company_domain, "
        "       c.batch AS company_batch "
        "FROM founders f JOIN companies c ON c.id = f.company_id "
        f"{where} ORDER BY c.batch, c.name, f.id", params).fetchall()

    by_founder: dict[int, list[dict[str, Any]]] = {}
    if founders:
        ids = [row["id"] for row in founders]
        # "?" * n produces the parameter list; join it into the SQL text
        # separately, because sqlite3 binds values but does not substitute them.
        marks = ",".join(["?"] * len(ids))
        for row in conn.execute(
            f"SELECT * FROM emails WHERE founder_id IN ({marks}) "
            "ORDER BY verified DESC, confidence DESC", ids
        ).fetchall():
            by_founder.setdefault(row["founder_id"], []).append(dict(row))

    people: list[dict[str, Any]] = []
    for row in founders:
        founder = dict(row)
        founder["emails"] = by_founder.get(row["id"], [])
        founder["addresses"] = [e["email"] for e in founder["emails"]]
        people.append(founder)

    if limit > 0:
        people = people[:limit]
    return people


def score_all(
    *,
    batch: str = "",
    limit: int = 0,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Score every stored founder, best first."""
    conn = conn or db.connect()
    unsubscribed = {str(row["email"]).strip().lower()
                    for row in conn.execute(
                        "SELECT email FROM unsubscribes").fetchall()}
    flags = db.get_flags()

    results = [
        score_person(founder, founder["emails"],
                     unsubscribed=unsubscribed, flags=flags)
        for founder in load_people(batch=batch, limit=limit, conn=conn)
    ]
    results.sort(key=lambda r: (r["blocked"], -r["score"],
                                str(r["name"] or "")))
    return results


# --------------------------------------------------------------------------- #
# Persisting
# --------------------------------------------------------------------------- #

def apply_scores(
    results: list[dict[str, Any]],
    *,
    conn: Any = None,
) -> dict[str, Any]:
    """Write raised scores back to ``founders.confidence``.

    Deliberately one-directional and deliberately timid:

      * A score is only ever raised. ``db._founder_confidence`` derives from
        evidence we did not look at here, so lowering would throw away
        information this module does not have.
      * A founder who already has a verified address is skipped entirely: their
        confidence comes from the mailbox check, which is stronger evidence than
        anything guessed from a title or a profile URL.
      * Nothing blocked is written. An unsubscribed person stays at whatever
        they were; there is no reason to record a score for them at all.

    Returns a summary of what changed.
    """
    conn = conn or db.connect()
    raised = 0
    skipped_verified = 0
    skipped_blocked = 0

    with db.write(conn):
        for result in results:
            if result["blocked"]:
                skipped_blocked += 1
                continue
            # Check "already verified" before "already high enough", so the
            # reported counts are honest. The order of these two does not change
            # what is written -- both paths skip -- but checking the cheap
            # numeric comparison first would report zero skipped_verified and
            # quietly lose the most interesting number in the summary.
            verified = conn.execute(
                "SELECT 1 FROM emails WHERE founder_id=? AND verified=1 LIMIT 1",
                (result["founder_id"],)).fetchone()
            if verified is not None:
                skipped_verified += 1
                continue
            row = conn.execute(
                "SELECT confidence FROM founders WHERE id=?",
                (result["founder_id"],)).fetchone()
            if row is None:
                continue
            if float(row["confidence"] or 0.0) >= result["score"]:
                continue
            conn.execute(
                "UPDATE founders SET confidence=? WHERE id=?",
                (result["score"], result["founder_id"]))
            raised += 1

    return {"raised": raised, "skipped_verified": skipped_verified,
            "skipped_blocked": skipped_blocked}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

_BAND_LABEL = {
    WRITE: "write",
    CAREFUL: "careful",
    HOLD: "hold",
}


def _fmt_table(results: list[dict[str, Any]]) -> str:
    if not results:
        return "no founders stored yet. Load a batch in the app first."

    width = max((len(str(r["name"] or "?")) for r in results), default=4)
    width = min(max(width, 12), 34)

    lines = [f"{'score':>5}  {'band':<7} {'id':>4}  "
             f"{'name':<{width}} {'company':<22} why"]
    lines.append("-" * (width + 60))
    for r in results:
        band = _BAND_LABEL.get(r["band"], r["band"])
        if r["blocked"]:
            band = "BLOCKED"
        name = str(r["name"] or "?")[:width]
        company = str(r["company_name"] or r["company_id"] or "")[:22]
        summary = r["summary"]
        if len(summary) > 58:
            summary = summary[:55] + "..."
        lines.append(f"{r['score']:>5.2f}  {band:<7} "
                     f"{str(r['founder_id'] or ''):>4}  "
                     f"{name:<{width}} {company:<22} {summary}")
    return "\n".join(lines)


def _fmt_why(result: dict[str, Any]) -> str:
    lines = [
        f"{result['name'] or '(unnamed)'}",
        f"  company     {result['company_name'] or result['company_id']}",
        f"  title       {result['title'] or '(none)'}",
        f"  score       {result['score']:.2f}  -> "
        f"{_BAND_LABEL.get(result['band'], result['band'])}",
        f"  contactable {'yes' if result['contactable'] else 'no'}",
    ]
    if result["blocked"]:
        lines.append("  BLOCKED     do not contact")
    lines.append("")

    lines.append("reasons, strongest first:")
    ordered = sorted(result["reasons"],
                     key=lambda r: -abs(r["points"]))
    for reason in ordered:
        points = f"{reason['points']:+.2f}"
        if abs(reason["points"]) < 0.005:
            points = " 0.00"
        lines.append(f"  {points}  {reason['code']}")
        if reason["detail"]:
            lines.append(f"          {reason['detail']}")

    if result["candidates"]:
        lines.append("")
        lines.append("candidate addresses (guesses, unverified, uncounted):")
        for candidate in result["candidates"]:
            tag = "role mailbox" if candidate["is_role"] else "person-shaped"
            lines.append(f"  #{candidate['rank']} {candidate['email']}  ({tag})")
    return "\n".join(lines)


def cmd_score(args: argparse.Namespace) -> int:
    results = score_all(batch=args.batch, limit=args.limit)
    if args.contactable:
        results = [r for r in results if r["contactable"] and not r["blocked"]]
    if args.min_score > 0:
        results = [r for r in results if r["score"] >= args.min_score]
    if args.hold:
        results = [r for r in results if r["band"] == HOLD or r["blocked"]]

    if args.json:
        print(json.dumps({"people": results}, indent=2))
        return 0

    print(_fmt_table(results))
    write = sum(1 for r in results if r["band"] == WRITE)
    careful = sum(1 for r in results if r["band"] == CAREFUL)
    hold = sum(1 for r in results if r["band"] == HOLD and not r["blocked"])
    blocked = sum(1 for r in results if r["blocked"])
    print(f"\n{write} worth writing to, {careful} write carefully, "
          f"{hold} hold, {blocked} blocked")

    if args.apply and not args.json:
        summary = apply_scores(results)
        print(f"persisted: {summary['raised']} raised, "
              f"{summary['skipped_verified']} skipped (already verified), "
              f"{summary['skipped_blocked']} skipped (blocked)")
    return 0


def cmd_why(args: argparse.Namespace) -> int:
    conn = db.connect()
    row = conn.execute(
        "SELECT f.*, c.name AS company_name, c.domain AS company_domain "
        "FROM founders f JOIN companies c ON c.id = f.company_id "
        "WHERE f.id=?", (args.founder_id,)).fetchone()
    if row is None:
        print(f"no founder with id {args.founder_id}", file=sys.stderr)
        return 1

    founder = dict(row)
    founder["emails"] = [dict(r) for r in conn.execute(
        "SELECT * FROM emails WHERE founder_id=? "
        "ORDER BY verified DESC, confidence DESC", (args.founder_id,)
    ).fetchall()]
    founder["addresses"] = [e["email"] for e in founder["emails"]]

    result = score_person(
        founder, founder["emails"],
        unsubscribed={str(r["email"]).strip().lower() for r in conn.execute(
            "SELECT email FROM unsubscribes").fetchall()},
        flags=db.get_flags())

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(_fmt_why(result))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="people.py",
        description="Score the founders you collected as people, not addresses.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Scores are judgements with reasons attached, not measurements.\n"
            "Run `why <id>` before acting on anything surprising. This tool\n"
            "never contacts anyone and never invents an address.\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    score = sub.add_parser("score", help="rank stored founders, best first")
    score.add_argument("--batch", default="", help="restrict to one YC batch")
    score.add_argument("--limit", type=int, default=0, metavar="N",
                       help="only score the first N founders")
    score.add_argument("--min-score", type=float, default=0.0, metavar="F",
                       help="drop anyone scoring below this")
    score.add_argument("--contactable", action="store_true",
                       help="only people with a personal route to them")
    score.add_argument("--hold", action="store_true",
                       help="show only the people you should not write to")
    score.add_argument("--json", action="store_true", help="machine-readable")
    score.add_argument("--apply", action="store_true",
                       help="persist raised scores (never lowers, never "
                            "overrides a verified address)")
    score.set_defaults(func=cmd_score)

    why = sub.add_parser("why", help="explain one founder in full")
    why.add_argument("founder_id", type=int, metavar="ID")
    why.add_argument("--json", action="store_true", help="machine-readable")
    why.set_defaults(func=cmd_why)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
