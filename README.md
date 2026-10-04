# ApplyOnce

Cold-email YC founders without building an account or a pipeline.

Pick a YC batch, click **Load companies**, and you get every founder with the
best email address this project can find — plus a finished draft you can copy or
open in your mail app. Nothing leaves your machine: no account, no analytics,
no hosted database.

Standard library Python and one HTML file. No npm, no build step, no framework,
no dependencies.

```
index.html          the entire web app
api/yc.py           YC directory data + website email scraping (Vercel function)
serve.py            local dev server
db.py               local SQLite storage (local-only, not deployed)
mailer.py           review queue + SMTP sending (local-only, not deployed)
people.py           score the founders you found as people (local-only)
scrape_batch.py     export a whole batch to JSON / CSV
enrich_apify.py     verify guessed addresses with Apify (optional, paid)
tests/              unittest suite (standard library only)
```

## Quick start

```bash
python3 -m venv .venv                 # optional, there is nothing to install
python3 serve.py                      # -> http://127.0.0.1:8000
```

`serve.py` mounts the *same* handler Vercel runs, so local and production behave
identically. Open the page, fill in your name and template, then pick a batch.

## How the email address is chosen

Every founder is resolved through one ladder, and each result is labelled with
where it came from:

| Order | Source | Badge |
|---|---|---|
| 1 | Apify verified / unverified (only if you paste a token) | **Verified** / **Unverified** |
| 2 | A personal address on the company's website that matches the founder's name | **On site** |
| 3 | A *role* address on that website | **On site (role)** |
| 4 | Pattern guesses | **Guess** |
| — | Nothing found | **No email** |

Two deliberate choices:

- **A personal address that does not match the founder is never used.** It is
  somebody else's mailbox. Handing it over is how a cold email ends up in a
  colleague's inbox, so those rows fall through to the role and guess rungs.
- **Guesses are ordered by what actually gets answered.** `first@domain` leads,
  then `first.last@`, `flast@`, `firstlast@`, `first_last@`, and so on, with role
  fallbacks (`founders@`, `hello@`, `contact@`, `team@`, …) last. Roughly two
  people own a mailbox at a typical batch company, so `first@` is right far more
  often than the dotted variants.

Everything is guessable, so **read the badge before you send**. Verified and
on-site addresses are worth writing individual emails about. A guess is a guess.

## Where the data comes from

`api/yc.py` talks to the same Algolia index the public YC directory uses, then
fetches each company's YC profile page to read its founders, LinkedIn and
Twitter handles. Email addresses are never on YC — they come from the company's
own website (homepage plus `/contact`, `/about`, `/team`, `/press`, …, crawled in
parallel under a hard 7-second budget) or from the patterns above.

The directory currently exposes 48 batches, newest first. One Algolia page holds
100 companies, so all 20-company pages in a batch cost a single upstream request.

## Drafting

The subject and body are templates. Placeholders:

`{first_name}` `{last_name}` `{company}` `{one_liner}` `{tags}` `{website}`
`{location}` `{team_size}` `{batch}` `{my_name}` `{portfolio}` `{github}`
`{resume}`

Any line that contains a placeholder you have not filled in is dropped
automatically, so a template with a `{github}` line does not send a dangling
label. The note under the editor tells you how many lines will drop and which
fields are unset before you send anything.

Each founder gets their own rendered preview with **copy email**, **copy body**,
**copy subject**, **copy subject + body**, and **open in mail app** (a
prefilled `mailto:`). Mark a company **sent** and it stays in place, dimmed, so
the list does not jump while you work — the **Sent** filter shows only those.

## Where your data lives

There are two stores, and they have different jobs.

**`localStorage`** (the `applyonce.v1` key) is the fast render cache. Drafts have
to paint with the network down, so this stays synchronous and in the browser. It
holds your details, your template, every batch you loaded, sent marks, and any
Apify token.

**`applyonce.db`** (a SQLite file next to the code) is the durable record. Open
the **Local database** panel at the bottom of the page:

- **Save to database** pushes the current browser state into SQLite. It is
  idempotent, so pressing it twice changes nothing.
- **Restore from database** replaces the browser copy with what is on disk.
  This is the recovery path after clearing a profile or switching machines.
- **Refresh counts** just re-reads the row totals.

What this buys you: `localStorage` is tied to one browser profile. Wipe it, get a
new laptop, hit a browser bug, and the research is gone. The database survives
all of that, and `python3 db.py export --out backup.json` gives you a portable
snapshot you can `python3 db.py import` later.

Still true, and worth keeping in mind:

- No account, no analytics, nothing leaves your machine.
- The **Reset** button clears only the browser copy, by design — it says so in
  the confirmation. The database is a separate record you can still restore from.
- Your Apify token is **never** written to the database, and never restored from
  it. It stays a browser-local secret.

The Vercel function is stateless. It reads public YC data and returns JSON; it
never receives or stores anything about you.

### Configuring the database

```bash
python3 serve.py                          # ./applyonce.db
python3 serve.py --db data/research.db    # somewhere else
export APPLYONCE_DB=data/research.db      # or via the environment
python3 db.py status                      # path, size, schema version
python3 db.py stats                       # row counts per table
python3 db.py companies --needs-email     # who is actually sendable
python3 db.py export --out backup.json    # portable snapshot
```

`db.py` is local-only on purpose. It is absent from `vercel.json`, because
Vercel's filesystem is ephemeral and read-only and a SQLite write could never
work there. The page feature-detects this via `/healthz`, so the same `index.html`
works on a Vercel deploy with the storage panel collapsed and explained.

## Apify enrichment (optional)

[Apify](https://apify.com) can verify the guessed addresses for you, which
upgrades a **Guess** into **Verified** or **Unverified**. It costs money per
contact, so it is off unless you opt in, and it is never required.

You need your own token from <https://console.apify.com/settings/integrations>.
Paste it into the app and it is kept in that browser only, or export it in the
shell for the CLI:

```bash
export APIFY_TOKEN='apify_api_...'
python3 enrich_apify.py --check-token
```

**Never commit a token.** It is why the CLI reads the environment variable
instead of taking a `--token` flag, which would land in your shell history and
show up in `ps` for every process on the machine. If you ever paste a token into
a chat, a paste site, or a repo, rotate it at
<https://console.apify.com/settings/integrations>.

The Actor defaults to `automation-lab/email-enrichment` and can be swapped in the
app or with `--actor`. Verification levels, cheapest first:

| `--level` | What it does |
|---|---|
| `format` | syntax only — effectively free, good for a trial run |
| `mx` | DNS + catch-all detection (default) |
| `smtp` | real mailbox check |

## CLI

### `scrape_batch.py` — export a batch

```bash
python3 scrape_batch.py --list-batches
python3 scrape_batch.py --batch "Fall 2026"                 # JSON + CSV
python3 scrape_batch.py --batch "S24" --no-founders --csv-only
python3 scrape_batch.py --batch "Fall 2026" --limit 20 --no-site-emails
```

Writes `data/<batch>.json` and `data/<batch>.csv`, one row per founder, with
`email`, `email_source` and up to five alternates. `--no-site-emails` skips all
website crawling and is much faster; `--no-founders` gives one row per company.

A full batch is 110 companies and each website crawl can take up to
`--site-timeout` seconds, so budget a few minutes.

### `enrich_apify.py` — verify with Apify

```bash
# see exactly what would be sent, for free
python3 enrich_apify.py data/fall2026.json --dry-run

# syntax-only over 25 contacts
python3 enrich_apify.py data/fall2026.json --level format --limit 25 \
    --out data/fall2026.apify.json

# real mailbox checks
python3 enrich_apify.py data/fall2026.json --level smtp \
    --out data/fall2026.apify.json
```

`--only-missing` skips contacts that already have an Apify result, so you can
resume a half-finished file. Results are written back onto the original rows: a
verified address always wins, an unverified one beats a bare guess.

## Sending (`mailer.py`)

Optional, and the only part of this project that talks to anyone. Nothing sends
until you have approved each draft by hand.

```bash
python3 mailer.py check                  # is the environment usable?
python3 mailer.py preview                # what would go out, and why
python3 mailer.py send --limit 10        # still a dry run
python3 mailer.py send --limit 10 --send # actually deliver, with a prompt
```

A row moves `draft → approved → sending → sent`, and only *approved* rows are
eligible. `preview` is the same code path as a real send with the transmission
removed, so what it shows you is what would happen.

Configure it through the environment — copy `.env.example` and export the
variables. There is deliberately **no `--password` flag**: arguments land in your
shell history and in `ps` for every user on the machine.

```bash
export SMTP_HOST=smtp.example.com
export SMTP_PORT=587                # 587 STARTTLS, or 465 for implicit TLS
export SMTP_USERNAME=you@example.com
export SMTP_APP_PASSWORD=xxxx       # an app password, never your account password
export SMTP_FROM=you@example.com
export SMTP_FROM_NAME='Your Name'
export SMTP_PHYSICAL_ADDRESS='1 Example Street, Springfield'
```

The **Send queue** card in the page is for reading and approving. Approving
means *you have read it*; nothing is transmitted from the browser. That split is
deliberate — delivery needs credentials that should never reach a page and a
typed confirmation, and `serve.py` listens on loopback where any open page could
reach it.

### What it refuses to do

These are enforced in code, not left to your judgement, because each one is a way
to damage a sending reputation or someone's inbox:

| Refusal | Why |
| --- | --- |
| Any send without a physical address | CAN-SPAM requires a valid mailing address in every commercial message |
| Any send with an empty `From` name | an anonymous sender is how you land in the spam folder |
| Addresses on the unsubscribes list, re-checked per recipient | so an unsubscribe that arrives mid-queue still stops the messages behind it |
| `info@`, `support@`, `noreply@` and ~25 other role addresses | departments don't reply, and writing to them earns complaints |
| A draft still containing `{first_name}` | a literal placeholder in an email you cannot recall |
| Catch-all domains, unless `MAILER_ALLOW_CATCH_ALL=1` | everything to nowhere; it is a deliverability probe, not a person |
| More than `SMTP_DAILY_LIMIT` per day, counted from the database | caps are only useful if they survive a restart |
| Less than `SMTP_DELAY_SECONDS` between messages | a burst from one IP is what gets a domain blocklisted |

Retries are bounded and classified: a `4xx` (greylisting, full mailbox) is
retried with a backoff, a `5xx` (no such user) fails immediately. If the run dies
mid-batch, rows are handed back rather than left stranded, and
`python3 mailer.py reap` recovers anything left over from a hard kill.

### Unsubscribe

Every message gets a `List-Unsubscribe` header and a footer with your address and
an unsubscribe route, both built by `mailer.py` rather than left to your
template — so editing a draft cannot quietly remove them.

With `SMTP_UNSUBSCRIBE_URL_BASE=https://…` set, the mailer also sends
`List-Unsubscribe-Post: List-Unsubscribe=One-Click`, which lets Gmail process an
unsubscribe with no action from the recipient. That needs an endpoint **you**
host which can write to the unsubscribes table; the Vercel function cannot,
because its filesystem is read-only. Without it the mailer falls back to a
`mailto:`, which asks the recipient to reply — weaker, but it requires nothing
hosted. Either way the address is honoured: add it with
`curl -X POST localhost:8000/api/db -d '{"action":"unsubscribe","email":"..."}'`.

There is no BCC, no HTML, no tracking pixel, and no mailing list. Unsolicited
mail earns its reputation by looking like a person wrote it to one person.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

`unittest` from the standard library, because this project has no dependencies
and a test runner is not a reason to start. 207 tests, no outbound network, no
fixtures on disk — each one points the database at a temporary file and throws
it away. The SMTP tests bind a throwaway server on `127.0.0.1`; no message can
reach a real inbox.

| File | Covers |
| --- | --- |
| `tests/test_db.py` | schema bootstrap, flags, export/import round trips, queue, unsubscribes, concurrent writers |
| `tests/test_serve.py` | routing, `/healthz`, request-body limits, keep-alive behaviour against a real socket |
| `tests/test_mailer.py` | screening, caps, headers, retries, and real delivery against a fake SMTP server |
| `tests/test_people.py` | every scoring rule individually, plus the properties that must hold for all inputs |
| `tests/test_wiring.py` | the string contracts between `index.html`, `serve.py` and `db.py` |

That last file earns its keep. `index.html`, `serve.py` and `db.py` agree only
through string literals — element ids and action names — so a rename in one file
leaves the other two importing, compiling and passing their own tests while the
storage button quietly stops working. `test_wiring.py` fails on that drift.

The bugs these tests were written for were all found by running them against
real behaviour rather than by reading the code, so they are worth keeping around
when you change `db.py`:

- `set_flag()` erased the record that you had already emailed someone. Marking a
  card *hidden* wiped its *sent* timestamp, because the `NOT NULL` insert
  coerced the omitted argument to `0` and made the `COALESCE` guard
  unreachable. That is how you email the same founder twice.
- `export_state()` returned an empty `activeBatch`. The UI draws one batch at a
  time, so a perfectly faithful restore looked like total data loss.
- `import db; db.upsert_company(...)` died with "no such table" on any fresh
  database, because only `serve.py` had been migrating.
- A `POST` to an unknown route answered `404` without draining the request body,
  and the leftover bytes were parsed as the next request on the same socket.

The mailer's own bugs were the same shape. `smtplib.SMTPException` inherits from
`OSError`, so the retry classifier decided a hard `550 no such user` was
retryable and would have burned three attempts and a reconnect on every bounced
address. A failed `login()` also left the socket open, leaking a file descriptor
per retry. And the unsubscribe `mailto:` was built without percent-encoding, so
`subject=unsubscribe` went out as `subject=3Dunsubscribe` and mail clients read
one malformed parameter instead of two.

Wiring the review queue to the API found three more of the same kind. `queue_list`
ignored the `limit` the page sent and `queue_status` only accepted one id, so
*Approve all drafts* had no way to work. The queue joined `company_id` but never
the company *name*, so the review list showed the slug `acme` instead of
`Acme Corp` — the wrong thing to show someone on the screen where they decide
whether to email a human. The name is joined at read time rather than copied
into the row, so renaming a company updates every queued draft instead of
leaving a stale copy behind. And `#queueActions` shipped at `display:none` with
nothing to reveal it, which is the sort of thing that compiles, tests, and is
still an empty box on screen; only driving a real browser caught it.

`serve.py` also accepts writes on loopback, so every page you have open can
reach it. A cross-origin `POST` carrying `Content-Type: text/plain` is a CORS
"simple request" — no preflight, and the browser will send it. Any such request
is refused with `403`, which is why the browser test asserts on the database
rather than on a status code the attacker cannot read anyway.

## Deploying to Vercel

```bash
npm i -g vercel
vercel          # preview
vercel --prod   # production
```

No build step and no environment variables. `vercel.json` serves `index.html`
statically, routes `/api/yc` to `api/yc.py`, and sets:

- `maxDuration: 10` — the function returns within that budget; the website crawl
  inside it has its own 7-second cap.
- `memory: 1024` — enough for the parallel crawl threads.
- A Content-Security-Policy that only permits `connect-src` to your own origin
  and `https://api.apify.com`, plus `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY` and `Referrer-Policy: no-referrer`.

A Vercel deploy is **research-only**. `db.py` is not part of the deployment, so
`/api/db` returns 404 and the page's storage panel explains that the database
needs a local `serve.py`. The YC half of the app is identical in both places —
`serve.py` routes `/api/yc` through the identical handler.

To deploy anywhere else, serve `index.html` as a static file and run
`api/yc.py`'s `handle_request` behind any WSGI server; it takes a parsed query
dict and returns `(status, payload)`.

## Judging people (`people.py`)

The hard part of this project is not finding addresses, it is knowing whether
the name in front of you is somebody you should write to. `people.py` scores
founders as *people*, and every judgement is a named reason with a number on
it, so a score is never a bare float you have to take on trust.

```bash
python3 people.py score                   # everyone, best first, with reasons
python3 people.py score --contactable     # only people you can actually reach
python3 people.py score --hold            # only who to leave alone
python3 people.py why 42                  # the full argument for one person
python3 people.py score --batch "Fall 2026"
```

```
score  band      id  name              company                why
 0.53  careful    3  Katherine Johnson Gamma Systems          'founder & president' decides things here
 0.35  hold       6  Barbara Liskov    Zeta Cloud             catch-all domain: every address resolves, so...
 0.00  hold       4  Team              Delta Health           'Team' is a department label, not a person
 0.88  BLOCKED    7  Edsger Dijkstra   Eta Robotics           edsger@eta.io opted out; never contact again
```

Some of the judgements are deliberately against the usual advice:

- **Personal webmail scores positive.** A founder on `ada@gmail.com` rather than
  the corporate pattern somebody made them pick is, for an email you did not
  ask for, a better signal — they arranged to be personally reachable.
- **A catch-all domain heavily discounts a name-shaped address.** If every
  address at the domain resolves, `barbara@zeta.cloud` carrying her name is not
  evidence that Barbara has a mailbox there.
- **Being unreachable caps the score rather than adding to it.** A CEO at a real
  company you cannot write to is not a lead.
- **"Team", "Founders" and "Hiring Team" are not people.** They get no
  candidate addresses either, because the ladder would happily offer five role
  mailboxes — the exact addresses `mailer.py` refuses to send to.
- **Blocked is not a lower score.** Someone on the unsubscribes list still
  scores 0.88; they are simply off-limits. Averaging that into a float would be
  a way of eventually mailing them again.

`--apply` writes raised scores back to the database. It only ever raises, it
never touches a founder who has a verified mailbox (that 0.95 came from a real
check and is stronger evidence than anything inferred from a title), and it
skips blocked people entirely.

There is no network code in the file. It is a judgement layer over data you
already have, and it never invents an address — candidate addresses come from
the same pattern ladder as everything else, are labelled as guesses, and
contribute exactly zero to the score.

## Privacy and rate limits

Both `scrape_batch.py` and the serverless function read public pages, but they
are still someone else's server. Requests carry a descriptive User-Agent, site
crawls are capped per page and per company, and nothing retries forever. If you
plan to run this at scale, put it behind a cache and respect the terms of the
sites you are reading.

MIT licensed. See [LICENSE](LICENSE).