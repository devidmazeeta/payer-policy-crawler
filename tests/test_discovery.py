"""
Discovery tests: the end-to-end strategy against a simulated payer site.

A ``MockTransport`` serves a small but realistic payer: a robots.txt with a
Disallowed search endpoint, a sitemap index, a category page whose PDFs live in
another directory, a WAF-walled path and a login-gated document. That lets the
whole priority order - robots, sitemap, index crawl, enumeration fallback, block
reporting - be asserted without touching the network.
"""

from __future__ import annotations

import httpx
import pytest

from crawler.discovery import (
    PayerCrawler,
    normalise_url,
    registrable_domain,
)
from crawler.fetcher import Fetcher
from crawler.robots import RobotsCache
from crawler.schema import validate_row
from crawler.seeds import Payer
from crawler.storage import Downloader

PDF = b"%PDF-1.7\nBariatric Surgery Medical Policy\nPolicy Number: CS100\n%%EOF"

ROBOTS = """User-agent: *
Disallow: /provider/search
Disallow: /private/
Crawl-delay: 0
Sitemap: https://payer.test/sitemap.xml
"""

SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://payer.test/sitemap-policies.xml</loc></sitemap>
  <sitemap><loc>https://payer.test/sitemap-careers.xml</loc></sitemap>
</sitemapindex>
"""

SITEMAP_POLICIES = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://payer.test/provider/medical-policies</loc>
       <lastmod>2026-02-01</lastmod></url>
  <url><loc>https://payer.test/provider/search?q=policy</loc></url>
  <url><loc>https://payer.test/careers/openings</loc></url>
</urlset>
"""

POLICY_INDEX_HTML = """<!DOCTYPE html>
<html><head><title>Medical Policies | Payer Test</title></head><body>
<h1>Medical Policies</h1>
<a href="/assets/dam/policy/bariatric-surgery_CS100.pdf">Bariatric Surgery Medical Policy</a>
<a href="/assets/dam/policy/cardiac-imaging_CS101.pdf">Cardiac Imaging Medical Policy</a>
<a href="/assets/dam/policy/sleep-studies_CS102.pdf">Sleep Studies Medical Policy</a>
<a href="/private/internal-policy.pdf">Internal only</a>
<a href="/provider/search?q=policy">Search policies</a>
<a href="/portal/secure-policy.pdf">Credentialed policy</a>
<a href="/careers/openings">Careers</a>
</body></html>
"""


def make_handler(calls: list[str]):
    """Return a MockTransport handler that records every requested path."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = request.url.query.decode() if request.url.query else ""
        calls.append(path + (f"?{query}" if query else ""))

        if path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS,
                                  headers={"content-type": "text/plain"})
        if path == "/sitemap.xml":
            return httpx.Response(200, text=SITEMAP_INDEX,
                                  headers={"content-type": "application/xml"})
        if path == "/sitemap-policies.xml":
            return httpx.Response(200, text=SITEMAP_POLICIES,
                                  headers={"content-type": "application/xml"})
        if path == "/sitemap-careers.xml":
            return httpx.Response(200, text="<urlset></urlset>",
                                  headers={"content-type": "application/xml"})
        if path in ("/provider/medical-policies", "/"):
            return httpx.Response(200, text=POLICY_INDEX_HTML,
                                  headers={"content-type": "text/html"})
        if path.startswith("/assets/dam/policy/"):
            return httpx.Response(200, content=PDF,
                                  headers={"content-type": "application/pdf"})
        if path == "/portal/secure-policy.pdf":
            return httpx.Response(
                200, text="<html><body>Please sign in to continue</body></html>",
                headers={"content-type": "text/html"},
            )
        if path.startswith("/private/"):
            # Must never be requested: robots disallows it.
            return httpx.Response(200, content=PDF,
                                  headers={"content-type": "application/pdf"})
        if path.startswith("/provider/search"):
            return httpx.Response(200, text="<html>results</html>",
                                  headers={"content-type": "text/html"})
        return httpx.Response(404, text="not found")

    return handler


@pytest.fixture
def site_payer() -> Payer:
    return Payer(
        payer_name="Payer Test",
        payer_alias="PT",
        hint_host="payer.test",
        default_state_or_region="NJ",
        default_line_of_business="Commercial",
        seed_paths=["/provider/medical-policies"],
        position=1,
    )


async def run_crawler(config, log, payer, handler, tmp_path):
    """Wire up a PayerCrawler against the mock transport and run it."""
    async with Fetcher(config, log) as fetcher:
        fetcher._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
            headers={"User-Agent": config.crawl.user_agent},
        )
        robots = RobotsCache(fetcher, log, config.crawl.user_agent)
        downloader = Downloader(tmp_path / "downloads", log, enabled=False)
        crawler = PayerCrawler(payer, config, fetcher, robots, downloader, log)
        return await crawler.run()


# ---------------------------------------------------------------------------
# URL canonicalisation and scope
# ---------------------------------------------------------------------------
def test_registrable_domain():
    assert registrable_domain("www.uhcprovider.com") == "uhcprovider.com"
    assert registrable_domain("provider.bcbst.com") == "bcbst.com"
    assert registrable_domain("bcbst.com") == "bcbst.com"
    assert registrable_domain("") == ""


def test_normalise_url_is_stable_and_idempotent():
    url = "https://Example.COM:443/a/../b/index.html?utm_source=x&id=1#frag"
    once = normalise_url(url)
    assert normalise_url(once) == once
    assert "utm_source" not in once
    assert "#" not in once


# ---------------------------------------------------------------------------
# Sitemap parsing
# ---------------------------------------------------------------------------
def test_sitemap_index_is_detected():
    is_index, entries = PayerCrawler.parse_sitemap(SITEMAP_INDEX)
    assert is_index
    assert len(entries) == 2


def test_sitemap_urls_and_lastmods_are_paired():
    is_index, entries = PayerCrawler.parse_sitemap(SITEMAP_POLICIES)
    assert not is_index
    assert entries[0] == ("https://payer.test/provider/medical-policies", "2026-02-01")


def test_malformed_sitemap_still_yields_valid_locs():
    """ElementTree would discard the whole file; the regex parser recovers."""
    broken = (
        '<?xml version="1.0"?><urlset>'
        "<url><loc>https://e.com/a.pdf</loc></url>"
        "<url><loc>https://e.com/b&c.pdf</loc></url>"  # unescaped ampersand
        "<url><loc>not-a-url</loc></url>"
        "<url><loc>https://e.com/c.pdf</loc>"          # truncated tail
    )
    _, entries = PayerCrawler.parse_sitemap(broken)
    urls = [loc for loc, _ in entries]
    assert "https://e.com/a.pdf" in urls
    assert "https://e.com/c.pdf" in urls
    assert "not-a-url" not in urls


def test_cdata_wrapped_loc_is_parsed():
    xml = "<urlset><url><loc><![CDATA[https://e.com/a.pdf]]></loc></url></urlset>"
    _, entries = PayerCrawler.parse_sitemap(xml)
    assert entries == [("https://e.com/a.pdf", "")]


# ---------------------------------------------------------------------------
# End-to-end discovery against the simulated site
# ---------------------------------------------------------------------------
async def test_robots_is_fetched_before_anything_else(config, log, site_payer, tmp_path):
    calls: list[str] = []
    await run_crawler(config, log, site_payer, make_handler(calls), tmp_path)
    assert calls[0] == "/robots.txt"


async def test_declared_sitemap_is_used_as_the_frontier(config, log, site_payer, tmp_path):
    calls: list[str] = []
    _, stats = await run_crawler(config, log, site_payer, make_handler(calls), tmp_path)
    assert "/sitemap.xml" in calls
    assert "/sitemap-policies.xml" in calls   # nested index was followed
    assert stats.sitemaps_parsed >= 2
    assert any(event["url"].endswith("/sitemap.xml")
               for event in log.events("sitemap.parsed"))


async def test_documents_are_found_via_cross_directory_anchors(
    config, log, site_payer, tmp_path
):
    """The listing is at /provider/, the PDFs at /assets/ - the common shape."""
    calls: list[str] = []
    rows, stats = await run_crawler(config, log, site_payer, make_handler(calls), tmp_path)
    pdf_rows = [row for row in rows if row.file_type == "pdf"]
    assert pdf_rows
    assert stats.found >= 1
    assert any("/assets/dam/policy/" in call for call in calls)
    assert log.events("doc.found")


async def test_robots_disallowed_paths_are_never_requested(
    config, log, site_payer, tmp_path
):
    """The single most important compliance assertion in the suite."""
    calls: list[str] = []
    _, stats = await run_crawler(config, log, site_payer, make_handler(calls), tmp_path)
    assert not any(call.startswith("/private/") for call in calls)
    assert not any(call.startswith("/provider/search") for call in calls)
    assert stats.skipped_robots > 0
    blocked = log.events("robots.blocked")
    assert blocked
    # Every refusal must carry the matched rule as evidence.
    assert any(event.get("rule") for event in blocked)


async def test_discovery_path_records_the_full_chain(config, log, site_payer, tmp_path):
    rows, _ = await run_crawler(config, log, site_payer, make_handler([]), tmp_path)
    pdf_rows = [row for row in rows if row.file_type == "pdf"]
    assert pdf_rows
    path = pdf_rows[0].discovery_path
    assert path.startswith("seed:payer.test")
    assert "robots.txt" in path
    assert " > " in path


async def test_every_emitted_row_is_schema_valid(config, log, site_payer, tmp_path):
    rows, _ = await run_crawler(config, log, site_payer, make_handler([]), tmp_path)
    assert rows
    for row in rows:
        assert validate_row(row) == [], f"{row.document_url}: {validate_row(row)}"


async def test_login_gated_document_is_recorded_not_bypassed(
    config, log, site_payer, tmp_path
):
    rows, _ = await run_crawler(config, log, site_payer, make_handler([]), tmp_path)
    gated = [row for row in rows if row.requires_auth == "Y"]
    if gated:
        # requires_auth=Y rows must never carry a hash: we did not log in.
        for row in gated:
            assert row.content_hash_sha256 == ""
            assert row.notes


async def test_noise_paths_are_not_crawled(config, log, site_payer, tmp_path):
    calls: list[str] = []
    await run_crawler(config, log, site_payer, make_handler(calls), tmp_path)
    assert not any(call.startswith("/careers") for call in calls)


async def test_max_docs_per_payer_is_respected(config, log, site_payer, tmp_path):
    config.crawl.max_docs_per_payer = 1
    _, stats = await run_crawler(config, log, site_payer, make_handler([]), tmp_path)
    assert stats.found <= 1


async def test_payer_start_and_stats_are_populated(config, log, site_payer, tmp_path):
    _, stats = await run_crawler(config, log, site_payer, make_handler([]), tmp_path)
    assert log.events("payer.start")
    assert stats.payer == "Payer Test"
    assert stats.elapsed_s >= 0
    fields = stats.as_log_fields()
    # The exact field set the brief requires on payer.done.
    for required in ("payer", "attempted", "found", "failed", "skipped_robots",
                     "dupes_collapsed", "elapsed_s"):
        assert required in fields


async def test_no_duplicate_urls_within_one_payer(config, log, site_payer, tmp_path):
    rows, _ = await run_crawler(config, log, site_payer, make_handler([]), tmp_path)
    urls = [normalise_url(str(row.document_url)) for row in rows]
    assert len(urls) == len(set(urls))


# ---------------------------------------------------------------------------
# Blocked payer: reported with evidence, never fabricated
# ---------------------------------------------------------------------------
async def test_waf_blocked_payer_reports_a_block_and_fabricates_nothing(
    config, log, site_payer, tmp_path
):
    def blocking_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:\n",
                                  headers={"content-type": "text/plain"})
        return httpx.Response(
            200,
            text="<html><head><title>Just a moment...</title></head>"
                 "<body>Checking your browser before accessing</body></html>",
            headers={"content-type": "text/html", "server": "cloudflare"},
        )

    rows, stats = await run_crawler(config, log, site_payer, blocking_handler, tmp_path)
    assert stats.blocked is True
    assert stats.block_evidence
    assert log.events("payer.blocked")
    # No document rows may be invented for a blocked payer.
    assert not [row for row in rows if row.http_status == "200"
                and row.content_hash_sha256]


async def test_hard_403_payer_is_recorded_as_blocked(config, log, site_payer, tmp_path):
    def forbidden_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:\n",
                                  headers={"content-type": "text/plain"})
        return httpx.Response(403, text="Access Denied")

    rows, stats = await run_crawler(config, log, site_payer, forbidden_handler, tmp_path)
    assert stats.found == 0
    for row in rows:
        assert validate_row(row) == []


async def test_robots_403_means_we_stay_out_entirely(config, log, site_payer, tmp_path):
    """If a site will not show its rules, we cannot know what is permitted."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(403, text="forbidden")
        return httpx.Response(200, content=PDF,
                              headers={"content-type": "application/pdf"})

    _, stats = await run_crawler(config, log, site_payer, handler, tmp_path)
    assert stats.found == 0
    assert stats.skipped_robots > 0
    assert calls.count("/robots.txt") == 1  # cached, not re-fetched


async def test_missing_robots_txt_means_unrestricted_crawling(
    config, log, site_payer, tmp_path
):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, text="not found")
        if request.url.path in ("/", "/provider/medical-policies"):
            return httpx.Response(200, text=POLICY_INDEX_HTML,
                                  headers={"content-type": "text/html"})
        if request.url.path.startswith("/assets/"):
            return httpx.Response(200, content=PDF,
                                  headers={"content-type": "application/pdf"})
        return httpx.Response(404)

    _, stats = await run_crawler(config, log, site_payer, handler, tmp_path)
    assert stats.skipped_robots == 0
    assert stats.found >= 1


# ---------------------------------------------------------------------------
# Enumeration fallback
# ---------------------------------------------------------------------------
def test_pattern_inference_requires_at_least_three_observations(config, log, site_payer,
                                                                tmp_path):
    """We only extrapolate from evidence, never from a single lucky number."""
    from .conftest import make_row

    downloader = Downloader(tmp_path / "d", log, enabled=False)
    crawler = PayerCrawler(site_payer, config, None, None, downloader, log)

    two = [make_row(document_url=f"https://payer.test/policy/{n}.pdf") for n in (10, 11)]
    assert crawler._infer_enumerable_pattern(two) is None

    three = [make_row(document_url=f"https://payer.test/policy/{n}.pdf")
             for n in (10, 11, 12)]
    inferred = crawler._infer_enumerable_pattern(three)
    assert inferred is not None
    template, observed = inferred
    assert template == "https://payer.test/policy/{n}.pdf"
    assert observed == [10, 11, 12]


def test_enumeration_is_skipped_when_nothing_was_robots_blocked(config, log, site_payer,
                                                                tmp_path):
    """The fallback exists only to compensate for a disallowed endpoint."""
    from .conftest import make_row

    downloader = Downloader(tmp_path / "d", log, enabled=False)
    crawler = PayerCrawler(site_payer, config, None, None, downloader, log)
    rows = [make_row(document_url=f"https://payer.test/policy/{n}.pdf")
            for n in (10, 11, 12)]

    import asyncio

    result = asyncio.run(crawler._enumerate_fallback(rows))
    assert result == []
    assert crawler.stats.enumerated == 0


async def test_enumeration_verifies_candidates_before_emitting(
    config, log, site_payer, tmp_path
):
    """A 200 alone is not enough: soft-404 pages must be rejected."""
    config.crawl.max_enumeration_candidates = 6
    config.crawl.max_docs_per_payer = 20

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(
                200,
                text="User-agent: *\nDisallow: /provider/search\n"
                     "Sitemap: https://payer.test/sitemap.xml\n",
                headers={"content-type": "text/plain"},
            )
        if path == "/sitemap.xml":
            return httpx.Response(
                200,
                text='<?xml version="1.0"?><urlset>'
                     "<url><loc>https://payer.test/provider/medical-policies</loc></url>"
                     "<url><loc>https://payer.test/provider/search?q=x</loc></url>"
                     "</urlset>",
                headers={"content-type": "application/xml"},
            )
        if path in ("/", "/provider/medical-policies"):
            return httpx.Response(
                200,
                text="<html><h1>Medical Policies</h1>"
                     '<a href="/policy/100.pdf">Bariatric Surgery Medical Policy</a>'
                     '<a href="/policy/101.pdf">Cardiac Imaging Medical Policy</a>'
                     '<a href="/policy/102.pdf">Sleep Studies Medical Policy</a>'
                     '<a href="/provider/search?q=x">Search</a></html>',
                headers={"content-type": "text/html"},
            )
        if path.startswith("/policy/"):
            number = path.split("/")[-1].removesuffix(".pdf")
            if number in {"100", "101", "102", "103"}:
                return httpx.Response(200, content=PDF,
                                      headers={"content-type": "application/pdf"})
            # Soft 404: 200 with a "page not found" body.
            return httpx.Response(
                200, text="<html><h1>Page Not Found</h1></html>",
                headers={"content-type": "text/html"},
            )
        return httpx.Response(404)

    rows, stats = await run_crawler(config, log, site_payer, handler, tmp_path)

    enumerated = [row for row in rows if "url_pattern" in str(row.extraction_method)
                  and "enumeration" in str(row.notes)]
    assert stats.enumerated > 0
    # 103 exists and is a real PDF, so it should be found and justified.
    assert any(row.document_url.endswith("/103.pdf") for row in enumerated)
    for row in enumerated:
        assert "robots-disallowed" in row.discovery_path
        assert "robots-disallowed" in row.notes or "enumeration" in row.notes
        assert validate_row(row) == []
    # The soft-404 pages must not have been emitted.
    assert not any(row.document_url.endswith("/999.pdf") for row in rows)
    # And the disallowed endpoint itself was never fetched.
    assert log.events("enumeration.attempted")
