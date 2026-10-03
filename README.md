# ApplyOnce

Cold-email YC founders without building a database, an account, or a pipeline.

Pick a YC batch, click **Load companies**, and you get every founder with the
best email address this project can find — plus a finished draft you can copy or
open in your mail app. Everything is stored in your browser. There is no server
holding your data.

Standard library Python and one HTML file. No npm, no build step, no framework,
no dependencies.

```
index.html          the entire web app
api/yc.py           YC directory data + website email scraping (Vercel function)
serve.py            local dev server
scrape_batch.py     export a whole batch to JSON / CSV
enrich_apify.py     verify guessed addresses with Apify (optional, paid)
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

## Your data stays in your browser

Everything lives in one `localStorage` key, `applyonce.v1`: your details, your
template, every batch you loaded, sent marks, and any Apify token.

- No account, no database, no analytics.
- **Clear everything** at the bottom of the page removes the key outright.
- Because it is browser-local, another browser or another device starts empty.

The Vercel function is stateless. It reads public YC data and returns JSON; it
never receives or stores anything about you.

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

To deploy anywhere else, serve `index.html` as a static file and run
`api/yc.py`'s `handle_request` behind any WSGI server; it takes a parsed query
dict and returns `(status, payload)`.

## Privacy and rate limits

Both `scrape_batch.py` and the serverless function read public pages, but they
are still someone else's server. Requests carry a descriptive User-Agent, site
crawls are capped per page and per company, and nothing retries forever. If you
plan to run this at scale, put it behind a cache and respect the terms of the
sites you are reading.

MIT licensed. See [LICENSE](LICENSE).