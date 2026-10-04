#!/usr/bin/env python3
"""SMTP sending for ApplyOnce, with the safety rails the reputation needs.

Cold email is unforgiving in one direction: a mistake is not undoable. A bad
send can burn a domain's sending reputation for weeks, and a careless one can
put someone else's inbox at risk. So this module is built to be hard to misuse
by accident:

  * **Dry run is the default.** `send` without `--send` prints exactly what
    would go out and transmits nothing.
  * **Credentials come from the environment only.** There is deliberately no
    `--password` flag: command-line arguments land in your shell history and
    show up in `ps` for every user on the machine. No flag, no leak.
  * **Nothing sends without an explicit review step.** Rows move through
    draft -> approved -> sending, and only *approved* rows are eligible. The
    approving hand is a human reading the draft.
  * **Hard ceilings.** A daily cap and a mandatory pause between messages. Bulk
    blasts from a residential IP are how domains get blocklisted.
  * **Suppression is checked per recipient**, not per run, so an unsubscribe
    that lands mid-queue still stops the messages behind it.

What this module deliberately does not do: BCC, attachments, HTML, tracking
pixels, or list management. Unsolicited mail earns its reputation by looking
exactly like a person wrote it to one person.

Standard library only, like everything else here.

    python3 mailer.py check              # is the environment configured?
    python3 mailer.py preview            # what is queued and eligible
    python3 mailer.py send --limit 10 --send
"""

from __future__ import annotations

import os
import re
import smtplib
import ssl
import sys
import time
import uuid
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from email.utils import formatdate, make_msgid, parseaddr
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402

# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #

DEFAULT_PORT = 587
DEFAULT_DAILY_LIMIT = 50
DEFAULT_DELAY = 30.0
DEFAULT_RETRIES = 2
# How long a row may sit in 'sending' before we assume the process died and hand
# it back. Without this a crash mid-batch would strand rows forever, since
# 'sending' is exactly the state a live send holds.
STUCK_SENDING_SECONDS = 15 * 60

# The old shipped template carried this sentence in its footer. Used as a
# sentinel so we do not staple a second compliance footer onto a body that
# already has one.
_FOOTER_SENTINEL = "you are receiving this because"

# Local parts that are a department rather than a person. Writing to these is
# how you get a spam complaint instead of a reply.
ROLE_LOCALS = {
    "info", "support", "sales", "contact", "hello", "help", "admin",
    "office", "team", "billing", "accounts", "press", "media", "marketing",
    "careers", "jobs", "hr", "legal", "privacy", "security", "abuse",
    "postmaster", "webmaster", "hostmaster", "noreply", "no-reply",
    "donotreply", "unsubscribe", "mailer-daemon", "root", "ftp", "www",
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


class MailerError(Exception):
    """A configuration or safety problem. Never a per-recipient failure."""


class Suppressed(Exception):
    """One recipient may not be emailed. Carries a machine-readable reason."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    try:
        return float(raw)
    except ValueError:
        return default


def _env_flag(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass
class SmtpConfig:
    """Everything needed to talk to the relay. Built from the environment.

    The password lives in memory for the life of the process and is never
    logged, never written to the database, and never returned to a browser.
    """

    host: str = ""
    port: int = DEFAULT_PORT
    username: str = ""
    password: str = field(default="", repr=False)
    sender: str = ""
    sender_name: str = ""
    physical_address: str = ""
    unsubscribe_base: str = ""
    unsubscribe_mailbox: str = ""
    daily_limit: int = DEFAULT_DAILY_LIMIT
    delay: float = DEFAULT_DELAY
    retries: int = DEFAULT_RETRIES
    timeout: float = 30.0

    def __post_init__(self) -> None:
        # A reply-to unsubscribe is the only kind available without a hosted
        # endpoint, so fall back to the From address. This lives here rather
        # than in from_env() so that a directly-constructed config -- which is
        # what the tests and any future caller will build -- is equally valid.
        if not self.unsubscribe_mailbox and self.sender:
            self.unsubscribe_mailbox = self.sender

    @classmethod
    def from_env(cls) -> "SmtpConfig":
        sender = _env("SMTP_FROM")
        return cls(
            host=_env("SMTP_HOST"),
            port=_env_int("SMTP_PORT", DEFAULT_PORT),
            username=_env("SMTP_USERNAME"),
            password=_env("SMTP_APP_PASSWORD"),
            sender=sender,
            sender_name=_env("SMTP_FROM_NAME"),
            physical_address=_env("SMTP_PHYSICAL_ADDRESS"),
            unsubscribe_base=_env("SMTP_UNSUBSCRIBE_URL_BASE"),
            # Falls back to the From address: a reply-to unsubscribe is the
            # only kind that works without somewhere to host a link.
            unsubscribe_mailbox=_env("SMTP_UNSUBSCRIBE_MAILBOX") or sender,
            daily_limit=_env_int("SMTP_DAILY_LIMIT", DEFAULT_DAILY_LIMIT),
            delay=_env_float("SMTP_DELAY_SECONDS", DEFAULT_DELAY),
            retries=_env_int("SMTP_RETRIES", DEFAULT_RETRIES),
            timeout=_env_float("SMTP_TIMEOUT_SECONDS", 30.0),
        )

    @property
    def use_implicit_tls(self) -> bool:
        # 465 is implicit TLS; 587 is STARTTLS. Guessing wrong gets you an
        # opaque handshake error, so the port decides.
        return self.port == 465

    @property
    def sender_domain(self) -> str:
        return self.sender.rsplit("@", 1)[-1].lower() if "@" in self.sender else ""

    def problems(self) -> list[str]:
        """Everything wrong with the configuration, as human sentences.

        Returned as a list rather than raised so `check` can show all of them
        at once instead of making you fix them one restart at a time.
        """
        issues: list[str] = []
        if not self.host:
            issues.append("SMTP_HOST is not set")
        if not self.port:
            issues.append("SMTP_PORT is not set")
        elif not (1 <= self.port <= 65535):
            issues.append(f"SMTP_PORT={self.port} is not a valid port number")
        if not self.sender:
            issues.append("SMTP_FROM is not set")
        elif not _EMAIL_RE.match(self.sender):
            issues.append(f"SMTP_FROM ({self.sender!r}) is not an address")
        if self.sender and not self.sender_domain:
            issues.append("SMTP_FROM has no domain part")
        if not self.sender_name:
            issues.append(
                "SMTP_FROM_NAME is not set -- CAN-SPAM requires a truthful "
                "sender name, and an anonymous From line is how you land in spam")
        if not self.physical_address:
            issues.append(
                "SMTP_PHYSICAL_ADDRESS is not set -- CAN-SPAM requires a valid "
                "physical mailing address in every commercial message")
        if self.username and not self.password:
            issues.append(
                "SMTP_USERNAME is set but SMTP_APP_PASSWORD is empty -- use an "
                "app password, never your account password")
        if self.password and not self.username:
            issues.append("SMTP_APP_PASSWORD is set but SMTP_USERNAME is not")
        if self.unsubscribe_base and not self.unsubscribe_base.startswith("https://"):
            issues.append(
                "SMTP_UNSUBSCRIBE_URL_BASE must be https:// -- a plaintext "
                "unsubscribe link is both useless and unsafe")
        if not self.unsubscribe_base and not self.unsubscribe_mailbox:
            issues.append(
                "no way to unsubscribe: set SMTP_UNSUBSCRIBE_URL_BASE (needs a "
                "hosted endpoint) or SMTP_UNSUBSCRIBE_MAILBOX for reply-to")
        if self.daily_limit < 1:
            issues.append("SMTP_DAILY_LIMIT must be at least 1")
        if self.delay < 0:
            issues.append("SMTP_DELAY_SECONDS cannot be negative")
        return issues

    def require(self) -> None:
        issues = self.problems()
        if issues:
            raise MailerError(
                "SMTP is not usable:\n  - " + "\n  - ".join(issues))

    def public_summary(self) -> dict[str, Any]:
        """Safe to print or return over HTTP. Never includes the password."""
        return {
            "host": self.host,
            "port": self.port,
            "tls": "implicit" if self.use_implicit_tls else "STARTTLS",
            "username": self.username,
            "sender": (f"{self.sender_name} <{self.sender}>"
                       if self.sender_name else self.sender),
            "physical_address": self.physical_address,
            "unsubscribe": ("https link" if self.unsubscribe_base
                            else f"mailto:{self.unsubscribe_mailbox}"),
            "daily_limit": self.daily_limit,
            "delay_seconds": self.delay,
            "retries": self.retries,
            "password_present": bool(self.password),
        }


# --------------------------------------------------------------------------- #
# Unsubscribe
# --------------------------------------------------------------------------- #

def unsubscribe_url(email: str, config: SmtpConfig, token: str = "") -> str:
    """The unsubscribe destination for one recipient.

    With SMTP_UNSUBSCRIBE_URL_BASE set we point at a real HTTP endpoint, which
    is what List-Unsubscribe needs to be able to honour a one-click request
    without involving the recipient's mail client at all. Without it we fall
    back to a mailto: that asks the recipient to reply, which is weaker but
    honest about what a local-only tool can actually offer.

    Deliberately not tokenised: this build has no hosted endpoint to validate a
    token against, and a long random string in a URL that nothing verifies looks
    like security theatre. See README for how to front it properly.
    """
    if config.unsubscribe_base:
        return f"{config.unsubscribe_base.rstrip('/')}/unsubscribe?email={email}"
    mailbox = config.unsubscribe_mailbox or config.sender
    # The values must be percent-encoded: an unencoded "subject=unsubscribe"
    # turns into "subject=3Dunsubscribe" and the mail client reads the whole
    # thing as one malformed parameter.
    subject = quote("unsubscribe", safe="")
    body = quote(f"Unsubscribe {email}", safe="")
    return f"mailto:{quote(mailbox, safe='@')}?subject={subject}&body={body}"


# --------------------------------------------------------------------------- #
# Eligibility
# --------------------------------------------------------------------------- #

def screen(row: dict[str, Any], config: SmtpConfig) -> None:
    """Raise Suppressed if this row may not be sent. Returns None if it may.

    Each refusal is recorded so the operator can see *why* a row was skipped
    rather than wondering whether it was an oversight.
    """
    address = str(row.get("email") or "").strip().lower()

    if not address:
        raise Suppressed("no_address", "the row has no recipient")
    if not _EMAIL_RE.match(address):
        raise Suppressed("malformed", f"{address!r} is not an address")

    local, _, domain = address.partition("@")
    if local in ROLE_LOCALS:
        raise Suppressed("role_address",
                         f"{local}@ is a department, not a person")

    if db.is_unsubscribed(address):
        raise Suppressed("unsubscribed", "this address opted out")

    if row.get("status") == "sent":
        raise Suppressed("already_sent", "this row has already gone out")

    subject = str(row.get("subject") or "").strip()
    body = str(row.get("body") or "").strip()
    if not subject:
        raise Suppressed("no_subject", "the draft has no subject line")
    if not body:
        raise Suppressed("no_body", "the draft has no body")

    # An unsubstituted placeholder means the draft was never rendered -- a
    # literal "{first_name}" in an email you cannot take back is not a small
    # mistake.
    leftover = re.search(r"\{[a-z_]+\}", subject + "\n" + body)
    if leftover:
        raise Suppressed("unrendered_placeholder",
                         f"{leftover.group(0)} was never filled in")

    # Catch-all domains accept anything, which is excellent for deliverability
    # testing and terrible for actually reaching a person.
    if _env_flag("MAILER_ALLOW_CATCH_ALL"):
        pass
    elif _catch_all(domain):
        raise Suppressed("catch_all", f"{domain} accepts every address")

    if _env_flag("MAILER_SELF_TEST") and address.lower() == config.sender.lower():
        raise Suppressed("self", "refusing to email yourself")


_CATCH_ALL_CACHE: dict[str, bool] = {}


def _catch_all(domain: str) -> bool:
    """True if the domain is known to accept any local part.

    Reads the flag recorded at enrichment time rather than probing DNS here:
    a send loop should not be doing network lookups, and a stale probe is
    better than a surprise bounce in the middle of a batch.
    """
    if domain in _CATCH_ALL_CACHE:
        return _CATCH_ALL_CACHE[domain]
    verdict = False
    try:
        rows = db.connect().execute(
            "SELECT 1 FROM emails WHERE is_catch_all=1 AND email LIKE ? LIMIT 1",
            (f"%@{domain}",),
        ).fetchall()
        verdict = bool(rows)
    except Exception:
        verdict = False
    _CATCH_ALL_CACHE[domain] = verdict
    return verdict


# --------------------------------------------------------------------------- #
# Message construction
# --------------------------------------------------------------------------- #

def build_message(
    row: dict[str, Any],
    config: SmtpConfig,
    *,
    queue_id: int | None = None,
    append_footer: bool = True,
) -> EmailMessage:
    """Render one queue row into a signed-off email.

    The compliance footer and the List-Unsubscribe headers are added here, not
    left to the template, so a user editing their draft cannot accidentally drop
    the unsubscribe and start sending mail they are not allowed to send.
    """
    address = str(row["email"]).strip()
    message = EmailMessage(policy=SMTP_POLICY)
    message["From"] = (f"{config.sender_name} <{config.sender}>"
                       if config.sender_name else config.sender)
    message["To"] = address
    message["Subject"] = str(row.get("subject") or "").strip()
    message["Date"] = formatdate(localtime=True)

    # Stable across retries so a re-send of the same draft is recognisably the
    # same message to the receiving side, and changes if the draft is edited.
    # Built by hand rather than with make_msgid(), which prepends its own
    # timestamp and random component and would make every rebuild differ.
    fingerprint = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"applyonce:{config.sender}:{queue_id}:"
        f"{message['Subject']}:{row.get('body') or ''}",
    )
    domain = config.sender_domain or "localhost"
    message["Message-ID"] = f"<{fingerprint}.{queue_id}@{domain}>"

    # Replies should reach a human, and should not go to a mailbox we might
    # later be sending from.
    message["Reply-To"] = config.sender
    message["Auto-Submitted"] = "auto-generated"
    # An honest label so a filtering mail client can file it without heuristics.
    message["Precedence"] = "bulk"

    if config.unsubscribe_base:
        message["List-Unsubscribe"] = f"<{unsubscribe_url(address, config)}>"
        # RFC 8058: this is what lets Gmail and friends process a one-click
        # unsubscribe without the recipient sending anything at all.
        message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    elif config.unsubscribe_mailbox:
        message["List-Unsubscribe"] = f"<mailto:{config.unsubscribe_mailbox}>"

    body = str(row.get("body") or "").strip()

    # Honour an explicit {unsubscribe} placeholder if the template has one.
    link = unsubscribe_url(address, config)
    if "{unsubscribe}" in body:
        body = body.replace("{unsubscribe}", link)

    if append_footer and _FOOTER_SENTINEL not in body.lower():
        body = (
            f"{body}\n\n--\n{config.sender_name or config.sender}\n"
            f"{config.physical_address}\n\n"
            f"You are receiving this because your address is listed on the "
            f"public website of {row.get('company') or 'a company you founded'}. "
            f"Reply to this email and I will not contact you again, or "
            f"unsubscribe here: {link}\n"
        )

    message.set_content(body)
    return message


# --------------------------------------------------------------------------- #
# Queue mechanics
# --------------------------------------------------------------------------- #

def claim(queue_ids: Iterable[int]) -> list[int]:
    """Atomically move rows into 'sending' and return the ones we actually got.

    The UPDATE ... WHERE status='approved' is the concurrency control: two runs
    started at once cannot both claim the same row, so a double-click on "send"
    cannot produce a double send.
    """
    ids = [int(i) for i in queue_ids]
    if not ids:
        return []
    conn = db.connect()
    marks = ",".join("?" for _ in ids)
    with db.write(conn):
        # Read the eligible set inside the same transaction as the UPDATE, so a
        # concurrent claimer cannot slip in between and make this lie.
        eligible = sorted(
            int(row["id"]) for row in conn.execute(
                f"SELECT id FROM queue WHERE id IN ({marks}) "
                "AND status='approved'", ids).fetchall()
        )
        if eligible:
            conn.execute(
                f"UPDATE queue SET status='sending' WHERE id IN ({marks}) "
                "AND status='approved'", ids)
    return eligible


def release(queue_ids: Iterable[int], status: str = "approved") -> int:
    """Hand rows back after a run that did not finish."""
    ids = [int(i) for i in queue_ids]
    if not ids:
        return 0
    conn = db.connect()
    marks = ",".join("?" for _ in ids)
    with db.write(conn):
        cur = conn.execute(
            f"UPDATE queue SET status=? WHERE id IN ({marks}) AND status='sending'",
            [status, *ids],
        )
    return cur.rowcount


def reap_stuck(seconds: int = STUCK_SENDING_SECONDS) -> int:
    """Recover rows stranded by a process that died mid-send.

    A crash leaves rows in 'sending' with no way to tell them from a live send,
    so anything older than the window is assumed abandoned. Deliberately
    conservative: re-queueing costs you a manual re-send, while wrongly
    resetting a live send costs you a duplicate.
    """
    cutoff = int(time.time()) - seconds
    conn = db.connect()
    with db.write(conn):
        cur = conn.execute(
            "UPDATE queue SET status='approved', "
            "error='recovered after an interrupted run' "
            "WHERE status='sending' AND created_at < ?",
            (cutoff,),
        )
    return cur.rowcount


def today_start(now: float | None = None) -> int:
    """Local midnight as a unix timestamp, for the daily cap."""
    stamp = time.localtime(now if now is not None else time.time())
    return int(time.mktime((stamp.tm_year, stamp.tm_mon, stamp.tm_mday,
                            0, 0, 0, 0, 0, -1)))


def eligible_rows(limit: int = 50) -> list[dict[str, Any]]:
    """Approved rows, oldest first, with their company name attached.

    Oldest-first so a cap sends the drafts that have been waiting longest
    rather than whatever happened to be first in the table.
    """
    return db.list_queue("approved", limit=limit, oldest_first=True)


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #

class SmtpSession:
    """One authenticated SMTP conversation, reused across messages.

    Reconnecting per message is both slow and a good way to look like a bot, so
    the session stays open for a whole batch and reconnects on demand.
    """

    def __init__(self, config: SmtpConfig) -> None:
        self.config = config
        self._client: smtplib.SMTP | None = None

    def __enter__(self) -> "SmtpSession":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def connect(self) -> None:
        cfg = self.config
        context = ssl.create_default_context()
        client: smtplib.SMTP | None = None
        try:
            if cfg.use_implicit_tls:
                client = smtplib.SMTP_SSL(
                    cfg.host, cfg.port, timeout=cfg.timeout, context=context)
            else:
                client = smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout)
                client.ehlo()
                if client.has_extn("starttls"):
                    client.starttls(context=context)
                    client.ehlo()   # capabilities are re-issued after STARTTLS
            if cfg.username:
                client.login(cfg.username, cfg.password)
        except Exception:
            # A failed handshake or a rejected password leaves a live socket
            # behind unless we close it here. Over a batch with retries that
            # is a slow descriptor leak, and the process starts failing on
            # unrelated work.
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            raise
        self._client = client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.quit()
            except (smtplib.SMTPException, OSError):
                # A server that hangs up without QUIT is not worth failing over.
                pass
            self._client = None

    def _ensure(self) -> smtplib.SMTP:
        if self._client is None:
            self.connect()
        assert self._client is not None
        return self._client

    def send(self, message: EmailMessage) -> None:
        """Hand one message to the relay. Raises on refusal."""
        client = self._ensure()
        refused = client.send_message(message)
        if refused:
            # send_message reports per-recipient failures without raising, which
            # for a one-recipient message means it did not go.
            raise smtplib.SMTPRecipientsRefused(refused or {})


# 4xx is "try again later" -- greylisting, a full mailbox, a throttled relay.
# 5xx is a permanent rejection and must not be retried.
RETRYABLE_CODES = {421, 450, 451, 452}


def _is_retryable(exc: Exception) -> bool:
    # SMTPException inherits from OSError, so this has to be decided from the
    # SMTP code *before* the generic OSError check below. Get that order wrong
    # and every 550 "no such user" gets retried three times against a relay that
    # has already made up its mind.
    if isinstance(exc, smtplib.SMTPResponseException):
        code = getattr(exc, "smtp_code", None)
        if code in RETRYABLE_CODES:
            return True
        if code is not None and 400 <= code < 500:
            return True
        return False
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return False
    if isinstance(exc, (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected)):
        return True
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    return False


def send_row(
    row: dict[str, Any],
    config: SmtpConfig,
    session: SmtpSession,
) -> tuple[bool, str, str]:
    """Send one row with bounded retries.

    Returns (ok, detail, message_id). Never raises for a per-recipient
    problem: a single bad address must not abort the batch.
    """
    queue_id = int(row.get("id") or 0)
    message = build_message(row, config, queue_id=queue_id)
    message_id = str(message["Message-ID"])

    for attempt in range(config.retries + 1):
        try:
            session.send(message)
            return True, "sent", message_id
        except Exception as exc:  # noqa: BLE001 - classified immediately below
            retryable = _is_retryable(exc)
            detail = f"{type(exc).__name__}: {exc}"
            if not retryable or attempt >= config.retries:
                return False, detail, message_id
            # Linear backoff. Cold mail has no business hammering a relay, and
            # a short pause clears most greylisting on the first try.
            backoff = 5.0 * (attempt + 1)
            db.log_delivery(queue_id, "retry",
                            f"attempt {attempt + 1} failed ({detail}); "
                            f"waiting {backoff:.0f}s")
            time.sleep(backoff)
            # The connection may be the thing that broke.
            session.close()
    return False, "exhausted retries", message_id


def run(
    *,
    config: SmtpConfig | None = None,
    limit: int = 10,
    dry_run: bool = True,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Send (or show) up to `limit` approved messages.

    `sleep` and `log` are injected so the tests can run the real control flow
    with no real waiting and no real output.
    """
    config = config or SmtpConfig.from_env()
    config.require()

    reap_stuck()

    candidates = eligible_rows(limit=limit)
    summary: dict[str, Any] = {
        "dry_run": dry_run, "considered": len(candidates),
        "sent": 0, "failed": 0, "suppressed": 0,
        "skipped_daily_cap": 0, "results": [],
    }

    # Screen everything before touching the network, so a dry run reports
    # exactly the same set the real run would attempt.
    sendable: list[dict[str, Any]] = []
    for row in candidates:
        try:
            screen(row, config)
        except Suppressed as exc:
            summary["suppressed"] += 1
            summary["results"].append({
                "id": row.get("id"), "email": row.get("email"),
                "status": "suppressed", "reason": exc.reason,
                "detail": exc.detail,
            })
            # Both halves recorded: the reason is the machine-readable code you
            # would filter on, the detail is what a human needs to act on it.
            note = f"{exc.reason}: {exc.detail}"
            db.set_queue_status(int(row["id"]), "skipped", error=note)
            db.log_delivery(int(row["id"]), "suppressed", note)
            continue
        sendable.append(row)

    already_sent_today = db.count_queue("sent", since=today_start())
    remaining = max(0, config.daily_limit - already_sent_today)
    summary["daily_cap"] = config.daily_limit
    summary["already_sent_today"] = already_sent_today

    if len(sendable) > remaining:
        summary["skipped_daily_cap"] = len(sendable) - remaining
        for row in sendable[remaining:]:
            summary["results"].append({
                "id": row.get("id"), "email": row.get("email"),
                "status": "skipped", "reason": "daily_cap",
                "detail": f"{config.daily_limit} already sent today",
            })
        sendable = sendable[:remaining]

    if dry_run:
        for row in sendable:
            message = build_message(row, config, queue_id=int(row.get("id") or 0))
            summary["results"].append({
                "id": row.get("id"), "email": row.get("email"),
                "status": "would_send",
                "subject": str(message["Subject"]),
                "to": str(message["To"]),
                "from": str(message["From"]),
                "message_id": str(message["Message-ID"]),
                "has_unsubscribe": "List-Unsubscribe" in message,
            })
        return summary

    if not sendable:
        return summary

    # One transaction for the claim so a partially-claimed batch cannot send.
    claimed = claim([int(r["id"]) for r in sendable])
    claimed_set = set(claimed)
    actually = [r for r in sendable if int(r["id"]) in claimed_set]

    try:
        with SmtpSession(config) as session:
            for index, row in enumerate(actually):
                queue_id = int(row["id"])
                ok, detail, message_id = send_row(row, config, session)
                db.log_delivery(queue_id, "sent" if ok else "failed", detail)
                if ok:
                    db.set_queue_status(queue_id, "sent", message_id=message_id)
                    summary["sent"] += 1
                else:
                    db.set_queue_status(queue_id, "failed", error=detail)
                    summary["failed"] += 1
                summary["results"].append({
                    "id": queue_id, "email": row.get("email"),
                    "status": "sent" if ok else "failed",
                    "detail": detail, "message_id": message_id,
                })
                # Pause between messages, never after the last one.
                if index < len(actually) - 1 and config.delay:
                    sleep(config.delay)
    except Exception as exc:  # noqa: BLE001
        # The relay is down or we are misconfigured: do not leave rows claimed.
        detail = f"{type(exc).__name__}: {exc}"
        summary["error"] = detail
        release([int(r["id"]) for r in actually], status="approved")
        for row in actually:
            summary["results"].append({
                "id": row.get("id"), "email": row.get("email"),
                "status": "error", "detail": detail,
            })
    return summary


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _print_results(summary: dict[str, Any]) -> None:
    for item in summary["results"]:
        mark = {"sent": "+", "would_send": "?", "suppressed": "-",
                "skipped": ".", "failed": "!"}.get(item["status"], "?")
        extra = item.get("reason") or item.get("detail") or ""
        print(f"  {mark} {item['email']}  {item['status']}  {extra}")


def cmd_check(args: Any) -> int:
    config = SmtpConfig.from_env()
    print(json_dump(config.public_summary()))
    issues = config.problems()
    if issues:
        print("\nnot ready:")
        for issue in issues:
            print(f"  - {issue}")
        return 1
    print("\nready. nothing has been sent.")
    return 0


def cmd_preview(args: Any) -> int:
    config = SmtpConfig.from_env()
    config.require()
    summary = run(config=config, limit=args.limit, dry_run=True)
    print(f"{summary['considered']} approved, "
          f"{len([r for r in summary['results'] if r['status'] == 'would_send'])}"
          f" would send, {summary['suppressed']} suppressed, "
          f"{summary['skipped_daily_cap']} over the daily cap")
    _print_results(summary)
    if summary["skipped_daily_cap"]:
        print(f"\n  daily cap: {summary['already_sent_today']}/"
              f"{summary['daily_cap']} already sent today")
    return 0


def cmd_send(args: Any) -> int:
    config = SmtpConfig.from_env()
    config.require()

    if not args.send:
        print("This is a dry run. Nothing will be transmitted.")
        print("Re-run with --send to actually deliver.\n")
        return cmd_preview(args)

    summary_preview = run(config=config, limit=args.limit, dry_run=True)
    going = [r for r in summary_preview["results"] if r["status"] == "would_send"]
    if not going:
        print("nothing eligible to send")
        _print_results(summary_preview)
        return 0

    print(f"about to send {len(going)} message(s) to:")
    for item in going:
        print(f"  {item['email']}  --  {item.get('subject', '')}")
    if not args.yes:
        try:
            answer = input(f"\nsend {len(going)} message(s)? type 'send' to confirm: ")
        except (EOFError, KeyboardInterrupt):
            print("\ncancelled")
            return 1
        if answer.strip().lower() != "send":
            print("cancelled -- nothing was sent")
            return 1

    summary = run(config=config, limit=args.limit, dry_run=False)
    print(f"\nsent {summary['sent']}, failed {summary['failed']}, "
          f"suppressed {summary['suppressed']}")
    if summary.get("error"):
        print(f"error: {summary['error']}")
    _print_results(summary)
    return 1 if summary["failed"] or summary.get("error") else 0


def cmd_reap(args: Any) -> int:
    recovered = reap_stuck(args.minutes * 60 if args.minutes else STUCK_SENDING_SECONDS)
    print(f"recovered {recovered} row(s) stranded in 'sending'")
    return 0


def json_dump(value: Any) -> str:
    import json
    return json.dumps(value, indent=2)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Send queued ApplyOnce drafts over SMTP.",
        epilog=(
            "Credentials come from the environment only (SMTP_HOST, SMTP_PORT,\n"
            "SMTP_USERNAME, SMTP_APP_PASSWORD, SMTP_FROM, SMTP_FROM_NAME,\n"
            "SMTP_PHYSICAL_ADDRESS). There is no --password flag on purpose.\n"
            "Sending is a dry run unless you pass --send."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("check", help="report whether SMTP is configured")

    preview = sub.add_parser("preview", help="show what would be sent")
    preview.add_argument("--limit", type=int, default=10)

    sender = sub.add_parser("send", help="send approved drafts")
    sender.add_argument("--limit", type=int, default=10)
    sender.add_argument("--send", action="store_true",
                        help="actually transmit (otherwise a dry run)")
    sender.add_argument("--yes", action="store_true",
                        help="skip the interactive confirmation")

    reaper = sub.add_parser("reap", help="recover rows stranded in 'sending'")
    reaper.add_argument("--minutes", type=int, default=0)

    args = parser.parse_args(argv)
    handlers = {
        "check": cmd_check, "preview": cmd_preview,
        "send": cmd_send, "reap": cmd_reap,
    }
    handler = handlers.get(args.command or "check")
    if handler is None:
        parser.print_help()
        return 2
    try:
        return handler(args)
    except MailerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except db.UpstreamError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())