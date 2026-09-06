"""
Near-duplicate collapsing tests.

Covers each tier of the documented rule, the transitive merging, the winner
selection order, and the invariant the schema actually mandates: after dedupe,
``document_url`` is unique across the whole output.
"""

from __future__ import annotations

from crawler.dedupe import choose_winner, dedupe_rows, rule_description, title_key
from crawler.discovery import normalise_url

from .conftest import make_row


# ---------------------------------------------------------------------------
# Tier 0: URL canonicalisation
# ---------------------------------------------------------------------------
def test_url_normalisation_collapses_cosmetic_differences():
    variants = [
        "https://Example-Payer.com/policies/bariatric.pdf",
        "https://example-payer.com/policies/bariatric.pdf#page=2",
        "https://example-payer.com:443/policies/bariatric.pdf",
        "https://example-payer.com/policies/bariatric.pdf?utm_source=news",
        "https://example-payer.com//policies//bariatric.pdf",
    ]
    canonical = {normalise_url(url) for url in variants}
    assert len(canonical) == 1


def test_url_normalisation_keeps_meaningful_query_parameters():
    a = normalise_url("https://example.com/doc?id=123")
    b = normalise_url("https://example.com/doc?id=456")
    assert a != b


def test_url_normalisation_sorts_query_parameters():
    assert normalise_url("https://e.com/d?b=2&a=1") == normalise_url("https://e.com/d?a=1&b=2")


def test_url_normalisation_strips_index_html_and_trailing_slash():
    assert normalise_url("https://e.com/policies/index.html") == \
        normalise_url("https://e.com/policies/")


def test_exact_duplicate_urls_collapse_to_one_row():
    rows = [
        make_row(document_url="https://example-payer.com/p/a.pdf"),
        make_row(document_url="https://example-payer.com/p/a.pdf?utm_campaign=x"),
    ]
    kept, report = dedupe_rows(rows)
    assert len(kept) == 1
    assert report.collapsed == 1
    assert report.by_tier["exact_url"] == 1


# ---------------------------------------------------------------------------
# Tier 1: identical bytes
# ---------------------------------------------------------------------------
def test_same_content_hash_at_two_urls_collapses():
    digest = "d" * 64
    rows = [
        make_row(document_url="https://example-payer.com/a/policy.pdf",
                 content_hash_sha256=digest, document_title="Policy A"),
        make_row(document_url="https://example-payer.com/mirror/b/policy.pdf",
                 content_hash_sha256=digest, document_title="Policy A copy"),
    ]
    kept, report = dedupe_rows(rows)
    assert len(kept) == 1
    assert report.by_tier["content_hash"] == 1


def test_identical_bytes_from_different_payers_are_both_kept():
    """
    Two payers publishing the same vendor document (MCG/InterQual criteria, a
    CMS form) are two genuine findings. Keying tier 1 on the digest alone would
    silently delete one payer's entire contribution.
    """
    digest = "f" * 64
    rows = [
        make_row(payer_name="Payer One", payer_alias="P1",
                 document_url="https://one.com/criteria.pdf",
                 content_hash_sha256=digest, policy_number=""),
        make_row(payer_name="Payer Two", payer_alias="P2",
                 document_url="https://two.com/criteria.pdf",
                 content_hash_sha256=digest, policy_number=""),
    ]
    kept, report = dedupe_rows(rows)
    assert len(kept) == 2
    assert report.collapsed == 0
    assert {row.payer_name for row in kept} == {"Payer One", "Payer Two"}


def test_hashes_are_not_compared_across_non_200_rows():
    """Non-200 rows carry an empty hash, so they must not all merge together."""
    rows = [
        make_row(document_url="https://example-payer.com/a.pdf", http_status=404,
                 content_hash_sha256="", document_title="Alpha Policy One",
                 policy_number="", confidence_score=0.25, notes="not retrievable"),
        make_row(document_url="https://example-payer.com/b.pdf", http_status=404,
                 content_hash_sha256="", document_title="Beta Policy Two",
                 policy_number="", confidence_score=0.25, notes="not retrievable"),
    ]
    kept, _ = dedupe_rows(rows)
    assert len(kept) == 2


# ---------------------------------------------------------------------------
# Tier 2: policy number
# ---------------------------------------------------------------------------
def test_same_policy_number_collapses_revisions():
    rows = [
        make_row(document_url="https://example-payer.com/p/cs123-2025.pdf",
                 policy_number="CS123", effective_date="2025-01-01",
                 content_hash_sha256="1" * 64),
        make_row(document_url="https://example-payer.com/p/cs123-2026.pdf",
                 policy_number="CS123", effective_date="2026-01-01",
                 content_hash_sha256="2" * 64),
    ]
    kept, report = dedupe_rows(rows)
    assert len(kept) == 1
    assert report.by_tier["policy_number"] == 1
    # The newest revision must be the survivor.
    assert kept[0].effective_date == "2026-01-01"


def test_same_policy_number_different_payers_do_not_collapse():
    rows = [
        make_row(payer_name="Payer One", policy_number="MP-001",
                 document_url="https://one.com/p.pdf", content_hash_sha256="3" * 64),
        make_row(payer_name="Payer Two", policy_number="MP-001",
                 document_url="https://two.com/p.pdf", content_hash_sha256="4" * 64),
    ]
    kept, _ = dedupe_rows(rows)
    assert len(kept) == 2


def test_same_policy_number_different_document_type_does_not_collapse():
    rows = [
        make_row(policy_number="MP-001", document_type="medical_policy",
                 document_url="https://e.com/policy.pdf", content_hash_sha256="5" * 64),
        make_row(policy_number="MP-001", document_type="pa_form",
                 document_url="https://e.com/form.pdf", content_hash_sha256="6" * 64,
                 document_title="Bariatric Surgery Request Form"),
    ]
    kept, _ = dedupe_rows(rows)
    assert len(kept) == 2


# ---------------------------------------------------------------------------
# Tier 3: normalised title
# ---------------------------------------------------------------------------
def test_title_key_strips_dates_versions_and_stopwords():
    assert title_key("Medical Policy: Bariatric Surgery (Effective 03/01/2026)") == \
        title_key("Bariatric Surgery Medical Policy - Revised 2025")


def test_title_key_is_empty_for_titles_with_no_distinguishing_content():
    """An empty key must never merge unrelated documents."""
    assert title_key("Policy") == ""
    assert title_key("2026") == ""
    assert title_key("") == ""


def test_dated_revisions_of_the_same_title_collapse():
    rows = [
        make_row(document_url="https://e.com/p/bariatric-2025.pdf",
                 document_title="Bariatric Surgery Policy 2025",
                 policy_number="", effective_date="2025-01-01",
                 content_hash_sha256="7" * 64),
        make_row(document_url="https://e.com/p/bariatric-2026.pdf",
                 document_title="Bariatric Surgery Policy - Effective 01/01/2026",
                 policy_number="", effective_date="2026-01-01",
                 content_hash_sha256="8" * 64),
    ]
    kept, report = dedupe_rows(rows)
    assert len(kept) == 1
    assert report.by_tier["title"] == 1
    assert kept[0].effective_date == "2026-01-01"


def test_different_titles_are_kept_separate():
    rows = [
        make_row(document_url="https://e.com/a.pdf", document_title="Bariatric Surgery",
                 policy_number="", content_hash_sha256="9" * 64),
        make_row(document_url="https://e.com/b.pdf", document_title="Cardiac Imaging",
                 policy_number="", content_hash_sha256="0" * 64),
    ]
    kept, _ = dedupe_rows(rows)
    assert len(kept) == 2


def test_same_title_different_file_type_is_kept():
    """A PDF and an HTML version are different artefacts of the same policy;
    the rule keeps them apart so a consumer can choose."""
    rows = [
        make_row(document_url="https://e.com/a.pdf", file_type="pdf", policy_number="",
                 content_hash_sha256="a" * 64),
        make_row(document_url="https://e.com/a.html", file_type="html", policy_number="",
                 content_hash_sha256="b" * 64),
    ]
    kept, _ = dedupe_rows(rows)
    assert len(kept) == 2


# ---------------------------------------------------------------------------
# Transitive merging and winner selection
# ---------------------------------------------------------------------------
def test_clusters_merge_transitively():
    """A shares a hash with B; B shares a title with C -> all three are one doc."""
    shared_hash = "c" * 64
    rows = [
        make_row(document_url="https://e.com/a.pdf", content_hash_sha256=shared_hash,
                 document_title="Alpha Beta Policy", policy_number=""),
        make_row(document_url="https://e.com/b.pdf", content_hash_sha256=shared_hash,
                 document_title="Alpha Beta Policy 2026", policy_number=""),
        make_row(document_url="https://e.com/c.pdf", content_hash_sha256="d" * 64,
                 document_title="Alpha Beta Policy - Revised", policy_number=""),
    ]
    kept, report = dedupe_rows(rows)
    assert len(kept) == 1
    assert report.collapsed == 2


def test_winner_prefers_newest_effective_date():
    older = make_row(document_url="https://e.com/old.pdf", effective_date="2024-01-01")
    newer = make_row(document_url="https://e.com/new.pdf", effective_date="2026-01-01")
    assert choose_winner([older, newer]) is newer
    assert choose_winner([newer, older]) is newer


def test_winner_falls_back_to_last_updated_then_confidence():
    a = make_row(document_url="https://e.com/a.pdf", effective_date="",
                 last_updated_date="2026-05-01", confidence_score=0.80)
    b = make_row(document_url="https://e.com/b.pdf", effective_date="",
                 last_updated_date="2026-01-01", confidence_score=0.95)
    assert choose_winner([a, b]) is a  # recency outranks confidence

    c = make_row(document_url="https://e.com/c.pdf", effective_date="",
                 last_updated_date="", confidence_score=0.95)
    d = make_row(document_url="https://e.com/d.pdf", effective_date="",
                 last_updated_date="", confidence_score=0.60,
                 notes="low confidence: title from URL")
    assert choose_winner([c, d]) is c


def test_winner_prefers_200_over_non_200():
    ok = make_row(document_url="https://e.com/a.pdf", effective_date="",
                  last_updated_date="", http_status=200)
    broken = make_row(document_url="https://e.com/b.pdf", effective_date="",
                      last_updated_date="", http_status=404,
                      content_hash_sha256="", confidence_score=0.25,
                      notes="not retrievable")
    assert choose_winner([broken, ok]) is ok


def test_winner_prefers_pdf_over_html_then_shorter_url():
    pdf = make_row(document_url="https://e.com/deep/path/a.pdf", file_type="pdf",
                   effective_date="", last_updated_date="")
    html = make_row(document_url="https://e.com/a.html", file_type="html",
                    effective_date="", last_updated_date="")
    assert choose_winner([html, pdf]) is pdf

    short = make_row(document_url="https://e.com/a.pdf", effective_date="",
                     last_updated_date="")
    deep = make_row(document_url="https://e.com/x/y/z/a.pdf", effective_date="",
                    last_updated_date="")
    assert choose_winner([deep, short]) is short


def test_winner_selection_is_deterministic():
    rows = [
        make_row(document_url="https://e.com/b.pdf", effective_date="",
                 last_updated_date=""),
        make_row(document_url="https://e.com/a.pdf", effective_date="",
                 last_updated_date=""),
    ]
    assert choose_winner(rows).document_url == choose_winner(list(reversed(rows))).document_url


# ---------------------------------------------------------------------------
# Invariants and reporting
# ---------------------------------------------------------------------------
def test_document_url_is_unique_after_dedupe():
    """The schema's one hard cross-row constraint."""
    rows = [make_row(document_url=f"https://e.com/p/{index}.pdf",
                     document_title=f"Policy Number {index} Coverage",
                     policy_number=f"MP-{index:03d}",
                     content_hash_sha256=f"{index:064d}")
            for index in range(20)]
    rows += rows[:5]  # exact duplicates
    kept, _ = dedupe_rows(rows)
    urls = [row.document_url for row in kept]
    assert len(urls) == len(set(urls)) == 20


def test_collapse_is_recorded_in_the_winner_notes():
    """A reviewer must be able to see that a merge happened and verify it."""
    rows = [
        make_row(document_url="https://e.com/a.pdf", content_hash_sha256="e" * 64),
        make_row(document_url="https://e.com/b.pdf", content_hash_sha256="e" * 64),
    ]
    kept, _ = dedupe_rows(rows)
    assert "collapsed 1 near-duplicate" in kept[0].notes
    assert "https://e.com/" in kept[0].notes


def test_input_order_of_survivors_is_preserved():
    rows = [
        make_row(document_url="https://e.com/1.pdf", document_title="Cardiac Imaging",
                 policy_number="P1", content_hash_sha256="1" * 64),
        make_row(document_url="https://e.com/2.pdf", document_title="Bariatric Surgery",
                 policy_number="P2", content_hash_sha256="2" * 64),
        make_row(document_url="https://e.com/3.pdf", document_title="Sleep Studies",
                 policy_number="P3", content_hash_sha256="3" * 64),
    ]
    kept, _ = dedupe_rows(rows)
    assert [row.document_url for row in kept] == [row.document_url for row in rows]


def test_empty_input_is_handled():
    kept, report = dedupe_rows([])
    assert kept == []
    assert report.collapsed == 0


def test_rule_description_documents_every_tier():
    """NOTES.md quotes this text, so it must actually describe the implementation."""
    text = rule_description()
    for expected in ("canonicalised URL", "content_hash_sha256", "policy_number",
                     "normalised title", "union-find", "effective_date"):
        assert expected in text
