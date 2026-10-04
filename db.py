#!/usr/bin/env python3
"""
ApplyOnce -- local durable storage.

SQLite is the canonical record for everything you collect. The browser keeps
using localStorage as a *render cache* (drafts must still paint with the
network down), so this module never forces the UI to become an async database
client. The split is deliberate:

    SQLite        durable, queryable, survives a cleared browser profile
    localStorage  fast, synchronous, disposable

Because the UI already renders the `_normalise_hit()` shape from api/yc.py
verbatim, every row keeps a ``raw`` JSON column holding that original object.
Only the fields worth *querying* get real columns. That way the upstream shape
can drift (it changes every time YC ships) without a migration, and a round trip
through the database is lossless.

Why this file is not under api/
------------------------------
api/yc.py is the Vercel function, and its docstring commits to importing no
sibling project files and no third-party packages. Vercel's filesystem is
ephemeral and read-only, so a SQLite write could never work there anyway. This
module is local-only by construction: it lives beside serve.py and is
deliberately absent from vercel.json, so it cannot be accidentally deployed.

Threading
---------
serve.py uses ThreadingHTTPServer, so every request is its own thread, and a
single site_emails request spawns several more. A single module-level
connection would therefore be shared across threads at random. sqlite3's
`check_same_thread=False` only disables the guard; it does not serialise access.
So connections are thread-local *and* every write runs inside a process-wide
lock. WAL plus busy_timeout then lets readers run concurrently with one writer,
which is the actual workload here.

Standard library only, like everything else in this project.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from api.yc import UpstreamError, split_name  # noqa: E402

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Default lives next to the project so `python3 serve.py` just works, but an
# explicit path wins so you can keep several research sets side by side.
DEFAULT_DB_PATH = Path(
    os.environ.get("APPLYONCE_DB") or (ROOT / "applyonce.db")
)

# A company is only worth a row if we can key it. The id comes from the source
# when it has one; otherwise we derive a stable key from the domain.
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

# JSON escaping used only for the handful of identifiers we build into SQL
# LIKE filters. Everything else goes through bound parameters.
_LIKE_ESCAPE_RE = re.compile(r"([\\%_])")

# Guard against a pathological import payload. serve.py refuses to read a
# request body larger than this, so a runaway localStorage blob cannot make the
# server allocate unbounded memory.
MAX_IMPORT_BYTES = 16 * 1024 * 1024

# Frozen so a code change cannot retroactively reinterpret what the UI means.
STATE_VERSION = 1
SCHEMA_VERSION = 1

# A fresh template row is created on an empty database so the sending UI always
# has something to show. Keyed by `key` rather than an autoincrement id so that
# re-seeding is idempotent.
DEFAULT_TEMPLATE_KEY = "default"

DEFAULT_TEMPLATE = {
    "subject": "{first_name} — quick question about {company}",
    "body": "\n".join([
        "Hi {first_name},",
        "",
        "I'm {my_name}. I came across {company} — {one_liner}",
        "",
        "I'm working on tools for early-stage technical founders and would value",
        "15 minutes of your time. What I like about {company} is the focus on",
        "{tags}.",
        "",
        "GitHub: {github}",
        "Portfolio: {portfolio}",
        "Resume: {resume}",
        "",
        "Open to a short call this week?",
        "",
        "— {my_name}",
        "",
        "-- ",
        "You are receiving this because your address appears on the public",
        "website of {company}. Reply to this email and I will not contact you",
        "again. To stop these emails, click here:",
        "{unsubscribe}",
    ]),
    "drop_empty_lines": True,
}


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
#
# `CREATE TABLE IF NOT EXISTS` everywhere, so every step is safe to re-run.
# Column additions therefore need no ALTER TABLE: ship a new migration that
# only *adds* tables/columns, and bump SCHEMA_VERSION.

_MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS companies (
    id            TEXT PRIMARY KEY,
    source        TEXT    NOT NULL DEFAULT 'yc',
    slug          TEXT    NOT NULL DEFAULT '',
    name          TEXT    NOT NULL DEFAULT '',
    domain        TEXT    NOT NULL DEFAULT '',
    website       TEXT    NOT NULL DEFAULT '',
    logo          TEXT    NOT NULL DEFAULT '',
    batch         TEXT    NOT NULL DEFAULT '',
    location      TEXT    NOT NULL DEFAULT '',
    team_size     TEXT    NOT NULL DEFAULT '',
    tags          TEXT    NOT NULL DEFAULT '[]',
    raw           TEXT    NOT NULL DEFAULT '{}',
    created_at    INTEGER NOT NULL DEFAULT 0,
    updated_at    INTEGER NOT NULL DEFAULT 0
);

-- Slug is a useful extra identity check *when the source provides one*, but a
-- plain UNIQUE(source, slug) collapses every slug-less company into a single
-- group: a domain-list import has no slugs at all, so the second row collides.
-- Partial index keeps the check where it means something and nowhere else.
CREATE UNIQUE INDEX IF NOT EXISTS ux_companies_source_slug
    ON companies (source, slug) WHERE slug != '';
CREATE INDEX IF NOT EXISTS ix_companies_domain ON companies (domain);
CREATE INDEX IF NOT EXISTS ix_companies_name ON companies (name);

-- 'sent' and 'hidden' are per-company flags, kept apart from the company row
-- on purpose. A flag can legitimately outlive its company: hide a card, then
-- clear the batch it came from, and the hidden list still means something. So
-- this table has no foreign key, and an id here is not evidence that a company
-- was ever fetched.
CREATE TABLE IF NOT EXISTS company_flags (
    company_id    TEXT    PRIMARY KEY,
    sent_at       INTEGER NOT NULL DEFAULT 0,
    hidden        INTEGER NOT NULL DEFAULT 0,
    updated_at    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_company_flags_sent ON company_flags (sent_at);

CREATE TABLE IF NOT EXISTS founders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id    TEXT    NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name          TEXT    NOT NULL,
    first_name    TEXT    NOT NULL DEFAULT '',
    last_name     TEXT    NOT NULL DEFAULT '',
    title         TEXT    NOT NULL DEFAULT '',
    confidence    REAL    NOT NULL DEFAULT 0,
    raw           TEXT    NOT NULL DEFAULT '{}',
    created_at    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (company_id, name)
);

CREATE INDEX IF NOT EXISTS ix_founders_company ON founders (company_id);

CREATE TABLE IF NOT EXISTS emails (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    founder_id    INTEGER REFERENCES founders(id) ON DELETE CASCADE,
    company_id    TEXT    NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    email         TEXT    NOT NULL,
    source        TEXT    NOT NULL DEFAULT 'guess',
    verified      INTEGER NOT NULL DEFAULT 0,
    is_role       INTEGER NOT NULL DEFAULT 0,
    is_catch_all  INTEGER NOT NULL DEFAULT 0,
    confidence    REAL    NOT NULL DEFAULT 0,
    raw           TEXT    NOT NULL DEFAULT '{}',
    checked_at    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (founder_id, email)
);

CREATE INDEX IF NOT EXISTS ix_emails_company ON emails (company_id);
CREATE INDEX IF NOT EXISTS ix_emails_address ON emails (email);

CREATE TABLE IF NOT EXISTS templates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    key           TEXT    NOT NULL UNIQUE,
    subject       TEXT    NOT NULL DEFAULT '',
    body          TEXT    NOT NULL DEFAULT '',
    updated_at    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key           TEXT PRIMARY KEY,
    value         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id    TEXT    NOT NULL,
    founder_id    INTEGER,
    founder_name  TEXT    NOT NULL DEFAULT '',
    email         TEXT    NOT NULL,
    subject       TEXT    NOT NULL DEFAULT '',
    body          TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL DEFAULT 'draft',
    message_id    TEXT    NOT NULL DEFAULT '',
    approved      INTEGER NOT NULL DEFAULT 0,
    error         TEXT    NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL DEFAULT 0,
    sent_at       INTEGER
);

CREATE INDEX IF NOT EXISTS ix_queue_status ON queue (status);
CREATE INDEX IF NOT EXISTS ix_queue_company ON queue (company_id);

CREATE TABLE IF NOT EXISTS deliveries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id      INTEGER NOT NULL REFERENCES queue(id) ON DELETE CASCADE,
    event         TEXT    NOT NULL,
    detail        TEXT    NOT NULL DEFAULT '',
    at            INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_deliveries_queue ON deliveries (queue_id);

CREATE TABLE IF NOT EXISTS unsubscribes (
    email         TEXT PRIMARY KEY,
    at            INTEGER NOT NULL DEFAULT 0,
    reason        TEXT    NOT NULL DEFAULT ''
);
"""

MIGRATIONS = [_MIGRATION_1]


# --------------------------------------------------------------------------- #
# Connections
# --------------------------------------------------------------------------- #

# The write lock. Every INSERT/UPDATE/DELETE runs under it, which is stricter
# than SQLite technically requires (it has its own file locking) but makes the
# concurrency behaviour of this module obvious rather than emergent.
_WRITE_LOCK = threading.RLock()

# One connection per thread, kept for the life of the process.
_THREAD_LOCAL = threading.local()


def db_path() -> Path:
    return Path(os.environ.get("APPLYONCE_DB") or DEFAULT_DB_PATH)


def connect() -> sqlite3.Connection:
    """This thread's connection, opened and configured on first use.

    check_same_thread=False is safe here specifically because a connection is
    only ever handed to the thread that created it.
    """
    existing: sqlite3.Connection | None = getattr(_THREAD_LOCAL, "conn", None)
    if existing is not None:
        return existing

    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row

    # Autocommit. The default isolation level opens an implicit transaction on
    # the first INSERT and holds it until someone commits -- so a bare
    # `with lock: execute(...)` releases the Python lock while SQLite still owns
    # a write transaction, and the next thread fails with "database is locked".
    # Explicit BEGIN in write() is the only place a transaction opens.
    conn.isolation_level = None

    # WAL lets readers proceed during a write. Without it SQLite serialises
    # everything, which would make a slow import block the whole UI.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        # Some filesystems (network mounts) refuse WAL. The default rollback
        # journal still works, just with coarser concurrency.
        pass
    conn.execute("PRAGMA foreign_keys=ON")
    # Wait instead of raising SQLITE_BUSY when another thread holds the write
    # lock. 5s is far longer than any single statement here should take.
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")

    _THREAD_LOCAL.conn = conn
    # Every public function reaches storage through connect(), so this is the
    # one place that can guarantee the schema exists. Without it, a fresh
    # process that calls db.upsert_company() directly gets "no such table:
    # companies" -- the server only avoided that by migrating on every request.
    _ensure_ready(conn)
    return conn


def reset_connection() -> None:
    """Drop this thread's connection. Used by tests and after a path change."""
    conn: sqlite3.Connection | None = getattr(_THREAD_LOCAL, "conn", None)
    if conn is not None:
        conn.close()
    _THREAD_LOCAL.conn = None
    # Must clear this too: the next connect() may point at a different file that
    # has never been migrated.
    _THREAD_LOCAL.ready = False


def _ensure_ready(conn: sqlite3.Connection) -> None:
    """Apply migrations once per connection. Must not call connect().

    connect() calls this, so anything reached from here that needs a connection
    would recurse. Hence the explicit conn parameter.
    """
    if getattr(_THREAD_LOCAL, "ready", False):
        return
    _apply_migrations(conn)
    _THREAD_LOCAL.ready = True


def _apply_migrations(conn: sqlite3.Connection) -> int:
    """Schema work for one connection. Caller owns the transaction."""
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current > SCHEMA_VERSION:
        raise UpstreamError(
            f"database is at schema v{current}, this build understands "
            f"v{SCHEMA_VERSION} — upgrade ApplyOnce or move the .db aside",
            500,
        )
    if current == SCHEMA_VERSION:
        return SCHEMA_VERSION

    with write(conn):
        for version in range(current, SCHEMA_VERSION):
            conn.executescript(MIGRATIONS[version])
            conn.execute(f"PRAGMA user_version={version + 1}")
    return SCHEMA_VERSION


def migrate(conn: sqlite3.Connection | None = None) -> int:
    """Bring the schema up to SCHEMA_VERSION. Safe to call on every boot.

    Cheap enough to run per request -- PRAGMA user_version is a header read, and
    once the database is current this does nothing but compare two integers.
    """
    conn = conn or connect()  # connect() has already applied migrations
    return _apply_migrations(conn)


def _now() -> int:
    return int(time.time())


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


@contextmanager
def write(conn: sqlite3.Connection | None = None) -> Iterator[sqlite3.Connection]:
    """A serialized write transaction. Every mutation in this module goes
    through here, for three reasons:

      1. Threading. serve.py runs ThreadingHTTPServer, so several requests can
         be inside the database at once. WAL allows concurrent *readers* but
         exactly one writer, so writers must be serialised in-process or they
         race for the RESERVED lock.
      2. Durability. Holding the Python lock and the transaction open together
         means the lock is never released while a write is still pending. An
         earlier version released the lock after execute() without committing,
         which made every second thread fail with SQLITE_BUSY.
      3. Transaction grouping. import_state() calls upsert_company() and
         friends, each of which opens its own transaction. Nesting has to be a
         no-op, so track a per-thread depth counter and let the outermost call
         own the real BEGIN/COMMIT.

    BEGIN IMMEDIATE rather than a deferred BEGIN: it takes the write lock up
    front, so a second thread blocks on busy_timeout instead of discovering the
    conflict mid-transaction, which SQLite cannot resolve without deadlocking.
    """
    conn = conn or connect()
    depth = getattr(_THREAD_LOCAL, "depth", 0)
    if depth:
        # Already inside a transaction on this thread: participate in it.
        _THREAD_LOCAL.depth = depth + 1
        try:
            yield conn
        finally:
            _THREAD_LOCAL.depth = depth
        return

    # This is the one place that takes the raw lock and the one place that
    # opens a real transaction. Everything else funnels through here.
    with _WRITE_LOCK:
        conn.execute("BEGIN IMMEDIATE")
        _THREAD_LOCAL.depth = 1
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            _THREAD_LOCAL.depth = 0


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def get_setting(key: str, default: str = "") -> str:
    row = connect().execute(
        "SELECT value FROM settings WHERE key=?", (key,)
    ).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with write() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def all_settings() -> dict[str, str]:
    rows = connect().execute("SELECT key, value FROM settings ORDER BY key")
    return {row["key"]: row["value"] for row in rows}


def delete_setting(key: str) -> None:
    with write() as conn:
        conn.execute("DELETE FROM settings WHERE key=?", (key,))


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #


def upsert_template(
    key: str,
    subject: str,
    body: str,
    conn: sqlite3.Connection | None = None,
) -> int:
    conn = conn or connect()
    now = _now()
    with write(conn):
        conn.execute(
            "INSERT INTO templates (key, subject, body, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "subject=excluded.subject, body=excluded.body, "
            "updated_at=excluded.updated_at",
            (key, subject, body, now),
        )
    return now


def list_templates(conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    conn = conn or connect()
    rows = conn.execute(
        "SELECT key, subject, body, updated_at FROM templates ORDER BY id"
    ).fetchall()
    return [dict(row) for row in rows]


def seed_default_template(conn: sqlite3.Connection | None = None) -> bool:
    """Insert the built-in template if the table is empty. Idempotent."""
    conn = conn or connect()
    row = conn.execute("SELECT 1 FROM templates LIMIT 1").fetchone()
    if row:
        return False
    upsert_template(
        DEFAULT_TEMPLATE_KEY,
        DEFAULT_TEMPLATE["subject"],
        DEFAULT_TEMPLATE["body"],
        conn,
    )
    return True


# --------------------------------------------------------------------------- #
# Companies, founders, emails
# --------------------------------------------------------------------------- #


def company_key(source: str, company: dict[str, Any]) -> str:
    """Stable identity for a company.

    Prefer the source's own id (YC's Algolia id), but a company imported from a
    domain list has none. Falling back to the domain keeps re-imports idempotent;
    falling back further to the name means even a domain-less row survives a
    round trip.
    """
    raw_id = str(company.get("id") or "").strip()
    if raw_id and _ID_RE.match(raw_id):
        return raw_id

    domain = str(company.get("domain") or "").strip().lower()
    if domain and _ID_RE.match(domain):
        return domain

    name = str(company.get("name") or "").strip().lower()
    if name:
        return re.sub(r"[^a-z0-9]+", "-", name).strip("-") or "unknown"

    raise UpstreamError("company needs an id, a domain, or a name", 400)


def upsert_company(
    company: dict[str, Any],
    *,
    source: str = "yc",
    conn: sqlite3.Connection | None = None,
) -> str:
    """Insert or update one company. Returns its id.

    The caller-visible ``raw`` blob is preserved verbatim: it is the upstream
    object the UI renders, so overwriting it with a partial would silently drop
    fields the current page depends on.
    """
    conn = conn or connect()
    cid = company_key(source, company)
    now = _now()
    tags = company.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    team_size = company.get("team_size")
    if team_size is None:
        team_size = ""

    with write(conn):
        conn.execute(
            "INSERT INTO companies (id, source, slug, name, domain, website, "
            "logo, batch, location, team_size, tags, raw, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "source=excluded.source, slug=excluded.slug, name=excluded.name, "
            "domain=excluded.domain, website=excluded.website, "
            "logo=excluded.logo, batch=excluded.batch, "
            "location=excluded.location, team_size=excluded.team_size, "
            "tags=excluded.tags, raw=excluded.raw, updated_at=excluded.updated_at",
            (
                cid,
                source,
                str(company.get("slug") or ""),
                str(company.get("name") or ""),
                str(company.get("domain") or ""),
                str(company.get("website") or ""),
                str(company.get("logo") or ""),
                str(company.get("batch") or ""),
                str(company.get("location") or ""),
                str(team_size),
                _dumps(tags),
                _dumps(company),
                now,
                now,
            ),
        )
    return cid


def upsert_founder(
    company_id: str,
    founder: dict[str, Any],
    *,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Insert or update one founder. Returns the founder row id."""
    conn = conn or connect()
    name = str(founder.get("name") or "").strip()
    if not name:
        raise UpstreamError("founder needs a name", 400)

    first = str(founder.get("first_name") or "").strip()
    last = str(founder.get("last_name") or "").strip()
    if not first and not last:
        first, last = split_name(name)

    try:
        confidence = float(founder.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0

    now = _now()
    with write(conn):
        conn.execute(
            "INSERT INTO founders (company_id, name, first_name, last_name, "
            "title, confidence, raw, created_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(company_id, name) DO UPDATE SET "
            "first_name=excluded.first_name, last_name=excluded.last_name, "
            "title=excluded.title, confidence=excluded.confidence, "
            "raw=excluded.raw",
            (
                company_id,
                name,
                first,
                last,
                str(founder.get("title") or ""),
                confidence,
                _dumps(founder),
                now,
            ),
        )
        row = conn.execute(
            "SELECT id FROM founders WHERE company_id=? AND name=?",
            (company_id, name),
        ).fetchone()
    return int(row["id"])


def upsert_email(
    company_id: str,
    email: str,
    *,
    founder_id: int | None = None,
    source: str = "guess",
    verified: bool = False,
    is_role: bool = False,
    is_catch_all: bool = False,
    confidence: float = 0.0,
    raw: dict[str, Any] | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    conn = conn or connect()
    address = str(email or "").strip().lower()
    if not address or "@" not in address:
        return

    now = _now()

    # SQLite treats every NULL as distinct inside a UNIQUE index, so a partial
    # index cannot dedupe the company-level rows (founder_id IS NULL) and the
    # ON CONFLICT clause below would never fire for them. Every re-import would
    # append duplicates. So look the row up explicitly and branch, which is
    # unambiguous for both the NULL and non-NULL cases.
    if founder_id is None:
        existing = conn.execute(
            "SELECT id FROM emails WHERE company_id=? AND email=? "
            "AND founder_id IS NULL",
            (company_id, address),
        ).fetchone()
    else:
        existing = conn.execute(
            "SELECT id FROM emails WHERE company_id=? AND email=? "
            "AND founder_id=?",
            (company_id, address, founder_id),
        ).fetchone()

    with write(conn):
        if existing:
            conn.execute(
                "UPDATE emails SET source=?, verified=?, is_role=?, "
                "is_catch_all=?, confidence=?, raw=?, checked_at=? WHERE id=?",
                (
                    source, 1 if verified else 0, 1 if is_role else 0,
                    1 if is_catch_all else 0, float(confidence or 0.0),
                    _dumps(raw or {}), now, existing["id"],
                ),
            )
            return
        conn.execute(
            "INSERT INTO emails (founder_id, company_id, email, source, "
            "verified, is_role, is_catch_all, confidence, raw, checked_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                founder_id,
                company_id,
                address,
                source,
                1 if verified else 0,
                1 if is_role else 0,
                1 if is_catch_all else 0,
                float(confidence or 0.0),
                _dumps(raw or {}),
                now,
            ),
        )


def set_flag(
    company_id: str,
    *,
    sent_at: int | None = None,
    hidden: bool | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Set sent/hidden independently of whether the company row exists.

    Only the arguments you pass are written, so marking a card hidden never
    clobbers the timestamp of when you emailed it.
    """
    conn = conn or connect()
    # The raw values are bound twice on purpose. The INSERT needs
    # COALESCE(?, 0) because the columns are NOT NULL, but that would also
    # coerce an omitted argument to a hard 0 -- and since excluded.sent_at is
    # then never NULL, the COALESCE in the DO UPDATE clause would overwrite a
    # real sent_at with 0. Passing the untouched parameter in the update clause
    # keeps NULL meaning "leave this alone".
    sent = None if sent_at is None else int(sent_at)
    hidden_value = None if hidden is None else int(bool(hidden))
    with write(conn):
        conn.execute(
            "INSERT INTO company_flags (company_id, sent_at, hidden, updated_at) "
            "VALUES (?, COALESCE(?, 0), COALESCE(?, 0), ?) "
            "ON CONFLICT(company_id) DO UPDATE SET "
            "sent_at=COALESCE(?, company_flags.sent_at), "
            "hidden=COALESCE(?, company_flags.hidden), "
            "updated_at=excluded.updated_at",
            (
                str(company_id),
                sent,
                hidden_value,
                _now(),
                sent,
                hidden_value,
            ),
        )


def get_flags() -> dict[str, dict[str, int]]:
    rows = connect().execute(
        "SELECT company_id, sent_at, hidden FROM company_flags"
    ).fetchall()
    return {
        row["company_id"]: {
            "sent_at": int(row["sent_at"] or 0),
            "hidden": int(row["hidden"] or 0),
        }
        for row in rows
    }


# --------------------------------------------------------------------------- #
# Import: browser state -> SQLite
# --------------------------------------------------------------------------- #

# The six email_source values api/yc.py documents, plus the two Apify variants
# the client writes. Anything else is stored as 'guess' so an unexpected value
# from a newer build cannot poison the send filter.
EMAIL_SOURCES = frozenset(
    {
        "verified", "unverified", "site", "site_role", "guess", "role", "none",
        "apify-verified", "apify-unverified",
    }
)


def _founder_confidence(founder: dict[str, Any], email_source: str) -> float:
    """How much we trust this founder as a person.

    A founder with a verified personal address is a real, reachable human, so
    they earn high confidence. A name scraped off a team page with no address
    at all stays at 0 -- that is precisely the set people.py will later have to
    score on its own.
    """
    if email_source in ("verified", "apify-verified"):
        return 0.95
    if email_source in ("site", "unverified", "apify-unverified"):
        return 0.85
    if email_source in ("site_role", "role"):
        return 0.5  # reaches a human, but almost certainly not *this* founder
    if email_source == "guess":
        return 0.6  # a name we derived and an address we pattern-matched
    return 0.0


def import_state(
    state: dict[str, Any],
    *,
    replace: bool = False,
    conn: sqlite3.Connection | None = None,
) -> dict[str, int]:
    """Upsert an entire browser state blob.

    Idempotent by construction: every write is keyed on a stable identity and
    uses ON CONFLICT, so re-running after a partial failure converges instead of
    duplicating.
    """
    if not isinstance(state, dict):
        raise UpstreamError("state must be a JSON object", 400)

    conn = conn or connect()
    migrate(conn)

    counters = {
        "companies": 0, "founders": 0, "emails": 0,
        "skipped": 0, "sent_marks": 0,
    }

    # One transaction for the whole import. A half-applied import is worse than
    # one that did not start, and the UI can simply retry. write() nests, so
    # every upsert_* helper below joins this transaction instead of opening its
    # own -- which is also what makes them individually atomic when called
    # outside this function.
    with write(conn):
        if replace:
            # Child rows first: foreign_keys=ON makes this order mandatory.
            for table in ("deliveries", "queue", "emails", "founders",
                          "company_flags", "companies"):
                conn.execute(f"DELETE FROM {table}")

        _import_template(conn, state)
        _import_sender(conn, state)
        _import_batches(conn, state, counters)
        _import_flags(conn, state, counters)

    return counters


def _import_template(conn: sqlite3.Connection, state: dict[str, Any]) -> None:
    template = state.get("template")
    if isinstance(template, dict):
        upsert_template(
            DEFAULT_TEMPLATE_KEY,
            str(template.get("subject") or DEFAULT_TEMPLATE["subject"]),
            str(template.get("body") or DEFAULT_TEMPLATE["body"]),
            conn,
        )
    else:
        seed_default_template(conn)


def _import_sender(conn: sqlite3.Connection, state: dict[str, Any]) -> None:
    """Persist the sender identity as settings, not as a company.

    Deliberately not the Apify token: that stays a browser-side localStorage
    secret and must never be written to a database file on disk.
    """
    details = state.get("details")
    if not isinstance(details, dict):
        return
    for key in ("my_name", "portfolio", "github", "resume"):
        if details.get(key):
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"sender.{key}", str(details[key])),
            )

    # Remember which batch was on screen. Without this a restore has to guess,
    # and the UI renders nothing until the user picks one.
    active = state.get("activeBatch")
    if isinstance(active, str) and active.strip():
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("ui.active_batch", active.strip()),
        )


def _import_batches(
    conn: sqlite3.Connection, state: dict[str, Any], counters: dict[str, int]
) -> None:
    batches = state.get("batches")
    if not isinstance(batches, dict):
        raise UpstreamError("state.batches must be an object", 400)

    seen_ids: set[str] = set()
    for batch_name, batch in batches.items():
        if not isinstance(batch, dict):
            continue
        companies = batch.get("companies")
        if not isinstance(companies, dict):
            continue

        for company in companies.values():
            if not isinstance(company, dict):
                counters["skipped"] += 1
                continue

            # A company can appear under two batch keys if batches were merged.
            # Count it once, and only walk its founders the first time.
            try:
                cid = upsert_company(company, source="yc", conn=conn)
            except UpstreamError:
                counters["skipped"] += 1
                continue

            # The batch name is a property of the browser's grouping and is not
            # in the raw object, so restore it into both the column and the blob.
            if batch_name and not company.get("batch"):
                blob = dict(company)
                blob["batch"] = str(batch_name)
                conn.execute(
                    "UPDATE companies SET batch=?, raw=? "
                    "WHERE id=? AND batch=''",
                    (str(batch_name), _dumps(blob), cid),
                )

            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            counters["companies"] += 1
            _import_founders(conn, cid, company, counters)
            _import_company_emails(conn, cid, company)


def _import_flags(
    conn: sqlite3.Connection, state: dict[str, Any], counters: dict[str, int]
) -> None:
    """Sent and hidden are per-company flags, never companies of their own.

    An earlier draft inserted placeholder rows for them, which then surfaced as
    blank cards in the UI. They now live in company_flags, which has no foreign
    key -- so a hidden id whose company row is gone is still remembered, and
    cannot turn into a nameless card.
    """
    sent_marks = state.get("sent")
    if isinstance(sent_marks, dict):
        for company_id, mark in sent_marks.items():
            at = mark.get("at") if isinstance(mark, dict) else 0
            # The browser stores milliseconds; keep that unit so a round trip
            # does not silently change the meaning of an existing timestamp.
            set_flag(str(company_id), sent_at=int(at or _now() * 1000), conn=conn)
            counters["sent_marks"] += 1

    hidden_ids = state.get("hidden")
    if isinstance(hidden_ids, list):
        for company_id in hidden_ids:
            set_flag(str(company_id), hidden=True, conn=conn)


def _import_founders(
    conn: sqlite3.Connection,
    company_id: str,
    company: dict[str, Any],
    counters: dict[str, int],
) -> None:
    founders = company.get("founders")
    if not isinstance(founders, list):
        return

    for founder in founders:
        if not isinstance(founder, dict):
            continue
        name = str(founder.get("name") or "").strip()
        if not name:
            counters["skipped"] += 1
            continue

        source = str(founder.get("email_source") or "none")
        if source not in EMAIL_SOURCES:
            source = "guess"

        confidence = founder.get("email_confidence")
        if not isinstance(confidence, (int, float)):
            confidence = _founder_confidence(founder, source)

        record = dict(founder)
        record["email_source"] = source
        record["confidence"] = float(confidence)
        # `key` and the email-derived fields are client bookkeeping, not
        # founder identity; drop the ones we re-derive on every import.
        record.pop("key", None)

        founder_id = upsert_founder(
            company_id, record, conn=conn
        )
        counters["founders"] += 1

        address = str(founder.get("email") or "").strip()
        if not address:
            continue
        upsert_email(
            company_id,
            address,
            founder_id=founder_id,
            source=source,
            verified=source in ("verified", "apify-verified"),
            is_role=source in ("site_role", "role"),
            confidence=float(confidence),
            raw={"email_notes": founder.get("email_notes") or ""},
            conn=conn,
        )
        counters["emails"] += 1


def _import_company_emails(
    conn: sqlite3.Connection, company_id: str, company: dict[str, Any]
) -> None:
    """Address with no founder attached: site_emails() output, page_emails."""
    for address in company.get("site_emails") or []:
        if isinstance(address, dict):
            value = str(address.get("email") or "")
            kind = str(address.get("kind") or "")
        else:
            value, kind = str(address or ""), ""
        if not value:
            continue
        upsert_email(
            company_id,
            value,
            founder_id=None,
            source="site" if kind != "role" else "site_role",
            is_role=kind == "role",
            conn=conn,
        )

    for address in company.get("page_emails") or []:
        value = address.get("email") if isinstance(address, dict) else address
        if not value:
            continue
        upsert_email(company_id, value, founder_id=None, source="site", conn=conn)


# --------------------------------------------------------------------------- #
# Import: SQLite -> browser state (restore)
# --------------------------------------------------------------------------- #


def export_state(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Rebuild a browser-shaped state blob from the database.

    Round-tripping with import_state() is lossless for the fields the UI reads,
    which means the browser can be wiped (new laptop, cleared profile) without
    losing research.
    """
    conn = conn or connect()
    seed_default_template(conn)

    template_row = conn.execute(
        "SELECT subject, body FROM templates WHERE key=?", (DEFAULT_TEMPLATE_KEY,)
    ).fetchone()

    batches: dict[str, Any] = {}
    rows = conn.execute(
        "SELECT * FROM companies ORDER BY created_at, name"
    ).fetchall()
    for row in rows:
        company = json.loads(row["raw"] or "{}")
        company.setdefault("id", row["id"])
        if not company.get("name"):
            company["name"] = row["name"]
        if not company.get("domain"):
            company["domain"] = row["domain"]

        founder_rows = conn.execute(
            "SELECT * FROM founders WHERE company_id=? ORDER BY id",
            (row["id"],),
        ).fetchall()
        if founder_rows:
            company["founders"] = []
            company.setdefault("founders_state", "loaded")
            for frow in founder_rows:
                founder = json.loads(frow["raw"] or "{}")
                founder.update({
                    "key": founder.get("key")
                           or f"{frow['first_name']}.{frow['last_name']}".lower(),
                    "name": frow["name"],
                    "first_name": frow["first_name"],
                    "last_name": frow["last_name"],
                })
                founder.setdefault("title", frow["title"])

                address_row = conn.execute(
                    "SELECT email, source, verified, confidence FROM emails "
                    "WHERE founder_id=? ORDER BY verified DESC, confidence DESC",
                    (frow["id"],),
                ).fetchone()
                if address_row:
                    founder["email"] = address_row["email"]
                    founder["email_source"] = address_row["source"]
                    founder["email_confidence"] = address_row["confidence"]
                    founder["has_email"] = bool(address_row["verified"])
                company["founders"].append(founder)

        address_rows = conn.execute(
            "SELECT email, source, is_role FROM emails "
            "WHERE company_id=? AND founder_id IS NULL ORDER BY id",
            (row["id"],),
        ).fetchall()
        if address_rows:
            company["site_emails"] = [
                {
                    "email": arow["email"],
                    "source": arow["source"],
                    "kind": "role" if arow["is_role"] else "person",
                }
                for arow in address_rows
            ]
            company.setdefault("site_state", "done")

        batch_name = row["batch"] or "Unbatched"
        entry = batches.setdefault(
            batch_name, {"offset": 0, "total": 0, "companies": {}}
        )
        entry["companies"][str(row["id"])] = company
        entry["total"] = len(entry["companies"])

    settings = all_settings()
    sender = {
        key: settings.get(f"sender.{key}", "")
        for key in ("my_name", "portfolio", "github", "resume")
    }

    # Anything marked sent at import time carries a timestamp; anything that
    # only existed as a bare id has none. That difference is what tells the two
    # groups apart here.
        # The batch the user was last looking at. The UI renders exactly one batch,
    # so this is what decides whether a restore lands somewhere useful. Prefer
    # the explicitly remembered one, then fall back to the most recently
    # imported batch rather than any hardcoded name -- batch names change every
    # few months and a literal here would rot.
    active_batch = get_setting("ui.active_batch", "")
    if active_batch not in batches:
        freshest = conn.execute(
            "SELECT batch, MAX(updated_at) AS at FROM companies "
            "WHERE batch != '' GROUP BY batch ORDER BY at DESC LIMIT 1"
        ).fetchone()
        active_batch = freshest["batch"] if freshest else ""
    if active_batch not in batches:
        active_batch = next(iter(batches), "")

    # Flags live in their own table, so a hidden or sent id survives even when
    # its company row is gone. That is the point: the browser's sent list is a
    # memory of what you already did, and losing it invites a duplicate email.
    flags = get_flags()
    sent: dict[str, Any] = {}
    hidden: list[str] = []
    for company_id, flag in flags.items():
        if flag["sent_at"]:
            sent[company_id] = {"at": flag["sent_at"], "emails": []}
        if flag["hidden"]:
            hidden.append(company_id)

    template: dict[str, Any] = {}
    if template_row:
        template = {
            "subject": template_row["subject"],
            "body": template_row["body"],
            "dropEmptyLines": True,
        }

    return {
        "version": STATE_VERSION,
        "details": sender,
        "template": template,
        "apify": {},  # never restored: the token must stay a local-only secret
        "batches": batches,
        # Must not be blank: the UI renders only the active batch, so returning
        # "" would restore a database that looks empty until the user picks one.
        "activeBatch": active_batch,
        "sent": sent,
        "hidden": hidden,
        "ui": {},
        "_meta": {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _now(),
            "companies": len(rows),
        },
    }


# --------------------------------------------------------------------------- #
# Queue
# --------------------------------------------------------------------------- #

QUEUE_STATUSES = ("draft", "queued", "approved", "sending", "sent",
                  "failed", "bounced", "skipped", "unsubscribed")


def enqueue(
    items: list[dict[str, Any]],
    *,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Add review items. Idempotent: the same company+address is not queued
    twice, so a double-click cannot produce a double send."""
    conn = conn or connect()
    now = _now()
    added = 0
    with write(conn):
        for item in items:
            address = str(item.get("email") or "").strip().lower()
            company_id = str(item.get("company_id") or "")
            if not address or not company_id:
                continue
            existing = conn.execute(
                "SELECT 1 FROM queue WHERE company_id=? AND email=? "
                "AND status NOT IN ('skipped')",
                (company_id, address),
            ).fetchone()
            if existing:
                continue
            conn.execute(
                "INSERT INTO queue (company_id, founder_id, founder_name, "
                "email, subject, body, status, created_at) "
                "VALUES (?,?,?,?,?,?, 'draft', ?)",
                (
                    company_id,
                    item.get("founder_id"),
                    str(item.get("founder_name") or ""),
                    address,
                    str(item.get("subject") or ""),
                    str(item.get("body") or ""),
                    now,
                ),
            )
            added += 1
    return added


def list_queue(
    status: str = "", *,
    limit: int = 0,
    oldest_first: bool = False,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Rows in the review queue.

    Newest first by default, which is what a reviewer wants: the draft you just
    made is the one you are looking at. The sender asks for oldest_first so a
    daily cap drains the drafts that have been waiting longest rather than
    whichever rows happen to be newest.

    The company name is joined in rather than stored on the queue, so renaming a
    company updates every queued draft instead of leaving a stale copy behind.
    """
    conn = conn or connect()
    order = "ASC" if oldest_first else "DESC"
    sql = ("SELECT queue.*, companies.name AS company_name "
           "FROM queue LEFT JOIN companies ON companies.id = queue.company_id")
    params: list[Any] = []
    if status:
        sql += " WHERE queue.status=?"
        params.append(status)
    # Qualified, because both tables have an id and an unqualified one would be
    # ambiguous as soon as the join is there.
    sql += f" ORDER BY queue.id {order}"
    if limit > 0:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def count_queue(status: str, *, since: int = 0,
                conn: sqlite3.Connection | None = None) -> int:
    conn = conn or connect()
    if since:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM queue WHERE status='sent' AND sent_at>=?",
            (since,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM queue WHERE status='sent'"
        ).fetchone()
    return int(row["n"] or 0)


def set_queue_status(
    queue_id: int,
    status: str,
    *,
    error: str = "",
    message_id: str = "",
    conn: sqlite3.Connection | None = None,
) -> None:
    if status not in QUEUE_STATUSES:
        raise UpstreamError(f"unknown queue status '{status}'", 400)
    conn = conn or connect()
    with write(conn):
        conn.execute(
            "UPDATE queue SET status=?, error=?, message_id=?, "
            "sent_at=CASE WHEN ?='sent' THEN ? ELSE sent_at END WHERE id=?",
            (status, error, message_id, status, _now(), queue_id),
        )


def set_queue_status_bulk(
    queue_ids: Iterable[int],
    status: str,
    *,
    error: str = "",
    conn: sqlite3.Connection | None = None,
) -> int:
    """Move many queue rows at once and report how many actually changed."""
    if status not in QUEUE_STATUSES:
        raise UpstreamError(f"unknown queue status '{status}'", 400)
    conn = conn or connect()
    ids = [int(i) for i in queue_ids]
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    with write(conn):
        cursor = conn.execute(
            "UPDATE queue SET status=?, error=?, "
            "sent_at=CASE WHEN ?='sent' THEN ? ELSE sent_at END "
            f"WHERE id IN ({placeholders})",
            [status, error, status, _now(), *ids],
        )
    return cursor.rowcount


def log_delivery(
    queue_id: int, event: str, detail: str = "",
    *, conn: sqlite3.Connection | None = None,
) -> None:
    conn = conn or connect()
    with write(conn):
        conn.execute(
            "INSERT INTO deliveries (queue_id, event, detail, at) "
            "VALUES (?,?,?,?)",
            (queue_id, event, detail[:500], _now()),
        )


def approve(queue_ids: list[int], *, conn: sqlite3.Connection | None = None) -> int:
    conn = conn or connect()
    if not queue_ids:
        return 0
    marks = ",".join("?" for _ in queue_ids)
    with write(conn):
        cur = conn.execute(
            f"UPDATE queue SET status='approved', approved=1 "
            f"WHERE id IN ({marks}) AND status IN ('draft','queued')",
            queue_ids,
        )
        return cur.rowcount


def is_unsubscribed(email: str, *, conn: sqlite3.Connection | None = None) -> bool:
    conn = conn or connect()
    row = conn.execute(
        "SELECT 1 FROM unsubscribes WHERE email=?",
        (str(email or "").strip().lower(),),
    ).fetchone()
    return row is not None


def unsubscribe(email: str, reason: str = "link",
                *, conn: sqlite3.Connection | None = None) -> bool:
    conn = conn or connect()
    address = str(email or "").strip().lower()
    if not address:
        return False
    with write(conn):
        conn.execute(
            "INSERT INTO unsubscribes (email, at, reason) VALUES (?,?,?) "
            "ON CONFLICT(email) DO NOTHING",
            (address, _now(), reason),
        )
        # Anything still waiting to go out for this address must not go.
        conn.execute(
            "UPDATE queue SET status='unsubscribed' "
            "WHERE email=? AND status IN ('draft','queued','approved')",
            (address,),
        )
    return True


def list_unsubscribed(conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    conn = conn or connect()
    rows = conn.execute(
        "SELECT email, at, reason FROM unsubscribes ORDER BY at DESC"
    ).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------- #
# Querying
# --------------------------------------------------------------------------- #


def list_companies(
    *,
    query: str = "",
    batch: str = "",
    needs_email: bool = False,
    limit: int = 50,
    offset: int = 0,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Search stored companies.

    `query` matches name or domain. `needs_email` restricts to companies that
    have at least one founder with an address -- the set worth sending to.
    """
    conn = conn or connect()
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))

    where: list[str] = []
    params: list[Any] = []

    if query:
        # Escape LIKE metacharacters so a literal '%' in a search box does not
        # turn into a wildcard scan.
        escaped = _LIKE_ESCAPE_RE.sub(r"\\\1", query.strip().lower())
        where.append(
            "(LOWER(name) LIKE ? ESCAPE '\\' OR LOWER(domain) LIKE ? ESCAPE '\\')"
        )
        params += [f"%{escaped}%", f"%{escaped}%"]

    if batch:
        where.append("batch = ?")
        params.append(batch)

    if needs_email:
        where.append(
            "id IN (SELECT company_id FROM emails WHERE founder_id IS NOT NULL)"
        )

    clause = f" WHERE {' AND '.join(where)}" if where else ""
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM companies{clause}", params
    ).fetchone()["n"]

    rows = conn.execute(
        f"SELECT id, source, slug, name, domain, logo, batch, location, "
        f"team_size, updated_at FROM companies{clause} "
        f"ORDER BY updated_at DESC, name LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()

    companies = []
    for row in rows:
        company = dict(row)
        company["founder_count"] = conn.execute(
            "SELECT COUNT(*) AS n FROM founders WHERE company_id=?",
            (row["id"],),
        ).fetchone()["n"]
        company["email_count"] = conn.execute(
            "SELECT COUNT(*) AS n FROM emails WHERE company_id=?",
            (row["id"],),
        ).fetchone()["n"]
        companies.append(company)

    return {
        "companies": companies,
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
    }


def stats(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    if conn is None:
        migrate()  # a fresh checkout has no schema yet; report zeros, not a traceback
        conn = connect()
    counts = {
        table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        for table in ("companies", "founders", "emails", "queue",
                      "deliveries", "unsubscribes", "templates")
    }
    counts["queue_pending"] = conn.execute(
        "SELECT COUNT(*) AS n FROM queue WHERE status IN "
        "('draft','queued','approved','sending')"
    ).fetchone()["n"]
    counts["queue_sent"] = conn.execute(
        "SELECT COUNT(*) AS n FROM queue WHERE status='sent'"
    ).fetchone()["n"]
    return counts


def clear(conn: sqlite3.Connection | None = None) -> None:
    """Wipe research data. Used by the `db_clear` action behind a confirmation.

    Settings and templates survive deliberately: the sender identity and the
    email template are expensive to retype and are not research output. Use
    `db_clear` plus deleting the file if you want a truly blank slate.
    """
    conn = conn or connect()
    with write(conn):
        for table in ("deliveries", "queue", "emails", "founders",
                      "company_flags", "companies"):
            conn.execute(f"DELETE FROM {table}")
    # VACUUM rewrites the file to reclaim the space, and SQLite refuses to run
    # it inside a transaction -- so it has to happen after write() commits.
    conn.execute("VACUUM")


# --------------------------------------------------------------------------- #
# HTTP router -- local only, never deployed
# --------------------------------------------------------------------------- #


def _one(query: dict[str, Any], name: str, default: str = "") -> str:
    value = query.get(name, default)
    if isinstance(value, (list, tuple)):
        return value[0] if value else default
    return value or default


def _footprint(path: Path) -> dict[str, int | bool]:
    """On-disk size of the database, counting the WAL.

    In WAL mode the main file can sit at a single 4 KiB page while every row
    lives in the -wal sidecar, so reporting the main file alone makes a
    populated database look empty -- which is exactly the question both the CLI
    and the browser's storage panel are asking.
    """
    exists = path.exists()
    file_bytes = path.stat().st_size if exists else 0
    wal = path.with_name(path.name + "-wal")
    wal_bytes = wal.stat().st_size if wal.exists() else 0
    return {
        "exists": exists,
        "file_bytes": file_bytes,
        "wal_bytes": wal_bytes,
        "total_bytes": file_bytes + wal_bytes,
    }


def handle_request(
    query: dict[str, Any], body: dict[str, Any] | None = None
) -> tuple[int, dict[str, Any]]:
    """Dispatch a /api/db call.

    GETs are reads, so they are safe and cacheable-by-accident. Anything that
    writes requires the state blob in `body` -- and because localStorage is the
    only durable record the browser has until this ships, the writes are
    deliberate, explicit POSTs rather than a background autosave. That also
    sidesteps every browser autocompletion and reload race.
    """
    body = body or {}

    # The action may arrive in the query string or in the body. POST callers put
    # everything in the body, so support both -- an explicit query wins, because
    # that is what the GET-side callers send.
    action = (
        _one(query, "action")
        or str(body.get("action") or "")
        or "status"
    ).strip().lower()

    if action == "status":
        migrate()
        path = db_path()
        return 200, {
            "ok": True,
            "available": True,
            "path": str(path),
            "schema_version": SCHEMA_VERSION,
            "state_version": STATE_VERSION,
            **_footprint(path),
            "stats": stats(),
        }

    if action == "import":
        payload = body.get("state") if isinstance(body.get("state"), dict) else body
        if not isinstance(payload, dict):
            raise UpstreamError(
                "expected {\"state\": {...}} in the request body", 400
            )
        counters = import_state(payload, replace=bool(body.get("replace")))
        return 200, {"ok": True, "imported": counters, "stats": stats()}

    if action == "export":
        return 200, {"ok": True, "state": export_state()}

    if action == "clear":
        clear()
        return 200, {"ok": True, "stats": stats()}

    if action == "companies":
        return 200, {
            "ok": True,
            **list_companies(
                query=_one(query, "q"),
                batch=_one(query, "batch"),
                needs_email=_one(query, "needs_email").lower() in ("1", "true"),
                limit=int(_one(query, "limit", "50") or 50),
                offset=int(_one(query, "offset", "0") or 0),
            ),
        }

    if action == "queue_list":
        return 200, {"ok": True, "queue": list_queue(
            _one(query, "status"),
            limit=int(_one(query, "limit", "200") or 200),
            oldest_first=_one(query, "oldest_first") in ("1", "true", "yes"),
        )}

    if action == "queue_add":
        items = body.get("items")
        if not isinstance(items, list):
            raise UpstreamError("expected {\"items\": [...]} in the body", 400)
        added = enqueue(items)
        return 200, {"ok": True, "added": added, "queue": list_queue()}

    if action == "queue_approve":
        ids = body.get("ids")
        if not isinstance(ids, list):
            raise UpstreamError("expected {\"ids\": [...]} in the body", 400)
        return 200, {"ok": True, "approved": approve([int(i) for i in ids])}

    if action == "queue_status":
        ids = body.get("ids")
        if isinstance(ids, list):
            # A reviewer approves or skips a list in one gesture, so let the
            # whole batch land in one transaction. Doing it per-row would leave
            # a half-applied change if the request failed partway. An empty
            # list is a deliberate no-op, not a missing argument.
            changed = set_queue_status_bulk(
                [int(i) for i in ids],
                str(body.get("status") or "skipped"),
                error=str(body.get("error") or ""),
            )
            return 200, {"ok": True, "changed": changed}
        queue_id = body.get("id") or _one(query, "id")
        if not queue_id:
            raise UpstreamError("id or ids is required", 400)
        set_queue_status(
            int(queue_id),
            str(body.get("status") or _one(query, "status") or "skipped"),
            error=str(body.get("error") or ""),
            message_id=str(body.get("message_id") or ""),
        )
        return 200, {"ok": True, "changed": 1}

    if action == "unsubscribe":
        address = str(body.get("email") or _one(query, "email")).strip()
        if not address:
            raise UpstreamError("email is required", 400)
        unsubscribe(address, str(body.get("reason") or _one(query, "reason") or "link"))
        return 200, {"ok": True, "unsubscribed": address}

    if action == "settings_set":
        for key, value in body.items():
            if key.startswith("_") or not isinstance(key, str):
                continue
            set_setting(key, str(value))
        return 200, {"ok": True, "settings": all_settings()}

    raise UpstreamError(f"unknown action '{action}'", 400)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
#
# Handy without the browser: inspect the database, pull a state blob out as
# JSON, and push one back in.

_EXAMPLES = """
Examples
--------
    python3 db.py status
    python3 db.py stats
    python3 db.py companies --needs-email
    python3 db.py export --out data/restore.json
    python3 db.py import data/restore.json

The database path comes from $APPLYONCE_DB, else ./applyonce.db.
Standard library only, like everything else in this project.
"""


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Inspect and manage the ApplyOnce database.",
        epilog=_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", help="override the database path")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="show path, size, and schema version")
    sub.add_parser("stats", help="row counts per table")

    companies = sub.add_parser("companies", help="search stored companies")
    companies.add_argument("--q", default="", help="match name or domain")
    companies.add_argument("--batch", default="", help="restrict to one batch")
    companies.add_argument("--needs-email", action="store_true",
                           help="only companies with a founder address")
    companies.add_argument("--limit", type=int, default=50)

    export = sub.add_parser("export", help="write a restorable state blob")
    export.add_argument("--out", default="-")

    importer = sub.add_parser("import", help="load a state blob")
    importer.add_argument("file")
    importer.add_argument("--replace", action="store_true",
                          help="wipe companies first instead of upserting")

    args = parser.parse_args()
    if args.db:
        os.environ["APPLYONCE_DB"] = args.db

    command = args.command or "status"
    try:
        if command == "status":
            # Read-only on purpose. Calling migrate() here would create the
            # database and its parent directory as a side effect of asking
            # where the database is, so `status` would answer "exists: true"
            # for a path the user has never touched.
            path = db_path()
            schema_version = None
            if path.exists():
                # A short-lived connection, and no migrate(): an unopened file
                # still reports its real version (0 = no schema yet).
                conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                try:
                    schema_version = conn.execute(
                        "PRAGMA user_version").fetchone()[0]
                finally:
                    conn.close()
            print(json.dumps({
                "path": str(path),
                # 0 means "file present but no schema applied yet".
                "schema_version": schema_version,
                **_footprint(path),
            }, indent=2))
        elif command == "stats":
            print(json.dumps(stats(), indent=2))
        elif command == "companies":
            print(json.dumps(list_companies(
                query=args.q, batch=args.batch,
                needs_email=args.needs_email, limit=args.limit,
            ), indent=2, ensure_ascii=False))
        elif command == "export":
            blob = export_state()
            text = json.dumps(blob, indent=2, ensure_ascii=False)
            if args.out == "-":
                print(text)
            else:
                out = Path(args.out)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(text, encoding="utf-8")
                print(f"wrote {out} ({len(blob['batches'])} batches)")
        elif command == "import":
            payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
            print(json.dumps(import_state(payload, replace=args.replace), indent=2))
    except UpstreamError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        # Safety net so no subcommand ever surfaces a raw traceback.
        print(f"database error: {exc}", file=sys.stderr)
        return 1
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())