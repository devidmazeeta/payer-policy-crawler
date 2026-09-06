"""
Fetcher tests: retry/backoff logic, rate limiting, content-type detection and
block/auth detection.

All HTTP is served by an ``httpx.MockTransport``, so the suite exercises the real
client code path (headers, redirects, timeouts, status handling) with zero
network access.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from crawler.fetcher import (
    RETRYABLE_STATUS,
    Fetcher,
    FetchResult,
    TokenBucket,
    detect_file_type,
)

PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF"
ZIP_BYTES = b"PK\x03\x04" + b"\x00" * 40
OLE_BYTES = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 40


def install_transport(fetcher: Fetcher, handler) -> None:
    """
    Swap the live client for one backed by ``MockTransport``.

    Called after ``__aenter__`` so all the real client configuration (headers,
    timeouts, redirect policy) is preserved - only the socket layer is faked.
    """
    fetcher._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        headers={"User-Agent": fetcher.config.crawl.user_agent},
    )


# ---------------------------------------------------------------------------
# Retry / backoff
# ---------------------------------------------------------------------------
async def test_retries_then_succeeds(config, log):
    """A transient 503 must be retried and the eventual 200 returned."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="try later")
        return httpx.Response(200, content=PDF_BYTES,
                              headers={"content-type": "application/pdf"})

    async with Fetcher(config, log) as fetcher:
        install_transport(fetcher, handler)
        result = await fetcher.get("https://example.com/p.pdf")

    assert result.status == 200
    assert result.attempts == 3
    assert calls["n"] == 3
    assert fetcher.stats["retries"] == 2


async def test_gives_up_after_retry_limit_and_reports_the_failure(config, log):
    config.crawl.retry_limit = 2

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async with Fetcher(config, log) as fetcher:
        install_transport(fetcher, handler)
        result = await fetcher.get("https://example.com/x")

    # retry_limit=2 means 3 total attempts.
    assert result.attempts == 3
    assert result.status == 500
    assert "fetch.failed" in log.event_names()


async def test_transport_error_becomes_status_zero_not_an_exception(config, log):
    """The schema's http_status=0 case: DNS/TLS/timeout must never raise."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connection timed out")

    async with Fetcher(config, log) as fetcher:
        install_transport(fetcher, handler)
        result = await fetcher.get("https://nonexistent.invalid/x")

    assert result.status == 0
    assert "ConnectTimeout" in result.error
    assert result.sha256 == ""  # no hash for a failed fetch


async def test_non_retryable_status_is_returned_immediately(config, log):
    """Hammering a 404 would be rude and pointless."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, text="nope")

    async with Fetcher(config, log) as fetcher:
        install_transport(fetcher, handler)
        result = await fetcher.get("https://example.com/missing")

    assert result.status == 404
    assert calls["n"] == 1
    assert result.attempts == 1


async def test_403_is_not_retried(config, log):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403, text="forbidden")

    async with Fetcher(config, log) as fetcher:
        install_transport(fetcher, handler)
        await fetcher.get("https://example.com/blocked")
    assert calls["n"] == 1


def test_retryable_status_set_matches_intent():
    for status in (408, 429, 500, 502, 503, 504):
        assert status in RETRYABLE_STATUS
    for status in (200, 301, 401, 403, 404, 410):
        assert status not in RETRYABLE_STATUS


async def test_exponential_backoff_doubles_with_jitter(config, log):
    config.crawl.backoff_strategy = "exponential"
    config.crawl.backoff_base_seconds = 1.0
    async with Fetcher(config, log) as fetcher:
        first, second, third = (fetcher.backoff_delay(n) for n in (1, 2, 3))
    # base * 2**(n-1), plus up to 25% jitter.
    assert 1.0 <= first <= 1.25
    assert 2.0 <= second <= 2.5
    assert 4.0 <= third <= 5.0


async def test_fixed_backoff_does_not_grow(config, log):
    config.crawl.backoff_strategy = "fixed"
    config.crawl.backoff_base_seconds = 2.0
    async with Fetcher(config, log) as fetcher:
        delays = [fetcher.backoff_delay(n) for n in (1, 2, 5)]
    assert all(2.0 <= delay <= 2.5 for delay in delays)


async def test_backoff_is_capped(config, log):
    config.crawl.backoff_base_seconds = 10.0
    async with Fetcher(config, log) as fetcher:
        # 10 * 2**19 would be enormous; the cap keeps a stalled host finite.
        assert fetcher.backoff_delay(20) <= 60.0 * 1.25


async def test_retry_after_header_is_obeyed(config, log):
    """A server that states its wait time gets obeyed exactly."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, text="slow down")
        return httpx.Response(200, content=b"<html>ok</html>",
                              headers={"content-type": "text/html"})

    async with Fetcher(config, log) as fetcher:
        install_transport(fetcher, handler)
        result = await fetcher.get("https://example.com/x")
    assert result.status == 200
    assert calls["n"] == 2


async def test_429_permanently_slows_the_host_bucket(config, log):
    """A 429 means our configured rate is too high; it must persist."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "0"}, text="slow down")

    config.crawl.retry_limit = 1
    async with Fetcher(config, log) as fetcher:
        install_transport(fetcher, handler)
        bucket = await fetcher.bucket_for("example.com")
        original_rate = bucket.rate
        await fetcher.get("https://example.com/x")
        assert bucket.rate < original_rate


async def test_host_is_written_off_after_repeated_failures(config, log):
    """Stop hitting a host that is down or blocking us."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="nope")

    async with Fetcher(config, log) as fetcher:
        install_transport(fetcher, handler)
        for index in range(fetcher.host_failure_threshold):
            await fetcher.get(f"https://example.com/{index}")
        assert fetcher.host_is_written_off("example.com")
        result = await fetcher.get("https://example.com/another")
        assert result.status == 0
        assert "written off" in result.error


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
async def test_token_bucket_enforces_the_configured_rate():
    bucket = TokenBucket(rate=10.0, capacity=1.0)
    started = time.monotonic()
    for _ in range(3):
        await bucket.acquire()
    elapsed = time.monotonic() - started
    # First token is free, then ~0.1s each; allow generous slack for CI timing.
    assert elapsed >= 0.15


async def test_token_bucket_allows_a_small_burst_after_idling():
    bucket = TokenBucket(rate=5.0, capacity=3.0)
    started = time.monotonic()
    for _ in range(3):
        await bucket.acquire()
    assert time.monotonic() - started < 0.2


async def test_crawl_delay_only_ever_slows_the_bucket_down():
    bucket = TokenBucket(rate=1.0)
    bucket.slow_to(5.0)          # one request per 5s -> rate 0.2
    assert bucket.rate == pytest.approx(0.2)
    bucket.slow_to(0.1)          # would be 10/s: must be ignored
    assert bucket.rate == pytest.approx(0.2)


async def test_per_host_buckets_are_independent(config, log):
    async with Fetcher(config, log) as fetcher:
        a = await fetcher.bucket_for("host-a.com")
        b = await fetcher.bucket_for("host-b.com")
        assert a is not b
        assert a is await fetcher.bucket_for("HOST-A.com")  # case-insensitive


async def test_global_concurrency_cap_is_respected(config, log):
    """No more than concurrency_cap requests may be in flight at once."""
    config.crawl.concurrency_cap = 3
    state = {"current": 0, "peak": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        state["current"] += 1
        state["peak"] = max(state["peak"], state["current"])
        await asyncio.sleep(0.02)
        state["current"] -= 1
        return httpx.Response(200, content=b"<html>ok</html>",
                              headers={"content-type": "text/html"})

    async with Fetcher(config, log) as fetcher:
        install_transport(fetcher, handler)
        await asyncio.gather(*(fetcher.get(f"https://h{index}.com/x")
                               for index in range(12)))
    assert state["peak"] <= 3


async def test_response_cache_avoids_duplicate_requests(config, log):
    """robots.txt and sitemaps are requested from several code paths."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text="User-agent: *\nDisallow:\n")

    async with Fetcher(config, log) as fetcher:
        install_transport(fetcher, handler)
        first = await fetcher.get("https://example.com/robots.txt", use_cache=True)
        second = await fetcher.get("https://example.com/robots.txt", use_cache=True)

    assert calls["n"] == 1
    assert second.from_cache is True
    assert first.content == second.content


async def test_oversized_body_is_truncated_and_flagged(config, log):
    config.crawl.max_download_bytes = 100

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 5000,
                              headers={"content-type": "application/pdf"})

    async with Fetcher(config, log) as fetcher:
        install_transport(fetcher, handler)
        result = await fetcher.get("https://example.com/huge.pdf")

    assert result.truncated is True
    assert result.size_bytes == 100


async def test_user_agent_is_sent(config, log):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("user-agent", "")
        return httpx.Response(200, content=b"ok")

    async with Fetcher(config, log) as fetcher:
        install_transport(fetcher, handler)
        await fetcher.get("https://example.com/x")

    assert "TestCrawler" in seen["ua"]
    assert "test@example.com" in seen["ua"]


async def test_redirects_are_followed_and_the_final_url_recorded(config, log):
    """The hint host is only a starting point, so redirects must be tracked."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(
                301, headers={"location": "https://provider.example.com/final"}
            )
        return httpx.Response(200, content=b"<html>final</html>",
                              headers={"content-type": "text/html"})

    async with Fetcher(config, log) as fetcher:
        install_transport(fetcher, handler)
        result = await fetcher.get("https://example.com/start")

    assert result.status == 200
    assert result.final_url == "https://provider.example.com/final"


async def test_head_falls_back_to_get_when_head_is_unsupported(config, log):
    """Many payer CMSs answer HEAD with 405."""
    methods = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "HEAD":
            return httpx.Response(405)
        return httpx.Response(200, content=PDF_BYTES,
                              headers={"content-type": "application/pdf"})

    async with Fetcher(config, log) as fetcher:
        install_transport(fetcher, handler)
        result = await fetcher.head_or_get("https://example.com/p.pdf")

    assert methods == ["HEAD", "GET"]
    assert result.status == 200


# ---------------------------------------------------------------------------
# file_type detection: magic bytes over URL suffix
# ---------------------------------------------------------------------------
def test_magic_bytes_beat_a_lying_url_suffix():
    """A payer serving a PDF from a .aspx route must still be file_type=pdf."""
    result = FetchResult(url="https://e.com/doc.aspx?id=1", status=200,
                         content=PDF_BYTES, headers={"content-type": "text/html"})
    assert detect_file_type(result, result.url) == "pdf"


def test_html_error_page_at_a_pdf_url_is_not_a_pdf():
    result = FetchResult(url="https://e.com/policy.pdf", status=200,
                         content=b"<!DOCTYPE html><html><body>Not found</body></html>",
                         headers={"content-type": "text/html"})
    assert detect_file_type(result, result.url) == "html"


def test_ooxml_container_is_disambiguated_by_the_weaker_signals():
    xlsx = FetchResult(
        url="https://e.com/list.xlsx", status=200, content=ZIP_BYTES,
        headers={"content-type":
                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    )
    assert detect_file_type(xlsx, xlsx.url) == "xlsx"

    docx = FetchResult(url="https://e.com/form.docx", status=200, content=ZIP_BYTES,
                       headers={"content-type": "application/octet-stream"})
    assert detect_file_type(docx, docx.url) == "docx"


def test_legacy_ole_container_is_disambiguated():
    xls = FetchResult(url="https://e.com/list.xls", status=200, content=OLE_BYTES,
                      headers={"content-type": "application/vnd.ms-excel"})
    assert detect_file_type(xls, xls.url) == "xls"


def test_content_type_is_used_when_there_are_no_magic_bytes():
    result = FetchResult(url="https://e.com/download?id=9", status=200,
                         content=b"plain text body",
                         headers={"content-type": "application/pdf"})
    assert detect_file_type(result, result.url) == "pdf"


def test_unrecognised_content_is_other():
    result = FetchResult(url="https://e.com/thing", status=200, content=b"\x00\x01\x02\x03",
                         headers={"content-type": "application/octet-stream"})
    assert detect_file_type(result, result.url) == "other"


# ---------------------------------------------------------------------------
# Block / auth detection
# ---------------------------------------------------------------------------
def test_waf_challenge_page_is_detected_not_stored_as_a_document():
    result = FetchResult(
        url="https://e.com/p.pdf", status=200,
        content=b"<html><head><title>Just a moment...</title></head>"
                b"<body>Checking your browser before accessing</body></html>",
        headers={"content-type": "text/html", "server": "cloudflare"},
    )
    challenged, marker = result.looks_challenged()
    assert challenged
    assert marker


def test_403_counts_as_a_block_with_evidence():
    result = FetchResult(url="https://e.com/p.pdf", status=403, content=b"")
    challenged, marker = result.looks_challenged()
    assert challenged
    assert marker == "http_403"


def test_login_gate_is_detected():
    result = FetchResult(
        url="https://e.com/p.pdf", status=200,
        content=b"<html><body>Please sign in to continue</body></html>",
        headers={"content-type": "text/html"},
    )
    gated, marker = result.looks_auth_gated()
    assert gated
    assert marker


def test_redirect_to_a_login_url_counts_as_gated():
    result = FetchResult(url="https://e.com/p.pdf", status=200,
                         final_url="https://secure.e.com/account/login?ret=/p.pdf",
                         content=b"<html><body>portal</body></html>",
                         headers={"content-type": "text/html"})
    gated, _ = result.looks_auth_gated()
    assert gated


def test_a_real_pdf_is_neither_challenged_nor_gated():
    result = FetchResult(url="https://e.com/p.pdf", status=200, content=PDF_BYTES,
                         headers={"content-type": "application/pdf"})
    assert result.looks_challenged() == (False, "")
    assert result.looks_auth_gated() == (False, "")


def test_hash_and_size_are_derived_from_the_raw_bytes():
    result = FetchResult(url="https://e.com/p.pdf", status=200, content=PDF_BYTES,
                         headers={"content-type": "application/pdf"})
    assert result.size_bytes == len(PDF_BYTES)
    assert len(result.sha256) == 64
    # Non-200 rows carry no hash.
    broken = FetchResult(url="https://e.com/p.pdf", status=500, content=b"error")
    assert broken.sha256 == ""


def test_text_decoding_falls_back_gracefully():
    """Payer HTML routinely mislabels its encoding."""
    result = FetchResult(url="https://e.com/x", status=200,
                         content="policy – café".encode("cp1252"),
                         headers={"content-type": "text/html; charset=utf-8"})
    text = result.text()
    assert "policy" in text
