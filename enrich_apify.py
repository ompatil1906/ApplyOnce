#!/usr/bin/env python3
"""
Apify enrichment for ApplyOnce.

Takes the JSON produced by ``scrape_batch.py`` and asks the
``automation-lab/email-enrichment`` Actor to verify the guessed addresses, then
writes the addresses back with a verification status. This is the paid step: it
costs money per contact, and it is optional. The web app does the same thing
from the browser so nothing ever touches a server.

The token is read from the ``APIFY_TOKEN`` environment variable on purpose.
Passing it as ``--token`` would put it in your shell history and in ``ps`` output
for every other process on the machine.

Examples
--------
    # see what would be sent, without spending anything
    APIFY_TOKEN=... python3 enrich_apify.py data/fall2026.json --dry-run

    # cheap syntax-only pass over 25 contacts
    APIFY_TOKEN=... python3 enrich_apify.py data/fall2026.json \\
        --level format --limit 25 --out data/fall2026.apify.json

    # real mailbox checks, full file
    APIFY_TOKEN=... python3 enrich_apify.py data/fall2026.json \\
        --level smtp --out data/fall2026.apify.json

    # is my token valid, and how much is left in the account?
    APIFY_TOKEN=... python3 enrich_apify.py --check-token

Standard library only, like everything else in this project.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from api.yc import normalize_domain  # noqa: E402

API = "https://api.apify.com/v2"
DEFAULT_ACTOR = "automation-lab/email-enrichment"
LEVELS = ("format", "mx", "smtp")
CHUNK = 25          # the Actor caches per-domain results, so batching is cheap
RUN_TIMEOUT = 300   # the Actor's own budget for one run, in seconds
HTTP_TIMEOUT = 360  # wall clock we are willing to wait for it


def die(message: str, code: int = 1) -> int:
    print(f"\nerror: {message}", file=sys.stderr)
    return code


def actor_id(actor: str) -> str:
    return actor.strip().replace("/", "~")


def call(path: str, token: str, payload: dict[str, Any] | None = None) -> Any:
    """One Apify API call. Raises ``RuntimeError`` with something readable."""
    url = f"{API}{path}" + urllib.parse.urlencode({"token": token})
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method="POST" if data else "GET",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "applyonce/1.0",
            "X-Apify-Request-Origin": "applyonce",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            message = json.loads(body)["error"]["message"]
        except Exception:
            message = body[:300] or exc.reason
        if exc.code in (401, 402, 403, 404):
            message = f"{message} (check APIFY_TOKEN)"
        raise RuntimeError(f"HTTP {exc.code}: {message}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"could not reach Apify: {exc.reason}") from None
    except json.JSONDecodeError:
        raise RuntimeError("Apify returned a non-JSON response") from None


# --------------------------------------------------------------------------- #
# input
# --------------------------------------------------------------------------- #


def load_rows(path: Path) -> list[dict[str, Any]]:
    """Accept either the JSON from scrape_batch.py or a plain CSV."""
    if not path.exists():
        raise SystemExit(die(f"no such file: {path}"))

    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return payload
    for key in ("data", "rows", "companies"):
        if isinstance(payload.get(key), list):
            return payload[key]
    raise SystemExit(die(f"{path} does not look like scrape_batch.py output"))


def build_contacts(
    rows: list[dict[str, Any]],
    *,
    only_missing: bool,
    limit: int | None,
) -> list[dict[str, Any]]:
    """Pick the rows worth paying for, de-duplicated by person+domain."""
    seen: set[tuple[str, str]] = set()
    contacts: list[dict[str, Any]] = []

    for row in rows:
        first = (row.get("founder_first") or row.get("firstName") or "").strip()
        last = (row.get("founder_last") or row.get("lastName") or "").strip()
        domain = normalize_domain(row.get("domain") or "")
        if not first or not last or not domain:
            continue
        if only_missing and (row.get("apify_email") or row.get("isVerified") is not None):
            continue
        key = (f"{first} {last}".lower(), domain)
        if key in seen:
            continue
        seen.add(key)
        contacts.append({"firstName": first, "lastName": last, "domain": domain})
        if limit and len(contacts) >= limit:
            break
    return contacts


# --------------------------------------------------------------------------- #
# actor run
# --------------------------------------------------------------------------- #


def enrich_chunk(
    contacts: list[dict[str, Any]],
    token: str,
    actor: str,
    level: str,
    max_patterns: int,
    dry_run: bool,
) -> list[dict[str, Any]]:
    payload = {
        "contacts": contacts,
        "verificationLevel": level,
        "detectCatchAll": level != "format",
        "maxPatternsToTest": max(1, min(14, max_patterns)),
        "checkDeliverability": False,
    }
    if dry_run:
        return []

    result = call(
        f"/acts/{urllib.parse.quote(actor_id(actor), safe='~')}"
        "/run-sync-get-dataset-items"
        f"?timeout={RUN_TIMEOUT}&format=json&memory=1024",
        token,
        payload,
    )
    if isinstance(result, dict):
        return result.get("items") or []
    return result if isinstance(result, list) else []


def contact_key(contact: dict[str, Any]) -> tuple[str, str, str]:
    return (
        contact.get("firstName", "").strip().lower(),
        contact.get("lastName", "").strip().lower(),
        normalize_domain(contact.get("domain", "")),
    )


def row_key(row: dict[str, Any]) -> tuple[str, str, str]:
    first = (row.get("founder_first") or row.get("firstName") or "").strip().lower()
    last = (row.get("founder_last") or row.get("lastName") or "").strip().lower()
    domain = normalize_domain(row.get("domain") or "").lower()
    return (first, last, domain)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def cmd_check_token(args: argparse.Namespace) -> int:
    token = args.token
    try:
        me = call("/users/me", token).get("data", {})
        print(f"token ok — {me.get('username', 'unknown')} "
              f"(plan: {me.get('plan', {}).get('id', 'unknown')})")
    except RuntimeError as exc:
        return die(str(exc), 2)

    try:
        usage = call("/users/me/usage/monthly", token).get("data", {})
        used = usage.get("totalUsageUsd")
        limit = usage.get("limitTotalUsdUsd")
        if used is not None:
            print(f"this month: ${used:.2f} used"
                  + (f" of ${limit:.2f}" if limit else ""))
    except RuntimeError:
        print("usage figures are not available on this plan")
    return 0


def cmd_enrich(args: argparse.Namespace) -> int:
    rows = load_rows(Path(args.input))
    contacts = build_contacts(
        rows, only_missing=args.only_missing, limit=args.limit
    )
    if not contacts:
        print("nothing to enrich: every row already has a name, a domain and "
              "(with --only-missing) no prior result", file=sys.stderr)
        return 1

    print(f"{len(contacts)} contact(s) from {args.input}", file=sys.stderr)
    if not args.dry_run:
        print(f"this will spend Apify credits at verification level {args.level!r}",
              file=sys.stderr)

    by_key = {contact_key(c): c for c in contacts}
    enriched: dict[tuple[str, str, str], dict[str, Any]] = {}
    processed = failed = 0
    started = time.time()

    for index in range(0, len(contacts), CHUNK):
        slice_ = contacts[index:index + CHUNK]
        label = f"[{index + len(slice_)}/{len(contacts)}]"
        try:
            items = enrich_chunk(
                slice_, args.token, args.actor, args.level,
                args.max_patterns, args.dry_run,
            )
        except RuntimeError as exc:
            print(f"{label} failed: {exc}", file=sys.stderr)
            failed += len(slice_)
            break

        if args.dry_run:
            print(f"{label} would send:\n"
                  f"{json.dumps({'contacts': slice_[:3], 'verificationLevel': args.level, 'detectCatchAll': args.level != 'format', 'maxPatternsToTest': max(1, min(14, args.max_patterns))}, indent=2)}"
                  + (f"\n  ... and {len(slice_) - 3} more" if len(slice_) > 3 else ""))
            processed += len(slice_)
            continue

        for item in items or []:
            if not item or not item.get("email"):
                continue
            key = contact_key(item)
            if key not in by_key:
                # the Actor normalised the name; fall back to the domain
                key = next((k for k in by_key if k[2] == key[2] and k[0] in key[0]), key)
            enriched[key] = {
                "apify_email": str(item["email"]).lower(),
                "isVerified": bool(item.get("isVerified")),
                "confidenceScore": item.get("confidenceScore"),
                "resultStatus": item.get("resultStatus"),
                "isCatchAll": bool(item.get("isCatchAll")),
                "isRoleAccount": bool(item.get("isRoleAccount")),
                "sources": item.get("sources") or [],
            }
        processed += len(slice_)
        print(f"{label} {len(items or [])} result(s)", file=sys.stderr)

    if args.dry_run:
        print(f"\ndry run: {processed} contact(s) would be sent, nothing was "
              f"requested and nothing was spent", file=sys.stderr)
        return 0

    # Write the results back onto the original rows, so the file stays a
    # complete dataset rather than a side table you have to join by hand.
    hit = 0
    for row in rows:
        result = enriched.get(row_key(row))
        if not result:
            continue
        hit += 1
        row.update(result)
        # A verified address always wins; an unverified one beats a bare guess.
        if result["isVerified"]:
            row["email"] = result["apify_email"]
            row["email_source"] = "apify-verified"
        elif not row.get("email") or row.get("email_source") == "guess":
            row["email"] = result["apify_email"]
            row["email_source"] = "apify-unverified"
        if result["isRoleAccount"]:
            row["email_confidence"] = "role"

    verified = len([r for r in enriched.values() if r["isVerified"]])
    print(
        f"\n{processed} processed · {len(enriched)} addresses returned "
        f"({verified} verified) · {hit} row(s) updated · "
        f"{time.time() - started:.1f}s",
        file=sys.stderr,
    )
    if failed:
        print(f"{failed} contact(s) were not processed", file=sys.stderr)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.suffix.lower() == ".csv":
            columns = list(dict.fromkeys(list(rows[0].keys()) if rows else []))
            with out.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns,
                                        extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
        else:
            payload = rows
            if isinstance(json.loads(Path(args.input).read_text(encoding="utf-8")), dict):
                original = json.loads(Path(args.input).read_text(encoding="utf-8"))
                original["data"] = rows
                original["apify"] = {
                    "actor": args.actor,
                    "level": args.level,
                    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "processed": processed,
                    "verified": verified,
                }
                payload = original
            out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify and enrich scraped emails with Apify.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("input", nargs="?",
                        help="JSON or CSV from scrape_batch.py")
    parser.add_argument("--check-token", action="store_true",
                        help="verify APIFY_TOKEN and show this month's spend")
    parser.add_argument("--actor", default=DEFAULT_ACTOR,
                        help=f"Actor to run (default: {DEFAULT_ACTOR})")
    parser.add_argument("--level", choices=LEVELS, default="mx",
                        help="verification level (default: mx). 'format' is "
                             "syntax-only and effectively free")
    parser.add_argument("--max-patterns", type=int, default=8, metavar="N",
                        help="patterns the Actor may test per contact, 1-14 "
                             "(default: 8)")
    parser.add_argument("--limit", type=int, metavar="N",
                        help="only enrich the first N contacts")
    parser.add_argument("--only-missing", action="store_true",
                        help="skip contacts that already carry an Apify result")
    parser.add_argument("--out", metavar="PATH",
                        help="write enriched rows here (.json or .csv). "
                             "Without it, nothing is saved.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the Actor payloads and make no requests")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    token = os.environ.get("APIFY_TOKEN", "").strip()
    if not token:
        parser.error("APIFY_TOKEN is not set. Put it in your environment, e.g.\n"
                     "    export APIFY_TOKEN='apify_api_...'\n"
                     "Do not pass it as a flag: it would end up in your shell "
                     "history.")

    args.token = token

    try:
        if args.check_token:
            return cmd_check_token(args)
        if not args.input:
            parser.error("an input file is required (or use --check-token)")
        return cmd_enrich(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except RuntimeError as exc:
        return die(str(exc), 2)


if __name__ == "__main__":
    raise SystemExit(main())