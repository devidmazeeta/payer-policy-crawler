"""
Canonical output schema for the Payer Policy Document Discovery dataset.

This module is the single source of truth for:

* ``COLUMNS``  - the exact 22 column names, in the exact order required by the
  case study. ``storage.py`` writes this tuple verbatim as the CSV/XLSX header
  row, so the order declared here *is* the delivered order.
* the controlled vocabularies (the "enums" sheet of ``schema_dictionary.xlsx``),
* the value formatting conventions (the "conventions" sheet): ISO-8601 dates,
  ``Y``/``N`` booleans, two-decimal confidence scores, lowercase hex hashes,
  plain integers, and - importantly - the rule that an unknown optional value is
  the *empty string*, never the literal text ``None``/``null``/``N/A``/``-``/``TBD``.

Every row that leaves the crawler passes through :func:`validate_row`, so a
malformed record is caught at the process boundary rather than discovered by a
reviewer reading output.csv.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# 1. The 22 columns - EXACT names, EXACT order. Do not reorder or extend.
# ---------------------------------------------------------------------------
COLUMNS: tuple[str, ...] = (
    "payer_name",
    "payer_alias",
    "state_or_region",
    "line_of_business",
    "document_title",
    "document_type",
    "document_url",
    "source_page_url",
    "discovery_path",
    "file_type",
    "policy_number",
    "effective_date",
    "last_updated_date",
    "http_status",
    "content_hash_sha256",
    "file_size_bytes",
    "requires_auth",
    "render_mode",
    "extraction_method",
    "confidence_score",
    "scrape_timestamp_utc",
    "notes",
)

# ---------------------------------------------------------------------------
# 2. Controlled vocabularies ("enums" sheet). Anything outside these sets is
#    reported by validate_row(); we would rather flag or drop a row than ship a
#    value a downstream consumer cannot interpret.
# ---------------------------------------------------------------------------
LINE_OF_BUSINESS = frozenset(
    {"Commercial", "Medicare", "Medicaid", "Exchange", "Federal", "Multiple", "Unknown"}
)

DOCUMENT_TYPE = frozenset(
    {
        "medical_policy",
        "pharmacy_policy",
        "coverage_guideline",
        "pa_form",
        "pa_list",
        "formulary",
        "drug_list",
        "provider_manual",
        "bulletin",
        "other",
    }
)

FILE_TYPE = frozenset({"html", "pdf", "doc", "docx", "xls", "xlsx", "other"})

RENDER_MODE = frozenset({"static", "headless", "api"})

# extraction_method is pipe-separated when several techniques contributed to a
# single row, e.g. "sitemap|css_selector" == located via a sitemap, title read
# out of the HTML with a CSS selector.
EXTRACTION_METHOD = frozenset(
    {
        "css_selector",
        "xpath",
        "regex",
        "json_api",
        "sitemap",
        "pdf_text",
        "pdf_annotation",
        "url_pattern",
        "manual",
    }
)

# Two-letter USPS codes (50 states + DC + the five territories) plus the
# sentinel "National" for payer-wide documents. state_or_region may be
# pipe-separated, e.g. "PA|WV|DE|NY" for Highmark's four-state footprint.
USPS_CODES = frozenset(
    """AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS
    MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY
    PR VI GU AS MP""".split()
)
STATE_OR_REGION_EXTRA = frozenset({"National"})
STATE_OR_REGION = frozenset(USPS_CODES | STATE_OR_REGION_EXTRA)

# Placeholder strings the conventions sheet explicitly forbids. clean_text()
# normalises these to "" so they can never reach the CSV.
FORBIDDEN_PLACEHOLDERS = frozenset(
    {"none", "null", "n/a", "na", "-", "--", "tbd", "nan", "not applicable"}
)

# Invisible / exotic-whitespace Unicode characters that infest payer HTML and
# would otherwise be carried into document_title verbatim. Declared as integer
# codepoints so this source file stays pure ASCII, and applied via str.translate
# (a single C-level pass instead of one str.replace per character).
_ZERO_WIDTH_CODEPOINTS = (
    0x200B,  # zero-width space
    0x200C,  # zero-width non-joiner
    0x200D,  # zero-width joiner
    0x200E,  # left-to-right mark
    0x200F,  # right-to-left mark
    0xFEFF,  # BOM / zero-width no-break space
    0x00AD,  # soft hyphen
)
_UNICODE_SPACE_CODEPOINTS = (
    0x00A0,  # no-break space
    0x1680,  # ogham space mark
    0x2007,  # figure space
    0x202F,  # narrow no-break space
    0x205F,  # medium mathematical space
    0x3000,  # ideographic space
    0x2028,  # line separator
    0x2029,  # paragraph separator
)
#: Translation table: zero-width marks are deleted, space-likes are folded to a
#: plain space so the generic ``\s+`` collapse in clean_text() can see them.
_WHITESPACE_TABLE: dict[int, str | None] = {
    **{cp: None for cp in _ZERO_WIDTH_CODEPOINTS},
    **{cp: " " for cp in _UNICODE_SPACE_CODEPOINTS},
}

# ISO-8601 calendar date, e.g. 2026-08-19. No time part, no other format.
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# UTC timestamp to the second with a mandatory Z suffix, e.g. 2026-08-19T09:41:12Z.
ISO_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
# Lowercase hex SHA-256.
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# confidence_score serialised with exactly two decimals.
CONFIDENCE_RE = re.compile(r"^(0\.\d{2}|1\.00)$")

#: Confidence at or below this value must be justified in ``notes`` (conventions
#: sheet: "Explain anything below 0.70 in notes").
CONFIDENCE_EXPLAIN_THRESHOLD = 0.70


class SchemaError(ValueError):
    """Raised when a row cannot be coerced into the canonical 22-column schema."""


# ---------------------------------------------------------------------------
# 3. Formatting helpers. Every writer path funnels through these, so each
#    convention is implemented in exactly one place.
# ---------------------------------------------------------------------------
def utc_now_iso() -> str:
    """Return the current UTC instant as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_text(value: Any, *, collapse_ws: bool = True) -> str:
    """
    Coerce an arbitrary value into a schema-safe string.

    ``None``, float NaN and every forbidden placeholder literal collapse to
    ``""``. Invisible Unicode characters are removed and runs of whitespace are
    folded to single spaces so that a title scraped out of a table cell does not
    carry page layout artefacts (or embedded newlines, which would break the CSV
    record) into the dataset.
    """
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value)
    text = text.translate(_WHITESPACE_TABLE)
    if collapse_ws:
        text = re.sub(r"\s+", " ", text)
    text = text.strip()
    if text.lower() in FORBIDDEN_PLACEHOLDERS:
        return ""
    return text


def sha256_hex(raw: bytes) -> str:
    """
    Lowercase hex SHA-256 of the *raw* response bytes.

    Deliberately takes ``bytes`` and not ``str``: the schema requires the hash of
    the body as it came off the wire, before any decoding, decompression at the
    application layer, or HTML/PDF parsing.
    """
    return hashlib.sha256(raw).hexdigest()


def format_confidence(value: Any) -> str:
    """Clamp to ``[0.00, 1.00]`` and render with exactly two decimals."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = 0.0
    if math.isnan(num):
        num = 0.0
    num = min(1.0, max(0.0, num))
    return f"{num:.2f}"


def format_int(value: Any) -> str:
    """Render a plain integer (no thousands separators); ``""`` when unknown."""
    if value is None or value == "":
        return ""
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return ""


def format_bool_yn(value: Any) -> str:
    """Render the single-character boolean the schema mandates: ``Y`` or ``N``."""
    if isinstance(value, str):
        upper = value.strip().upper()
        if upper in {"Y", "N"}:
            return upper
        return "Y" if upper in {"YES", "TRUE", "1"} else "N"
    return "Y" if bool(value) else "N"


def normalise_iso_date(value: Any) -> str:
    """
    Normalise a date *as printed on the source* to ``YYYY-MM-DD``.

    Accepts the handful of shapes payers actually publish (ISO, US slash form,
    long and abbreviated month names, and month-year with no day). Returns ``""``
    for anything unparseable: we never substitute the crawl date for a missing
    effective date, because that silently fabricates provenance.

    A month-year source value ("January 2026") is anchored to the first of the
    month; callers lower ``confidence_score`` and say so in ``notes`` when they
    rely on that, since the day component is inferred rather than printed.
    """
    text = clean_text(value)
    if not text:
        return ""
    if ISO_DATE_RE.match(text):
        # Reject impossible dates such as 2026-13-45 that match the shape.
        try:
            datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            return ""
        return text

    # Strip label noise, e.g. "Effective: 03/01/2026" -> "03/01/2026".
    text = re.sub(
        r"^(effective(\s+date)?|revised|reviewed|updated|last\s+updated"
        r"|last\s+reviewed|published|date)\b[:\s]*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    text = text.rstrip(".,;")

    day_formats = (
        "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%d %B %Y", "%d %b %Y",
        "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y", "%Y/%m/%d", "%Y%m%d",
        "%d-%b-%Y", "%d-%B-%Y",
    )
    for fmt in day_formats:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    for fmt in ("%B %Y", "%b %Y", "%m/%Y", "%Y-%m"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def normalise_multi_enum(value: Any, allowed: Iterable[str]) -> str:
    """
    Normalise a pipe-separated multi-value enum field.

    Input order is preserved and duplicates are dropped, so ``"PA|PA|WV"``
    becomes ``"PA|WV"``. Members outside *allowed* are discarded, which keeps the
    field either valid or empty - never valid-looking but wrong.
    """
    allowed_set = set(allowed)
    text = clean_text(value)
    if not text:
        return ""
    kept: list[str] = []
    for part in text.split("|"):
        part = part.strip()
        if part and part in allowed_set and part not in kept:
            kept.append(part)
    return "|".join(kept)


# ---------------------------------------------------------------------------
# 4. The row model.
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class DocumentRow:
    """
    One output record == one discovered document URL.

    Field names and declaration order mirror :data:`COLUMNS` exactly. Every field
    defaults to the schema's "unknown" representation so that a partially
    discovered document - for instance a transport-level failure carrying
    ``http_status=0`` - is still a legal row. Non-200 rows are explicitly wanted
    by the case study, so they must be representable without hacks.

    ``local_path`` and ``content_text`` are internal bookkeeping (see
    :data:`INTERNAL_FIELDS`): they travel with the row inside the process but are
    stripped by :meth:`to_output_dict` and never appear as schema columns.
    """

    payer_name: str = ""
    payer_alias: str = ""
    state_or_region: str = ""
    line_of_business: str = "Unknown"
    document_title: str = ""
    document_type: str = "other"
    document_url: str = ""
    source_page_url: str = ""
    discovery_path: str = ""
    file_type: str = "other"
    policy_number: str = ""
    effective_date: str = ""
    last_updated_date: str = ""
    http_status: Any = 0
    content_hash_sha256: str = ""
    file_size_bytes: Any = ""
    requires_auth: Any = "N"
    render_mode: str = "static"
    extraction_method: str = "sitemap"
    confidence_score: Any = 0.50
    scrape_timestamp_utc: str = field(default_factory=utc_now_iso)
    notes: str = ""

    # --- internal, non-schema ------------------------------------------------
    local_path: str = ""
    content_text: str = ""

    def to_output_dict(self) -> dict[str, str]:
        """Project the row onto the 22 schema columns as already-formatted strings."""
        return {name: str(getattr(self, name)) for name in COLUMNS}


#: Dataclass fields carried for internal use that must never be written out.
INTERNAL_FIELDS = frozenset({"local_path", "content_text"})


def normalise_row(row: DocumentRow) -> DocumentRow:
    """
    Apply every formatting convention to *row*, in place, and return it.

    This is the last chance to make a row schema-legal, and it is idempotent, so
    it is safe to call on rows rehydrated from the resume checkpoint. It does not
    invent data: fields it cannot normalise become the empty string, which
    :func:`validate_row` then reports if the field was required.
    """
    row.payer_name = clean_text(row.payer_name)
    row.payer_alias = clean_text(row.payer_alias)
    row.state_or_region = normalise_multi_enum(row.state_or_region, STATE_OR_REGION)

    lob = clean_text(row.line_of_business)
    row.line_of_business = lob if lob in LINE_OF_BUSINESS else "Unknown"

    # Titles are truncated defensively: some payer PDFs have a "title" that is
    # really the first paragraph of the document.
    title = clean_text(row.document_title)
    row.document_title = title[:500]

    dtype = clean_text(row.document_type)
    row.document_type = dtype if dtype in DOCUMENT_TYPE else "other"

    row.document_url = clean_text(row.document_url, collapse_ws=False).replace(" ", "%20")
    row.source_page_url = clean_text(row.source_page_url, collapse_ws=False)
    row.discovery_path = clean_text(row.discovery_path)

    ftype = clean_text(row.file_type).lower()
    row.file_type = ftype if ftype in FILE_TYPE else "other"

    row.policy_number = clean_text(row.policy_number)
    row.effective_date = normalise_iso_date(row.effective_date)
    row.last_updated_date = normalise_iso_date(row.last_updated_date)

    row.http_status = format_int(row.http_status) or "0"
    row.requires_auth = format_bool_yn(row.requires_auth)

    rmode = clean_text(row.render_mode).lower()
    row.render_mode = rmode if rmode in RENDER_MODE else "static"

    row.extraction_method = normalise_multi_enum(row.extraction_method, EXTRACTION_METHOD)

    row.content_hash_sha256 = clean_text(row.content_hash_sha256).lower()
    row.file_size_bytes = format_int(row.file_size_bytes)

    # --- cross-field consistency rules from the conventions sheet ------------
    # A hash may only be present for a genuine 200 response, and a row we marked
    # requires_auth=Y must have an empty hash because we never logged in to get
    # the bytes.
    if row.http_status != "200" or row.requires_auth == "Y":
        row.content_hash_sha256 = ""
        # file_size_bytes describes the *document*. On a non-200 the bytes we
        # received are an error page or a login form, so reporting their length
        # would be actively misleading - a 404 row would claim a 59 KB document.
        # Unknown means the empty string.
        row.file_size_bytes = ""

    row.confidence_score = format_confidence(row.confidence_score)
    ts = clean_text(row.scrape_timestamp_utc)
    row.scrape_timestamp_utc = ts if ISO_TS_RE.match(ts) else utc_now_iso()
    # Notes are free text but must not contain raw newlines: keep one CSV record
    # per row so the file stays trivially greppable.
    row.notes = clean_text(row.notes)
    return row


def validate_row(row: DocumentRow, *, strict: bool = False) -> list[str]:
    """
    Validate an already-normalised row and return a list of human-readable problems.

    An empty list means the row is schema-conformant. With ``strict=True`` the
    first problem is raised as :class:`SchemaError` instead - used by the unit
    tests and by ``--strict-schema`` on the CLI, where shipping a bad row is
    worse than failing loudly.
    """
    problems: list[str] = []

    if not row.payer_name:
        problems.append("payer_name is required")
    if not row.document_url:
        problems.append("document_url is required")
    elif not re.match(r"^https?://", str(row.document_url)):
        problems.append(f"document_url must be absolute http(s): {row.document_url!r}")
    if not row.discovery_path:
        problems.append("discovery_path is required (reviewer must be able to reproduce the find)")

    if row.line_of_business not in LINE_OF_BUSINESS:
        problems.append(f"line_of_business {row.line_of_business!r} outside enum")
    if row.document_type not in DOCUMENT_TYPE:
        problems.append(f"document_type {row.document_type!r} outside enum")
    if row.file_type not in FILE_TYPE:
        problems.append(f"file_type {row.file_type!r} outside enum")
    if row.render_mode not in RENDER_MODE:
        problems.append(f"render_mode {row.render_mode!r} outside enum")

    for part in str(row.extraction_method).split("|"):
        if part and part not in EXTRACTION_METHOD:
            problems.append(f"extraction_method member {part!r} outside enum")
    if not row.extraction_method:
        problems.append("extraction_method is required")

    for part in str(row.state_or_region).split("|"):
        if part and part not in STATE_OR_REGION:
            problems.append(f"state_or_region member {part!r} is not a USPS code or 'National'")

    for name in ("effective_date", "last_updated_date"):
        value = str(getattr(row, name))
        if value and not ISO_DATE_RE.match(value):
            problems.append(f"{name} {value!r} is not ISO-8601 YYYY-MM-DD")

    if str(row.requires_auth) not in {"Y", "N"}:
        problems.append(f"requires_auth {row.requires_auth!r} must be exactly 'Y' or 'N'")

    status_text = str(row.http_status)
    if not re.match(r"^\d+$", status_text):
        problems.append(f"http_status {row.http_status!r} must be a plain integer")
    else:
        status = int(status_text)
        digest = str(row.content_hash_sha256)
        if status == 200 and str(row.requires_auth) == "N":
            if not digest:
                problems.append("content_hash_sha256 is required for a 200 response")
            elif not SHA256_RE.match(digest):
                problems.append("content_hash_sha256 must be lowercase hex sha-256")
        elif digest:
            problems.append(
                "content_hash_sha256 must be empty when http_status != 200 or requires_auth=Y"
            )

    size_text = str(row.file_size_bytes)
    if size_text and not re.match(r"^\d+$", size_text):
        problems.append(f"file_size_bytes {row.file_size_bytes!r} must be a plain integer")

    conf_text = str(row.confidence_score)
    if not CONFIDENCE_RE.match(conf_text):
        problems.append(f"confidence_score {conf_text!r} must be 0.00-1.00 with two decimals")
    elif float(conf_text) < CONFIDENCE_EXPLAIN_THRESHOLD and not row.notes:
        problems.append(f"confidence_score {conf_text} < 0.70 requires an explanation in notes")

    if not ISO_TS_RE.match(str(row.scrape_timestamp_utc)):
        problems.append("scrape_timestamp_utc must be UTC ISO-8601 to the second with a Z suffix")

    for name in COLUMNS:
        value = str(getattr(row, name))
        if value.lower() in FORBIDDEN_PLACEHOLDERS and value != "":
            problems.append(f"{name} uses forbidden placeholder {value!r}; use an empty string")

    if strict and problems:
        raise SchemaError(f"{row.document_url}: " + "; ".join(problems))
    return problems
