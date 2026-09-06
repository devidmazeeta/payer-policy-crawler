# payer-policy-crawler

Discovers publicly available provider / medical-policy documents on health-payer
websites and emits them as a 22-column dataset with full provenance for every
row. Public pages only: no accounts, no logins, no CAPTCHA or WAF bypass.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows;  source .venv/bin/activate on POSIX
pip install -r requirements.txt
```

Python 3.11+ (developed and tested on 3.14). Dependencies: `httpx` (async HTTP),
`beautifulsoup4` + `lxml` (HTML), `pypdf` (PDF text), `PyYAML` (config),
`openpyxl` (XLSX output), `pytest` + `pytest-asyncio` (tests). All free and
open-source; there are no paid proxies or APIs.

Before the first run, put your own contact address in `crawl.user_agent` in
`config.yaml` — startup validation rejects a User-Agent without a contact email
or URL, because a payer's operations team must be able to identify and reach you
from a single access-log line.

## Run

One command runs the whole project:

```bash
python -m crawler.main --config config.yaml
```

Any config value can be overridden on the command line, and **CLI > config file
> built-in default**:

```bash
python -m crawler.main --config config.yaml --max-docs-per-payer 20 \
    --payer-range 1-5 --payer-csv payer_seed_list.csv
```

Useful flags: `--dry-run` (validate config + seed list, list payers, fetch
nothing), `--show-config` (print the merged configuration), `--explain-dedupe`
(print the near-duplicate rule), `--also-xlsx`, `--no-resume`, `--quiet`.
`python -m crawler.main --help` lists all of them.

**Expected runtime:** ~2-3 minutes for all 10 payers at the shipped defaults
(50 docs/payer, 8 concurrent requests, 1 req/s per host). The floor is set by
politeness, not by CPU — the per-host rate limit means a single payer with 50
documents takes at least ~50 seconds no matter how much parallelism you add.
Raising `max_docs_per_payer` scales runtime roughly linearly.

Interrupt any run with Ctrl-C and re-issue the same command: it resumes from
`state/checkpoint.db` and will not re-fetch or re-emit what it already has.

## Running fresh

Because resume is on by default, re-issuing the same command after a completed
run finishes in under a second and re-emits the same dataset — every payer is
already marked done in the checkpoint. To actually crawl again:

```bash
# Re-crawl everything from scratch (clears the checkpoint first).
python -m crawler.main --config config.yaml --no-resume
```

That is all that is required: `output.csv`/`output.xlsx` are overwritten, and
`downloads/` is content-addressed (same bytes produce the same filename), so
existing copies are reused rather than duplicated. For a completely empty slate —
new logs, no stale documents — clear the artefact folders too:

```bash
rm -rf output/* downloads/* logs/*        # PowerShell: Remove-Item -Recurse -Force output\*, downloads\*, logs\*
rm -f  state/checkpoint.db*               # optional; --no-resume clears it anyway
python -m crawler.main --config config.yaml
```

Two things worth knowing:

- **`--no-resume` clears the *whole* checkpoint, not just the selected payers.**
  Combining it with `--payer-range` therefore discards the other payers' rows,
  and `output.csv` will contain only the payers you selected — the dataset is
  assembled from the checkpoint, which is what makes resume work. To re-crawl one
  payer without disturbing the rest, give it its own checkpoint and output
  directory:

  ```bash
  python -m crawler.main --config config.yaml --payer-range 9 --no-resume \
      --state-file state/scratch.db --output-dir output/scratch
  ```

- **If something else holds `state/checkpoint.db` open** — a DB browser, or
  PyCharm's Database panel, which attaches to `.db` files automatically — the
  file cannot be deleted on Windows. `--no-resume` handles this by clearing the
  tables through the open handle instead and logging `resume.reset_fallback`, so
  the run still starts clean. If even that fails it stops with an error rather
  than silently resuming. Detaching the data source in the IDE avoids the whole
  situation.

Re-runs are not guaranteed to be identical: payer sites change, and several of
the ten block automated access intermittently rather than consistently. See
NOTES.md for the per-payer findings from the recorded run.

## Outputs

| Path | Contents |
|---|---|
| `output/output.csv` | the dataset — 22 columns, UTF-8, RFC 4180 (`--also-xlsx` adds `output.xlsx`, data on sheet 1) |
| `output/schema_problems.txt` | written only if a row failed schema validation |
| `downloads/<payer_name>/` | a copy of every fetched document, one folder per payer |
| `logs/{run_id}.jsonl` | structured JSON-lines audit trail (one object per line) |
| `logs/{run_id}.log` | human-readable rotating log |
| `logs/latest.jsonl`, `logs/latest.log` | pointers to the most recent run |
| `logs/summary-{run_id}.txt` | per-payer attempted / found / failed / skipped table |
| `state/checkpoint.db` | SQLite resume state (use a `.json` suffix for the JSON backend) |

## Architecture

```
crawler/
  main.py            CLI, config merge, orchestration, run summary
  config.py          config load/validate; CLI > file > default precedence
  seeds.py           payer_seed_list.csv loading
  robots.py          robots.txt fetch + full RFC 9309 rule parsing
  fetcher.py         async HTTP: per-host token bucket, global cap, retries
  discovery.py       robots -> sitemaps -> index crawl -> enumeration fallback
  extractor.py       HTML/PDF -> schema rows, classification, confidence
  dedupe.py          near-duplicate collapsing rule
  schema.py          the 22 columns, enums, validation
  storage.py         downloads/<payer>/ writer, CSV/XLSX writers
  state.py           SQLite/JSON checkpoint for resume
  mailer.py          optional Gmail delivery
  logging_setup.py   JSON-lines + human logs
```

Per payer, in this order: fetch and fully parse `robots.txt`; use its declared
sitemaps as an index frontier; crawl the index/category pages for anchors
pointing at documents (including cross-directory and cross-subdomain links);
fetch each document, hash the raw bytes and extract metadata. Where a listing or
search endpoint that would have helped is robots-Disallowed, we do not crawl it —
instead a bounded, rate-limited enumeration of an observed numeric URL pattern
runs, verifying every candidate with a 200 plus a content/title match. If a payer
blocks automated access, we record the block with evidence and emit no fabricated
rows.

Concurrency is `asyncio`: a global semaphore caps total in-flight requests, and
each host has its own token bucket (further slowed by any `Crawl-delay`), so
adding payers only adds parallelism across *different* hosts. Each payer runs
isolated — one payer crashing is logged and the run continues.

## Optional: email delivery

Off by default (`email.enabled: false`); with it off the run needs no
credentials and contacts no mail service. A send failure logs an ERROR and never
fails the crawl.

**SMTP (simplest).** Set `email.smtp_or_gmail_api: smtp`, then export an
[app password](https://myaccount.google.com/apppasswords) — a Google account
password will not work if 2FA is on:

```bash
export CRAWLER_SMTP_USER="you@gmail.com"
export CRAWLER_SMTP_PASSWORD="abcd efgh ijkl mnop"   # 16-char app password
python -m crawler.main --config config.yaml --email-enabled \
    --email-recipients someone@example.com
```

**Gmail API (OAuth).** Set `email.smtp_or_gmail_api: gmail_api` and uncomment the
`google-*` lines in `requirements.txt`. Create an OAuth *desktop* client in
Google Cloud Console with the Gmail API enabled, save it as `credentials.json`
(path configurable via `email.gmail_credentials_file`), and run the crawler once
interactively to authorise — consent opens a browser and caches a token in
`token.json`. Later unattended runs refresh that token automatically. Scope is
`gmail.send` only, which grants no read access to the mailbox.

Credentials are read exclusively from environment variables or the token files
named in the config. Nothing is hard-coded, and no secret is written to a log.

## Optional: proxy

`crawl.proxy_url` routes requests through an outbound proxy. It is empty by
default and the project neither needs nor assumes one.

## Tests

```bash
python -m pytest
```

411 tests, fully offline — all HTTP is served by `httpx.MockTransport`. Coverage
concentrates on schema validation and the formatting conventions, the robots.txt
rule engine, the de-duplication rule, retry/backoff and rate limiting, resume,
and an end-to-end run against a simulated two-payer site.

See `NOTES.md` for the approach, per-payer findings, the de-duplication rule,
known gaps, and where the data may be wrong.
