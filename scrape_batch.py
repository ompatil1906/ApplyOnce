#!/usr/bin/env python3
"""
Batch exporter for ApplyOnce.

Dumps one YC batch to JSON and/or CSV, optionally resolving an email address
for every founder using the same ladder the web app uses:

    1. a personal address published on the company's own website
    2. a role address published on that website
    3. pattern guesses (first@domain first), then role fallbacks

Apify verification is deliberately *not* here: that costs money per contact and
needs your token. Use ``enrich_apify.py`` for that, or the browser app.

Examples
--------
    # what batches exist?
    python3 scrape_batch.py --list-batches

    # the newest batch, every founder, both formats
    python3 scrape_batch.py --batch "Fall 2026"

    # just the company rows, no founder profiles, CSV only
    python3 scrape_batch.py --batch "S24" --no-founders --csv-only

    # cheap run: 20 companies, guess only, never touch a website
    python3 scrape_batch.py --batch "Fall 2026" --limit 20 --no-site-emails

A full batch is 110 companies x ~2 founders, and each website crawl costs up to
``--site-timeout`` seconds, so budget a few minutes per batch.

Standard library only, like everything else in this project.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from api.yc import (  # noqa: E402
    UpstreamError,
    guess_emails,
    get_company,
    is_role_email,
    list_batches,
    list_companies,
    name_matches_email,
    normalize_domain,
    site_emails,
    slugify,
    split_name,
)

PAGE_SIZE = 100  # one Algolia page holds 100 companies
WORKERS = 6

CSV_COLUMNS = [
    "company",
    "slug",
    "batch",
    "one_liner",
    "domain",
    "website",
    "location",
    "team_size",
    "industry",
    "tags",
    "status",
    "founder_name",
    "founder_title",
    "founder_linkedin",
    "founder_twitter",
    "founder_first",
    "founder_last",
    "email",
    "email_source",
    "email_confidence",
    "alternate_emails",
]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def resolve_email(
    domain: str,
    first_name: str,
    last_name: str,
    site: list[dict[str, Any]],
) -> tuple[str, str, str]:
    """One rung of the ladder. Returns ``(email, source, alternates)``.

    A personal address on the site that does not match this founder's name is
    somebody else's mailbox, so it is never returned: that is how a cold email
    ends up in a colleague's inbox.
    """
    domain = normalize_domain(domain)
    if not domain:
        return "", "", ""

    # 1. a personal address on their own site that matches this founder
    if first_name or last_name:
        for entry in site:
            if entry.get("kind") == "role":
                continue
            if name_matches_email(entry["email"], first_name, last_name):
                return entry["email"], "site", ""

    # 2. a role address on their own site
    for entry in site:
        if entry.get("kind") == "role":
            return entry["email"], "site-role", ""

    # 3. guesses
    guesses = guess_emails(first_name, last_name, domain)
    if guesses:
        alternates = "; ".join(guesses[1:6])
        return guesses[0], "guess", alternates

    return "", "", ""


def founder_rows(
    company: dict[str, Any],
    *,
    want_site: bool,
    site_timeout: float,
) -> list[dict[str, Any]]:
    """Enrich one company and flatten it to one row per founder."""
    domain = normalize_domain(company.get("domain") or company.get("website") or "")
    founders = company.get("founders") or []

    site: list[dict[str, Any]] = []
    if want_site and domain:
        try:
            site = (site_emails(domain, deadline_seconds=site_timeout) or {}).get(
                "emails", []
            )
        except UpstreamError:
            site = []
        except Exception:  # a hostile site must not kill the run
            site = []

    if not founders:
        email, source, alternates = resolve_email(domain, "", "", site)
        return [
            {
                "company": company.get("name", ""),
                "slug": company.get("slug", ""),
                "batch": company.get("batch", ""),
                "one_liner": one_line(company.get("one_liner")),
                "domain": domain,
                "website": company.get("website", ""),
                "location": company.get("location", ""),
                "team_size": company.get("team_size", ""),
                "industry": company.get("industry", ""),
                "tags": "; ".join(company.get("tags") or []),
                "status": company.get("status", ""),
                "founder_name": "",
                "founder_title": "",
                "founder_linkedin": "",
                "founder_twitter": "",
                "founder_first": "",
                "founder_last": "",
                "email": email,
                "email_source": source,
                "email_confidence": "",
                "alternate_emails": alternates,
            }
        ]

    rows = []
    for founder in founders:
        raw_name = founder.get("name") or ""
        first, last = split_name(raw_name)
        first = first or founder.get("first_name", "")
        last = last or founder.get("last_name", "")
        email, source, alternates = resolve_email(domain, first, last, site)
        rows.append(
            {
                "company": company.get("name", ""),
                "slug": company.get("slug", ""),
                "batch": company.get("batch", ""),
                "one_liner": one_line(company.get("one_liner")),
                "domain": domain,
                "website": company.get("website", ""),
                "location": company.get("location", ""),
                "team_size": company.get("team_size", ""),
                "industry": company.get("industry", ""),
                "tags": "; ".join(company.get("tags") or []),
                "status": company.get("status", ""),
                "founder_name": raw_name,
                "founder_title": founder.get("title", ""),
                "founder_linkedin": founder.get("linkedin", ""),
                "founder_twitter": founder.get("twitter", ""),
                "founder_first": first,
                "founder_last": last,
                "email": email,
                "email_source": source,
                "email_confidence": (
                    "role" if email and is_role_email(email) else ""
                ),
                "alternate_emails": alternates,
            }
        )
    return rows


def one_line(text: str) -> str:
    return " ".join((text or "").split())


def fetch_company(slug: str) -> dict[str, Any] | None:
    try:
        return get_company(slug)
    except Exception as exc:  # one dead profile must not stop the batch
        print(f"  ! profile failed for {slug}: {exc}", file=sys.stderr)
        return None


def all_companies(batch: str, limit: int | None) -> list[dict[str, Any]]:
    """Page a whole batch out of Algolia, one 100-company page at a time."""
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        payload = list_companies(batch, offset=offset, limit=PAGE_SIZE)
        window = payload.get("companies") or []
        if not window:
            break
        out.extend(window)
        offset += len(window)
        total = payload.get("total") or len(out)
        print(f"  fetched {len(out)}/{total}", file=sys.stderr)
        if offset >= total:
            break
    if limit:
        out = out[:limit]
    return out


def write_outputs(rows: list[dict[str, Any]], batch: str, out_dir: Path,
                  want_json: bool, want_csv: bool, quiet: bool) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = slugify(batch) or "batch"
    written: list[Path] = []

    if want_json:
        path = out_dir / f"{stem}.json"
        payload = {
            "batch": batch,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "rows": len(rows),
            "companies": len({r["slug"] for r in rows if r["slug"]}),
            "founders": len([r for r in rows if r["founder_name"]]),
            "with_email": len([r for r in rows if r["email"]]),
            "data": rows,
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        written.append(path)

    if want_csv:
        path = out_dir / f"{stem}.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        written.append(path)

    if not quiet:
        for path in written:
            print(f"wrote {path}")
    return written


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def cmd_list_batches(args: argparse.Namespace) -> int:
    batches = list_batches()
    if args.json:
        print(json.dumps(batches, indent=2, ensure_ascii=False))
        return 0
    print(f"{len(batches)} batches (newest first)\n")
    for entry in batches:
        bar = "#" * max(1, round(entry["count"] / 4))
        print(f"  {entry['name']:<14} {entry['count']:>4}  {bar}")
    return 0


def cmd_scrape(args: argparse.Namespace) -> int:
    batches = list_batches()
    if args.batch:
        batch = args.batch
    else:
        batch = batches[0]["name"]
        print(f"no --batch given, using the newest: {batch}", file=sys.stderr)

    started = time.time()
    companies = all_companies(batch, args.limit)
    if not companies:
        print(f"no companies found for batch {batch!r}. "
              f"Try --list-batches to see the exact name.", file=sys.stderr)
        return 1
    print(f"\n{len(companies)} companies in {batch!r}", file=sys.stderr)

    if not args.no_founders:
        slugs = [c["slug"] for c in companies if c.get("slug")]
        print(f"loading {len(slugs)} founder profiles...", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            profiles = list(pool.map(fetch_company, slugs))
        by_slug = {p["slug"]: p for p in profiles if p}
        for company in companies:
            profile = by_slug.get(company.get("slug"))
            if profile:
                company["founders"] = profile.get("founders") or []
                if not company.get("domain"):
                    company["domain"] = profile.get("domain", "")

    print(f"resolving emails (site crawls {'on' if not args.no_site_emails else 'off'})...",
          file=sys.stderr)
    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for done, chunk in enumerate(
            pool.map(
                lambda c: founder_rows(
                    c,
                    want_site=not args.no_site_emails,
                    site_timeout=args.site_timeout,
                ),
                companies,
            ),
            start=1,
        ):
            rows.extend(chunk)
            if not args.quiet and done % 10 == 0:
                print(f"  {done}/{len(companies)} companies", file=sys.stderr)

    rows.sort(key=lambda r: (r["company"].lower(), r["founder_name"].lower()))

    written = write_outputs(
        rows, batch, Path(args.out_dir),
        want_json=not args.csv_only,
        want_csv=not args.json_only,
        quiet=args.quiet,
    )

    found = len([r for r in rows if r["email"]])
    print(
        f"\n{len(rows)} founder rows · {found} with an address "
        f"({round(100 * found / len(rows)) if rows else 0}%) · "
        f"{len(written)} file(s) · {time.time() - started:.1f}s",
        file=sys.stderr,
    )
    return 0


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export a YC batch to JSON and/or CSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--batch", metavar="NAME",
                        help='batch to export, e.g. "Fall 2026" or "S24". '
                             "Defaults to the newest batch.")
    parser.add_argument("--list-batches", action="store_true",
                        help="list every batch with its company count, then exit")
    parser.add_argument("--json", action="store_true",
                        help="with --list-batches, print JSON instead of a table")
    parser.add_argument("--out-dir", default="data",
                        help="directory for the output files (default: data)")
    parser.add_argument("--json-only", action="store_true", help="skip the CSV")
    parser.add_argument("--csv-only", action="store_true", help="skip the JSON")
    parser.add_argument("--limit", type=int, metavar="N",
                        help="stop after N companies (good for a trial run)")
    parser.add_argument("--no-founders", action="store_true",
                        help="do not load founder profiles; one row per company")
    parser.add_argument("--no-site-emails", action="store_true",
                        help="never crawl websites; guesses only, much faster")
    parser.add_argument("--site-timeout", type=float, default=7.0, metavar="SEC",
                        help="per-company crawl budget (default: 7)")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="suppress progress output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.list_batches and not args.batch:
        print("note: no --batch given, so the newest batch will be exported\n",
              file=sys.stderr)

    try:
        if args.list_batches:
            return cmd_list_batches(args)
        return cmd_scrape(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except UpstreamError as exc:
        print(f"\nupstream error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())