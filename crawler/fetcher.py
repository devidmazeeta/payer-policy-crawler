"""
Polite, rate-limited, retrying async HTTP client.

Politeness is enforced at two independent levels, because a single global cap is
not enough - eight concurrent workers all aimed at one payer would still be
eight simultaneous requests to that payer:

* a **global semaphore** bounded by ``crawl.concurrency_cap``, limiting total
  in-flight requests for the whole run, and
* a **per-host token bucket** bounded by ``crawl.per_domain_rate_limit_per_sec``
  (further reduced by any robots.txt ``Crawl-delay``), so each host sees a
  steady, slow trickle regardless of how many coroutines want it.

Retries use exponential (or fixed) backoff with jitter and are applied only to
failures that retrying can plausibly fix: timeouts, connection errors, 429 and
5xx. A 403/404 is returned immediately - hammering it would be rude and useless.

The client is deliberately honest about failure: every unrecoverable error comes
back as a :class:`FetchResult` with ``status=0`` and a populated ``error``, which
is exactly what the schema's "http_status=0 means transport-level failure" rule
needs. Nothing here raises for a network problem.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field, replace
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

from .schema import sha256_hex

#: Status codes worth another attempt. 429 and 503 are the polite-backoff pair;
#: 500/502/504 are usually transient at payer scale; 408 is a server-side timeout.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Signals that the response is a bot wall / login gate rather than a document.
#: Used to set requires_auth=Y and to distinguish "publishes nothing" from
#: "we were blocked" in the logs, which the brief calls out specifically.
#: 406 is included because several payer WAFs (Akamai in particular) answer a
#: non-browser TLS fingerprint with "406 Not Acceptable" and an empty body
#: regardless of the Accept header sent - it is a bot block, not content
#: negotiation.
BLOCK_STATUS = frozenset({401, 403, 406, 407, 451})

#: Body markers of interstitial challenge pages that answer with 200. Detecting
#: these lets us record a block with evidence instead of storing the challenge
#: page as if it were a policy document. We never attempt to solve them.
CHALLENGE_MARKERS = (
    "just a moment",
    "attention required! | cloudflare",
    "checking your browser before accessing",
    "enable javascript and cookies to continue",
    "request unsuccessful. incapsula incident id",
    "access denied",
    "you have been blocked",
    "radware",
    "captcha",
    "px-captcha",
    "_incapsula_resource",
    "distil_r_captcha",
    "bot detection",
)

#: Body markers of a login / portal gate: public discovery stops here.
AUTH_MARKERS = (
    "sign in to continue",
    "please sign in",
    "please log in",
    "login required",
    "you must be logged in",
    "session has expired",
    "one healthcare id",
    "register for an account",
)

#: Magic-byte prefixes -> file_type. The schema forbids deciding file_type from
#: the URL suffix alone, and payers routinely serve a PDF from a .aspx URL, so
#: content sniffing is the primary signal and Content-Type the secondary one.
MAGIC_PREFIXES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "pdf"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole"),  # legacy .doc/.xls container
    (b"PK\x03\x04", "zip"),                        # OOXML .docx/.xlsx container
    (b"{\\rtf", "doc"),
)


@dataclass(slots=True)
class FetchResult:
    """
    Outcome of one HTTP fetch, carrying everything the schema needs.

    ``status == 0`` marks a transport-level failure (DNS, TLS, timeout, refused
    connection) with the reason in ``error``; the row emitted from such a result
    keeps ``content_hash_sha256`` empty per the conventions sheet.
    """

    url: str                        # URL as requested
    final_url: str = ""             # URL after redirects (may differ in host!)
    status: int = 0
    headers: Mapping[str, str] = field(default_factory=dict)
    content: bytes = b""
    elapsed_s: float = 0.0
    error: str = ""
    attempts: int = 0
    from_cache: bool = False
    truncated: bool = False         # body exceeded crawl.max_download_bytes

    # -- derived helpers -----------------------------------------------------
    @property
    def ok(self) -> bool:
        """True for a 200 with a non-empty body."""
        return self.status == 200 and bool(self.content)

    @property
    def content_type(self) -> str:
        """Lowercased Content-Type with parameters stripped."""
        raw = str(self.headers.get("content-type", "") or "")
        return raw.split(";")[0].strip().lower()

    @property
    def charset(self) -> str:
        """Charset from Content-Type, or "" when the server did not declare one."""
        raw = str(self.headers.get("content-type", "") or "")
        for part in raw.split(";")[1:]:
            key, _, value = part.strip().partition("=")
            if key.strip().lower() == "charset":
                return value.strip().strip('"').lower()
        return ""

    @property
    def size_bytes(self) -> int:
        """Body size actually received."""
        return len(self.content)

    @property
    def sha256(self) -> str:
        """Hash of the raw bytes; empty unless this is a genuine 200 body."""
        return sha256_hex(self.content) if self.ok else ""

    def text(self, limit: int | None = None) -> str:
        """
        Decode the body to text, tolerating the mislabelled encodings payers ship.

        Tries the declared charset, then UTF-8, then cp1252 (very common in
        older payer HTML), and finally falls back to lossy UTF-8 so a single bad
        byte never costs us an entire index page.
        """
        raw = self.content[:limit] if limit else self.content
        for encoding in (self.charset, "utf-8", "cp1252"):
            if not encoding:
                continue
            try:
                return raw.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", errors="replace")

    def sniff_kind(self) -> str:
        """
        Return a coarse content kind from magic bytes: ``pdf``, ``zip``, ``ole``,
        ``html``, ``xml`` or ``""``. Consulted before Content-Type by
        :func:`detect_file_type`.
        """
        head = self.content[:2048]
        if not head:
            return ""
        for prefix, kind in MAGIC_PREFIXES:
            if head.startswith(prefix):
                return kind
        stripped = head.lstrip()[:600].lower()
        if stripped.startswith(b"<?xml") or stripped.startswith(b"<urlset") \
                or stripped.startswith(b"<sitemapindex"):
            return "xml"
        if b"<html" in stripped or b"<!doctype html" in stripped:
            return "html"
        return ""

    def looks_challenged(self) -> tuple[bool, str]:
        """
        Detect a bot wall / CAPTCHA interstitial served with a 200.

        Returns ``(True, marker)`` on a hit. Callers record the marker as
        evidence in ``notes`` and stop; we never try to defeat the challenge.
        """
        if self.status in BLOCK_STATUS:
            return True, f"http_{self.status}"
        header_hits = " ".join(
            str(self.headers.get(name, ""))
            for name in ("server", "cf-mitigated", "x-iinfo", "x-datadome")
        ).lower()
        if "cf-mitigated" in {key.lower() for key in self.headers}:
            return True, "cloudflare_challenge_header"
        if "captcha" in header_hits:
            return True, "captcha_header"
        if self.sniff_kind() not in {"html", ""}:
            return False, ""
        body = self.text(limit=20000).lower()
        for marker in CHALLENGE_MARKERS:
            if marker in body:
                return True, marker
        return False, ""

    def looks_auth_gated(self) -> tuple[bool, str]:
        """Detect a login/portal gate. Same contract as :meth:`looks_challenged`."""
        if self.status in {401, 407}:
            return True, f"http_{self.status}"
        if self.sniff_kind() not in {"html", ""}:
            return False, ""
        body = self.text(limit=20000).lower()
        for marker in AUTH_MARKERS:
            if marker in body:
                return True, marker
        # A redirect that lands on a login URL is the other common shape.
        lowered = (self.final_url or self.url).lower()
        for fragment in ("/login", "/signin", "/sign-in", "secure.", "auth0", "/idp/",
                         "adfs", "b2clogin", "/account/login"):
            if fragment in lowered:
                return True, f"redirected_to_{fragment.strip('/')}"
        return False, ""


def detect_file_type(result: FetchResult, url: str = "") -> str:
    """
    Decide ``file_type`` from magic bytes first, then Content-Type, then (last
    resort) the URL suffix.

    The schema explicitly forbids trusting the URL suffix alone, and for good
    reason: payers serve PDFs from extension-less CMS routes and HTML error
    pages from ``.pdf`` URLs. Magic bytes cannot lie about either.

    OOXML and legacy OLE containers are ambiguous by nature (a .docx and a .xlsx
    are both ZIPs), so the URL/Content-Type is used only to pick *within* the
    container family - never to override it.
    """
    kind = result.sniff_kind()
    content_type = result.content_type
    suffix = urlsplit(url or result.final_url or result.url).path.rsplit(".", 1)
    extension = suffix[-1].lower() if len(suffix) == 2 and len(suffix[-1]) <= 5 else ""

    if kind == "pdf":
        return "pdf"
    if kind == "zip":
        # Disambiguate the OOXML family using the weaker signals.
        if "spreadsheet" in content_type or extension in {"xlsx", "xlsm"}:
            return "xlsx"
        if "wordprocessing" in content_type or extension in {"docx", "docm"}:
            return "docx"
        return "other"
    if kind == "ole":
        if "excel" in content_type or extension in {"xls", "xlt"}:
            return "xls"
        if "word" in content_type or extension in {"doc", "dot"}:
            return "doc"
        return "other"
    if kind in {"html", "xml"}:
        return "html"

    mapping = {
        "application/pdf": "pdf",
        "application/x-pdf": "pdf",
        "text/html": "html",
        "application/xhtml+xml": "html",
        "text/xml": "html",
        "application/xml": "html",
        "application/msword": "doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "application/vnd.ms-excel": "xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    }
    if content_type in mapping:
        return mapping[content_type]
    if extension in {"pdf", "html", "htm", "doc", "docx", "xls", "xlsx"}:
        return "html" if extension == "htm" else extension
    return "other"


class TokenBucket:
    """
    Async token bucket limiting one host to *rate* requests per second.

    A bucket (rather than a plain sleep) lets a host that has been idle absorb a
    small burst - capped at ``max(1, rate)`` tokens - while still averaging the
    configured rate over time. ``acquire`` holds an ``asyncio.Lock`` so that
    concurrent waiters are serialised and cannot all wake on the same token.
    """

    __slots__ = ("rate", "capacity", "_tokens", "_updated", "_lock")

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        self.rate = max(float(rate), 0.001)
        self.capacity = max(1.0, float(capacity if capacity is not None else self.rate))
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> float:
        """Block until a token is available; return how long we waited, in seconds."""
        waited = 0.0
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._updated) * self.rate
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return waited
                deficit = (1.0 - self._tokens) / self.rate
                waited += deficit
                await asyncio.sleep(deficit)

    def slow_to(self, min_interval_seconds: float) -> None:
        """
        Clamp the bucket to at most one request per *min_interval_seconds*.

        Called when robots.txt declares a ``Crawl-delay`` (or when a 429 arrives
        with a ``Retry-After``): the site's stated preference always wins over
        our configured rate, and only ever downward.
        """
        if min_interval_seconds <= 0:
            return
        requested = 1.0 / min_interval_seconds
        if requested < self.rate:
            self.rate = requested
            self.capacity = 1.0
            self._tokens = min(self._tokens, 1.0)


class Fetcher:
    """
    The run's single HTTP client.

    Owns the connection pool, the global concurrency semaphore and one
    :class:`TokenBucket` per host. Use it as an async context manager so the
    pool is always closed::

        async with Fetcher(config, log) as fetcher:
            result = await fetcher.get(url)
    """

    def __init__(self, config: Any, log: Any) -> None:
        self.config = config
        self.log = log
        crawl = config.crawl
        self._semaphore = asyncio.Semaphore(crawl.concurrency_cap)
        self._buckets: dict[str, TokenBucket] = {}
        self._buckets_lock = asyncio.Lock()
        #: Per-host consecutive hard-failure counter. After several failures in a
        #: row we stop hitting that host: it is either down or blocking us, and
        #: either way more requests add nothing but load.
        self._host_failures: dict[str, int] = {}
        self.host_failure_threshold = 8
        #: Response cache keyed by URL. robots.txt and sitemaps get requested
        #: from several code paths; caching avoids duplicate requests entirely.
        self._cache: dict[str, FetchResult] = {}
        self._client: httpx.AsyncClient | None = None
        #: Run-wide counters surfaced in the run.done log record.
        self.stats: dict[str, int] = {
            "requests": 0, "retries": 0, "failures": 0, "cache_hits": 0, "bytes": 0,
        }

    # -- lifecycle -----------------------------------------------------------
    async def __aenter__(self) -> "Fetcher":
        crawl = self.config.crawl
        limits = httpx.Limits(
            max_connections=crawl.concurrency_cap,
            # At most 2 keep-alive connections per host: we are trickling, not
            # streaming, so a large per-host pool would only invite trouble.
            max_keepalive_connections=max(2, crawl.concurrency_cap // 2),
        )
        timeout = httpx.Timeout(
            crawl.request_timeout_seconds,
            connect=min(15.0, float(crawl.request_timeout_seconds)),
        )
        kwargs: dict[str, Any] = {
            "limits": limits,
            "timeout": timeout,
            "follow_redirects": crawl.follow_redirects,
            "max_redirects": 8,
            "headers": {
                "User-Agent": crawl.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/pdf,"
                          "application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                # Ask for identity where we can: we hash raw bytes, and letting
                # the transport transparently inflate content is fine, but we
                # avoid brotli/zstd surprises across httpx builds.
                "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive",
            },
            # Payer TLS chains are occasionally misconfigured; we still verify.
            "verify": True,
        }
        if crawl.proxy_url:
            # Optional and off by default (the brief forbids mandatory paid
            # proxies). Documented in README.md under "Optional proxy support".
            kwargs["proxy"] = crawl.proxy_url
        self._client = httpx.AsyncClient(**kwargs)
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- rate limiting -------------------------------------------------------
    async def bucket_for(self, host: str) -> TokenBucket:
        """Get (or lazily create) the token bucket for *host*."""
        host = host.lower()
        async with self._buckets_lock:
            bucket = self._buckets.get(host)
            if bucket is None:
                bucket = TokenBucket(self.config.crawl.per_domain_rate_limit_per_sec)
                self._buckets[host] = bucket
            return bucket

    async def apply_crawl_delay(self, host: str, delay_seconds: float) -> None:
        """Honour a robots.txt ``Crawl-delay`` for *host* (only ever slows us down)."""
        bucket = await self.bucket_for(host)
        bucket.slow_to(delay_seconds)

    def host_is_written_off(self, host: str) -> bool:
        """True once *host* has failed enough consecutive times to stop trying."""
        return self._host_failures.get(host.lower(), 0) >= self.host_failure_threshold

    # -- backoff -------------------------------------------------------------
    def backoff_delay(self, attempt: int) -> float:
        """
        Seconds to sleep before retry *attempt* (1-based).

        ``exponential`` -> ``base * 2**(attempt-1)``; ``fixed`` -> ``base``. Both
        get up to 25% positive jitter so that a batch of coroutines that failed
        together does not retry in lockstep, and both are capped at 60s to keep
        a stalled host from stretching the run indefinitely.
        """
        crawl = self.config.crawl
        base = float(crawl.backoff_base_seconds)
        if crawl.backoff_strategy == "fixed":
            delay = base
        else:
            delay = base * (2 ** max(0, attempt - 1))
        delay = min(delay, 60.0)
        return delay * (1.0 + random.random() * 0.25)

    @staticmethod
    def _retry_after_seconds(headers: Mapping[str, str]) -> float:
        """Parse a ``Retry-After`` header (delta-seconds form only), clamped to 120s."""
        raw = str(headers.get("retry-after", "") or "").strip()
        if not raw:
            return 0.0
        try:
            return min(120.0, max(0.0, float(raw)))
        except ValueError:
            return 0.0

    # -- the actual fetch ----------------------------------------------------
    async def get(
        self,
        url: str,
        *,
        method: str = "GET",
        use_cache: bool = False,
        extra_headers: Mapping[str, str] | None = None,
        max_bytes: int | None = None,
        context: str = "",
    ) -> FetchResult:
        """
        Fetch *url*, respecting the global cap, the host's bucket and the retry policy.

        Never raises for a network or HTTP problem: unrecoverable errors come back
        as a :class:`FetchResult` with ``status=0`` and ``error`` set. *context* is
        a free-text label ("robots", "sitemap", "document", ...) that is echoed
        into the ``fetch.failed`` log record so a reviewer can see which stage of
        discovery a failure belongs to.
        """
        if self._client is None:
            raise RuntimeError("Fetcher must be used as an async context manager")

        if use_cache and url in self._cache:
            self.stats["cache_hits"] += 1
            cached = self._cache[url]
            # Copy so a caller mutating the result cannot poison the cache.
            # dataclasses.replace (not __dict__) because FetchResult uses slots.
            return replace(cached, from_cache=True)

        host = (urlsplit(url).hostname or "").lower()
        cap = max_bytes if max_bytes is not None else self.config.crawl.max_download_bytes

        if self.host_is_written_off(host):
            return FetchResult(
                url=url,
                error=f"host {host} written off after "
                      f"{self._host_failures.get(host, 0)} consecutive failures",
            )

        bucket = await self.bucket_for(host)
        attempts = 0
        last_error = ""
        last_status = 0
        last_headers: Mapping[str, str] = {}

        while attempts <= self.config.crawl.retry_limit:
            attempts += 1
            started = time.monotonic()
            try:
                # Order matters: take the cheap per-host token first, then a slot
                # in the global pool, so a rate-limited host does not sit on a
                # concurrency slot while it waits for its turn.
                await bucket.acquire()
                async with self._semaphore:
                    self.stats["requests"] += 1
                    response = await self._client.request(
                        method, url, headers=dict(extra_headers or {})
                    )
                    body = response.content
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPError) as exc:
                # Transport-level problem: no status exists, so this is the
                # http_status=0 case from the conventions sheet.
                last_error = f"{type(exc).__name__}: {exc}".strip()[:400]
                last_status = 0
                if attempts > self.config.crawl.retry_limit:
                    break
                self.stats["retries"] += 1
                delay = self.backoff_delay(attempts)
                self.log.debug(
                    "fetch.retry",
                    f"{url} -> {last_error}; retrying in {delay:.1f}s",
                    url=url, attempt=attempts, delay_s=round(delay, 2), context=context,
                )
                await asyncio.sleep(delay)
                continue
            except Exception as exc:  # pragma: no cover - defensive
                # A bug in parsing a pathological response must not kill the run.
                last_error = f"unexpected {type(exc).__name__}: {exc}".strip()[:400]
                last_status = 0
                break

            elapsed = time.monotonic() - started
            truncated = False
            if len(body) > cap:
                # Oversized document: keep the prefix so we can still identify it,
                # but flag it so the hash is not presented as the whole file.
                body = body[:cap]
                truncated = True

            result = FetchResult(
                url=url,
                final_url=str(response.url),
                status=response.status_code,
                headers={key.lower(): value for key, value in response.headers.items()},
                content=body,
                elapsed_s=round(elapsed, 3),
                attempts=attempts,
                truncated=truncated,
            )
            self.stats["bytes"] += len(body)

            if response.status_code in RETRYABLE_STATUS and attempts <= self.config.crawl.retry_limit:
                self.stats["retries"] += 1
                # A server that tells us how long to wait gets obeyed exactly.
                retry_after = self._retry_after_seconds(result.headers)
                delay = retry_after or self.backoff_delay(attempts)
                if response.status_code == 429:
                    # Persistently slow this host down for the rest of the run,
                    # not just for this retry: 429 means our rate is too high.
                    bucket.slow_to(max(retry_after, 1.0 / max(bucket.rate, 0.001) * 2))
                self.log.debug(
                    "fetch.retry",
                    f"{url} -> HTTP {response.status_code}; retrying in {delay:.1f}s",
                    url=url, attempt=attempts, status=response.status_code,
                    delay_s=round(delay, 2), context=context,
                )
                last_status = response.status_code
                last_error = f"HTTP {response.status_code}"
                last_headers = result.headers
                await asyncio.sleep(delay)
                continue

            # Terminal response (success or a non-retryable error).
            if 200 <= response.status_code < 400:
                self._host_failures[host] = 0
            else:
                self._host_failures[host] = self._host_failures.get(host, 0) + 1
                self.stats["failures"] += 1
                # The fetcher is the single emitter of fetch.failed, so every
                # unsuccessful fetch produces exactly one such record no matter
                # which discovery stage asked for it. 5xx is a server problem
                # worth a warning; a 4xx is normal crawl attrition.
                emit = self.log.warn if response.status_code >= 500 else self.log.info
                emit(
                    "fetch.failed",
                    f"{url} -> HTTP {response.status_code} after "
                    f"{attempts} attempt(s)",
                    url=url, status=response.status_code, attempts=attempts,
                    context=context or None,
                    final_url=result.final_url if result.final_url != url else None,
                )
            if use_cache:
                self._cache[url] = result
            return result

        # Every attempt was exhausted.
        self.stats["failures"] += 1
        self._host_failures[host] = self._host_failures.get(host, 0) + 1
        self.log.warn(
            "fetch.failed",
            f"{url} failed after {attempts} attempt(s): {last_error}",
            url=url, status=last_status, attempts=attempts,
            error=last_error, context=context,
        )
        return FetchResult(
            url=url,
            status=last_status,
            headers=last_headers,
            error=last_error or "exhausted retries",
            attempts=attempts,
        )

    async def head_or_get(self, url: str, *, context: str = "") -> FetchResult:
        """
        Probe *url* cheaply with ``HEAD``, falling back to ``GET``.

        Used by the enumeration fallback, where most candidates do not exist:
        a HEAD costs a fraction of a PDF download. Many payer CMSs answer HEAD
        with 405 or a bare 200 and no useful headers, hence the fallback.
        """
        result = await self.get(url, method="HEAD", context=context)
        if result.status in {405, 501} or (result.status == 200 and not result.content):
            return await self.get(url, context=context)
        return result
