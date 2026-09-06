"""
Turning fetched bytes into schema rows: link extraction, metadata extraction,
classification and confidence scoring.

Three jobs live here:

1. **Link extraction** (:func:`extract_links`) - pull anchors out of an index
   page, resolved to absolute URLs, including cross-directory targets.
2. **Metadata extraction** - title, policy number, effective / last-updated
   dates, line of business and state, read out of HTML or PDF text. Dates are
   taken *as printed on the source*; nothing is ever back-filled with the crawl
   date, which would fabricate provenance.
3. **Classification and scoring** - map a document onto the ``document_type``
   enum and assign a ``confidence_score`` that honestly reflects how much of the
   row was read versus inferred. Anything below 0.70 gets its reason written into
   ``notes``, as the conventions sheet requires.

Parser choice: BeautifulSoup with lxml when available, falling back to Python's
``html.parser``, and finally to a regex sweep. Payer HTML is frequently invalid
enough to defeat a strict parser, and losing a whole index page to one unclosed
tag would cost real documents.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Iterable
from urllib.parse import urljoin, urlsplit

from .schema import (
    DOCUMENT_TYPE,
    LINE_OF_BUSINESS,
    STATE_OR_REGION,
    DocumentRow,
    clean_text,
    normalise_iso_date,
    normalise_multi_enum,
)

if TYPE_CHECKING:  # pragma: no cover - imports for type checkers only
    from .discovery import Candidate
    from .fetcher import FetchResult
    from .seeds import Payer

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - exercised only in minimal installs
    BeautifulSoup = None  # type: ignore[assignment]

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover
    PdfReader = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# URL-shape heuristics
# ---------------------------------------------------------------------------
#: Extensions that are documents for our purposes.
DOCUMENT_EXTENSIONS: tuple[str, ...] = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".rtf")

#: Extensions that are never a policy document. Filtering these before fetching
#: saves a large fraction of the request budget on asset-heavy payer sites.
NEGATIVE_URL_HINTS: tuple[str, ...] = (
    ".css", ".js", ".mjson", ".json", ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".webp", ".ico", ".woff", ".woff2", ".ttf", ".eot", ".otf", ".mp4", ".mp3",
    ".webm", ".mov", ".avi", ".zip", ".exe", ".dmg", ".rss", ".atom", ".map",
)

#: Query-string markers of a CMS document handler. Payers commonly serve PDFs
#: from an extension-less route, so a URL without a suffix can still be a doc.
DOCUMENT_QUERY_HINTS: tuple[str, ...] = (
    "download", "attachment", "getfile", "getdocument", "docid", "fileid",
    "assetid", "contentid", "streamfile", "viewfile", "openfile", "blobid",
)

#: Anchor-text phrases that mark a *listing* page rather than a document.
INDEX_TEXT_HINTS: tuple[str, ...] = (
    "policies", "policy list", "guidelines", "medical policy", "clinical policy",
    "coverage policies", "policy index", "view all", "see all", "browse",
    "a-z", "a to z", "index", "library", "search policies", "all policies",
    "policy updates", "bulletins", "newsletters", "provider manual",
    "prior authorization", "formulary", "drug list", "pharmacy policy",
)

# ---------------------------------------------------------------------------
# Classification vocabulary. Order matters: the first matching entry wins, so
# the more specific document types are listed before the broader ones.
# ---------------------------------------------------------------------------
#: ``(document_type, (keyword, ...))`` matched against the URL and the title.
_TYPE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pa_list", ("prior authorization list", "pa list", "precertification list",
                 "prior auth list", "authorization requirements list",
                 "services requiring prior authorization", "pa-list", "palist",
                 "prior-authorization-list", "code list", "cpt code list")),
    ("pa_form", ("prior authorization form", "pa form", "request form",
                 "authorization form", "precertification request",
                 "prior-authorization-form", "pa-form", "prior auth form",
                 "exception request", "coverage determination request",
                 "predetermination form", "referral form", "appeal form")),
    ("formulary", ("formulary", "preferred drug list", "pdl", "covered drug list",
                   "drug formulary", "comprehensive formulary")),
    ("drug_list", ("drug list", "specialty drug", "medication list",
                   "self-administered drug", "drug-list", "step therapy list",
                   "quantity limit")),
    ("pharmacy_policy", ("pharmacy policy", "pharmacy medical necessity",
                         "drug policy", "medication policy", "pharmacy-polic",
                         "drug-polic", "pharmacy guideline", "specialty pharmacy")),
    ("provider_manual", ("provider manual", "provider administrative manual",
                         "administrative guide", "provider guide",
                         "provider reference", "care provider manual",
                         "provider-manual", "administrative-guide", "handbook",
                         "provider administrative guide")),
    ("bulletin", ("bulletin", "newsletter", "network notification", "provider update",
                  "policy update", "medical policy update", "announcement",
                  "provider notice", "network bulletin", "provider news",
                  "policy-update", "notification")),
    ("coverage_guideline", ("coverage guideline", "coverage determination",
                            "utilization management guideline", "um guideline",
                            "clinical guideline", "clinical criteria",
                            "coverage-guideline", "clinical-guideline",
                            "medical necessity criteria", "coverage summary",
                            "clinical utilization management")),
    ("medical_policy", ("medical policy", "clinical policy", "coverage policy",
                        "medical coverage", "payment policy", "reimbursement policy",
                        "administrative policy", "medical-polic", "clinical-polic",
                        "coverage-polic", "policy", "coverage rationale")),
)

#: Line-of-business keywords. "Multiple" is inferred when two or more distinct
#: lines are named, which is the honest answer for a combined policy document.
_LOB_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Medicare", ("medicare", "medicare advantage", " ma ", "part d", "part b",
                  "dual eligible", "d-snp", "dsnp", "c-snp", "chronic snp", "pdp")),
    ("Medicaid", ("medicaid", "chip ", "managed medicaid", "medi-cal", "husky",
                  "familycare", "healthchoice", "community plan", "star kids",
                  "long-term services and supports", "ltss")),
    ("Exchange", ("exchange", "marketplace", "aca ", "individual and family",
                  "qhp", "on-exchange", "off-exchange", "healthcare.gov")),
    ("Federal", ("federal employee", "fehb", "fep ", "federal employee program",
                 "tricare", "veterans", "va community care")),
    ("Commercial", ("commercial", "employer group", "group health", "self-funded",
                    "fully insured", "aso ", "administrative services only",
                    "small group", "large group", "ppo", "hmo", "epo")),
)

#: Policy-number shapes actually seen across these ten payers. Tried in order;
#: the first hit wins, and the pattern that matched is remembered so the
#: extraction_method can honestly say "regex".
_POLICY_NUMBER_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Explicitly labelled, which is the only fully reliable form.
    re.compile(r"\b(?:policy|guideline|bulletin|document|coverage)\s*(?:number|no\.?|#|id)"
               r"\s*[:\-]?\s*([A-Z0-9][A-Z0-9._\-/]{2,24})", re.IGNORECASE),
    re.compile(r"\b(?:policy|number)\s*[:\-]\s*([A-Z]{1,5}[\-_. ]?\d{2,8}[A-Z0-9.\-]{0,8})",
               re.IGNORECASE),
    # Common unlabelled shapes, e.g. CS123.045, MP-042, 2024-0031, PHARM.00012.
    re.compile(r"\b((?:CS|MP|CP|CPG|UM|MED|PHARM|DRUG|ADMIN|CLPD|MCP|MPP|SURG|LAB|RAD|"
               r"TRANS|GENE|DME|BEH)[\-_. ]?\d{2,6}(?:\.\d{1,4})?)\b"),
    re.compile(r"\b(\d{4}-\d{3,5})\b"),
)

#: Effective / last-updated date labels. Each pattern captures the date text so
#: :func:`normalise_iso_date` can decide whether it is parseable.
_DATE_VALUE = (
    r"((?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4})"
    r"|(?:\d{4}-\d{2}-\d{2})"
    r"|(?:(?:January|February|March|April|May|June|July|August|September|October"
    r"|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+"
    r"\d{1,2},?\s+\d{4})"
    r"|(?:\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September"
    r"|October|November|December)\s+\d{4}))"
)
_EFFECTIVE_DATE_RE = re.compile(
    r"(?:effective|effective\s+date|date\s+effective|in\s+effect)\b\s*(?:date)?\s*[:\-]?\s*"
    + _DATE_VALUE,
    re.IGNORECASE,
)
_UPDATED_DATE_RE = re.compile(
    r"(?:last\s+(?:updated|revised|reviewed|review)|revised|revision\s+date|updated"
    r"|reviewed|last\s+modified|approval\s+date|publish(?:ed)?\s+date)\b\s*(?:date)?\s*"
    r"[:\-]?\s*" + _DATE_VALUE,
    re.IGNORECASE,
)

#: Anchor extraction fallback when no HTML parser is importable.
_ANCHOR_RE = re.compile(
    r"<a\b[^>]*?href\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)[^>]*>(.*?)</a\s*>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_TITLE_TAG_RE = re.compile(r"<title\b[^>]*>(.*?)</title\s*>", re.IGNORECASE | re.DOTALL)
_H1_TAG_RE = re.compile(r"<h1\b[^>]*>(.*?)</h1\s*>", re.IGNORECASE | re.DOTALL)

#: Boilerplate suffixes payers append to every <title>; stripped so the title
#: column carries the document name rather than the site name.
#: Separator characters payers use between the document name and the site name,
#: including the en dash (U+2013) and em dash (U+2014), spelled via chr() so this
#: source file stays pure ASCII.
_TITLE_SEPARATORS = "|-:" + chr(0x2013) + chr(0x2014)
_TITLE_BOILERPLATE_RE = re.compile(
    r"\s*[" + re.escape(_TITLE_SEPARATORS) + r"]\s*"
    r"(?:UHCprovider\.com|UnitedHealthcare|Horizon\s+BCBSNJ"
    r"|Horizon\s+Blue\s+Cross[^|]*|Cigna(?:\s+Healthcare)?|UPMC(?:\s+Health\s+Plan)?"
    r"|Anthem(?:\s+Blue\s+Cross[^|]*)?|Elevance\s+Health|CareSource|Centene"
    r"|Geisinger(?:\s+Health\s+Plan)?|Highmark(?:\s+Blue[^|]*)?|BlueCross\s+BlueShield"
    r"[^|]*|BCBST)\s*$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# 1. Link extraction
# ---------------------------------------------------------------------------
def _soup(html: str):
    """
    Build a BeautifulSoup tree with the best available parser, or ``None``.

    lxml is preferred (fast and very forgiving); ``html.parser`` is the
    dependency-free fallback. Returning ``None`` lets callers drop to regex
    extraction rather than raise.
    """
    if BeautifulSoup is None:
        return None
    for parser in ("lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:  # pragma: no cover - parser missing or malformed input
            continue
    return None


def extract_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """
    Extract ``(absolute_url, anchor_text)`` pairs from an index page.

    Relative, root-relative and protocol-relative hrefs are all resolved against
    *base_url*, so a link like ``../../assets/policy.pdf`` becomes an absolute
    URL and a cross-directory reference is followed correctly. ``mailto:``,
    ``tel:``, ``javascript:`` and pure fragments are dropped, and duplicates are
    collapsed keeping the first (usually most descriptive) anchor text.

    When an anchor has no text - a very common pattern for icon-only PDF links -
    the ``title``/``aria-label`` attribute is used instead, because that text
    becomes our best ``document_title`` hint.
    """
    pairs: list[tuple[str, str]] = []
    soup = _soup(html)

    if soup is not None:
        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href") or "").strip()
            text = clean_text(anchor.get_text(" ", strip=True))
            if not text:
                for attribute in ("title", "aria-label", "data-title", "alt"):
                    text = clean_text(anchor.get(attribute))
                    if text:
                        break
            if not text:
                image = anchor.find("img")
                if image is not None:
                    text = clean_text(image.get("alt") or image.get("title"))
            pairs.append((href, text))
        # Framed / JS-driven listings sometimes only expose the document URL in an
        # iframe or an embed, so those count as links too.
        for tag, attribute in (("iframe", "src"), ("embed", "src"), ("object", "data")):
            for node in soup.find_all(tag):
                value = str(node.get(attribute) or "").strip()
                if value:
                    pairs.append((value, clean_text(node.get("title"))))
    else:
        for match in _ANCHOR_RE.finditer(html):
            href = match.group(1).strip().strip("\"'")
            text = clean_text(_TAG_STRIP_RE.sub(" ", match.group(2)))
            pairs.append((href, text))

    resolved: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href, text in pairs:
        if not href:
            continue
        lowered = href.lower()
        if lowered.startswith(("mailto:", "tel:", "javascript:", "data:", "#", "sms:",
                               "ftp:", "file:")):
            continue
        try:
            absolute = urljoin(base_url, href)
        except ValueError:
            continue
        if not absolute.lower().startswith(("http://", "https://")):
            continue
        absolute = absolute.split("#", 1)[0]
        if absolute in seen:
            continue
        seen.add(absolute)
        resolved.append((absolute, text))
    return resolved


def looks_like_document_url(url: str) -> bool:
    """
    True when *url* is shaped like a downloadable document.

    Intentionally a *shape* test only - the authoritative ``file_type`` decision
    happens after the fetch, from magic bytes (:func:`fetcher.detect_file_type`).
    This function exists to spend the request budget on plausible candidates.
    """
    lowered = url.lower()
    path = urlsplit(lowered).path
    if any(path.endswith(extension) for extension in NEGATIVE_URL_HINTS):
        return False
    if any(path.endswith(extension) for extension in DOCUMENT_EXTENSIONS):
        return True
    query = urlsplit(lowered).query
    if any(hint in query for hint in DOCUMENT_QUERY_HINTS):
        return True
    if any(hint in path for hint in ("/download", "/attachment", "/getfile",
                                     "/streamdocument", "/documents/", "/assets/",
                                     "/dam/", "/media/", "/content/dam/")):
        # A CMS asset route: likely a document even without a suffix, but only
        # worth trying when the path also mentions policy-ish content.
        return any(hint in lowered for hint in ("polic", "guideline", "formular",
                                                "manual", "bulletin", "auth", "drug",
                                                "criteria", "coverage"))
    return False


def looks_like_policy_index(anchor_text: str) -> bool:
    """True when anchor text advertises a listing of policies worth crawling."""
    lowered = clean_text(anchor_text).lower()
    if not lowered or len(lowered) > 120:
        return False
    return any(hint in lowered for hint in INDEX_TEXT_HINTS)


def score_candidate(url: str, anchor_text: str = "") -> float:
    """
    Heuristic crawl priority for a candidate URL (higher is crawled sooner).

    Cheap, transparent and deliberately not machine-learned: a PDF whose URL and
    link text both mention "medical policy" outranks an unlabelled asset link, so
    a limited ``max_docs_per_payer`` budget is spent on the most relevant
    documents rather than whatever the page happened to list first.
    """
    lowered = f"{url} {anchor_text}".lower()
    score = 1.0
    if urlsplit(url.lower()).path.endswith(".pdf"):
        score += 1.2
    for keyword, weight in (
        ("medical polic", 1.5), ("clinical polic", 1.5), ("coverage polic", 1.3),
        ("medical-polic", 1.5), ("clinical-polic", 1.5), ("coverage-polic", 1.3),
        ("pharmacy polic", 1.2), ("pharmacy-polic", 1.2),
        ("prior auth", 1.1), ("prior-auth", 1.1), ("formular", 1.0),
        ("guideline", 0.9), ("criteria", 0.8), ("provider manual", 0.9),
        ("provider-manual", 0.9), ("bulletin", 0.6), ("drug list", 0.8),
        ("polic", 0.5), ("provider", 0.3),
    ):
        if keyword in lowered:
            score += weight
    for keyword, penalty in (
        ("archive", 0.8), ("historical", 0.8), ("retired", 1.0), ("inactive", 0.8),
        ("member", 0.4), ("espanol", 1.0), ("spanish", 0.6), ("brochure", 0.5),
    ):
        if keyword in lowered:
            score -= penalty
    # Deep paths tend to be leaves; a mild bonus keeps the crawl moving downward.
    score += min(0.4, urlsplit(url).path.count("/") * 0.05)
    return round(max(0.0, score), 3)


# ---------------------------------------------------------------------------
# 2. Text and metadata extraction
# ---------------------------------------------------------------------------
def html_title(html: str) -> tuple[str, str]:
    """
    Best document title from an HTML page, plus the method that found it.

    Preference order: ``<h1>`` (usually the document's own heading), then
    ``og:title``, then ``<title>`` with site boilerplate stripped. Returns
    ``(title, "css_selector")`` or ``("", "")``.
    """
    soup = _soup(html)
    if soup is not None:
        heading = soup.find(["h1"])
        if heading is not None:
            text = clean_text(heading.get_text(" ", strip=True))
            if 4 <= len(text) <= 300:
                return text, "css_selector"
        meta = soup.find("meta", attrs={"property": "og:title"}) or \
            soup.find("meta", attrs={"name": "title"}) or \
            soup.find("meta", attrs={"name": "dc.title"})
        if meta is not None:
            text = clean_text(meta.get("content"))
            if 4 <= len(text) <= 300:
                return text, "css_selector"
        if soup.title is not None:
            text = clean_text(soup.title.get_text(" ", strip=True))
            text = clean_text(_TITLE_BOILERPLATE_RE.sub("", text))
            if text:
                return text, "css_selector"
        return "", ""

    match = _H1_TAG_RE.search(html) or _TITLE_TAG_RE.search(html)
    if match:
        text = clean_text(_TAG_STRIP_RE.sub(" ", match.group(1)))
        text = clean_text(_TITLE_BOILERPLATE_RE.sub("", text))
        if text:
            return text, "regex"
    return "", ""


def html_text(html: str, limit: int = 200_000) -> str:
    """
    Flatten an HTML page to readable text for metadata regexes.

    ``script``/``style``/``nav``/``footer`` content is dropped so that navigation
    boilerplate cannot masquerade as a policy's effective date - a real failure
    mode when a site footer carries a "last updated" stamp for the whole website.
    """
    soup = _soup(html)
    if soup is not None:
        for tag in soup.find_all(["script", "style", "noscript", "svg", "nav",
                                  "footer", "header", "form"]):
            tag.decompose()
        text = soup.get_text(" ", strip=True)
    else:
        without_blocks = re.sub(
            r"<(script|style|noscript|nav|footer|header)\b.*?</\1\s*>", " ", html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        text = _TAG_STRIP_RE.sub(" ", without_blocks)
    return clean_text(text)[:limit]


def pdf_text_and_title(raw: bytes, max_pages: int = 6) -> tuple[str, str, str]:
    """
    Extract ``(text, title, method)`` from PDF bytes.

    Only the first *max_pages* pages are read: policy metadata lives on page one,
    and a 400-page provider manual would otherwise dominate the run's CPU time.
    The XMP/Info ``/Title`` is preferred when it is meaningful - many payer PDFs
    have a useful title there - but is rejected when it is a filename or a
    generator artefact ("Microsoft Word - CS123.doc"), in which case the first
    substantial text line is used instead.

    Returns ``("", "", "")`` when no PDF library is installed or the file is
    unreadable (encrypted, truncated, malformed), which downstream turns into a
    lower confidence score plus an explanation in ``notes``.
    """
    if PdfReader is None:
        return "", "", ""
    import io

    try:
        reader = PdfReader(io.BytesIO(raw), strict=False)
    except Exception:
        return "", "", ""

    title = ""
    try:
        if reader.is_encrypted:
            # Some payer PDFs are "encrypted" with an empty owner password, which
            # is legal to open and requires no credential guessing.
            try:
                reader.decrypt("")
            except Exception:
                return "", "", ""
        metadata = reader.metadata or {}
        raw_title = clean_text(metadata.get("/Title", ""))
        if _plausible_pdf_title(raw_title):
            title = raw_title
    except Exception:
        pass

    chunks: list[str] = []
    try:
        for page in reader.pages[:max_pages]:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:
                continue
    except Exception:
        pass
    text = clean_text(" ".join(chunks))[:200_000]

    if not title and text:
        # Fall back to the first substantial line, which is the document heading
        # on essentially every payer policy PDF.
        for line in re.split(r"(?<=[a-z])\s{2,}|[\r\n]+", text[:1200]):
            candidate = clean_text(line)
            if 8 <= len(candidate) <= 200 and not candidate.lower().startswith(
                ("page ", "confidential", "copyright", "proprietary", "http")
            ):
                title = candidate
                break
    return text, title, "pdf_text" if text else ""


def _plausible_pdf_title(title: str) -> bool:
    """Reject PDF ``/Title`` values that are filenames or generator artefacts."""
    if not title or len(title) < 6 or len(title) > 300:
        return False
    lowered = title.lower()
    if lowered.startswith("microsoft word -") or lowered.startswith("microsoft powerpoint -"):
        return False
    if re.fullmatch(r"[\w\-. ]+\.(docx?|pdf|xlsx?|pptx?|rtf|tmp)", lowered):
        return False
    if lowered in {"untitled", "document", "document1", "print", "form", "pdf"}:
        return False
    return True


def title_from_url(url: str) -> str:
    """
    Derive a readable title from a URL's filename, as a last resort.

    ``/assets/medical-policy_CS123_2026-01.pdf`` becomes
    ``"Medical Policy CS123 2026 01"``. Marked as ``url_pattern`` extraction and
    scored low, because a filename is a weak proxy for a document's real name.
    """
    path = urlsplit(url).path
    stem = path.rsplit("/", 1)[-1]
    for extension in DOCUMENT_EXTENSIONS:
        if stem.lower().endswith(extension):
            stem = stem[: -len(extension)]
            break
    stem = re.sub(r"[_\-+.]+", " ", stem)
    stem = re.sub(r"%[0-9A-Fa-f]{2}", " ", stem)
    stem = clean_text(stem)
    if not stem or len(stem) < 3:
        return ""
    # Title-case only all-lower words so acronyms (CPT, HCPCS, PA) survive.
    words = [word if (word.isupper() or any(ch.isdigit() for ch in word))
             else word.capitalize() for word in stem.split()]
    return " ".join(words)[:200]


def extract_policy_number(text: str, url: str = "", title: str = "") -> str:
    """
    Find a policy number in the document text, title or URL.

    Labelled occurrences in the body are trusted first; the URL is consulted last
    because a number in a path is often a CMS id rather than the payer's own
    policy number. Returns ``""`` when nothing convincing is present - an empty
    string is the correct schema value, and a guessed identifier would be worse
    than none.
    """
    for haystack in (text[:8000], title, url):
        if not haystack:
            continue
        for pattern in _POLICY_NUMBER_PATTERNS:
            match = pattern.search(haystack)
            if match:
                value = clean_text(match.group(1)).strip(".,;:-_/")
                # A bare year is never a policy number.
                if re.fullmatch(r"(19|20)\d{2}", value):
                    continue
                if 3 <= len(value) <= 32:
                    return value.upper() if value.isascii() else value
    return ""


def extract_dates(text: str) -> tuple[str, str]:
    """
    Extract ``(effective_date, last_updated_date)`` as printed on the source.

    Only the first labelled occurrence of each is used, and only the first 12,000
    characters are searched: policy PDFs put their dates in the header block, and
    scanning further mostly finds dates belonging to cited references.
    """
    window = text[:12000]
    effective = ""
    updated = ""
    match = _EFFECTIVE_DATE_RE.search(window)
    if match:
        effective = normalise_iso_date(match.group(1))
    match = _UPDATED_DATE_RE.search(window)
    if match:
        updated = normalise_iso_date(match.group(1))
    return effective, updated


def classify_document_type(url: str, title: str = "", text: str = "") -> tuple[str, float]:
    """
    Map a document onto the ``document_type`` enum.

    Returns ``(document_type, strength)`` where *strength* in ``[0, 1]`` says how
    well-evidenced the choice is; it feeds directly into ``confidence_score``.
    The title and URL are weighted above the body text, since a policy PDF's body
    mentions many document kinds while its title names only its own.
    """
    strong = f"{title} {url}".lower()
    weak = text[:4000].lower()
    for document_type, keywords in _TYPE_RULES:
        for keyword in keywords:
            if keyword in strong:
                # "policy" alone is a weak signal; the specific phrases are strong.
                return document_type, 0.9 if len(keyword) > 8 else 0.65
    for document_type, keywords in _TYPE_RULES:
        for keyword in keywords:
            if len(keyword) > 8 and keyword in weak:
                return document_type, 0.5
    return "other", 0.2


def infer_line_of_business(url: str, title: str, text: str, default: str = "Unknown") -> str:
    """
    Infer ``line_of_business`` from the URL, title and body.

    Two or more distinct lines named in the strong signals yields ``Multiple``,
    which is the accurate answer for the combined documents payers publish
    (a single PA list covering Commercial and Medicare, say). Falls back to the
    payer's seed default, and finally to ``Unknown`` - never to a guess.
    """
    strong = f"{title} {url}".lower()
    hits: list[str] = []
    for lob, keywords in _LOB_RULES:
        if any(keyword in strong for keyword in keywords):
            hits.append(lob)
    if len(hits) >= 2:
        return "Multiple"
    if len(hits) == 1:
        return hits[0]

    weak = text[:6000].lower()
    weak_hits = [lob for lob, keywords in _LOB_RULES
                 if any(keyword in weak for keyword in keywords)]
    if len(weak_hits) >= 3:
        return "Multiple"
    if len(weak_hits) == 1:
        return weak_hits[0]
    return default if default in LINE_OF_BUSINESS else "Unknown"


#: State names -> USPS codes, for reading a state out of a document title/URL.
_STATE_NAMES: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "puerto rico": "PR", "virgin islands": "VI", "guam": "GU",
}


def infer_state_or_region(url: str, title: str, default: str = "") -> str:
    """
    Infer ``state_or_region`` from the URL and title, falling back to the seed default.

    Full state names are matched first (unambiguous), then a two-letter code in a
    path segment (``/nj/``) or a state-prefixed subdomain. A bare two-letter token
    inside prose is deliberately *not* matched: "OR" and "IN" appear constantly as
    English words and would poison the column.
    """
    haystack = f"{title} {url}".lower()
    hits: list[str] = []
    for name, code in _STATE_NAMES.items():
        if name in haystack and code not in hits:
            hits.append(code)
    lowered_url = url.lower()
    for code in STATE_OR_REGION:
        if code == "National" or code in hits:
            continue
        token = code.lower()
        if re.search(rf"/{token}/|/{token}-|[/_-]{token}\.|//{token}\.", lowered_url):
            hits.append(code)
    if hits:
        return "|".join(hits[:6])
    if "national" in haystack or "all states" in haystack:
        return "National"
    return normalise_multi_enum(default, STATE_OR_REGION)


# ---------------------------------------------------------------------------
# 3. Row construction
# ---------------------------------------------------------------------------
def build_row(
    payer: "Payer",
    candidate: "Candidate",
    result: "FetchResult",
    fetched_at: str,
    *,
    file_type: str = "",
    requires_auth: bool = False,
    notes: str = "",
) -> DocumentRow:
    """
    Assemble one schema row from a fetch result.

    This is the single place where a fetched response becomes a dataset record,
    so all of the conventions land here consistently:

    * ``scrape_timestamp_utc`` is the moment ``document_url`` was fetched, passed
      in by the caller rather than recomputed, so retries do not shift it.
    * ``content_hash_sha256`` comes from the raw bytes and is left empty unless
      this was a genuine 200 - and always empty when ``requires_auth`` is ``Y``.
    * ``extraction_method`` accumulates every technique that actually contributed,
      pipe-separated, rather than being hard-coded per branch.
    * ``confidence_score`` degrades honestly, and every score below 0.70 gets a
      reason appended to ``notes``.
    """
    from .schema import normalise_row  # local import avoids a cycle at module load

    url = candidate.url
    is_success = result.status == 200 and bool(result.content) and not requires_auth
    methods: list[str] = []
    # The discovery route always contributes a method.
    origin_method = {
        "sitemap": "sitemap",
        "sitemap_doc": "sitemap",
        "anchor": "css_selector",
        "index": "css_selector",
        "html_policy": "css_selector",
        "url_pattern": "url_pattern",
    }.get(candidate.origin, "css_selector")
    methods.append(origin_method)

    title = ""
    text = ""
    reasons: list[str] = []
    confidence = 0.5

    if is_success:
        effective_type = file_type or "other"
        if effective_type == "pdf":
            text, pdf_title, method = pdf_text_and_title(result.content)
            if method:
                methods.append(method)
            title = pdf_title
            if not text:
                reasons.append(
                    "PDF text extraction returned nothing (likely a scanned image "
                    "or an unsupported encoding); metadata inferred from the URL"
                )
        elif effective_type == "html":
            page = result.text()
            title, method = html_title(page)
            if method:
                methods.append(method)
            text = html_text(page)
        else:
            # doc/docx/xls/xlsx: we identify and hash them, but do not parse the
            # binary formats. Honest, and reflected in the confidence score.
            reasons.append(
                f"{effective_type} content is identified and hashed but not parsed; "
                "title and dates inferred from the URL and link text"
            )

    # Titles, in decreasing order of trustworthiness.
    if not title:
        anchor_title = clean_text(candidate.link_text)
        if 4 <= len(anchor_title) <= 300:
            title = anchor_title
            if "css_selector" not in methods:
                methods.append("css_selector")
        else:
            title = title_from_url(url)
            if title:
                if "url_pattern" not in methods:
                    methods.append("url_pattern")
                reasons.append("title derived from the URL filename, not from the document")

    policy_number = extract_policy_number(text, url=url, title=title)
    if policy_number and "regex" not in methods:
        methods.append("regex")
    effective_date, last_updated = extract_dates(text) if text else ("", "")
    if (effective_date or last_updated) and "regex" not in methods:
        methods.append("regex")

    document_type, type_strength = classify_document_type(url, title, text)
    line_of_business = infer_line_of_business(
        url, title, text, default=payer.default_line_of_business
    )
    state_or_region = infer_state_or_region(
        url, title, default=payer.default_state_or_region
    )

    # ---- confidence -------------------------------------------------------
    # Built up from what we actually managed to read. The weights are arbitrary
    # but the ordering is not: a parsed document with a real title, a known type
    # and a printed date is the only thing that earns a high score.
    if is_success:
        confidence = 0.45
        confidence += 0.20 * type_strength
        if title:
            confidence += 0.12 if text else 0.06
        if text:
            confidence += 0.08
        if policy_number:
            confidence += 0.07
        if effective_date or last_updated:
            confidence += 0.08
        if line_of_business != "Unknown":
            confidence += 0.03
        if candidate.origin == "url_pattern":
            # Enumerated finds are verified but inherently less certain than a
            # document a payer actually linked to.
            confidence -= 0.10
            reasons.append("found by bounded URL-pattern enumeration rather than a link")
        if result.truncated:
            confidence -= 0.10
            reasons.append("document exceeded the download cap and was truncated; "
                           "content_hash_sha256 covers only the retrieved prefix")
    elif requires_auth:
        # We know the URL exists and is gated. That is a real, useful finding.
        confidence = 0.40
        reasons.append("document is behind authentication; metadata limited to what "
                       "the public URL and link text reveal")
    else:
        confidence = 0.25
        reasons.append(f"document could not be retrieved (http_status "
                       f"{result.status or 0}); row kept as evidence of the attempt")

    confidence = max(0.0, min(1.0, confidence))
    note_parts = [clean_text(notes)] if notes else []
    if confidence < 0.70 or result.truncated:
        note_parts.extend(reasons)
    if result.final_url and result.final_url != url:
        note_parts.append(f"redirected to {result.final_url}")
    # De-duplicate: the caller's note and the auto-generated reason often say the
    # same thing (both mention the status code), and a row whose notes repeat
    # themselves reads like a bug even when the data is right.
    deduped: list[str] = []
    for part in note_parts:
        part = clean_text(part).strip("; ")
        if part and part not in deduped:
            deduped.append(part)
    note_parts = deduped

    row = DocumentRow(
        payer_name=payer.payer_name,
        payer_alias=payer.payer_alias,
        state_or_region=state_or_region,
        line_of_business=line_of_business,
        document_title=title,
        document_type=document_type,
        document_url=url,
        source_page_url=candidate.source_page_url,
        discovery_path=candidate.discovery_path,
        file_type=file_type or ("html" if is_success else "other"),
        policy_number=policy_number,
        effective_date=effective_date,
        last_updated_date=last_updated,
        http_status=result.status,
        content_hash_sha256=result.sha256 if is_success else "",
        file_size_bytes=result.size_bytes if result.content else "",
        requires_auth="Y" if requires_auth else "N",
        # Everything here is fetched with a plain HTTP client; no headless
        # browser is used, so "static" is the truthful value for every row.
        render_mode="static",
        extraction_method="|".join(dict.fromkeys(methods)),
        confidence_score=confidence,
        scrape_timestamp_utc=fetched_at,
        notes="; ".join(note_parts)[:1000].strip("; "),
    )
    row.content_text = text[:20000]
    return normalise_row(row)


def iter_document_types() -> Iterable[str]:
    """The ``document_type`` enum in classification-priority order (for docs/tests)."""
    return tuple(document_type for document_type, _ in _TYPE_RULES) + ("other",)


def assert_enums_consistent() -> None:
    """
    Guard that the classifier can only produce enum-legal values.

    Called by the unit tests: a typo in :data:`_TYPE_RULES` would otherwise
    produce rows that fail schema validation only when a matching document is
    encountered in the wild.
    """
    for document_type, _ in _TYPE_RULES:
        if document_type not in DOCUMENT_TYPE:
            raise AssertionError(f"_TYPE_RULES produces {document_type!r}, not in the enum")
    for lob, _ in _LOB_RULES:
        if lob not in LINE_OF_BUSINESS:
            raise AssertionError(f"_LOB_RULES produces {lob!r}, not in the enum")
    for code in _STATE_NAMES.values():
        if code not in STATE_OR_REGION:
            raise AssertionError(f"_STATE_NAMES produces {code!r}, not in the enum")
