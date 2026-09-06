# NOTES

## Approach

Discovery runs the brief's priority order literally, per payer, with every
candidate URL passing through one robots gate (`PayerCrawler._allowed`) before it
is requested — a single choke point to audit for compliance.

1. **robots.txt, fully parsed.** `crawler/robots.py` implements RFC 9309 rather
   than using `urllib.robotparser`, because I needed three things the stdlib does
   not give: per-group `Crawl-delay`, `$` end-anchoring, and the *identity of the
   matched rule* so a skip can be justified in the log and in `notes`. A
   `Crawl-delay` always narrows the token bucket, never widens it.
2. **Declared sitemaps as an index frontier**, following nested `<sitemapindex>`
   documents and gzipped sitemaps. Sitemap entries that are already documents
   skip the index stage. Parsed with regexes, not ElementTree: a meaningful
   fraction of payer sitemaps are not well-formed XML (unescaped `&`, truncated
   tails), and a strict parser throws away the whole file on the first error.
3. **Index/category crawl** for anchors, keeping cross-directory and
   cross-subdomain targets (payers habitually list at `/policies/` and serve the
   PDFs from `/content/dam/` or a separate host of their own). Anchor `title` and
   image `alt` attributes are used when the link text is empty, which is the norm
   for icon-only PDF links.
4. **Bounded enumeration fallback**, only where a listing/search endpoint that
   would have helped is robots-Disallowed. The disallowed endpoint is never
   fetched; instead a numeric URL template is inferred from documents already
   found legitimately (≥3 observations required), neighbours of the observed range
   are probed HEAD-first, and each candidate must return 200 **and** pass a
   content/title check before it is emitted. Capped by
   `max_enumeration_candidates` and paced by the same per-host bucket.
5. **Blocks reported, never papered over.** WAF interstitials that answer 200,
   refusal statuses (401/403/406/451), and a robots.txt we were refused all set a
   payer-level `blocked` flag with the matched evidence. No fabricated rows.

Politeness: one global semaphore (`concurrency_cap`) plus a per-host token bucket
(`per_domain_rate_limit_per_sec`), so adding payers only adds parallelism across
*different* hosts. Retries are exponential-with-jitter and only for failures a
retry can fix (timeouts, connection errors, 429, 5xx); a 429's `Retry-After` is
obeyed exactly and permanently slows that host for the rest of the run. Config
validation rejects >10 req/s per host and >64 concurrency outright.

## Run results (10 payers, 50 docs/payer cap)

**109 rows, 4 payers with documents, 5 hard-blocked, ~140s wall clock.** Full
table in `logs/summary-*.txt`; console transcript in `run_console.txt`.

| # | Payer | found | outcome |
|---|---|---|---|
| 1 | UnitedHealthcare | 50 | hit the per-payer cap; the library is far larger |
| 2 | Horizon BCBSNJ | 0 | **blocked** — Imperva/Incapsula, 200 + challenge body |
| 3 | Cigna | 50 | hit the cap |
| 4 | UPMC Health Plan | 0 | 8 policy links found, all 8 return 404 on their side |
| 5 | Elevance/Anthem | 0 | **blocked** — robots.txt itself times out (Akamai) |
| 6 | CareSource | 0 | **blocked** — 406 to every request |
| 7 | Centene | 0 | **blocked** — CAPTCHA on the corporate newsroom |
| 8 | Geisinger | 0 | provider section redirected to a maintenance page |
| 9 | Highmark | 4 | policy CMS on `securecms.highmark.com` |
| 10 | BCBS Tennessee | 0 | **blocked** — 403 on robots.txt (bare *and* `www.`) |

Data quality across the 109 rows: all schema-valid, `document_url` unique, 100
PDFs with extracted text, 43 with a policy number and a printed effective date,
median `confidence_score` 0.89, and 8 non-200 rows retained as evidence. Every
row below 0.70 carries an explanation in `notes`.

## Per-payer difficulty notes

- **UHC** publishes the most accessible library of the ten (`/content/dam/provider/docs/public/policies/`,
  with policy numbers and effective dates printed in the PDFs). Its `robots.txt`
  is a CMS debug dump — thousands of `Content Updated Start Path...!!` lines and
  no directives — which my parser correctly reduces to zero rules.
- **Cigna** is reachable but noisy: `robots.txt` disallows `/sites/` (where some
  policy PDFs live, so those are correctly skipped) and all of
  `legacy.cigna.com`. Its sitemaps include ~12,000 Healthwise consumer-health
  articles and a full `/es-us/` mirror; both are filtered, without which the
  frontier budget produces zero documents.
- **Highmark** keeps its medical-policy CMS on `securecms.highmark.com` while
  `www.highmark.com` soft-404s the obvious provider paths with a 135 KB page.
  I seed the CMS host by absolute URL in the seed list — the capability exists
  precisely because the hint host is only a starting point.
- **UPMC** links 8 policy PDFs from `/providers/medical/resources` that all 404.
  Their site is broken, not mine; the rows are emitted with `http_status=404`.
- **Horizon, CareSource, Centene, BCBST, Anthem** all refuse automated access at
  the network edge. Anthem is the most awkward: `robots.txt` never responds, so
  under fail-closed rules I cannot know what is permitted and must stay out.
- **Geisinger** was in maintenance during the run (`geisinger.org/health-plan/providers`
  → a CloudFront maintenance page). A re-run on another day may well differ.

## Near-duplicate collapsing rule

Four tiers, strongest first, merged **transitively** (union-find) so a chain
(A shares bytes with B, B shares a title with C) collapses to one row.
`crawler/dedupe.py:rule_description()` is the authoritative text and
`--explain-dedupe` prints it.

0. **Exact canonicalised URL** — scheme/host lowercased, fragment and tracking
   parameters dropped, default ports removed, `index.html` and trailing slash
   stripped, query parameters sorted. This guarantees the schema's uniqueness
   constraint absolutely.
1. **Identical `content_hash_sha256`, same payer**, among 200 rows.
2. **Same payer + `policy_number` + `document_type`** — a payer-assigned policy
   number identifies one policy across its revisions.
3. **Same payer + `document_type` + `file_type` + normalised title**, where
   normalisation lowercases and strips dates, version/revision markers,
   punctuation and stopwords. This is the tier that catches
   `policy-cs123-2025.pdf` vs `policy-cs123-2026.pdf`. A title that reduces to
   fewer than two significant words yields an empty key and merges nothing.

Tier 1 is **scoped to the payer**, which was a deliberate correction during
development: keying on the digest alone silently deleted an entire payer's
contribution when two payers served byte-identical bytes. That is common in this
domain — shared vendor criteria (MCG, InterQual), CMS forms, jointly published
drug lists — and each payer publishing that document is a genuine finding.

Survivor: newest `effective_date`, then newest `last_updated_date`, then higher
`confidence_score`, then 200 over non-200, then `pdf` over `html`, then the
shorter/shallower URL, then lexicographic order for determinism. The survivor's
`notes` records how many duplicates it absorbed and an example URL, so a
reviewer can verify the merge rather than take it on trust. This run collapsed 3.

## Where the data may be wrong, and why

- **`document_type` and `line_of_business` are keyword-inferred**, not read from
  a declared field. A UHC dental review guideline classified as
  `coverage_guideline` rather than `medical_policy` is a judgement call the
  keyword table makes, not ground truth. `line_of_business` is the weaker of the
  two; `Multiple` (63 of 109 rows) is often genuinely correct but is also what
  you get when a document mentions several lines in passing.
- **`state_or_region` leans on the seed default.** 92 rows say `National`
  because that is the payer's default, not because the document said so. Bare
  two-letter codes are deliberately not matched inside prose — "OR" and "IN" are
  English words and would poison the column — so a state named only in body text
  is missed.
- **Month-year dates are anchored to the 1st.** "January 2026" becomes
  `2026-01-01`; the day is inferred, not printed. The crawl date is *never*
  substituted for a missing date.
- **`file_size_bytes` and `content_hash_sha256` are empty on non-200 rows** by
  design: the bytes received were an error page, and reporting their length would
  let a 404 row claim a 59 KB document.
- **Titles from filenames** (`title_from_url`) are marked `url_pattern` in
  `extraction_method` and scored below 0.70 with a note. Treat them as weak.
- **`registrable_domain` is an approximation**, not the public-suffix list. Fine
  for ten known `.com`/`.org` domains; would need `publicsuffix2` for arbitrary
  input.
- **doc/docx/xls/xlsx are identified and hashed but not parsed.** Those rows
  carry URL-derived metadata and say so in `notes`.
- **Blocked payers may publish plenty.** A 0 against Horizon means "we were
  stopped at the edge", not "nothing exists" — the summary's `outcome` column and
  the `payer.blocked` records make that distinction explicit, which the brief
  calls out as scoring-relevant.

## Known gaps

- **No headless rendering.** Every row is `render_mode=static`. Payers with a
  JS-driven policy search (Anthem's, Cigna's provider portal) are unreachable
  without a browser. `render_mode` and the enum already accommodate `headless`.
- **UHC and Cigna hit the 50-document cap**, so 109 rows is a floor, not a
  ceiling — the crawler stopped because it was told to, not because it ran out.
- **The enumeration fallback did not fire in this run** (`enumerated=0`
  everywhere). The payers with a robots-disallowed search endpoint were also the
  payers where no documents were found legitimately, and inference needs ≥3
  observed URLs. It is exercised by `tests/test_discovery.py`.
- **`crawl.allowed_extra_hosts` and per-payer `extra_hosts` are hand-curated.**
  A payer moving its document host would need a seed-list edit.
- **No incremental/diff mode.** Re-running produces the current state; it will
  not tell you what changed since last week.

## What I would do with two more weeks

1. **Playwright for the JS-driven payers**, behind `render_mode: headless` and a
   config flag, with the same robots gate in front of it. Highest-value item by
   far: it is what unlocks Anthem and the deeper Cigna/UPMC listings.
2. **Per-payer adapters.** The generic crawler gets ~4 payers; the remaining six
   each need a small amount of site knowledge (Highmark's CMS routes, Centene's
   operating brands as separate seeds, Horizon's public PDF host). A thin
   adapter-per-payer layer over the generic engine, kept honest by the shared
   schema.
3. **Better metadata extraction.** Per-payer selectors and title/date patterns
   would lift the 43/109 policy-number coverage substantially, and a real PDF
   layout pass (pdfplumber) would read the header tables that `pypdf`'s flat text
   loses.
4. **Change detection.** The content hash and the checkpoint already make this
   easy: store run history and emit an added/changed/removed diff per payer.
5. **Correctness harness.** Hand-label ~100 documents per payer and measure
   precision/recall of discovery *and* of `document_type`, so the heuristics can
   be tuned against a number instead of an impression.
6. **Contact the payers.** Several blocks would likely be lifted for an
   identified research crawler; the User-Agent already carries a contact address
   for exactly that reason.

## Where time went

Roughly: 30% on discovery (the sitemap/index/enumeration pipeline and the
politeness machinery), 20% on the schema and its conventions — the 22 columns,
the enums, and the cross-field rules are where a wrong deliverable hides, so
`validate_row` gates every write — 20% on the test suite (412 offline tests),
15% on the real-payer iteration that produced the findings above, and 15% on
config/logging/resume/email plumbing.

Five real defects were found by running against live sites rather than fixtures,
which is the argument for doing it: cross-payer de-duplication deleting a
payer's whole contribution; index pages being re-crawled because they were never
added to the seen set; blocked payers being reported as "publishes nothing";
`file_size_bytes` reporting error-page sizes; and `--no-resume` silently doing
nothing when the checkpoint file was locked by another process.
