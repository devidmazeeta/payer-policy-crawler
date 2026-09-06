"""
Extractor tests: link extraction, metadata extraction, classification, scoring
and row construction.

The row-construction tests are the important ones: they assert that a row built
from a real-shaped response satisfies the schema, and that a *failed* fetch also
produces a legal row - which is what the brief means by "non-200 rows are wanted".
"""

from __future__ import annotations

import pytest

from crawler.discovery import Candidate
from crawler.extractor import (
    build_row,
    classify_document_type,
    extract_dates,
    extract_links,
    extract_policy_number,
    html_text,
    html_title,
    infer_line_of_business,
    infer_state_or_region,
    looks_like_document_url,
    looks_like_policy_index,
    score_candidate,
    title_from_url,
)
from crawler.fetcher import FetchResult
from crawler.schema import utc_now_iso, validate_row
from crawler.seeds import Payer

INDEX_HTML = """
<!DOCTYPE html>
<html><head><title>Medical Policies | Example Payer</title></head>
<body>
  <nav><a href="/about-us">About us</a></nav>
  <h1>Medical Policies</h1>
  <ul>
    <li><a href="policies/bariatric-surgery.pdf">Bariatric Surgery Medical Policy</a></li>
    <li><a href="../../assets/dam/cardiac-imaging.pdf">Cardiac Imaging Policy</a></li>
    <li><a href="https://cdn.other-domain.com/x.pdf">Off-domain PDF</a></li>
    <li><a href="/policies/pharmacy">Pharmacy Policies</a></li>
    <li><a href="mailto:provider@example.com">Email us</a></li>
    <li><a href="javascript:void(0)">Print</a></li>
    <li><a href="#top">Back to top</a></li>
    <li><a href="/forms/pa-form.pdf" title="Prior Authorization Request Form"></a></li>
    <li><a href="/icon.pdf"><img alt="Sleep Study Policy" src="/i.png"></a></li>
  </ul>
  <footer>Last updated: 01/01/2020</footer>
</body></html>
"""


@pytest.fixture
def test_payer() -> Payer:
    return Payer(
        payer_name="Example Payer",
        payer_alias="EP",
        hint_host="example-payer.com",
        default_state_or_region="NJ",
        default_line_of_business="Commercial",
        position=1,
    )


# ---------------------------------------------------------------------------
# Link extraction
# ---------------------------------------------------------------------------
def test_relative_links_are_resolved_to_absolute():
    links = dict(extract_links(INDEX_HTML, "https://example-payer.com/providers/policies"))
    assert "https://example-payer.com/providers/policies/bariatric-surgery.pdf" in links


def test_cross_directory_links_are_followed():
    """Payers list at /policies/ and keep the PDFs at /assets/ - both are needed."""
    links = dict(extract_links(INDEX_HTML, "https://example-payer.com/a/b/policies"))
    assert "https://example-payer.com/assets/dam/cardiac-imaging.pdf" in links


def test_non_http_schemes_and_fragments_are_dropped():
    links = dict(extract_links(INDEX_HTML, "https://example-payer.com/p"))
    for url in links:
        assert url.startswith("https://") or url.startswith("http://")
        assert "#" not in url
    assert not any("mailto" in url for url in links)
    assert not any("javascript" in url for url in links)


def test_off_domain_links_are_returned_and_filtered_later_by_scope():
    """extract_links is scope-agnostic; the host check lives in discovery."""
    links = dict(extract_links(INDEX_HTML, "https://example-payer.com/p"))
    assert "https://cdn.other-domain.com/x.pdf" in links


def test_anchor_title_attribute_is_used_when_the_link_has_no_text():
    """Icon-only PDF links are common and their title is our best title hint."""
    links = dict(extract_links(INDEX_HTML, "https://example-payer.com/p"))
    assert links["https://example-payer.com/forms/pa-form.pdf"] == \
        "Prior Authorization Request Form"


def test_image_alt_text_is_used_as_a_last_resort():
    links = dict(extract_links(INDEX_HTML, "https://example-payer.com/p"))
    assert links["https://example-payer.com/icon.pdf"] == "Sleep Study Policy"


def test_duplicate_links_collapse_keeping_the_first_anchor_text():
    html = ('<a href="/a.pdf">Descriptive Policy Name</a>'
            '<a href="/a.pdf">click here</a>')
    links = extract_links(html, "https://e.com/")
    assert len(links) == 1
    assert links[0][1] == "Descriptive Policy Name"


def test_malformed_html_still_yields_links():
    """A single unclosed tag must not cost us a whole index page."""
    broken = '<ul><li><a href="/a.pdf">Policy A<li><a href="/b.pdf">Policy B</ul>'
    links = extract_links(broken, "https://e.com/")
    assert len(links) >= 2


def test_iframe_and_embed_sources_count_as_links():
    html = '<iframe src="/viewer/policy.pdf" title="Embedded Policy"></iframe>'
    links = dict(extract_links(html, "https://e.com/"))
    assert "https://e.com/viewer/policy.pdf" in links


# ---------------------------------------------------------------------------
# URL shape heuristics
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://e.com/policies/a.pdf", True),
        ("https://e.com/forms/a.docx", True),
        ("https://e.com/lists/a.xlsx", True),
        ("https://e.com/download?docid=123", True),
        ("https://e.com/dam/medical-policy/x", True),   # CMS asset + policy hint
        ("https://e.com/dam/marketing/banner", False),  # asset, no policy hint
        ("https://e.com/policies", False),
        ("https://e.com/style.css", False),
        ("https://e.com/logo.png", False),
        ("https://e.com/bundle.js", False),
    ],
)
def test_looks_like_document_url(url, expected):
    assert looks_like_document_url(url) is expected


def test_looks_like_policy_index():
    assert looks_like_policy_index("View all medical policies")
    assert looks_like_policy_index("Formulary")
    assert not looks_like_policy_index("Careers")
    assert not looks_like_policy_index("")


def test_scoring_prefers_relevant_documents():
    relevant = score_candidate("https://e.com/medical-policy/bariatric.pdf",
                               "Bariatric Surgery Medical Policy")
    generic = score_candidate("https://e.com/assets/file.pdf", "Download")
    assert relevant > generic


def test_scoring_penalises_archived_and_retired_content():
    current = score_candidate("https://e.com/medical-policy/a.pdf", "Medical Policy")
    retired = score_candidate("https://e.com/medical-policy/retired/a.pdf",
                              "Retired Medical Policy")
    assert current > retired


# ---------------------------------------------------------------------------
# Title extraction
# ---------------------------------------------------------------------------
def test_h1_is_preferred_over_title_tag():
    title, method = html_title(INDEX_HTML)
    assert title == "Medical Policies"
    assert method == "css_selector"


def test_site_boilerplate_is_stripped_from_the_title_tag():
    html = "<html><head><title>Bariatric Surgery Policy | UnitedHealthcare</title></head></html>"
    title, _ = html_title(html)
    assert title == "Bariatric Surgery Policy"


def test_og_title_is_used_when_there_is_no_h1():
    html = ('<html><head><meta property="og:title" content="Cardiac Imaging Policy">'
            '<title>Site</title></head><body></body></html>')
    title, _ = html_title(html)
    assert title == "Cardiac Imaging Policy"


def test_html_text_excludes_nav_and_footer_boilerplate():
    """A site-wide footer 'last updated' must not become a policy's date."""
    text = html_text(INDEX_HTML)
    assert "About us" not in text
    assert "01/01/2020" not in text


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://e.com/assets/medical-policy_CS123_2026-01.pdf",
         "Medical Policy CS123 2026 01"),
        ("https://e.com/p/bariatric-surgery.pdf", "Bariatric Surgery"),
        ("https://e.com/", ""),
    ],
)
def test_title_from_url(url, expected):
    assert title_from_url(url) == expected


# ---------------------------------------------------------------------------
# Policy numbers and dates
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("Policy Number: CS123.045", "CS123.045"),
        ("Policy No. MP-042", "MP-042"),
        ("Guideline Number: CDG.010.05", "CDG.010.05"),
        ("This policy CS0123 applies", "CS0123"),
        ("Bulletin 2024-0031 issued", "2024-0031"),
        ("no identifier here", ""),
        ("Effective 2026", ""),          # a bare year is never a policy number
    ],
)
def test_extract_policy_number(text, expected):
    assert extract_policy_number(text) == expected


@pytest.mark.parametrize(
    "text,effective,updated",
    [
        ("Effective Date: 03/01/2026 Last Revised: 01/15/2026",
         "2026-03-01", "2026-01-15"),
        ("Effective March 1, 2026", "2026-03-01", ""),
        ("Last Updated: 2026-02-10", "", "2026-02-10"),
        ("no dates at all", "", ""),
    ],
)
def test_extract_dates_uses_what_the_source_printed(text, effective, updated):
    assert extract_dates(text) == (effective, updated)


def test_dates_are_never_backfilled_with_the_crawl_date():
    """The single most important date rule in the conventions sheet."""
    effective, updated = extract_dates("This document has no printed date.")
    assert effective == ""
    assert updated == ""


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url,title,expected",
    [
        ("https://e.com/x.pdf", "Bariatric Surgery Medical Policy", "medical_policy"),
        ("https://e.com/x.pdf", "Pharmacy Policy: Humira", "pharmacy_policy"),
        ("https://e.com/x.pdf", "Clinical Criteria for MRI", "coverage_guideline"),
        ("https://e.com/x.pdf", "Prior Authorization Request Form", "pa_form"),
        ("https://e.com/x.pdf", "Services Requiring Prior Authorization", "pa_list"),
        ("https://e.com/x.pdf", "2026 Comprehensive Formulary", "formulary"),
        ("https://e.com/x.pdf", "Specialty Drug List", "drug_list"),
        ("https://e.com/x.pdf", "Provider Administrative Manual", "provider_manual"),
        ("https://e.com/x.pdf", "March 2026 Provider Bulletin", "bulletin"),
        ("https://e.com/medical-policy/a.pdf", "", "medical_policy"),
        ("https://e.com/x.pdf", "Annual Report", "other"),
    ],
)
def test_classify_document_type(url, title, expected):
    document_type, strength = classify_document_type(url, title)
    assert document_type == expected
    assert 0.0 <= strength <= 1.0


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Medicare Advantage Coverage Policy", "Medicare"),
        ("Medicaid Managed Care Policy", "Medicaid"),
        ("Marketplace Exchange Plan Policy", "Exchange"),
        ("Federal Employee Program Policy", "Federal"),
        ("Commercial Group Policy", "Commercial"),
        ("Medicare and Medicaid Combined Policy", "Multiple"),
        ("Bariatric Surgery", "Commercial"),   # falls back to the seed default
    ],
)
def test_infer_line_of_business(title, expected):
    assert infer_line_of_business("https://e.com/x.pdf", title, "",
                                  default="Commercial") == expected


def test_line_of_business_falls_back_to_unknown_when_there_is_no_default():
    assert infer_line_of_business("https://e.com/x.pdf", "Bariatric Surgery", "",
                                  default="") == "Unknown"


def test_state_inference_from_full_name_and_url_segment():
    assert infer_state_or_region("https://e.com/x.pdf", "New Jersey Medicaid Policy") == "NJ"
    assert infer_state_or_region("https://e.com/nj/policies/x.pdf", "Policy") == "NJ"


def test_state_inference_ignores_two_letter_english_words():
    """'OR' and 'IN' appear constantly as words and would poison the column."""
    result = infer_state_or_region("https://e.com/x.pdf",
                                   "Coverage in or around the facility", default="")
    assert result == ""


def test_state_inference_falls_back_to_the_seed_default():
    assert infer_state_or_region("https://e.com/x.pdf", "Policy", default="TN") == "TN"


# ---------------------------------------------------------------------------
# Row construction
# ---------------------------------------------------------------------------
def make_candidate(**overrides) -> Candidate:
    defaults = {
        "url": "https://example-payer.com/policies/bariatric.pdf",
        "source_page_url": "https://example-payer.com/policies",
        "discovery_path": "seed:example-payer.com > robots.txt > sitemap.xml > "
                          "index:/policies > anchor[href$='.pdf']",
        "origin": "anchor",
        "link_text": "Bariatric Surgery Medical Policy",
    }
    defaults.update(overrides)
    return Candidate(**defaults)


def test_successful_html_row_is_schema_valid(test_payer):
    body = (
        "<html><head><title>Bariatric Surgery Medical Policy</title></head><body>"
        "<h1>Bariatric Surgery Medical Policy</h1>"
        "<p>Policy Number: CS123.045</p>"
        "<p>Effective Date: 03/01/2026</p>"
        "<p>Last Revised: 01/15/2026</p>"
        "<p>Medical necessity criteria and coverage rationale follow.</p>"
        "</body></html>"
    ).encode("utf-8")
    result = FetchResult(
        url="https://example-payer.com/policies/bariatric",
        status=200, content=body, headers={"content-type": "text/html"},
    )
    row = build_row(test_payer, make_candidate(url=result.url), result,
                    utc_now_iso(), file_type="html")

    assert validate_row(row) == []
    assert row.document_title == "Bariatric Surgery Medical Policy"
    assert row.policy_number == "CS123.045"
    assert row.effective_date == "2026-03-01"
    assert row.last_updated_date == "2026-01-15"
    assert row.document_type == "medical_policy"
    assert row.file_type == "html"
    assert row.http_status == "200"
    assert len(row.content_hash_sha256) == 64
    assert row.render_mode == "static"
    assert float(row.confidence_score) >= 0.70


def test_row_records_every_extraction_method_that_contributed(test_payer):
    body = b"<html><h1>Cardiac Imaging Policy</h1><p>Policy Number: MP-042</p></html>"
    result = FetchResult(url="https://e.com/a", status=200, content=body,
                         headers={"content-type": "text/html"})
    row = build_row(test_payer, make_candidate(url=result.url, origin="sitemap"),
                    result, utc_now_iso(), file_type="html")
    methods = set(str(row.extraction_method).split("|"))
    assert "sitemap" in methods        # how it was discovered
    assert "css_selector" in methods   # how the title was read
    assert "regex" in methods          # how the policy number was read


def test_transport_failure_produces_a_legal_row(test_payer):
    """Non-200 rows are wanted, not penalised."""
    result = FetchResult(url="https://e.com/a.pdf", status=0,
                         error="ConnectTimeout: timed out", attempts=4)
    row = build_row(test_payer, make_candidate(url=result.url), result,
                    utc_now_iso(), notes="transport-level failure")
    assert validate_row(row) == []
    assert row.http_status == "0"
    assert row.content_hash_sha256 == ""
    assert row.file_size_bytes == ""
    assert row.notes


def test_404_row_is_legal_and_explains_itself(test_payer):
    result = FetchResult(url="https://e.com/a.pdf", status=404, content=b"Not found",
                         headers={"content-type": "text/html"})
    row = build_row(test_payer, make_candidate(url=result.url), result,
                    utc_now_iso(), notes="non-200 response (HTTP 404)")
    assert validate_row(row) == []
    assert row.http_status == "404"
    assert row.content_hash_sha256 == ""
    assert "404" in row.notes


def test_auth_gated_row_has_no_hash_and_says_why(test_payer):
    result = FetchResult(url="https://e.com/a.pdf", status=200,
                         content=b"<html>Please sign in</html>",
                         headers={"content-type": "text/html"})
    row = build_row(test_payer, make_candidate(url=result.url), result,
                    utc_now_iso(), file_type="html", requires_auth=True,
                    notes="public URL resolves to a credentialed portal")
    assert validate_row(row) == []
    assert row.requires_auth == "Y"
    assert row.content_hash_sha256 == ""
    assert "portal" in row.notes


def test_low_confidence_row_always_carries_an_explanation(test_payer):
    """The conventions sheet requires it, and validate_row enforces it."""
    result = FetchResult(url="https://e.com/assets/file.xlsx", status=200,
                         content=b"PK\x03\x04" + b"\x00" * 40,
                         headers={"content-type": "application/octet-stream"})
    row = build_row(test_payer, make_candidate(url=result.url, link_text=""),
                    result, utc_now_iso(), file_type="xlsx")
    assert validate_row(row) == []
    if float(row.confidence_score) < 0.70:
        assert row.notes


def test_truncated_download_is_flagged_in_notes(test_payer):
    result = FetchResult(url="https://e.com/big.pdf", status=200,
                         content=b"%PDF-1.7" + b"x" * 100,
                         headers={"content-type": "application/pdf"}, truncated=True)
    row = build_row(test_payer, make_candidate(url=result.url), result,
                    utc_now_iso(), file_type="pdf")
    assert "truncated" in row.notes
    assert validate_row(row) == []


def test_redirect_is_recorded_in_notes(test_payer):
    result = FetchResult(url="https://e.com/a.pdf",
                         final_url="https://provider.e.com/a.pdf",
                         status=200, content=b"%PDF-1.7 body",
                         headers={"content-type": "application/pdf"})
    row = build_row(test_payer, make_candidate(url="https://e.com/a.pdf"), result,
                    utc_now_iso(), file_type="pdf")
    assert "redirected to https://provider.e.com/a.pdf" in row.notes


def test_enumerated_row_is_scored_lower_than_a_linked_one(test_payer):
    body = b"<html><h1>Bariatric Surgery Medical Policy</h1><p>Policy Number: CS1</p></html>"
    result = FetchResult(url="https://e.com/p/123", status=200, content=body,
                         headers={"content-type": "text/html"})
    linked = build_row(test_payer, make_candidate(url=result.url, origin="anchor"),
                       result, utc_now_iso(), file_type="html")
    enumerated = build_row(
        test_payer, make_candidate(url=result.url, origin="url_pattern", link_text=""),
        result, utc_now_iso(), file_type="html",
    )
    assert float(enumerated.confidence_score) < float(linked.confidence_score)
    assert "url_pattern" in str(enumerated.extraction_method)


def test_discovery_path_is_preserved_verbatim(test_payer):
    """A reviewer must be able to reproduce the find from this column."""
    path = ("seed:example-payer.com > robots.txt > sitemap.xml > "
            "index:/providers/policies > anchor[href$='.pdf']")
    result = FetchResult(url="https://e.com/a.pdf", status=200, content=b"%PDF-1.7 x",
                         headers={"content-type": "application/pdf"})
    row = build_row(test_payer, make_candidate(discovery_path=path, url=result.url),
                    result, utc_now_iso(), file_type="pdf")
    assert row.discovery_path == path


def test_scrape_timestamp_is_the_passed_fetch_moment(test_payer):
    """Passed in rather than recomputed, so retries do not shift it."""
    stamp = "2026-08-19T09:41:12Z"
    result = FetchResult(url="https://e.com/a.pdf", status=200, content=b"%PDF-1.7 x",
                         headers={"content-type": "application/pdf"})
    row = build_row(test_payer, make_candidate(url=result.url), result, stamp,
                    file_type="pdf")
    assert row.scrape_timestamp_utc == stamp


def test_payer_defaults_flow_into_the_row(test_payer):
    result = FetchResult(url="https://e.com/a.pdf", status=200, content=b"%PDF-1.7 x",
                         headers={"content-type": "application/pdf"})
    row = build_row(test_payer, make_candidate(url=result.url), result,
                    utc_now_iso(), file_type="pdf")
    assert row.payer_name == "Example Payer"
    assert row.payer_alias == "EP"
    assert row.state_or_region == "NJ"
