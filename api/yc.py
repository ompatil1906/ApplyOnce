"""
ApplyOnce -- YC directory data access.

A single, self-contained module that is simultaneously:

  1. A Vercel serverless function   (``class handler(BaseHTTPRequestHandler)``)
  2. A router for the local dev server (``handle_request(query) -> (status, payload)``)
  3. An importable library for the CLI scripts (``from api.yc import ...``)

Why self-contained? Vercel's Python builder traces imports to work out what to
bundle. Keeping this file dependency-free (standard library only, no imports of
sibling project files) removes an entire class of "works locally, 500s on
deploy" failure.

Data sources
------------
YC's public company directory is backed by Algolia. The *search-only* API key is
embedded in the public ``ycombinator.com/companies`` page, so we borrow it. It
is restricted to search: it cannot read, write, or delete anything.

  * Batch list + batch pages  ->  POST /1/indexes/YCCompany_production/query
  * Founders + full profile  ->  GET  ycombinator.com/companies/<slug>
    The company page is a Rails/Inertia app; the profile JSON lives HTML-escaped
    inside the ``data-page="..."`` attribute of a <div>.
  * Email addresses          ->  fetch the company's own website

Standard library only. No third-party packages, ever.
"""

from __future__ import annotations

import html
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

YC_BASE = "https://www.ycombinator.com"
DIRECTORY_URL = f"{YC_BASE}/companies"

# Search-only Algolia credentials, lifted from the public directory page.
# They are safe to ship in a public repo: this key can only run search queries
# against an index that ycombinator.com already exposes to every visitor.
# If YC ever rotates the key, fetch a fresh one with:
#     curl -s https://www.ycombinator.com/companies | grep -o 'AlgoliaOpts = {[^;]*'
ALGOLIA_APP_ID = "45BWZJ1SGC"
ALGOLIA_KEY = (
    "NzJmMWExZWYxYzY5OGYwN2VkYWM5YzRiM2VlNDFlM2I0ODU2YjQ2Yjg0MTFi"
    "NWE5NzY0NTMyZGI1OWEwMzVjY2FuYWx5dGljc1RhZ3M9eWNkYyZyZXN0cmljdEl"
    "uZGljZXM9WUNDb21wYW55X3Byb2R1Y3Rpb24lMkNZQ0NvbXBhbnlfQnlfTGF1bmNo"
    "X0RhdGVfcHJvZHVjdGlvbiZ0YWdGaWx0ZXJzPSU1QiUyMnljZGNfcHVibGljJTIyJTVE"
)
ALGOLIA_INDEX = "YCCompany_production"

# Algolia serves one application from several equivalent hostnames. A single
# one of them can be unreachable from a given network (DNS filtering, regional
# anycast weirdness) while the others are perfectly healthy, so we try them in
# order rather than treating one hostname as a single point of failure.
ALGOLIA_HOSTS = [
    f"{ALGOLIA_APP_ID}-dsn.algolia.net",
    f"{ALGOLIA_APP_ID}.algolia.net",
    f"{ALGOLIA_APP_ID.lower()}-1.algolianet.com",
    f"{ALGOLIA_APP_ID.lower()}-2.algolianet.com",
]

# Browser-ish User-Agent. Many small startup sites 403 a bare python-urllib
# request but happily serve a normal browser string.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Algolia serves 100 hits per page, so one call covers five "Load more" clicks
# (5 x 20). Keeps the number of upstream requests low.
ALGOLIA_PAGE_SIZE = 100
MAX_COMPANIES_PER_CALL = 100  # hard ceiling we advertise to the client

# Timeouts. Vercel's Hobby plan caps a function at 10s (see vercel.json), so
# every budget below is sized to finish inside that even on a bad network.
HTTP_TIMEOUT = 6.0
SITE_FETCH_TIMEOUT = 4.0  # per-page cap inside the site_emails crawl
SITE_DEADLINE = 7.0  # hard wall-clock budget for the whole site_emails crawl
MAX_HTML_BYTES = 600_000

# Batch list hygiene. YC's index contains a handful of placeholder rows.
MIN_BATCH_SIZE = 5
EXCLUDED_BATCHES = {"unspecified", "unclear", "none", ""}

# Cache TTLs, in seconds. Warm serverless instances reuse these across hits.
BATCHES_TTL = 3600
COMPANIES_TTL = 900
COMPANY_TTL = 3600

# Slug guard: only ever request https://www.ycombinator.com/companies/<slug>
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# --------------------------------------------------------------------------- #
# Small HTTP helper
# --------------------------------------------------------------------------- #


class UpstreamError(Exception):
    """Raised when an upstream (YC / Algolia / target site) call fails."""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


def _open_with_retry(
    request: urllib.request.Request,
    *,
    timeout: float,
    retries: int,
    max_bytes: int,
) -> tuple[int, str]:
    """Perform a request, retrying transport-level failures.

    An HTTP error status is a definitive answer and is returned as-is (a 404
    from a contact page is information, not a fault). Transport failures --
    DNS blips, refused connections, timeouts, truncated TLS -- are retried,
    because they are routinely transient and are exactly what a shared
    datacentre network produces under load.
    """
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(max_bytes)
                charset = response.headers.get_content_charset() or "utf-8"
                return response.status, raw.decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read(4096).decode("utf-8", errors="replace")
            except Exception:  # pragma: no cover - best effort only
                pass
            return exc.code, body
        except Exception as exc:  # URLError, gaierror, timeout, ssl errors...
            last_error = exc
            if attempt < retries:
                # Exponential backoff: resolver blips and refused connections
                # usually clear within a second or two.
                time.sleep(0.35 * (2 ** attempt))

    raise UpstreamError(f"{request.get_method()} {request.full_url} failed: {last_error}")


def http_get(
    url: str,
    *,
    timeout: float = HTTP_TIMEOUT,
    accept: str = "*/*",
    max_bytes: int = MAX_HTML_BYTES,
    retries: int = 3,
) -> tuple[int, str]:
    """GET a URL and return ``(status, body_text)``.

    Caps the read at ``max_bytes`` so a pathological page cannot blow up memory
    inside a 1GB serverless function.
    """
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",  # avoids gzip handling entirely
        },
    )
    return _open_with_retry(request, timeout=timeout, retries=retries,
                            max_bytes=max_bytes)


def http_post_json(
    url: str,
    payload: dict[str, Any],
    *,
    timeout: float = HTTP_TIMEOUT,
    headers: dict[str, str] | None = None,
    retries: int = 3,
) -> dict[str, Any]:
    """POST a JSON body and parse a JSON response."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            **(headers or {}),
        },
    )
    try:
        status, text = _open_with_retry(request, timeout=timeout, retries=retries,
                                         max_bytes=MAX_HTML_BYTES)
    except UpstreamError as exc:
        # No HTTP status survived (this is a transport failure), so report 502.
        raise UpstreamError(f"POST {url} failed: {exc}", 502) from exc

    if status != 200:
        # Preserve the status so callers can tell "bad request / bad key" apart
        # from "host unreachable". Trim the body: upstream error pages can be
        # large and the client only needs the reason.
        raise UpstreamError(f"POST {url} -> HTTP {status}: {text[:200].strip()}",
                            status)

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise UpstreamError(f"POST {url} returned non-JSON") from exc


# --------------------------------------------------------------------------- #
# Tiny TTL cache
# --------------------------------------------------------------------------- #


class TTLCache:
    """Thread-safe-enough key/value store with per-entry expiry.

    Serverless instances are reused aggressively, so a warm hit here removes
    both latency and upstream load. Bounded to ``maxsize`` entries so a crawler
    cannot grow it without limit.
    """

    def __init__(self, maxsize: int = 512) -> None:
        self._store: dict[str, tuple[float, Any]] = {}
        self._maxsize = maxsize

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at < time.time():
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        if len(self._store) >= self._maxsize:
            # Cheap eviction: drop the oldest insertion (dicts keep order).
            self._store.pop(next(iter(self._store)), None)
        self._store[key] = (time.time() + ttl, value)

    def clear(self) -> None:
        self._store.clear()


BATCHES_CACHE = TTLCache(maxsize=8)
COMPANIES_CACHE = TTLCache(maxsize=256)
COMPANY_CACHE = TTLCache(maxsize=512)


# --------------------------------------------------------------------------- #
# Name parsing and email-pattern guessing
# --------------------------------------------------------------------------- #
#
# These live server-side so the CLI scripts can reuse them. index.html carries
# a small JS mirror of the same logic (it has to, to render drafts offline);
# keep the two in sync.

_SUFFIXES = {
    "jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "phd", "ph.d.", "md", "m.d.",
    "msc", "m.s.", "bsc", "b.s.", "esq", "cpa", "mba",
}

# Lowercase particles that, when they appear, belong to the surname.
_PARTICLES = {"de", "del", "della", "di", "da", "das", "dos", "du", "van",
              "von", "der", "den", "ten", "ter", "la", "le", "el", "al", "bin",
              "ibn", "mac", "mc", "o", "san"}


def clean_name(raw: str) -> str:
    """Strip titles, suffixes and punctuation YC occasionally stores.

    Also normalises the two orderings we see in the directory. ``"Doe, Jane"``
    is flipped to ``"Jane Doe"``; a second comma-separated chunk is an
    affiliation rather than part of the name, so it is dropped.
    """
    text = html.unescape(raw or "").replace(" ", " ")
    text = re.sub(r"\s+", " ", text).strip()
    # Drop anything wrapped in brackets/parens -- those are disambiguators like
    # "Jane Doe (Guest)".
    text = re.sub(r"[\(\[][^\)\]]*[\)\]]", "", text)

    # "Last, First" -> "First Last". A single-token first chunk is treated as
    # a surname; any further chunks are affiliations and are dropped.
    chunks = [c.strip() for c in text.split(",") if c.strip()]
    if len(chunks) >= 2 and " " not in chunks[0]:
        text = " ".join([chunks[1], chunks[0]])
    else:
        text = chunks[0] if chunks else ""

    # Leading honorifics.
    text = re.sub(
        r"^(mr|mrs|ms|miss|dr|prof|professor|rev|hon)\.?\s+", "", text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"[^\w\s'\-À-ɏ]", "", text).strip()
    text = re.sub(r"\s+", " ", text)
    return text


def split_name(raw: str) -> tuple[str, str]:
    """Split a full name into ``(first_name, last_name)``.

    Handles the shapes that actually show up in the YC directory:

      "Jane Doe"          -> ("Jane",   "Doe")
      "Jane Q. Doe"        -> ("Jane",   "Doe")
      "Jane van Doe"       -> ("Jane",   "van Doe")
      "Maria de la Cruz"   -> ("Maria",  "de la Cruz")
      "Doe, Jane"          -> ("Jane",   "Doe")
      "Jane Doe Jr."       -> ("Jane",   "Doe")
      "Cher"               -> ("Cher",   "")
    """
    name = clean_name(raw)
    if not name:
        return "", ""

    parts = [p for p in name.split(" ") if p]
    # Trailing suffixes: keep the last *real* token as the surname.
    while len(parts) > 2 and parts[-1].lower() in _SUFFIXES:
        parts.pop()
    # Leading suffixes ("Jr. Jane Doe") -- rare but cheap to handle.
    while len(parts) > 2 and parts[0].lower() in _SUFFIXES:
        parts.pop(0)

    if len(parts) == 1:
        return parts[0], ""

    # Walk right-to-left: skip middle names until we hit either a particle
    # (start of a multi-word surname) or the first non-particle token, which is
    # the head of the surname.
    index = len(parts) - 1
    if parts[index].lower() not in _PARTICLES:
        last = parts[index]
        first_parts = parts[:index]
    else:
        # Trailing particle, e.g. "Ludwig van" -- treat as surname head.
        index -= 1
        while index >= 0 and parts[index].lower() in _PARTICLES:
            index -= 1
        if index < 0:
            first, last = parts[0], " ".join(parts[1:])
            return first, last
        last = " ".join(parts[index:])
        first_parts = parts[:index]

    # "Maria de la Cruz" -> first_parts == ["Maria"] (de/la were swallowed by the
    # particle walk only when adjacent to the tail). If we have leftovers that
    # are themselves particles, fold them into the surname.
    if not first_parts:
        return parts[0], " ".join(parts[1:])

    # Drop a middle name/initial: ["Jane", "Q."] -> first "Jane".
    first = first_parts[0]
    remainder = first_parts[1:]
    if remainder and all(p.lower() in _PARTICLES for p in remainder):
        last = " ".join(remainder + [last])

    return first, last


def slugify(text: str) -> str:
    """ASCII-fold a name into an email local part."""
    text = html.unescape(text or "").lower()
    replacements = {
        "ß": "ss", "æ": "ae", "ø": "o", "å": "a", "ä": "ae",
        "ö": "oe", "ü": "ue", "é": "e", "è": "e", "ê": "e",
        "ë": "e", "á": "a", "à": "a", "â": "a", "ã": "a",
        "í": "i", "ì": "i", "î": "i", "ï": "i", "ó": "o",
        "ò": "o", "ô": "o", "õ": "o", "ú": "u", "ù": "u",
        "û": "u", "ñ": "n", "ç": "c", "š": "s", "ž": "z",
        "ł": "l", "đ": "d", "ğ": "g", "ı": "i", "þ": "th", "ð": "d",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    text = re.sub(r"[^a-z0-9]+", "", text)  # drop apostrophes, dots, hyphens
    return text


def email_patterns(first_name: str, last_name: str) -> list[str]:
    """Ordered local-part candidates for one person.

    Order is deliberate. Research across cold-email datasets consistently puts
    ``first@`` far ahead, and YC companies skew heavily toward it because a
    typical batch company is two people who each own one mailbox. The dot and
    last-name-first variants are kept as fallbacks rather than noise.
    """
    first = slugify(first_name)
    last = slugify(last_name)
    if not first:
        return []

    candidates = [
        first,                                  # jane@
        f"{first}.{last}" if last else None,    # jane.doe@
        f"{first}{last}" if last else None,     # janedoe@
        f"{first[0]}{last}" if last else None,  # jdoe@
        f"{first}_{last}" if last else None,    # jane_doe@
        f"{last}{first}" if last else None,     # doejane@
        f"{first}-{last}" if last else None,    # jane-doe@
        f"{first[0]}.{last}" if last else None,  # j.doe@
        f"{last}" if last else None,            # doe@
        f"{first}{last[0]}" if last else None,  # janeo@  (dropped if too short)
    ]

    ordered: list[str] = []
    for candidate in candidates:
        if not candidate or len(candidate) < 2:
            continue
        if candidate not in ordered:
            ordered.append(candidate)
    return ordered


# Role addresses tried last, after every person-specific pattern has failed.
ROLE_LOCAL_PARTS = [
    "founders", "hello", "hi", "info", "contact", "team", "sales",
    "support", "office", "general", "press", "careers", "jobs", "admin",
]


def guess_emails(first_name: str, last_name: str, domain: str) -> list[str]:
    """Full ordered guess list for one founder at ``domain``.

    Person-specific patterns first (in ``email_patterns`` order), then role
    addresses. The caller treats index 0 as the primary guess and the rest as
    alternates.
    """
    domain = normalize_domain(domain)
    if not domain:
        return []

    emails = [f"{local}@{domain}" for local in email_patterns(first_name, last_name)]
    emails += [f"{role}@{domain}" for role in ROLE_LOCAL_PARTS]

    # De-duplicate, keep order, and drop anything that would not survive a
    # round trip through a mail client.
    seen: set[str] = set()
    unique: list[str] = []
    for email in emails:
        if email in seen or not EMAIL_RE.fullmatch(email):
            continue
        seen.add(email)
        unique.append(email)
    return unique


# --------------------------------------------------------------------------- #
# Website helpers (domain extraction, email harvesting)
# --------------------------------------------------------------------------- #


def normalize_domain(value: str) -> str:
    """Turn a website URL, hostname or ``user@host`` into a bare lowercase host.

    Returns "" when the input cannot yield a plausible public domain.
    """
    text = (value or "").strip().lower()
    if not text:
        return ""
    if "@" in text and "/" not in text:
        text = text.split("@", 1)[1]
    if "//" not in text:
        text = "//" + text
    try:
        parsed = urllib.parse.urlparse(text)
        host = parsed.hostname or ""
    except ValueError:
        return ""
    host = host.split(":")[0]
    if not host or "." not in host:
        return ""
    if not re.match(r"^[a-z0-9.\-]+$", host):
        return ""
    # "www.acme.com" and "acme.com" accept mail at the same place; guessing
    # "jane@www.acme.com" is always wrong.
    if host.startswith("www."):
        host = host[4:]
    return host or ""


def origin_of(url: str) -> str:
    """``https://acme.com/x/y?q=1`` -> ``https://acme.com``."""
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return url
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


# Matches a bare email in HTML/text. Deliberately greedy about TLDs but still
# rejects obvious image filenames such as "logo@2x.png" (see is_noise_email).
EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)+"
)

# Local parts that are never a person you want to cold email.
_NOISE_LOCAL_PARTS = {
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "postmaster", "hostmaster", "webmaster", "abuse", "spam", "admin",
    "administrator", "mailer-daemon", "bounce", "bounces", "nobody",
    "example", "yourname", "youremail", "your-email", "youremailaddress",
    "email", "you", "your", "user", "username", "test", "testing", "demo",
    "someone", "somebody", "name", "firstname", "lastname", "sentry",
    "wixpress", "wixpress-noreply", "godaddy", "cloudflare", "domain",
    "examplecom", "emailcom", "acme", "mycompnay", "yourcompnay",
}

# Domains whose addresses are placeholders in scraped demo pages.
_NOISE_DOMAINS = {
    "example.com", "example.org", "example.net", "domain.com", "yourdomain.com",
    "email.com", "mail.com", "test.com", "acme.com", "company.com",
    "yourcompany.com", "your-company.com", "mydomain.com", "site.com",
    "email.net", "domain.net", "sentry.io", "wixpress.com", "godaddy.com",
}

# Asset extensions that show up in emails scraped out of srcset/data-URI noise.
_ASSET_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".css", ".js",
    ".woff", ".woff2", ".ttf", ".pdf", ".json", ".xml", ".webmanifest",
)


def is_noise_email(email: str) -> bool:
    """Reject placeholder addresses and image-file false positives."""
    email = email.strip().lower().strip(".,;:<>()[]{}\"'")
    if not email or email.count("@") != 1:
        return True
    local, _, domain = email.partition("@")
    if not local or not domain or "." not in domain:
        return True
    if len(email) > 120 or len(local) > 64:
        return True
    # "logo@2x.png" style matches from srcset attributes.
    if domain.endswith(_ASSET_SUFFIXES):
        return True
    if domain in _NOISE_DOMAINS or domain.endswith(".example"):
        return True
    if local in _NOISE_LOCAL_PARTS:
        return True
    # Cloudflare challenge / analytics placeholders.
    if re.search(r"(?:\d[a-f0-9]{6,}|@[0-9a-f]{8,}\.)", local + domain):
        return True
    if re.fullmatch(r"[0-9a-f]{32,}", local):
        return True
    return False


def extract_emails(text: str) -> list[str]:
    """Return unique, non-noise emails found in a blob of HTML, in order."""
    found: list[str] = []
    seen: set[str] = set()
    for match in EMAIL_RE.finditer(text or ""):
        email = match.group(0).strip().lower().strip(".,;:<>()[]{}\"'")
        if email in seen or is_noise_email(email):
            continue
        seen.add(email)
        found.append(email)
    return found


def _mailto_emails(html_text: str) -> list[str]:
    """Emails behind ``mailto:`` links, which are the highest-signal ones."""
    emails = [
        urllib.parse.unquote(m.group(1))
        for m in re.finditer(r"mailto:([^\"'?>\s]+)", html_text or "", re.IGNORECASE)
    ]
    return [e for e in emails if not is_noise_email(e)]


def name_matches_email(email: str, first_name: str, last_name: str) -> bool:
    """True when a scraped address plausibly belongs to this founder."""
    local = email.partition("@")[0].lower()
    first = slugify(first_name)
    last = slugify(last_name)
    if not first:
        return False
    local_no_dots = local.replace(".", "").replace("_", "").replace("-", "")
    if first == local or first in local.split(".") or first in local.split("_"):
        return True
    if first == local_no_dots:
        return True
    if first and local.startswith(first[0]) and last and last in local_no_dots:
        return True
    if last and len(last) > 3 and local_no_dots.startswith(last):
        return True
    return False


def is_role_email(email: str) -> bool:
    return email.partition("@")[0].lower() in set(ROLE_LOCAL_PARTS) or \
        email.partition("@")[0].lower() in {"sales", "support", "press", "careers", "jobs"}


# Pages worth checking, in descending order of signal. These are conventional
# paths; we also follow same-origin hrefs that look like contact pages.
CONTACT_PATHS = ["/", "/contact", "/contact-us", "/contact/", "/about",
                 "/about-us", "/team", "/company"]


# --------------------------------------------------------------------------- #
# Algolia: batch list + batch pages
# --------------------------------------------------------------------------- #

_ALGOLIA_HEADERS = {
    "x-algolia-application-id": ALGOLIA_APP_ID,
    "x-algolia-api-key": ALGOLIA_KEY,
}


def _algolia_query(payload: dict[str, Any]) -> dict[str, Any]:
    """POST a search payload, trying each known hostname in turn.

    A 4xx from Algolia is a real answer and is returned immediately. Only
    transport failures move on to the next host.
    """
    last_error: Exception | None = None
    for host in ALGOLIA_HOSTS:
        url = f"https://{host}/1/indexes/{ALGOLIA_INDEX}/query"
        try:
            return http_post_json(url, payload, headers=_ALGOLIA_HEADERS,
                                  timeout=8.0)
        except UpstreamError as exc:
            last_error = exc
            # A rejected key or a malformed query will fail identically on
            # every host; retrying is pointless and only wastes time.
            if exc.status in (400, 401, 403):
                raise
    raise UpstreamError(f"all Algolia hosts failed: {last_error}", 502)


def _batch_sort_key(name: str) -> tuple[int, int]:
    """Sort ``"Fall 2026"`` -> ``(2026, 2)`` so newest batches come first.

    Seasons are numbered so that Spring < Summer < Fall < Winter, matching the
    calendar year a batch is named after.
    """
    match = re.match(r"([A-Za-z]+)\s+(\d{4})", name.strip())
    if not match:
        return (0, 0)
    season, year = match.group(1).lower(), int(match.group(2))
    order = {"winter": 0, "spring": 1, "summer": 2, "fall": 3, "autumn": 3}
    return (year, order.get(season, 9))


def list_batches() -> list[dict[str, Any]]:
    """All YC batches with company counts, newest first."""
    cached = BATCHES_CACHE.get("batches")
    if cached is not None:
        return cached

    response = _algolia_query({
        "query": "",
        "hitsPerPage": 0,
        "facets": ["batch"],
        "maxValuesPerFacet": 500,
    })
    facets = (response.get("facets") or {}).get("batch") or {}

    batches = [
        {"name": name, "count": int(count)}
        for name, count in facets.items()
        if name.strip().lower() not in EXCLUDED_BATCHES
        and int(count) >= MIN_BATCH_SIZE
    ]
    batches.sort(key=lambda b: _batch_sort_key(b["name"]), reverse=True)

    BATCHES_CACHE.set("batches", batches, BATCHES_TTL)
    return batches


def _normalise_hit(hit: dict[str, Any]) -> dict[str, Any]:
    """Flatten an Algolia hit down to what the UI actually renders."""
    website = (hit.get("website") or "").strip()
    return {
        "id": hit.get("id"),
        "slug": hit.get("slug") or "",
        "name": hit.get("name") or "",
        "former_names": hit.get("former_names") or [],
        "one_liner": (hit.get("one_liner") or "").strip(),
        "long_description": (hit.get("long_description") or "").strip(),
        "website": website,
        "domain": normalize_domain(website),
        "batch": hit.get("batch") or "",
        "team_size": hit.get("team_size"),
        "status": hit.get("status") or "",
        "industry": hit.get("industry") or "",
        "location": hit.get("all_locations") or "",
        "tags": [t for t in (hit.get("tags") or []) if t][:8],
        "logo": hit.get("small_logo_thumb_url") or "",
        "yc_url": f"{YC_BASE}/companies/{hit.get('slug')}" if hit.get("slug") else "",
        # Founder data is filled in lazily by the `company` action so that
        # paging through a batch costs one upstream call, not twenty.
        "founders": [],
        "founders_state": "idle",  # idle | loading | loaded | error
    }


def list_companies(
    batch: str, offset: int = 0, limit: int = 20
) -> dict[str, Any]:
    """One window of companies from ``batch``.

    A single Algolia page holds 100 companies, so paging is done locally:
    offsets 0-99 come from page 0, 100-199 from page 1, and so on. That means
    five "Load more" clicks cost one upstream request.
    """
    batch = (batch or "").strip()
    if not batch:
        raise UpstreamError("batch is required", 400)

    offset = max(0, int(offset))
    limit = max(1, min(int(limit), MAX_COMPANIES_PER_CALL))

    cache_key = f"{batch}|{offset // ALGOLIA_PAGE_SIZE}"
    cached = COMPANIES_CACHE.get(cache_key)
    if cached is None:
        cached = _algolia_query({
            "query": "",
            "hitsPerPage": ALGOLIA_PAGE_SIZE,
            "page": offset // ALGOLIA_PAGE_SIZE,
            "filters": f'batch:"{batch.replace(chr(34), "")}"',
        })
        COMPANIES_CACHE.set(cache_key, cached, COMPANIES_TTL)

    total = int(cached.get("nbHits") or 0)
    hits = cached.get("hits") or []
    start = offset - (offset // ALGOLIA_PAGE_SIZE) * ALGOLIA_PAGE_SIZE
    window = hits[start:start + limit]

    return {
        "batch": batch,
        "total": total,
        "offset": offset,
        "limit": limit,
        "page": offset // ALGOLIA_PAGE_SIZE,
        "page_size": ALGOLIA_PAGE_SIZE,
        "has_more": (offset + len(window)) < total,
        "companies": [_normalise_hit(hit) for hit in window],
    }


# --------------------------------------------------------------------------- #
# Company detail: founders, scraped from the public company page
# --------------------------------------------------------------------------- #

_DATA_PAGE_PREFIX = 'data-page="'


def extract_data_page(page_html: str) -> dict[str, Any]:
    """Pull the Inertia JSON payload out of a rendered YC company page.

    Rails/Inertia apps emit ``<div data-page="{...}">`` where the JSON is HTML
    escaped. We slice on the *raw* text (the closing quote is a literal ``"``
    because inner quotes are ``&quot;``), then unescape and parse.
    """
    start = page_html.find(_DATA_PAGE_PREFIX)
    if start == -1:
        raise UpstreamError("company page did not contain a data-page payload")

    body = page_html[start + len(_DATA_PAGE_PREFIX):]
    end = body.find('"')
    if end == -1:
        raise UpstreamError("unterminated data-page attribute")

    try:
        return json.loads(html.unescape(body[:end]))
    except json.JSONDecodeError as exc:
        raise UpstreamError(f"malformed data-page JSON: {exc}") from exc


def _founder_from_payload(raw: dict[str, Any]) -> dict[str, Any]:
    full_name = clean_name(raw.get("full_name") or "")
    first, last = split_name(full_name)
    return {
        "key": raw.get("user_id") or f"{first}.{last}".lower(),
        "name": full_name,
        "first_name": first,
        "last_name": last,
        "title": (raw.get("title") or "").strip(),
        "bio": (raw.get("founder_bio") or "").strip(),
        "twitter": (raw.get("twitter_url") or "").strip(),
        "linkedin": (raw.get("linkedin_url") or "").strip(),
        "yc_profile": f"{YC_BASE}/people/{raw.get('username')}"
                      if raw.get("username") else "",
        "has_email": bool(raw.get("has_email")),
        # Filled by the client from the Apify / website / guess ladders.
        "email": "",
        "email_source": "none",  # verified|unverified|site|site_role|guess|role|none
        "email_confidence": None,
        "email_notes": "",
    }


def get_company(slug: str) -> dict[str, Any]:
    """Full profile for one company, including its founders.

    YC only reveals founder email addresses to logged-in users, but company
    pages frequently mention an address in prose (e.g. a ``founders@`` in the
    long description), so we sweep the page text too.
    """
    slug = (slug or "").strip().lower()
    if not _SLUG_RE.match(slug):
        raise UpstreamError("invalid slug", 400)

    cached = COMPANY_CACHE.get(slug)
    if cached is not None:
        return cached

    url = f"{YC_BASE}/companies/{slug}"
    status, page = http_get(url, accept="text/html")
    if status != 200 or not page:
        raise UpstreamError(f"YC returned HTTP {status} for /companies/{slug}")

    payload = extract_data_page(page)
    company = (payload.get("props") or {}).get("company") or {}
    if not company:
        raise UpstreamError(f"no company payload for '{slug}'", 404)

    founders = [
        _founder_from_payload(f)
        for f in (company.get("founders") or [])
        if f.get("full_name")
    ]

    website = (company.get("website") or "").strip()
    profile = {
        "slug": slug,
        "id": company.get("id"),
        "name": (company.get("name") or "").strip(),
        "one_liner": (company.get("one_liner") or "").strip(),
        "long_description": (company.get("long_description") or "").strip(),
        "website": website,
        "domain": normalize_domain(website),
        "batch": company.get("batch_name") or company.get("batch") or "",
        "team_size": company.get("team_size"),
        "status": company.get("ycdc_status") or "",
        "industry": company.get("subindustry") or company.get("city_tag") or "",
        "location": " ".join(
            p for p in [company.get("city") or "", company.get("country") or ""]
            if p
        ).strip(),
        "tags": [t for t in (company.get("tags") or []) if t][:8],
        "logo": company.get("small_logo_url") or "",
        "linkedin": company.get("linkedin_url") or "",
        "twitter": company.get("twitter_url") or "",
        "github": company.get("github_url") or "",
        "founded": company.get("year_founded"),
        "yc_url": url,
        "founders": founders,
        # Addresses mentioned in the page body itself.
        "page_emails": extract_emails(page)[:10],
        "founders_state": "loaded",
    }

    COMPANY_CACHE.set(slug, profile, COMPANY_TTL)
    return profile


# --------------------------------------------------------------------------- #
# Website email harvesting
# --------------------------------------------------------------------------- #


def _fetch_site(url: str, deadline: float) -> tuple[str, int, str]:
    """Fetch one page under a shared deadline.

    Returns ``(url, status, body)``; ``status == 0`` means "we gave up", which
    is a normal outcome here, not an error.

    Note the deliberate ``retries=0``: we already parallelise across eight
    pages, and a retry of a hanging site would blow the function's time budget
    for a request that is going to hang again anyway.
    """
    remaining = deadline - time.time()
    if remaining <= 0.4:
        return url, 0, ""
    try:
        status, body = http_get(
            url, timeout=min(SITE_FETCH_TIMEOUT, remaining), retries=0
        )
        return url, status, body
    except Exception:
        return url, 0, ""


def _crawl(urls: list[str], deadline: float) -> list[tuple[str, int, str]]:
    """Fetch a set of pages concurrently, never returning after ``deadline``.

    Plain daemon threads rather than a ThreadPoolExecutor, for one specific
    reason: ``socket.getaddrinfo`` is a blocking C call that the socket
    timeout does *not* cover, so a site whose nameserver hangs can pin a worker
    well past its deadline. ``Future.result()`` would wait for it;
    ``Thread.join(timeout=...)`` lets us walk away instead. Daemon threads also
    cannot delay interpreter shutdown, so an abandoned lookup is harmless.
    """
    if not urls:
        return []
    if deadline - time.time() <= 0.4:
        return [(url, 0, "") for url in urls]

    results: dict[str, tuple[str, int, str]] = {}

    def worker(url: str) -> None:
        results[url] = _fetch_site(url, deadline)

    threads = []
    for url in urls:
        thread = threading.Thread(target=worker, args=(url,), daemon=True)
        thread.start()
        threads.append(thread)

    for thread in threads:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        thread.join(timeout=remaining)

    return [results.get(url, (url, 0, "")) for url in urls]


def site_emails(url: str, deadline_seconds: float = SITE_DEADLINE) -> dict[str, Any]:
    """Harvest public email addresses from a company's own website.

    Crawls the homepage plus conventional contact pages in parallel under a hard
    wall-clock deadline, so the serverless function always returns inside its
    10s budget. Failures are expected and unremarkable: plenty of small sites
    block datacentre IPs outright, and the guess ladder simply falls through to
    the next rung when nothing is found here.
    """
    domain = normalize_domain(url)
    if not domain:
        raise UpstreamError("could not derive a domain from url", 400)

    # Respect an explicit scheme when the caller gave one; urllib follows
    # redirects, so an http:// start still reaches an https:// site. That
    # removes the need for a separate probe request.
    origin = origin_of(url if "//" in (url or "") else "//" + url)
    if not origin:
        origin = f"https://{domain}"

    deadline = time.time() + deadline_seconds
    results = _crawl([origin + path for path in CONTACT_PATHS], deadline)

    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def record(email: str, source_url: str, via_mailto: bool) -> None:
        email = email.strip().lower()
        if email in seen or is_noise_email(email):
            return
        # Keep addresses on the company's own domain; third-party ones embedded
        # in a page (analytics, support vendors, CDNs) are noise here.
        if normalize_domain(email) != domain:
            return
        seen.add(email)
        found.append({
            "email": email,
            "source_url": source_url,
            "via_mailto": via_mailto,
            "kind": "role" if is_role_email(email) else "person",
        })

    def harvest(page_url: str, status: int, body: str) -> bool:
        """Record emails from one page. Returns True if the page was readable."""
        if status != 200 or not body:
            return False
        # mailto: links first: they are the addresses the company chose to
        # publish, which is exactly the signal we want.
        for email in _mailto_emails(body):
            record(email, page_url, True)
        for email in extract_emails(body)[:15]:
            record(email, page_url, False)
        return True

    pages_ok = 0
    for page_url, status, body in results:
        if harvest(page_url, status, body):
            pages_ok += 1

    # Follow up to two same-origin links that look like a contact page. Some
    # sites use /pages/contact-us or /get-in-touch instead of a flat path.
    # Only if enough budget is left to actually fetch them.
    pages_attempted = len(results)
    if not found and deadline - time.time() > SITE_FETCH_TIMEOUT + 0.5:
        extra_urls = _discover_contact_links(origin, results, seen_domain=domain)
        for page_url, status, body in _crawl(extra_urls, deadline):
            pages_attempted += 1
            if harvest(page_url, status, body):
                pages_ok += 1

    return {
        "domain": domain,
        "origin": origin,
        "emails": found,
        "pages_ok": pages_ok,
        "pages_attempted": pages_attempted,
        "timed_out": time.time() > deadline,
    }


_CONTACT_HREF_RE = re.compile(
    r'href=["\']([^"\']*(?:contact|get-in-touch|reach-us|talk-to|hello|enquir)'
    r'[^\'"]*)["\']',
    re.IGNORECASE,
)


def _discover_contact_links(
    origin: str,
    results: list[tuple[str, int, str]],
    *,
    seen_domain: str,
    limit: int = 2,
) -> list[str]:
    """Same-origin hrefs from already-fetched pages that look like contact pages."""
    candidates: list[str] = []
    already = {url for url, _, _ in results}

    for _page_url, status, body in results:
        if status != 200 or not body:
            continue
        for match in _CONTACT_HREF_RE.finditer(body):
            href = html.unescape(match.group(1)).strip()
            if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            absolute = urllib.parse.urljoin(origin + "/", href)
            try:
                parsed = urllib.parse.urlparse(absolute)
            except ValueError:
                continue
            if parsed.scheme not in ("http", "https"):
                continue
            if normalize_domain(absolute) != seen_domain:
                continue
            if absolute in already or absolute in candidates:
                continue
            candidates.append(absolute)
            if len(candidates) >= limit:
                return candidates
    return candidates


# --------------------------------------------------------------------------- #
# Router
# --------------------------------------------------------------------------- #


def handle_request(query: dict[str, list[str]] | dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Single entry point shared by the Vercel function and serve.py.

    ``query`` is a parsed query string (as produced by ``urllib.parse.parse_qs``
    or Vercel's ``request.query``). Returns ``(http_status, json_payload)``.
    """

    def one(name: str, default: str = "") -> str:
        value = query.get(name, default)
        if isinstance(value, (list, tuple)):
            return value[0] if value else default
        return value or default

    action = one("action", "batches").strip().lower()

    if action == "batches":
        batches = list_batches()
        latest = batches[0]["name"] if batches else ""
        return 200, {"batches": batches, "latest": latest,
                     "source": DIRECTORY_URL}

    if action == "companies":
        try:
            offset = int(one("offset", "0") or 0)
            limit = int(one("limit", "20") or 20)
        except ValueError:
            raise UpstreamError("offset and limit must be integers", 400)
        return 200, list_companies(one("batch"), offset=offset, limit=limit)

    if action == "company":
        return 200, get_company(one("slug"))

    if action == "site_emails":
        return 200, site_emails(one("url"))

    raise UpstreamError(f"unknown action '{action}'", 400)


# --------------------------------------------------------------------------- #
# Vercel serverless adapter
# --------------------------------------------------------------------------- #
#
# Vercel's Python runtime hands each request to a BaseHTTPRequestHandler
# subclass. This shim is the only thing in the file that knows about HTTP
# verbs; everything reusable lives above.

# The browser is served from the same origin, but CORS is opened up anyway so
# you can host the static page anywhere (GitHub Pages, a local file, ...) and
# still point it at this endpoint.
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
}


def _json_response(status: int, payload: dict[str, Any]) -> None:
    import sys

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(b"HTTP/1.1 %d %s\r\n" % (status, _status_text(status)))
    sys.stdout.buffer.write(b"Content-Type: application/json; charset=utf-8\r\n")
    sys.stdout.buffer.write(b"Content-Length: %d\r\n" % len(body))
    for key, value in CORS_HEADERS.items():
        sys.stdout.buffer.write(f"{key}: {value}\r\n".encode())
    sys.stdout.buffer.write(b"Cache-Control: no-store\r\n")
    sys.stdout.buffer.write(b"\r\n")
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


_STATUS_TEXT = {
    200: "OK", 400: "Bad Request", 404: "Not Found",
    429: "Too Many Requests", 500: "Internal Server Error", 502: "Bad Gateway",
}


def _status_text(status: int) -> str:
    return _STATUS_TEXT.get(status, "Error")


try:  # pragma: no cover - only exercised on Vercel
    from http.server import BaseHTTPRequestHandler

    class handler(BaseHTTPRequestHandler):  # noqa: N801 - Vercel's required name
        """Vercel entry point."""

        protocol_version = "HTTP/1.1"
        server_version = "ApplyOnce"

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter logs
            pass

        def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib naming
            _json_response(200, {"ok": True})

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)

            if parsed.path.rstrip("/") not in ("/api/yc", "/api/yc.py", ""):
                _json_response(404, {"error": "not found",
                                     "actions": ["batches", "companies",
                                                 "company", "site_emails"]})
                return

            try:
                status, payload = handle_request(query)
            except UpstreamError as exc:
                status = exc.status if exc.status >= 400 else 502
                payload = {"error": str(exc)}
            except Exception as exc:  # never leak a stack trace to the client
                status = 500
                payload = {"error": f"internal error: {exc}"}

            _json_response(status, payload)

except ImportError:  # pragma: no cover
    handler = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# CLI entry point (handy for `python3 api/yc.py batches`)
# --------------------------------------------------------------------------- #

if __name__ == "__main__":  # pragma: no cover
    import sys

    args = dict(arg.split("=", 1) for arg in sys.argv[1:] if "=" in arg)
    action = sys.argv[1] if len(sys.argv) > 1 and "=" not in sys.argv[1] else "batches"
    args.setdefault("action", action)
    try:
        code, data = handle_request(args)
    except UpstreamError as exc:
        code, data = exc.status, {"error": str(exc)}
    print(json.dumps(data, indent=2, ensure_ascii=False))
    raise SystemExit(0 if code == 200 else 1)