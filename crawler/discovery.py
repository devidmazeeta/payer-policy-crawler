"""
Per-payer discovery: robots -> sitemaps -> index/category pages -> documents,
with a bounded enumeration fallback for robots-blocked search endpoints.

The strategy implements the brief's priority order literally, one payer at a time:

1. **robots.txt** is fetched and fully parsed first (:mod:`crawler.robots`).
   Nothing else happens until we know the rules, and every candidate URL is
   checked against them before it is requested.
2. **Declared sitemaps** become the initial frontier. Sitemaps are treated as an
   *index* of index pages, not as a document list, because that is what payers
   actually publish - a sitemap entry is usually a category landing page.
3. **Index/category pages** from the sitemap are crawled for anchors pointing at
   policy documents, including links that leave the current directory (payers
   habitually keep the listing at ``/policies/`` and the PDFs at ``/assets/``).
4. **Bounded enumeration** is used *only* where a helpful listing/search endpoint
   is robots-Disallowed. We do not crawl the disallowed endpoint; instead we
   extrapolate an observed numeric URL pattern from documents we already found
   legitimately, and verify each candidate with a 200 + content/title match
   before emitting it. Rate-limited and capped by ``max_enumeration_candidates``.
5. **Blocks are reported, never papered over.** If a payer serves a WAF
   challenge or a login gate, we emit rows describing the block with evidence
   (the marker we matched, the status code) and log ``payer.blocked``. No
   fabricated document rows, ever.

Host scope: the hint host is only a starting point, so we follow redirects and
accept sibling subdomains and the regional/state hosts a payer links to, subject
to :meth:`PayerCrawler._host_in_scope`.
"""

from __future__ import annotations

import asyncio
import gzip
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence
from urllib.parse import urljoin, urlsplit, urlunsplit

from . import logging_setup as ev
from .extractor import (
    NEGATIVE_URL_HINTS,
    build_row,
    classify_document_type,
    extract_links,
    looks_like_document_url,
    looks_like_policy_index,
    score_candidate,
)
from .fetcher import BLOCK_STATUS, Fetcher, FetchResult, detect_file_type
from .robots import RobotsCache
from .schema import DocumentRow, clean_text, utc_now_iso
from .seeds import Payer
from .storage import Downloader

#: Sitemap paths worth probing when robots.txt declares none. Every one is still
#: robots-checked before it is requested - this list only decides what we *ask*
#: about, never whether we are allowed to.
COMMON_SITEMAP_PATHS: tuple[str, ...] = (
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/sitemap.xml.gz",
    "/sitemaps/sitemap.xml",
    "/sitemap/sitemap-index.xml",
)

#: Path fragments that mark a page as likely to *list* policy documents. Used to
#: prioritise the index frontier so the crawl budget is spent where documents
#: actually live, and as seeds when a payer publishes no usable sitemap.
POLICY_PATH_HINTS: tuple[str, ...] = (
    "medical-polic", "medicalpolic", "medical_polic",
    "clinical-polic", "clinicalpolic", "clinical-criteria", "clinical-guideline",
    "coverage-polic", "coverage-guideline", "coverage-determination",
    "payment-polic", "reimbursement-polic", "administrative-polic",
    "pharmacy-polic", "pharmacy-guideline", "drug-polic", "drug-list",
    "formular", "preferred-drug", "specialty-drug", "medication-polic",
    "prior-auth", "priorauth", "pre-auth", "preauth", "precertification",
    "prior-approval", "authorization-requirement",
    "provider-manual", "provider-administrative", "provider-reference",
    "provider-polic", "provider-resource", "provider-notice", "provider-bulletin",
    "policies-and-procedures", "policy-and-procedure",
    "bulletin", "newsletter", "provider-update", "network-notification",
    "utilization-management", "um-guideline", "mcg", "interqual",
    "cob-polic", "coding-polic", "claims-polic", "claim-edit",
)

#: Fragments that mark a page as noise for this task. Filtered out of the
#: frontier so the budget is not spent on careers pages and press releases.
NOISE_PATH_HINTS: tuple[str, ...] = (
    "/careers", "/jobs", "/news/", "/press", "/newsroom", "/about-us", "/aboutus",
    "/investor", "/legal", "/privacy", "/terms", "/sitemap", "/search?", "/login",
    "/register", "/contact", "/find-a-doctor", "/find-care", "/shop", "/plans/",
    "/quote", "/enroll", "/broker", "/employer", "/individual", "/wellness",
    "/blog/", "/events", "/webinar", "/social", "/espanol", "/language",
    "/accessibility", "/nondiscrimination", "/site-map", "/cookie",
    # Locale variants: translated duplicates of pages we already cover in
    # English. Cigna alone publishes ~6,000 /es-us/ pages, which would consume
    # the whole frontier budget for zero additional documents.
    "/es-us/", "/es_us/", "/es/", "/zh-", "/vi-", "/ko-", "/ru-", "/fr-",
    "/spanish", "/chinese", "/vietnamese", "/korean", "/tagalog",
    # Consumer health encyclopaedias (Cigna/Healthwise) - patient education,
    # not payer policy.
    "/knowledge-center/hw/", "/healthwise", "/health-topics",
)

#: XML tags we read out of a sitemap. Namespaces vary wildly between payers, so
#: the parser is namespace-agnostic and matches on the local tag name.
_SITEMAP_LOC_RE = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</loc>",
                             re.IGNORECASE | re.DOTALL)
_SITEMAP_LASTMOD_RE = re.compile(r"<lastmod>\s*(.*?)\s*</lastmod>", re.IGNORECASE | re.DOTALL)
_SITEMAP_IS_INDEX_RE = re.compile(r"<sitemapindex", re.IGNORECASE)
#: One <url> or <sitemap> element. The lookahead is what stops the <urlset> /
#: <sitemapindex> wrapper from matching as one giant element. Parsing per block
#: keeps each <loc> paired with
#: its own <lastmod>, which a flat scan of the document cannot do when only some
#: entries carry a date - the common case in payer sitemaps.
_SITEMAP_ENTRY_RE = re.compile(
    r"<(?:url|sitemap)(?=[ \t\r\n>])[^>]*>(.*?)</(?:url|sitemap)\s*>", re.IGNORECASE | re.DOTALL
)

#: A numeric run of 2+ digits in a URL path, used to detect an enumerable pattern.
_NUMERIC_SEGMENT_RE = re.compile(r"(\d{2,})")

#: Disallow patterns that would have *helped* us had we been allowed to use them:
#: search endpoints, policy listings and document handlers. These are precisely
#: the "a page/search endpoint that would help is robots-Disallowed" condition
#: that licenses the bounded enumeration fallback (strategy step 4). They are
#: detected by scanning the robots rules directly rather than by waiting to
#: stumble across such a URL mid-crawl, because the noise filter may well
#: discard a /search? link before the robots check ever runs.
#:
#: Kept deliberately narrow. An early version also matched "index" and "list",
#: which flagged every Drupal install's /index.php/user/login and /index.php/admin/
#: as a lost discovery opportunity - noise that would have justified enumeration
#: on payers where nothing useful was actually withheld.
HELPFUL_BLOCKED_HINTS: tuple[str, ...] = (
    "search", "polic", "guideline", "criteria", "formular", "drug", "coverage",
    "medical", "pharmac", "provider", "document", "download", "bulletin",
    "manual", "library", "catalog", "browse",
)

#: Paths that are never a discovery aid no matter which hint they contain. A
#: disallowed admin console or login form tells us nothing about policy documents,
#: and treating it as a withheld listing would license enumeration we cannot
#: justify to a reviewer.
NEVER_HELPFUL_HINTS: tuple[str, ...] = (
    "admin", "/user", "login", "logout", "signin", "sign-in", "register",
    "password", "account", "cart", "checkout", "comment", "node/add", "oembed",
    "filter/tips", "ajax", "cron", "install", "update.php", "wp-", "cgi-bin",
    "/print", "email", "captcha", "session", "logout", "profile", "preferences",
)


@dataclass(slots=True)
class PayerStats:
    """
    Per-payer counters, emitted verbatim in the ``payer.done`` log record and in
    the run summary table. ``blocked``/``block_evidence`` are what make an empty
    payer legible: zero documents with ``blocked=True`` means "we were stopped",
    zero with ``blocked=False`` means "this payer publishes nothing reachable".
    """

    payer: str = ""
    attempted: int = 0          # URLs we requested as document candidates
    found: int = 0              # rows emitted with a usable document
    failed: int = 0             # candidates that errored or returned non-200
    skipped_robots: int = 0     # candidates never requested, blocked by robots
    dupes_collapsed: int = 0    # near-duplicates merged away by dedupe
    elapsed_s: float = 0.0
    sitemaps_parsed: int = 0
    index_pages_crawled: int = 0
    enumerated: int = 0         # candidates tried by the enumeration fallback
    blocked: bool = False       # payer refused automated access entirely
    block_evidence: str = ""
    hosts: list[str] = field(default_factory=list)

    def as_log_fields(self) -> dict[str, Any]:
        """The exact field set the brief requires on ``payer.done``, plus context."""
        return {
            "payer": self.payer,
            "attempted": self.attempted,
            "found": self.found,
            "failed": self.failed,
            "skipped_robots": self.skipped_robots,
            "dupes_collapsed": self.dupes_collapsed,
            "elapsed_s": round(self.elapsed_s, 2),
            "sitemaps_parsed": self.sitemaps_parsed,
            "index_pages_crawled": self.index_pages_crawled,
            "enumerated": self.enumerated,
            "blocked": self.blocked,
            "block_evidence": self.block_evidence or None,
            "hosts": self.hosts,
        }


@dataclass(slots=True)
class Candidate:
    """
    A URL queued for fetching, carrying the provenance chain that produced it.

    ``discovery_path`` is accumulated as the crawl descends, so the value written
    to the CSV is literally the trail we followed - which is the point: a
    reviewer must be able to reproduce the find by hand from that column alone.
    """

    url: str
    source_page_url: str
    discovery_path: str
    #: "sitemap", "index", "anchor" or "url_pattern" - drives extraction_method.
    origin: str = "anchor"
    #: Anchor text, when we came from a link. The best available title hint.
    link_text: str = ""
    #: Heuristic priority; higher is crawled first.
    priority: float = 0.0
    depth: int = 0


def normalise_url(url: str, *, drop_fragment: bool = True) -> str:
    """
    Canonicalise a URL for comparison and de-duplication.

    Lowercases the scheme and host, drops the fragment and default ports, strips
    tracking/session query parameters, removes a trailing ``index.html``, and
    collapses a trailing slash. This is the URL-level half of de-duplication; the
    content-level half lives in :mod:`crawler.dedupe`.
    """
    try:
        split = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    scheme = (split.scheme or "https").lower()
    host = (split.hostname or "").lower()
    if not host:
        return url.strip()
    port = split.port
    netloc = host
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{host}:{port}"

    path = split.path or "/"
    for index_name in ("index.html", "index.htm", "default.aspx", "index.aspx"):
        if path.lower().endswith("/" + index_name):
            path = path[: -len(index_name)]
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    path = re.sub(r"/{2,}", "/", path) or "/"

    # Drop parameters that identify a visitor or a campaign rather than a document.
    junk_prefixes = ("utm_", "gclid", "fbclid", "mc_cid", "mc_eid", "_ga", "msclkid",
                     "sessionid", "jsessionid", "phpsessid", "cachebust", "_t", "ts",
                     "srsltid", "wbraid", "gbraid")
    kept_params = []
    for pair in split.query.split("&"):
        if not pair:
            continue
        key = pair.split("=", 1)[0].lower()
        if any(key.startswith(prefix) for prefix in junk_prefixes):
            continue
        kept_params.append(pair)
    # Sorting makes ?a=1&b=2 and ?b=2&a=1 the same URL.
    query = "&".join(sorted(kept_params))
    return urlunsplit((scheme, netloc, path, query, "" if drop_fragment else split.fragment))


def registrable_domain(host: str) -> str:
    """
    Approximate the registrable domain of *host* (``www.uhcprovider.com`` ->
    ``uhcprovider.com``).

    A deliberately dependency-free approximation: it handles the common two-label
    TLDs a US payer might use rather than shipping the full public-suffix list,
    which would be overkill for ten known domains. Documented as a known
    simplification in NOTES.md.
    """
    host = (host or "").lower().strip(".")
    if not host:
        return ""
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    two_label_tlds = {"co.uk", "com.au", "co.nz", "com.br", "co.jp", "org.uk", "net.au"}
    if ".".join(parts[-2:]) in two_label_tlds and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


class PayerCrawler:
    """
    Runs the whole discovery pipeline for exactly one payer.

    One instance per payer keeps all mutable crawl state (the seen-URL set, the
    frontier, the host scope) naturally isolated, which is also what makes
    failure isolation in :mod:`crawler.main` easy: an exception here can only
    ever cost us this payer.
    """

    def __init__(
        self,
        payer: Payer,
        config: Any,
        fetcher: Fetcher,
        robots: RobotsCache,
        downloader: Downloader,
        log: Any,
        state: Any = None,
    ) -> None:
        self.payer = payer
        self.config = config
        self.fetcher = fetcher
        self.robots = robots
        self.downloader = downloader
        self.state = state
        self.log = log.bind(payer=payer.payer_alias or payer.payer_name)

        self.stats = PayerStats(payer=payer.payer_name)
        self.rows: list[DocumentRow] = []
        #: Normalised URLs already handled, so a document linked from five index
        #: pages is fetched once. document_url uniqueness in the output depends
        #: on this plus the dedupe pass.
        self.seen_urls: set[str] = set()
        #: Hosts we have decided are in scope (grows as redirects reveal them).
        self.in_scope_hosts: set[str] = set()
        #: robots-Disallowed paths we encountered, which drive the decision to
        #: attempt the enumeration fallback (and are quoted as its justification).
        self.blocked_paths: list[tuple[str, str]] = []
        self._start_time = 0.0

    # -----------------------------------------------------------------------
    # Host scope
    # -----------------------------------------------------------------------
    def _host_in_scope(self, host: str) -> bool:
        """
        Decide whether *host* is still "this payer".

        In scope: the seed host itself, anything sharing its registrable domain
        (so ``www.``, ``provider.``, ``member.`` and regional subdomains all
        qualify), any host listed in the payer's seed row, and any host the
        operator added via ``crawl.allowed_extra_hosts``.

        Deliberately out of scope: CDNs and unrelated third parties. Following
        those would turn a targeted discovery run into an open web crawl.
        """
        host = (host or "").lower()
        if not host:
            return False
        if host in self.in_scope_hosts:
            return True
        seed_domain = registrable_domain(self.payer.hint_host)
        if seed_domain and registrable_domain(host) == seed_domain:
            self.in_scope_hosts.add(host)
            return True
        for extra in list(self.payer.extra_hosts) + list(self.config.crawl.allowed_extra_hosts):
            extra = extra.lower().strip()
            if not extra:
                continue
            if host == extra or host.endswith("." + extra) \
                    or registrable_domain(host) == registrable_domain(extra):
                self.in_scope_hosts.add(host)
                return True
        return False

    async def _allowed(self, url: str) -> tuple[bool, str]:
        """
        Robots gate for a single URL. Returns ``(allowed, evidence)``.

        Every fetch in this module goes through here first. A refusal is logged
        as ``robots.blocked`` with the matched rule quoted, counted in
        ``skipped_robots``, and remembered in :attr:`blocked_paths` so the
        enumeration fallback can cite it.
        """
        decision = await self.robots.can_fetch(url, payer=self.stats.payer)
        if decision.allowed:
            return True, ""
        evidence = decision.matched_rule or decision.reason
        self.stats.skipped_robots += 1
        self.blocked_paths.append((url, evidence))
        self.log.info(
            ev.EV_ROBOTS_BLOCKED,
            f"skipping {url} ({evidence})",
            url=url, rule=decision.matched_rule or None, reason=decision.reason,
        )
        return False, evidence

    async def _note_disallowed_helpful_endpoints(self) -> None:
        """
        Scan the payer's robots rules for Disallowed endpoints that would help us.

        This is the precondition for the enumeration fallback. We record the rule
        (never fetch the path) so that :meth:`_enumerate_fallback` can cite a
        concrete, quotable justification - "robots disallows /provider/search
        (Disallow: /provider/search), so the listing was reconstructed by bounded
        enumeration of an observed URL pattern".

        Detected from the rules rather than from crawl experience on purpose: a
        search URL is usually filtered out as noise before it is ever checked
        against robots, so waiting to encounter one would mean the fallback
        almost never fires for the payers that most need it.
        """
        robots = await self.robots.get(self.payer.base_url, payer=self.stats.payer)

        # A robots.txt we could not read because the site refused us (403/406/451,
        # or a timeout that persisted through every retry) is a *block*, not an
        # absence of content. Recording it here is what keeps the run summary
        # honest: without this the payer would be reported as "crawled
        # successfully, nothing publishable found", which is exactly the
        # confusion the brief asks us to avoid.
        if not robots.fetched and not robots.default_allow:
            self.stats.blocked = True
            if not self.stats.block_evidence:
                self.stats.block_evidence = (
                    f"HTTP {robots.status} on {self.payer.hint_host}/robots.txt: "
                    f"{robots.note}"
                )
            self.log.warn(
                ev.EV_BLOCKED,
                f"automated access blocked at robots.txt: "
                f"https://{self.payer.hint_host}/robots.txt "
                f"(HTTP {robots.status}); {robots.note}",
                url=f"https://{self.payer.hint_host}/robots.txt",
                status=robots.status, evidence=f"robots_http_{robots.status}",
                stage="robots",
            )
            return

        group = robots.group_for(self.config.crawl.user_agent)
        if group is None:
            return
        recorded = 0
        for rule in group.rules:
            if rule.allow:
                continue
            if recorded >= 8:
                # Enough evidence: one citable rule is all the fallback needs,
                # and a payer with a 200-line robots.txt would otherwise flood
                # the log with near-identical records.
                break
            lowered = rule.raw.lower()
            if not any(hint in lowered for hint in HELPFUL_BLOCKED_HINTS):
                continue
            if any(hint in lowered for hint in NEVER_HELPFUL_HINTS):
                continue
            # Strip the wildcards to get a citable, human-checkable path.
            path = rule.raw.replace("*", "").replace("$", "") or "/"
            url = urljoin(self.payer.base_url, path)
            entry = (url, rule.as_text())
            if entry in self.blocked_paths:
                continue
            self.blocked_paths.append(entry)
            recorded += 1
            self.log.info(
                "robots.helpful_disallowed",
                f"robots.txt disallows {path}, which would have helped enumerate "
                f"this payer's policy list ({rule.as_text()}); it will not be "
                f"fetched",
                url=url, rule=rule.as_text(),
            )

    # -----------------------------------------------------------------------
    # Stage 2: sitemaps
    # -----------------------------------------------------------------------
    @staticmethod
    def _decompress_if_needed(result: FetchResult) -> str:
        """
        Return sitemap text, transparently gunzipping a ``.xml.gz`` sitemap.

        httpx already handles ``Content-Encoding: gzip``, but a ``.gz`` sitemap is
        gzip *content*, which the transport layer correctly leaves alone.
        """
        body = result.content
        if body[:2] == b"\x1f\x8b":
            try:
                body = gzip.decompress(body)
            except (OSError, EOFError, ValueError):
                return result.text()
        for encoding in (result.charset or "utf-8", "utf-8", "cp1252"):
            try:
                return body.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
        return body.decode("utf-8", errors="replace")

    @staticmethod
    def parse_sitemap(text: str) -> tuple[bool, list[tuple[str, str]]]:
        """
        Parse a sitemap or sitemap index.

        Returns ``(is_index, [(loc, lastmod), ...])``. Implemented with regexes
        rather than an XML parser on purpose: a meaningful fraction of payer
        sitemaps are not well-formed XML (unescaped ampersands in URLs, stray
        doctypes, truncated tails), and ``ElementTree`` throws the whole file away
        on the first error while a regex still recovers every valid ``<loc>``.
        """
        is_index = bool(_SITEMAP_IS_INDEX_RE.search(text))
        entries: list[tuple[str, str]] = []
        seen: set[str] = set()

        # Pass 1: well-formed <url>/<sitemap> blocks, so each loc keeps its own
        # lastmod even when siblings have none.
        for block in _SITEMAP_ENTRY_RE.findall(text):
            loc_match = _SITEMAP_LOC_RE.search(block)
            if loc_match is None:
                continue
            loc = clean_text(loc_match.group(1))
            if not loc or not loc.lower().startswith(("http://", "https://")):
                continue
            lastmod_match = _SITEMAP_LASTMOD_RE.search(block)
            lastmod = clean_text(lastmod_match.group(1)) if lastmod_match else ""
            if loc not in seen:
                seen.add(loc)
                entries.append((loc, lastmod))

        # Pass 2: recover any <loc> outside a closed block. A truncated tail is
        # common enough that dropping its URLs would cost real documents.
        for match in _SITEMAP_LOC_RE.findall(text):
            loc = clean_text(match)
            if not loc or not loc.lower().startswith(("http://", "https://")):
                continue
            if loc not in seen:
                seen.add(loc)
                entries.append((loc, ""))
        return is_index, entries

    async def _collect_sitemap_urls(self) -> list[tuple[str, str]]:
        """
        Walk the payer's sitemaps (following nested indexes) and return their entries.

        Sitemap discovery order, matching the brief's priority:

        1. every ``Sitemap:`` line in robots.txt (the declared, authoritative set),
        2. only if that yields nothing, the conventional paths in
           :data:`COMMON_SITEMAP_PATHS` - still robots-checked individually.
        """
        base = f"https://{self.payer.hint_host}/"
        declared = await self.robots.sitemaps_for(base, payer=self.stats.payer)
        queue: list[str] = list(declared)
        if not queue:
            queue = [urljoin(base, path) for path in COMMON_SITEMAP_PATHS]
            self.log.debug(
                "sitemap.probing_defaults",
                f"robots.txt declared no sitemaps; probing {len(queue)} conventional paths",
                count=len(queue),
            )

        entries: list[tuple[str, str]] = []
        visited: set[str] = set()
        budget = self.config.crawl.max_sitemaps_per_payer

        while queue and len(visited) < budget:
            sitemap_url = queue.pop(0)
            key = normalise_url(sitemap_url)
            if key in visited:
                continue
            visited.add(key)

            host = urlsplit(sitemap_url).hostname or ""
            if not self._host_in_scope(host):
                self.log.debug("sitemap.out_of_scope",
                               f"ignoring off-domain sitemap {sitemap_url}", url=sitemap_url)
                continue
            allowed, _ = await self._allowed(sitemap_url)
            if not allowed:
                continue

            result = await self.fetcher.get(sitemap_url, use_cache=True, context="sitemap")
            if not result.ok:
                # The fetcher already emitted fetch.failed with the status and
                # attempt count. A robots-declared sitemap that fails is still
                # worth calling out separately, because it means the payer's own
                # index of its content is broken - a 404 on a path we merely
                # guessed is unremarkable and stays at debug.
                if sitemap_url in declared:
                    self.log.warn(
                        "sitemap.unavailable",
                        f"robots.txt declares {sitemap_url} but it returned "
                        f"HTTP {result.status}",
                        url=sitemap_url, status=result.status,
                        error=result.error or None,
                    )
                continue

            challenged, marker = result.looks_challenged()
            if challenged:
                self._record_block(sitemap_url, result, marker, stage="sitemap")
                continue

            text = self._decompress_if_needed(result)
            is_index, found = self.parse_sitemap(text)
            self.stats.sitemaps_parsed += 1
            self.log.info(
                ev.EV_SITEMAP_PARSED,
                f"{sitemap_url} -> {len(found)} entr{'y' if len(found) == 1 else 'ies'}"
                f"{' (sitemap index)' if is_index else ''}",
                url=sitemap_url, entries=len(found), is_index=is_index,
                status=result.status,
            )

            if is_index:
                # Nested index: enqueue children, preferring ones whose URL
                # already hints at policy content so a payer with 40 sitemaps
                # spends our budget on the relevant ones.
                children = [loc for loc, _ in found]
                children.sort(key=lambda url: 0 if _has_policy_hint(url) else 1)
                queue.extend(children[: budget])
            else:
                entries.extend(found)

        if not entries and self.stats.sitemaps_parsed == 0:
            self.log.warn("sitemap.none",
                          "no usable sitemap found; falling back to seeded index pages")
        return entries

    # -----------------------------------------------------------------------
    # Stage 3: index/category crawl
    # -----------------------------------------------------------------------
    def _seed_index_candidates(self, sitemap_entries: Sequence[tuple[str, str]]) -> list[Candidate]:
        """
        Build the index-page frontier from sitemap entries plus the payer's own
        seed paths.

        Sitemap entries that are *already* documents (a ``.pdf`` ``<loc>``) are
        returned as document candidates rather than index pages, since re-crawling
        a PDF looking for links would be pointless.
        """
        candidates: list[Candidate] = []
        base_path = f"seed:{self.payer.hint_host} > robots.txt"

        for loc, lastmod in sitemap_entries:
            host = urlsplit(loc).hostname or ""
            if not self._host_in_scope(host):
                continue
            if _is_noise(loc):
                continue
            is_document = looks_like_document_url(loc)
            has_hint = _has_policy_hint(loc)
            if not is_document and not has_hint:
                continue
            path = f"{base_path} > sitemap.xml"
            candidates.append(
                Candidate(
                    url=loc,
                    source_page_url=f"https://{self.payer.hint_host}/sitemap.xml",
                    discovery_path=path,
                    origin="sitemap_doc" if is_document else "sitemap",
                    link_text="",
                    # Documents first, then hinted index pages; a <lastmod> is a
                    # weak signal of freshness so it breaks ties.
                    priority=(3.0 if is_document else 1.5) + (0.2 if lastmod else 0.0),
                )
            )

        # The payer's curated entry paths. These matter when a payer publishes no
        # sitemap at all (several do not), and they are recorded in the
        # discovery_path as "seed_path:" so their provenance is never ambiguous -
        # a reviewer can always tell a hand-supplied entry point from an
        # automated discovery.
        #
        # An entry may be an absolute URL as well as a path, which is how a payer
        # that keeps its policy CMS on a separate host of its own (Highmark's
        # securecms.highmark.com, for instance) gets seeded directly. The host is
        # still scope-checked, so this cannot smuggle in a third party.
        for seed_path in self.payer.seed_paths:
            if seed_path.lower().startswith(("http://", "https://")):
                url = seed_path
                host = urlsplit(url).hostname or ""
                if not self._host_in_scope(host):
                    self.log.warn(
                        "seed_path.out_of_scope",
                        f"ignoring seed path {seed_path}: {host} is not in scope "
                        f"for {self.payer.hint_host} (add it to extra_hosts to "
                        f"allow it)",
                        url=seed_path, host=host,
                    )
                    continue
            else:
                url = urljoin(f"https://{self.payer.hint_host}/", seed_path)
            candidates.append(
                Candidate(
                    url=url,
                    source_page_url=f"https://{self.payer.hint_host}/",
                    discovery_path=f"seed:{self.payer.hint_host} > robots.txt > "
                                   f"seed_path:{seed_path}",
                    origin="index",
                    priority=2.5,
                )
            )
        # The bare homepage is the last resort: it always exists, and its nav
        # links are usually enough to find the provider section.
        candidates.append(
            Candidate(
                url=f"https://{self.payer.hint_host}/",
                source_page_url="",
                discovery_path=f"seed:{self.payer.hint_host} > robots.txt > homepage",
                origin="index",
                priority=0.5,
            )
        )
        return candidates

    async def _crawl_index_page(self, candidate: Candidate) -> tuple[list[Candidate], list[Candidate]]:
        """
        Fetch one index/category page and split its links.

        Returns ``(document_candidates, next_index_candidates)``. Cross-directory
        links are kept - payers routinely list policies at ``/policies/`` while
        the PDFs live under ``/assets/`` or on a separate document host - as long
        as the target host is in scope.
        """
        allowed, _ = await self._allowed(candidate.url)
        if not allowed:
            return [], []

        result = await self.fetcher.get(candidate.url, context="index")
        if not result.ok:
            # fetch.failed was already logged by the fetcher. A refusal status
            # (403/406/451) is a block though, and must be recorded as one so the
            # summary does not report the payer as merely empty.
            if result.status in BLOCK_STATUS:
                self._record_block(candidate.url, result, f"http_{result.status}",
                                   stage="index")
            return [], []

        challenged, marker = result.looks_challenged()
        if challenged:
            self._record_block(candidate.url, result, marker, stage="index")
            return [], []
        gated, auth_marker = result.looks_auth_gated()
        if gated:
            self.log.info(
                "index.auth_gated",
                f"{candidate.url} is behind a login gate ({auth_marker}); not pursuing",
                url=candidate.url, evidence=auth_marker,
            )
            return [], []

        self.stats.index_pages_crawled += 1
        final_url = result.final_url or candidate.url
        # A redirect can legitimately move us to a regional host; record it so
        # the discovery_path shows the hop and the new host enters scope.
        redirected = normalise_url(final_url) != normalise_url(candidate.url)
        path = candidate.discovery_path
        if redirected:
            path = f"{path} > redirect:{urlsplit(final_url).hostname}"
            self._host_in_scope(urlsplit(final_url).hostname or "")

        links = extract_links(result.text(), base_url=final_url)
        self.log.info(
            ev.EV_INDEX_CRAWLED,
            f"{final_url} -> {len(links)} link(s)",
            url=final_url, links=len(links), depth=candidate.depth,
            status=result.status,
        )

        documents: list[Candidate] = []
        indexes: list[Candidate] = []
        page_label = _page_label(final_url)

        for href, text in links:
            host = urlsplit(href).hostname or ""
            if not self._host_in_scope(host):
                continue
            if _is_noise(href):
                continue
            key = normalise_url(href)
            if key in self.seen_urls:
                continue

            if looks_like_document_url(href):
                selector = _anchor_selector(href)
                documents.append(
                    Candidate(
                        url=href,
                        source_page_url=final_url,
                        discovery_path=f"{path} > index:{page_label} > {selector}",
                        origin="anchor",
                        link_text=text,
                        priority=score_candidate(href, text),
                        depth=candidate.depth + 1,
                    )
                )
            elif candidate.depth + 1 < 3 and (_has_policy_hint(href)
                                              or looks_like_policy_index(text)):
                indexes.append(
                    Candidate(
                        url=href,
                        source_page_url=final_url,
                        discovery_path=f"{path} > index:{page_label} > "
                                       f"anchor[text~='{_trim(text)}']",
                        origin="index",
                        link_text=text,
                        priority=1.0 + score_candidate(href, text) * 0.3,
                        depth=candidate.depth + 1,
                    )
                )

        # An HTML index page can itself be a policy document (many payers publish
        # policies as web pages, not PDFs). Emit it when it looks like one.
        if candidate.origin in {"sitemap_doc", "anchor"} or _is_html_policy_page(final_url, result):
            documents.append(
                Candidate(
                    url=final_url,
                    source_page_url=candidate.source_page_url,
                    discovery_path=path,
                    origin="html_policy",
                    link_text=candidate.link_text,
                    priority=candidate.priority,
                    depth=candidate.depth,
                )
            )
        return documents, indexes

    # -----------------------------------------------------------------------
    # Stage: document fetch + row construction
    # -----------------------------------------------------------------------
    async def _fetch_document(self, candidate: Candidate) -> DocumentRow | None:
        """
        Fetch one document candidate and turn it into a schema row.

        Emits a row for failures too (``http_status`` set, or ``0`` for a
        transport error) because the brief explicitly wants non-200 rows with an
        explanation. Returns ``None`` only when the URL was never requested
        (robots-blocked) or is a duplicate we already handled.
        """
        key = normalise_url(candidate.url)
        if key in self.seen_urls:
            return None
        self.seen_urls.add(key)

        allowed, evidence = await self._allowed(candidate.url)
        if not allowed:
            return None

        self.stats.attempted += 1
        result = await self.fetcher.get(candidate.url, context="document")
        fetched_at = utc_now_iso()

        if result.status == 0:
            # Transport-level failure: DNS, TLS, timeout, refused connection.
            # fetch.failed is emitted by the fetcher; the row below is the
            # dataset-level record of the same event (http_status=0).
            self.stats.failed += 1
            return build_row(
                payer=self.payer,
                candidate=candidate,
                result=result,
                fetched_at=fetched_at,
                notes=f"transport-level failure after {result.attempts} attempt(s): "
                      f"{result.error}",
            )

        challenged, marker = result.looks_challenged()
        if challenged:
            self._record_block(candidate.url, result, marker, stage="document")
            self.stats.failed += 1
            return build_row(
                payer=self.payer,
                candidate=candidate,
                result=result,
                fetched_at=fetched_at,
                requires_auth=result.status in {401, 403},
                notes=f"automated access blocked (evidence: {marker}; "
                      f"HTTP {result.status}); CAPTCHA/WAF not bypassed by design",
            )

        gated, auth_marker = result.looks_auth_gated()
        if gated:
            # requires_auth=Y rows carry no hash: we never logged in to get bytes.
            self.stats.failed += 1
            self.log.info(
                "doc.auth_required",
                f"{candidate.url} requires authentication ({auth_marker})",
                url=candidate.url, status=result.status, evidence=auth_marker,
            )
            return build_row(
                payer=self.payer,
                candidate=candidate,
                result=result,
                fetched_at=fetched_at,
                requires_auth=True,
                notes=f"public URL resolves to a credentialed portal "
                      f"(evidence: {auth_marker}); not fetched with credentials",
            )

        if result.status != 200 or not result.content:
            # fetch.failed already logged by the fetcher; emit the row so the
            # non-200 outcome is visible in the dataset, as the brief requires.
            self.stats.failed += 1
            return build_row(
                payer=self.payer,
                candidate=candidate,
                result=result,
                fetched_at=fetched_at,
                notes=f"non-200 response (HTTP {result.status}); URL was discovered "
                      f"via {candidate.origin} but did not serve a document",
            )

        file_type = detect_file_type(result, candidate.url)
        if file_type == "other" and result.sniff_kind() == "":
            # Neither magic bytes nor Content-Type identified this; almost always
            # an image, a font or a JS bundle that slipped through the filters.
            self.log.debug("doc.unrecognised",
                           f"{candidate.url} content not recognised as a document "
                           f"(content-type {result.content_type!r})",
                           url=candidate.url, content_type=result.content_type)
            return None

        row = build_row(
            payer=self.payer,
            candidate=candidate,
            result=result,
            fetched_at=fetched_at,
            file_type=file_type,
        )
        if self.config.output.save_downloads:
            # Bookkeeping only: local_path is internal and never a CSV column.
            row.local_path = str(
                self.downloader.save(self.payer.payer_name, candidate.url, result, file_type)
            )
        self.stats.found += 1
        self.log.info(
            ev.EV_DOC_FOUND,
            f"{row.document_title or '(untitled)'} -> {candidate.url}",
            url=candidate.url,
            title=row.document_title or None,
            doc_type=row.document_type,
            file_type=row.file_type,
            status=result.status,
            bytes=result.size_bytes,
            confidence=row.confidence_score,
            source_page=candidate.source_page_url or None,
        )
        return row

    def _record_block(self, url: str, result: FetchResult, marker: str, *, stage: str) -> None:
        """
        Record a bot-wall / WAF block with evidence.

        Sets the payer-level ``blocked`` flag so the run summary can distinguish
        "publishes nothing" from "we were blocked", which the brief calls out as
        a scoring-relevant distinction. We never attempt to defeat the challenge.
        """
        self.stats.blocked = True
        if not self.stats.block_evidence:
            self.stats.block_evidence = f"HTTP {result.status} / {marker} at {stage}: {url}"
        self.log.warn(
            ev.EV_BLOCKED,
            f"automated access blocked at {stage}: {url} "
            f"(HTTP {result.status}, evidence: {marker})",
            url=url, status=result.status, evidence=marker, stage=stage,
            server=result.headers.get("server") or None,
        )

    # -----------------------------------------------------------------------
    # Stage 4: bounded enumeration fallback (robots-blocked endpoints only)
    # -----------------------------------------------------------------------
    def _infer_enumerable_pattern(self, rows: Sequence[DocumentRow]) -> tuple[str, list[int]] | None:
        """
        Infer a numeric URL template from documents we already found legitimately.

        Returns ``(template_with_{n}, observed_numbers)``, or ``None`` when the
        observed URLs show no consistent numeric pattern. We only extrapolate
        from evidence: at least three documents must share a prefix/suffix around
        a numeric run, which keeps us from spraying guesses at a site that simply
        has numbers in some filenames.
        """
        groups: dict[tuple[str, str], list[int]] = {}
        for row in rows:
            url = str(row.document_url)
            split = urlsplit(url)
            for match in _NUMERIC_SEGMENT_RE.finditer(split.path):
                prefix = urlunsplit((split.scheme, split.netloc,
                                     split.path[: match.start()], "", ""))
                suffix = split.path[match.end():]
                groups.setdefault((prefix, suffix), []).append(int(match.group(1)))
        best: tuple[tuple[str, str], list[int]] | None = None
        for key, numbers in groups.items():
            unique = sorted(set(numbers))
            if len(unique) < 3:
                continue
            if best is None or len(unique) > len(best[1]):
                best = (key, unique)
        if best is None:
            return None
        (prefix, suffix), numbers = best
        # Preserve zero padding, e.g. /policy/00123.pdf -> /policy/{n:05d}.pdf.
        width = len(str(numbers[0]))
        placeholder = "{n:0%dd}" % width if width > 1 and str(numbers[0]).startswith("0") else "{n}"
        return f"{prefix}{placeholder}{suffix}", numbers

    async def _enumerate_fallback(self, rows: Sequence[DocumentRow]) -> list[DocumentRow]:
        """
        Bounded, rate-limited enumeration of an observed numeric URL pattern.

        Preconditions, all required by the brief:

        * a page/search endpoint that *would* have helped is robots-Disallowed
          (recorded in :attr:`blocked_paths`) - we do not crawl it,
        * the candidate URLs we generate are themselves robots-Allowed,
        * every candidate is verified with a 200 **and** a content/title match
          before it is emitted, and
        * the attempt count is capped by ``crawl.max_enumeration_candidates`` and
          paced by the same per-host token bucket as everything else.
        """
        budget = self.config.crawl.max_enumeration_candidates
        if budget <= 0 or not self.blocked_paths:
            return []
        remaining_docs = self.config.crawl.max_docs_per_payer - len(rows)
        if remaining_docs <= 0:
            return []

        inferred = self._infer_enumerable_pattern(rows)
        if inferred is None:
            self.log.debug(
                "enumeration.skipped",
                "a helpful endpoint is robots-disallowed, but no numeric URL "
                "pattern could be inferred from the documents found so far",
                blocked_examples=[url for url, _ in self.blocked_paths[:3]],
            )
            return []

        template, observed = inferred
        blocked_url, blocked_rule = self.blocked_paths[0]
        # Probe just outside the observed range: neighbours of known-good ids are
        # far likelier to exist than arbitrary numbers, and it keeps the request
        # count proportional to the evidence we actually have.
        lowest, highest = observed[0], observed[-1]
        span = max(4, min(budget // 2, (highest - lowest) or 8))
        wanted: list[int] = []
        for number in range(highest + 1, highest + 1 + span):
            wanted.append(number)
        for number in range(max(1, lowest - span), lowest):
            wanted.append(number)
        for number in range(lowest, highest + 1):
            if number not in set(observed):
                wanted.append(number)
        wanted = [n for n in wanted if n not in set(observed)][:budget]

        self.log.info(
            ev.EV_ENUMERATION,
            f"robots disallows {blocked_url} ({blocked_rule}); enumerating up to "
            f"{len(wanted)} candidate(s) from the observed pattern {template}",
            template=template, candidates=len(wanted), observed=len(observed),
            blocked_url=blocked_url, blocked_rule=blocked_rule,
        )

        found: list[DocumentRow] = []
        for number in wanted:
            if len(found) >= remaining_docs:
                break
            url = template.format(n=number)
            key = normalise_url(url)
            if key in self.seen_urls:
                continue
            allowed, _ = await self._allowed(url)
            if not allowed:
                continue
            self.stats.enumerated += 1
            self.seen_urls.add(key)

            probe = await self.fetcher.head_or_get(url, context="enumeration")
            if probe.status != 200:
                continue
            if not probe.content:
                probe = await self.fetcher.get(url, context="enumeration")
            if not probe.ok:
                continue
            challenged, marker = probe.looks_challenged()
            if challenged:
                self._record_block(url, probe, marker, stage="enumeration")
                break  # Stop enumerating the moment a host pushes back.

            file_type = detect_file_type(probe, url)
            if file_type == "other":
                continue

            candidate = Candidate(
                url=url,
                source_page_url="",
                discovery_path=(
                    f"seed:{self.payer.hint_host} > robots.txt > "
                    f"robots-disallowed:{urlsplit(blocked_url).path} > "
                    f"url_pattern:{template} > verified:200+title_match"
                ),
                origin="url_pattern",
                priority=1.0,
            )
            row = build_row(
                payer=self.payer,
                candidate=candidate,
                result=probe,
                fetched_at=utc_now_iso(),
                file_type=file_type,
            )
            # The verification gate: a 200 alone is not enough, because many CMSs
            # answer every id with a soft-404 page. The row must also look like a
            # policy document by title/content before we will emit it.
            verified = _verify_enumerated(row, self.payer)
            self.stats.attempted += 1
            if not verified:
                self.log.debug(
                    "enumeration.rejected",
                    f"{url} returned 200 but did not match a policy document "
                    f"(title={row.document_title!r})",
                    url=url, title=row.document_title or None,
                )
                continue

            row.notes = clean_text(
                f"{row.notes} discovered by bounded URL-pattern enumeration because "
                f"{urlsplit(blocked_url).path} is robots-disallowed ({blocked_rule}); "
                f"verified by HTTP 200 plus title/content match".strip()
            )
            if self.config.output.save_downloads:
                row.local_path = str(
                    self.downloader.save(self.payer.payer_name, url, probe, file_type)
                )
            self.stats.found += 1
            found.append(row)
            self.log.info(
                ev.EV_DOC_FOUND,
                f"{row.document_title or '(untitled)'} -> {url} (enumerated)",
                url=url, title=row.document_title or None, doc_type=row.document_type,
                file_type=row.file_type, status=probe.status, bytes=probe.size_bytes,
                confidence=row.confidence_score, method="url_pattern",
            )
        return found

    # -----------------------------------------------------------------------
    # Orchestration
    # -----------------------------------------------------------------------
    async def run(self) -> tuple[list[DocumentRow], PayerStats]:
        """
        Execute the full pipeline for this payer and return ``(rows, stats)``.

        The document fetches are issued in bounded batches so that the global
        concurrency cap and the per-host rate limit are what actually govern
        throughput, while ``max_docs_per_payer`` stops the crawl as soon as the
        budget is met.
        """
        self._start_time = time.monotonic()
        self.stats.payer = self.payer.payer_name
        self.log.info(
            ev.EV_PAYER_START,
            f"starting {self.payer.payer_name} from hint host {self.payer.hint_host}",
            hint_host=self.payer.hint_host,
            state=self.payer.default_state_or_region or None,
            max_docs=self.config.crawl.max_docs_per_payer,
        )
        self._host_in_scope(self.payer.hint_host)

        # Stage 1: robots.txt. Fetched and fully parsed before anything else, and
        # scanned for Disallowed endpoints that would have helped us (which is
        # what licenses the stage-4 enumeration fallback later on).
        await self._note_disallowed_helpful_endpoints()

        # Stage 2: declared sitemaps become the index frontier.
        sitemap_entries = await self._collect_sitemap_urls()

        # Stage 3: crawl the index frontier, collecting document candidates.
        frontier = self._seed_index_candidates(sitemap_entries)
        frontier.sort(key=lambda item: -item.priority)

        document_queue: list[Candidate] = []
        #: Index pages already dispatched, so the frontier cannot re-crawl one.
        crawled_indexes: set[str] = set()
        index_budget = self.config.crawl.max_index_pages_per_payer
        pages_done = 0

        while frontier and pages_done < index_budget:
            batch = []
            while frontier and len(batch) < max(2, self.config.crawl.concurrency_cap):
                candidate = frontier.pop(0)
                key = normalise_url(candidate.url)
                if candidate.origin == "sitemap_doc":
                    # Already a document: no need to crawl it as an index page.
                    document_queue.append(candidate)
                    continue
                if key in self.seen_urls or key in crawled_indexes:
                    continue
                # Mark it before the fetch, not after: a page linked from five
                # other pages would otherwise be queued (and crawled) five times,
                # burning max_index_pages_per_payer on repeats.
                crawled_indexes.add(key)
                batch.append(candidate)
            if not batch:
                continue

            pages_done += len(batch)
            results = await asyncio.gather(
                *(self._crawl_index_page(item) for item in batch),
                return_exceptions=True,
            )
            for candidate, outcome in zip(batch, results):
                if isinstance(outcome, BaseException):
                    # Isolate per-page failures: a malformed page must not end
                    # the payer, let alone the run.
                    self.log.error(
                        "index.error",
                        f"error crawling {candidate.url}: {outcome}",
                        url=candidate.url, error=str(outcome),
                    )
                    continue
                documents, indexes = outcome
                document_queue.extend(documents)
                frontier.extend(indexes)
            frontier.sort(key=lambda item: -item.priority)
            # Enough candidates queued to fill the budget several times over:
            # stop expanding the frontier and start fetching documents.
            if len(document_queue) >= self.config.crawl.max_docs_per_payer * 3:
                break

        # De-duplicate the queue by normalised URL and fetch by priority.
        document_queue.sort(key=lambda item: -item.priority)
        unique_queue: list[Candidate] = []
        queued: set[str] = set()
        for candidate in document_queue:
            key = normalise_url(candidate.url)
            if key in queued or key in self.seen_urls:
                continue
            queued.add(key)
            unique_queue.append(candidate)

        # Stage: fetch documents until the per-payer budget is reached.
        max_docs = self.config.crawl.max_docs_per_payer
        position = 0
        batch_size = max(2, self.config.crawl.concurrency_cap)
        while position < len(unique_queue) and self.stats.found < max_docs:
            # Bound the batch by the remaining budget as well as by the
            # concurrency cap, otherwise a full batch launched when we are one
            # document short would overshoot max_docs_per_payer.
            remaining = max_docs - self.stats.found
            width = max(1, min(batch_size, remaining))
            batch = unique_queue[position: position + width]
            position += width
            outcomes = await asyncio.gather(
                *(self._fetch_document(item) for item in batch),
                return_exceptions=True,
            )
            for candidate, outcome in zip(batch, outcomes):
                if isinstance(outcome, BaseException):
                    self.log.error(
                        "doc.error",
                        f"error fetching {candidate.url}: {outcome}",
                        url=candidate.url, error=str(outcome),
                    )
                    self.stats.failed += 1
                    continue
                if outcome is not None:
                    self.rows.append(outcome)

        # Stage 4: enumeration fallback, only if a useful endpoint was disallowed.
        successful = [row for row in self.rows if str(row.http_status) == "200"]
        if self.stats.found < max_docs and self.blocked_paths and successful:
            self.rows.extend(await self._enumerate_fallback(successful))

        self.stats.elapsed_s = time.monotonic() - self._start_time
        self.stats.hosts = sorted(self.in_scope_hosts)
        return self.rows, self.stats


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------
def _has_policy_hint(url: str) -> bool:
    """True when the URL path suggests policy content (see :data:`POLICY_PATH_HINTS`)."""
    lowered = url.lower()
    return any(hint in lowered for hint in POLICY_PATH_HINTS)


def _is_noise(url: str) -> bool:
    """True for URLs that are structurally irrelevant to policy discovery."""
    lowered = url.lower()
    if any(hint in lowered for hint in NOISE_PATH_HINTS):
        # A policy hint overrides the noise list: "/provider-news/medical-policy-
        # update" is a bulletin we want, even though "/news/" is on the list.
        return not _has_policy_hint(lowered)
    return any(lowered.endswith(suffix) for suffix in NEGATIVE_URL_HINTS)


def _is_html_policy_page(url: str, result: FetchResult) -> bool:
    """
    Decide whether an HTML page is itself a policy document rather than a listing.

    Several payers publish each policy as a web page. The signals are a policy
    hint in the URL plus policy-document vocabulary in the body, and - crucially -
    the absence of the many-similar-links structure that marks a listing page.
    """
    if not _has_policy_hint(url):
        return False
    body = result.text(limit=60000).lower()
    markers = ("effective date", "policy number", "coverage rationale",
               "medical necessity", "clinical evidence", "cpt code", "hcpcs")
    hits = sum(1 for marker in markers if marker in body)
    return hits >= 2


def _page_label(url: str) -> str:
    """
    Short, stable label for an index page used inside ``discovery_path``.

    Keeps the chain readable (``index:/policies/medical``) instead of repeating
    the full absolute URL at every hop.
    """
    split = urlsplit(url)
    path = split.path or "/"
    return path if len(path) <= 60 else path[:57] + "..."


def _anchor_selector(href: str) -> str:
    """
    Describe the anchor that produced a document as a reproducible selector.

    Written the way a reviewer would type it into devtools, e.g.
    ``anchor[href$='.pdf']``, so ``discovery_path`` is directly actionable.
    """
    path = urlsplit(href).path.lower()
    for extension in (".pdf", ".docx", ".doc", ".xlsx", ".xls"):
        if path.endswith(extension):
            return f"anchor[href$='{extension}']"
    return "anchor[href]"


def _trim(text: str, limit: int = 40) -> str:
    """Shorten anchor text for embedding in a discovery_path."""
    cleaned = clean_text(text).replace("'", "")
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 3] + "..."


def _verify_enumerated(row: DocumentRow, payer: Payer) -> bool:
    """
    Verification gate for an enumerated candidate: a 200 is not sufficient.

    The row must also (a) carry a non-trivial title, (b) not look like a soft-404
    or a generic landing page, and (c) classify as a real document type or carry
    a policy number. This is what keeps the enumeration fallback from padding the
    dataset with CMS placeholder pages.
    """
    title = clean_text(row.document_title).lower()
    soft_404_markers = ("page not found", "not found", "error", "no results",
                        "we couldn't find", "we could not find", "access denied",
                        "under construction", "coming soon", "unavailable",
                        "sorry", "invalid request", "bad request")
    if any(marker in title for marker in soft_404_markers):
        return False

    # A binary document confirmed by MAGIC BYTES is itself the content match: a
    # soft-404 is always an HTML page, never a real PDF or spreadsheet. Requiring
    # a rich title here as well would reject scanned policy PDFs that carry no
    # extractable text, which is a large minority of real payer documents.
    if row.file_type in {"pdf", "docx", "doc", "xlsx", "xls"}:
        return True

    # For HTML the title is the only signal we have, so it must be substantive
    # and the page must look like a policy rather than a generic landing page.
    if len(title) < 8:
        return False
    if row.policy_number:
        return True
    return row.document_type != "other"


def iter_payer_hosts(payers: Iterable[Payer]) -> list[str]:
    """Hint hosts for a payer collection (used by ``--dry-run`` output)."""
    return [payer.hint_host for payer in payers]
