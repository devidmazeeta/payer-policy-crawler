"""
Schema tests: the 22 columns, the enums and every formatting convention.

These are the highest-value tests in the suite, because a schema violation is
the one class of bug that silently produces a *plausible-looking* but wrong
deliverable.
"""

from __future__ import annotations

import pytest

from crawler.extractor import assert_enums_consistent
from crawler.schema import (
    COLUMNS,
    DOCUMENT_TYPE,
    FILE_TYPE,
    LINE_OF_BUSINESS,
    RENDER_MODE,
    STATE_OR_REGION,
    DocumentRow,
    SchemaError,
    clean_text,
    format_bool_yn,
    format_confidence,
    format_int,
    normalise_iso_date,
    normalise_multi_enum,
    normalise_row,
    sha256_hex,
    utc_now_iso,
    validate_row,
)

from .conftest import make_row


# ---------------------------------------------------------------------------
# Column contract
# ---------------------------------------------------------------------------
def test_exact_22_columns_in_exact_order():
    """The column list is the deliverable's contract; assert it literally."""
    expected = [
        "payer_name", "payer_alias", "state_or_region", "line_of_business",
        "document_title", "document_type", "document_url", "source_page_url",
        "discovery_path", "file_type", "policy_number", "effective_date",
        "last_updated_date", "http_status", "content_hash_sha256",
        "file_size_bytes", "requires_auth", "render_mode", "extraction_method",
        "confidence_score", "scrape_timestamp_utc", "notes",
    ]
    assert list(COLUMNS) == expected
    assert len(COLUMNS) == 22


def test_output_dict_has_only_schema_columns():
    """Internal bookkeeping (local_path, content_text) must never be written out."""
    row = make_row()
    row.local_path = r"C:\downloads\thp\policy.pdf"
    row.content_text = "some extracted text"
    output = row.to_output_dict()
    assert list(output) == list(COLUMNS)
    assert "local_path" not in output
    assert "content_text" not in output


# ---------------------------------------------------------------------------
# Formatting conventions
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-03-01", "2026-03-01"),
        ("03/01/2026", "2026-03-01"),
        ("3/1/2026", "2026-03-01"),
        ("March 1, 2026", "2026-03-01"),
        ("Mar 1, 2026", "2026-03-01"),
        ("1 March 2026", "2026-03-01"),
        ("Effective: 03/01/2026", "2026-03-01"),
        ("Last Updated: 2026-03-01", "2026-03-01"),
        ("January 2026", "2026-01-01"),   # month-year anchors to the 1st
        ("", ""),
        (None, ""),
        ("not a date", ""),
        ("2026-13-45", ""),               # shape matches but is not a real date
        ("N/A", ""),                      # forbidden placeholder -> empty
    ],
)
def test_normalise_iso_date(raw, expected):
    assert normalise_iso_date(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [(True, "Y"), (False, "N"), ("Y", "Y"), ("n", "N"), ("yes", "Y"),
     ("true", "Y"), ("", "N"), ("anything else", "N")],
)
def test_format_bool_yn_is_single_character(raw, expected):
    result = format_bool_yn(raw)
    assert result == expected
    assert len(result) == 1


@pytest.mark.parametrize(
    "raw,expected",
    [(0.9, "0.90"), (1.0, "1.00"), (0, "0.00"), (0.856, "0.86"),
     (1.5, "1.00"), (-2, "0.00"), ("0.7", "0.70"), ("garbage", "0.00")],
)
def test_format_confidence_two_decimals_clamped(raw, expected):
    assert format_confidence(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [(1234567, "1234567"), ("42", "42"), (0, "0"), ("", ""), (None, ""),
     ("1,234", "")],
)
def test_format_int_has_no_separators(raw, expected):
    assert format_int(raw) == expected


@pytest.mark.parametrize("placeholder", ["None", "null", "N/A", "-", "TBD", "nan", "NA"])
def test_forbidden_placeholders_become_empty_string(placeholder):
    """The conventions sheet forbids these literals; they must normalise to ""."""
    assert clean_text(placeholder) == ""


def test_clean_text_strips_invisible_unicode_and_collapses_whitespace():
    messy = "Bariatric" + chr(0x00A0) + "Surgery" + chr(0x200B) + "\n\t  Policy"
    assert clean_text(messy) == "Bariatric Surgery Policy"


def test_clean_text_removes_newlines_so_csv_records_stay_single_line():
    assert "\n" not in clean_text("line one\nline two")


def test_sha256_is_lowercase_hex_of_raw_bytes():
    digest = sha256_hex(b"%PDF-1.7 fake pdf bytes")
    assert len(digest) == 64
    assert digest == digest.lower()
    assert all(char in "0123456789abcdef" for char in digest)


def test_utc_timestamp_shape():
    stamp = utc_now_iso()
    assert stamp.endswith("Z")
    assert len(stamp) == 20  # YYYY-MM-DDTHH:MM:SSZ


def test_normalise_multi_enum_dedupes_and_filters():
    assert normalise_multi_enum("PA|PA|WV|ZZ", STATE_OR_REGION) == "PA|WV"
    assert normalise_multi_enum("", STATE_OR_REGION) == ""
    assert normalise_multi_enum("National", STATE_OR_REGION) == "National"


# ---------------------------------------------------------------------------
# Enum enforcement
# ---------------------------------------------------------------------------
def test_out_of_enum_values_are_coerced_or_reported():
    row = make_row(line_of_business="Dental", document_type="press_release",
                   file_type="jpeg", render_mode="browser")
    normalise_row(row)
    # Normalisation coerces to the enum's defined fallbacks rather than shipping
    # an illegal value.
    assert row.line_of_business == "Unknown"
    assert row.document_type == "other"
    assert row.file_type == "other"
    assert row.render_mode == "static"
    assert validate_row(row) == []


def test_enum_membership_matches_the_spec():
    assert LINE_OF_BUSINESS == {"Commercial", "Medicare", "Medicaid", "Exchange",
                                "Federal", "Multiple", "Unknown"}
    assert DOCUMENT_TYPE == {"medical_policy", "pharmacy_policy", "coverage_guideline",
                             "pa_form", "pa_list", "formulary", "drug_list",
                             "provider_manual", "bulletin", "other"}
    assert FILE_TYPE == {"html", "pdf", "doc", "docx", "xls", "xlsx", "other"}
    assert RENDER_MODE == {"static", "headless", "api"}


def test_extractor_classifiers_only_produce_enum_values():
    """Guards against a typo in the classification tables."""
    assert_enums_consistent()


def test_state_or_region_rejects_non_usps_codes():
    row = make_row(state_or_region="XX")
    normalise_row(row)
    assert row.state_or_region == ""
    row = make_row(state_or_region="PA|WV|DE|NY")
    normalise_row(row)
    assert row.state_or_region == "PA|WV|DE|NY"
    assert validate_row(row) == []


# ---------------------------------------------------------------------------
# Cross-field rules
# ---------------------------------------------------------------------------
def test_hash_is_cleared_for_non_200_rows():
    """content_hash_sha256 may only be present for a genuine 200."""
    row = make_row(http_status=404, content_hash_sha256="b" * 64)
    normalise_row(row)
    assert row.content_hash_sha256 == ""
    assert validate_row(row) == []


def test_hash_is_cleared_when_requires_auth_is_yes():
    """We never log in to fetch bytes, so a gated row cannot carry a hash."""
    row = make_row(requires_auth="Y", content_hash_sha256="c" * 64,
                   notes="behind a portal login")
    normalise_row(row)
    assert row.requires_auth == "Y"
    assert row.content_hash_sha256 == ""
    assert validate_row(row) == []


def test_200_row_without_hash_is_reported():
    row = make_row(content_hash_sha256="")
    problems = validate_row(row)
    assert any("content_hash_sha256 is required" in problem for problem in problems)


def test_uppercase_hash_is_lowercased():
    row = make_row(content_hash_sha256="A" * 64)
    normalise_row(row)
    assert row.content_hash_sha256 == "a" * 64
    assert validate_row(row) == []


def test_transport_failure_row_is_valid_with_status_zero():
    """http_status=0 means a DNS/TLS/timeout failure; such rows are wanted."""
    row = make_row(
        http_status=0, content_hash_sha256="", file_size_bytes="",
        confidence_score=0.25, notes="transport-level failure: ConnectTimeout",
    )
    normalise_row(row)
    assert row.http_status == "0"
    assert validate_row(row) == []


def test_low_confidence_requires_notes():
    row = make_row(confidence_score=0.55, notes="")
    problems = validate_row(row)
    assert any("requires an explanation in notes" in problem for problem in problems)

    row = make_row(confidence_score=0.55, notes="title derived from the URL filename")
    assert validate_row(row) == []


def test_missing_required_fields_are_reported():
    row = make_row(document_url="", discovery_path="", payer_name="")
    problems = validate_row(row)
    joined = " ".join(problems)
    assert "payer_name is required" in joined
    assert "document_url is required" in joined
    assert "discovery_path is required" in joined


def test_relative_document_url_is_rejected():
    row = make_row(document_url="/policies/bariatric.pdf")
    problems = validate_row(row)
    assert any("must be absolute" in problem for problem in problems)


def test_strict_mode_raises():
    row = make_row(document_url="")
    with pytest.raises(SchemaError):
        validate_row(row, strict=True)


def test_extraction_method_accepts_pipe_separated_members():
    row = make_row(extraction_method="sitemap|css_selector|pdf_text")
    normalise_row(row)
    assert validate_row(row) == []

    row = make_row(extraction_method="sitemap|telepathy")
    normalise_row(row)
    # The illegal member is dropped, leaving a legal field.
    assert row.extraction_method == "sitemap"


def test_normalise_row_is_idempotent():
    """Rows rehydrated from the checkpoint get re-normalised; it must be stable."""
    row = make_row()
    first = normalise_row(row).to_output_dict()
    second = normalise_row(row).to_output_dict()
    assert first == second


def test_defaults_produce_a_representable_row():
    """A bare DocumentRow must be constructible without raising."""
    row = DocumentRow()
    normalise_row(row)
    problems = validate_row(row)
    # It is incomplete (no URL/payer), but it is representable and the problems
    # are reported rather than crashing.
    assert isinstance(problems, list)
    assert problems
