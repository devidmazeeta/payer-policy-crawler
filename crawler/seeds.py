"""
Payer seed list loading.

``payer_seed_list.csv`` is the run's input: one row per payer, giving the name,
alias, hint host and a few optional hints. The hint host is explicitly *only a
starting point* - :mod:`crawler.discovery` follows redirects, subdomains and
regional sites away from it - so nothing here is treated as a hard boundary.

Only ``payer_name`` and ``hint_host`` are required. Every other column is
optional so that a minimal two-column CSV works, and unknown columns are ignored
rather than rejected, because the case study's own seed file may carry extra
context columns we do not need.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from .config import ConfigError, parse_payer_range
from .schema import LINE_OF_BUSINESS, STATE_OR_REGION, clean_text, normalise_multi_enum

#: Accepted header spellings -> canonical field name. Case- and separator-
#: insensitive matching happens in :func:`_canonical_header`, so "Payer Name",
#: "payer_name" and "payer-name" are all the same column.
HEADER_ALIASES: dict[str, str] = {
    "payer": "payer_name",
    "payername": "payer_name",
    "name": "payer_name",
    "payeralias": "payer_alias",
    "alias": "payer_alias",
    "shortname": "payer_alias",
    "hinthost": "hint_host",
    "host": "hint_host",
    "hostname": "hint_host",
    "domain": "hint_host",
    "seedhost": "hint_host",
    "website": "hint_host",
    "url": "hint_host",
    "stateorregion": "default_state_or_region",
    "state": "default_state_or_region",
    "region": "default_state_or_region",
    "defaultstateorregion": "default_state_or_region",
    "lineofbusiness": "default_line_of_business",
    "lob": "default_line_of_business",
    "defaultlineofbusiness": "default_line_of_business",
    "seedpaths": "seed_paths",
    "paths": "seed_paths",
    "extrahosts": "extra_hosts",
    "additionalhosts": "extra_hosts",
    "notes": "notes",
    "note": "notes",
    "id": "payer_id",
    "payerid": "payer_id",
}


@dataclass(slots=True)
class Payer:
    """
    One payer to crawl.

    ``seed_paths`` and ``extra_hosts`` are the two hand-curated hints that make a
    real difference in practice: a payer that publishes no sitemap still has a
    well-known provider-policy path, and a payer whose documents live on a
    separate document host needs that host declared in scope. Both are optional
    and both are recorded in ``discovery_path`` when used, so a reviewer can see
    exactly where a hint (rather than an automated discovery) did the work.
    """

    payer_name: str
    hint_host: str
    payer_alias: str = ""
    #: Default ``state_or_region`` for rows where the document itself gives no
    #: better signal, e.g. "NJ" for Horizon or "National" for Cigna.
    default_state_or_region: str = ""
    #: Default ``line_of_business`` fallback, used the same way.
    default_line_of_business: str = "Unknown"
    #: Extra entry paths, pipe- or comma-separated in the CSV.
    seed_paths: list[str] = field(default_factory=list)
    #: Additional in-scope hosts (regional sites, document CDNs run by the payer).
    extra_hosts: list[str] = field(default_factory=list)
    notes: str = ""
    payer_id: str = ""
    #: 1-based position in the seed CSV, used by ``--payer-range``.
    position: int = 0

    @property
    def label(self) -> str:
        """Short identifier for logs: the alias when present, else the full name."""
        return self.payer_alias or self.payer_name

    @property
    def base_url(self) -> str:
        """``https://<hint_host>/`` - the starting point for discovery."""
        return f"https://{self.hint_host}/"


def _canonical_header(raw: str) -> str:
    """
    Map a CSV header cell onto a canonical field name.

    Punctuation and case are discarded before lookup, so "Hint Host", "hint_host"
    and "HINT-HOST" all resolve to ``hint_host``.
    """
    squashed = "".join(char for char in str(raw).lower() if char.isalnum())
    if squashed in HEADER_ALIASES:
        return HEADER_ALIASES[squashed]
    # Already canonical (with separators) is the common case.
    canonical = str(raw).strip().lower().replace(" ", "_").replace("-", "_")
    return canonical


def _split_list(value: Any) -> list[str]:
    """Split a pipe- or comma-separated CSV cell into a clean list of strings."""
    text = clean_text(value)
    if not text:
        return []
    separator = "|" if "|" in text else ","
    return [part.strip() for part in text.split(separator) if part.strip()]


def _normalise_host(value: Any) -> str:
    """
    Reduce a hint-host cell to a bare hostname.

    Accepts anything a human might type: ``uhcprovider.com``,
    ``https://www.uhcprovider.com/``, or ``www.uhcprovider.com/policies``. The
    leading ``www.`` is kept if given, since :func:`registrable_domain` matching
    in discovery makes it irrelevant for scope decisions.
    """
    text = clean_text(value).strip().strip("/")
    if not text:
        return ""
    if "://" in text:
        text = urlsplit(text).hostname or text
    else:
        text = text.split("/")[0]
    return text.lower().lstrip("@").strip(".")


def load_payers(csv_path: str | Path) -> list[Payer]:
    """
    Load and validate the payer seed CSV.

    Raises :class:`ConfigError` with a row-numbered message on a missing required
    column or an unusable host, because a silently skipped payer would show up
    later as an unexplained gap in the dataset.
    """
    path = Path(csv_path)
    if not path.is_file():
        raise ConfigError(f"payer seed CSV not found: {path}")

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header_row = next(reader)
        except StopIteration:
            raise ConfigError(f"{path} is empty; expected a header row") from None
        headers = [_canonical_header(cell) for cell in header_row]
        if "payer_name" not in headers:
            raise ConfigError(
                f"{path}: no payer name column found (looked for one of "
                f"payer_name / payer / name). Header was: {header_row}"
            )
        if "hint_host" not in headers:
            raise ConfigError(
                f"{path}: no hint host column found (looked for one of "
                f"hint_host / host / domain / website). Header was: {header_row}"
            )

        payers: list[Payer] = []
        problems: list[str] = []
        for line_number, raw_row in enumerate(reader, start=2):
            if not any(cell.strip() for cell in raw_row):
                continue  # tolerate blank separator lines
            record = {
                headers[index]: raw_row[index] if index < len(raw_row) else ""
                for index in range(len(headers))
            }
            name = clean_text(record.get("payer_name"))
            host = _normalise_host(record.get("hint_host"))
            if not name:
                problems.append(f"line {line_number}: payer_name is empty")
                continue
            if not host or "." not in host:
                problems.append(
                    f"line {line_number} ({name}): hint_host "
                    f"{record.get('hint_host')!r} is not a hostname"
                )
                continue

            lob = clean_text(record.get("default_line_of_business")) or "Unknown"
            if lob not in LINE_OF_BUSINESS:
                problems.append(
                    f"line {line_number} ({name}): default_line_of_business {lob!r} "
                    f"is outside the enum {sorted(LINE_OF_BUSINESS)}"
                )
                continue

            payers.append(
                Payer(
                    payer_name=name,
                    hint_host=host,
                    payer_alias=clean_text(record.get("payer_alias")),
                    default_state_or_region=normalise_multi_enum(
                        record.get("default_state_or_region"), STATE_OR_REGION
                    ),
                    default_line_of_business=lob,
                    seed_paths=_split_list(record.get("seed_paths")),
                    extra_hosts=[_normalise_host(item)
                                 for item in _split_list(record.get("extra_hosts"))],
                    notes=clean_text(record.get("notes")),
                    payer_id=clean_text(record.get("payer_id")),
                    position=len(payers) + 1,
                )
            )

    if problems:
        bullets = "\n".join(f"  - {item}" for item in problems)
        raise ConfigError(f"invalid rows in {path}:\n{bullets}")
    if not payers:
        raise ConfigError(f"{path} contains a header but no payer rows")
    return payers


def select_payers(payers: list[Payer], payer_range: str) -> list[Payer]:
    """
    Apply a ``payer_range`` spec to the loaded seed list.

    Selection is by 1-based position in the CSV, which is what makes
    ``--payer-range 1-5`` a stable, reproducible way to split a run.
    """
    indexes = parse_payer_range(payer_range, len(payers))
    return [payers[index] for index in indexes]


def describe_payers(payers: Iterable[Payer]) -> str:
    """Human-readable listing for ``--dry-run`` / ``--list-payers``."""
    lines = [f"{'#':>3}  {'payer':<38} {'alias':<12} hint host"]
    lines.append("-" * 92)
    for payer in payers:
        lines.append(
            f"{payer.position:>3}  {payer.payer_name[:38]:<38} "
            f"{payer.payer_alias[:12]:<12} {payer.hint_host}"
        )
    return "\n".join(lines)
