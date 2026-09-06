"""
Persisting downloaded documents and writing the dataset.

Two responsibilities, deliberately separated:

* :class:`Downloader` saves each fetched document under
  ``downloads/<payer_name>/<sanitized_filename>.<ext>``, one subfolder per payer.
  The local path is *internal bookkeeping only* - it is carried on the row object
  but is never written as a schema column, per the brief.

* :class:`OutputWriter` writes the dataset itself: ``output.csv`` (UTF-8,
  RFC 4180, header row) and/or ``output.xlsx`` with the data on the first sheet.
  Both carry exactly the 22 columns from :data:`crawler.schema.COLUMNS`, in that
  order, and nothing else.
"""

from __future__ import annotations

import csv
import hashlib
import os
import re
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import unquote, urlsplit

from .schema import COLUMNS, DocumentRow, normalise_row, validate_row

#: Characters Windows forbids in a filename, plus the ones that make shell and
#: URL handling awkward. Replaced with "_" by :func:`sanitize_filename`.
_UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')

#: Reserved Windows device names. A file called "con.pdf" cannot be created on
#: Windows at all, so these get a prefix.
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{digit}" for digit in range(1, 10)}
    | {f"lpt{digit}" for digit in range(1, 10)}
)

#: Longest filename stem we will write. Keeps the full path well inside the
#: 260-character default limit on Windows even for deeply nested output dirs.
MAX_STEM_LENGTH = 90


def sanitize_filename(name: str, *, fallback: str = "document") -> str:
    """
    Make *name* safe to use as a filename on Windows, macOS and Linux.

    Percent-decodes, strips directory separators and control characters,
    collapses whitespace to single underscores, guards the Windows reserved
    device names, and trims to :data:`MAX_STEM_LENGTH`. Returns *fallback* when
    nothing usable survives.
    """
    text = unquote(str(name or "")).strip()
    text = _UNSAFE_FILENAME_RE.sub("_", text)
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"_{2,}", "_", text).strip("._ ")
    if not text:
        return fallback
    if text.split(".")[0].lower() in _WINDOWS_RESERVED:
        text = f"file_{text}"
    return text[:MAX_STEM_LENGTH] or fallback


def sanitize_dirname(name: str, *, fallback: str = "unknown_payer") -> str:
    """Sanitize a payer name into a directory name (same rules, shorter cap)."""
    return sanitize_filename(name, fallback=fallback)[:60] or fallback


class Downloader:
    """
    Writes fetched documents to ``downloads/<payer>/`` and records their paths.

    Naming rule: the URL's own filename stem when it has one, otherwise a slug
    built from the URL path, always suffixed with the first 10 hex characters of
    the content hash. That suffix does three things at once - it keeps two
    different documents that share a filename from colliding, it makes a re-run
    idempotent (same bytes, same path), and it puts the identity of the file in
    its name for anyone auditing the folder by hand.
    """

    def __init__(self, root: str | os.PathLike[str], log: Any, enabled: bool = True) -> None:
        self.root = Path(root)
        self.log = log
        self.enabled = enabled
        #: Counters surfaced in the ``run.done`` record.
        self.saved = 0
        self.skipped_existing = 0
        self.errors = 0
        if self.enabled:
            self.root.mkdir(parents=True, exist_ok=True)

    def payer_dir(self, payer_name: str) -> Path:
        """Return (creating if needed) the per-payer download subfolder."""
        directory = self.root / sanitize_dirname(payer_name)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @staticmethod
    def _extension_for(file_type: str) -> str:
        """Map a schema ``file_type`` onto a filename extension."""
        return {
            "html": ".html", "pdf": ".pdf", "doc": ".doc", "docx": ".docx",
            "xls": ".xls", "xlsx": ".xlsx",
        }.get(file_type, ".bin")

    def target_path(self, payer_name: str, url: str, digest: str, file_type: str) -> Path:
        """
        Compute the on-disk path for a document without writing anything.

        Split out from :meth:`save` so the resume path can check "do we already
        have these bytes?" without re-downloading, and so the naming rule is
        directly unit-testable.
        """
        path = urlsplit(url).path
        stem = unquote(path.rsplit("/", 1)[-1]) if path else ""
        for extension in (".pdf", ".html", ".htm", ".doc", ".docx", ".xls", ".xlsx", ".aspx"):
            if stem.lower().endswith(extension):
                stem = stem[: -len(extension)]
                break
        # A stem that is missing, very short, or purely numeric tells a human
        # auditing the downloads folder nothing, so borrow context from the
        # parent path segments: "/content/dam/policy/12345" becomes
        # "policy_12345" rather than a bare "12345".
        if not stem or len(stem) < 3 or stem.isdigit():
            segments = [segment for segment in path.split("/") if segment][-3:]
            stem = "_".join(segments) or "document"
        stem = sanitize_filename(stem)
        short_hash = (digest or hashlib.sha256(url.encode("utf-8")).hexdigest())[:10]
        return self.payer_dir(payer_name) / f"{stem}_{short_hash}{self._extension_for(file_type)}"

    def save(self, payer_name: str, url: str, result: Any, file_type: str) -> Path:
        """
        Write the response bytes for *url* and return the path written.

        Never raises: a disk error is logged and counted, because failing to
        archive a copy must not lose the dataset row - the row is the deliverable
        and the download is a convenience.
        """
        target = self.target_path(payer_name, url, getattr(result, "sha256", ""), file_type)
        if not self.enabled:
            return target
        try:
            if target.exists() and target.stat().st_size == len(result.content):
                # Same name and same size means same content (the name carries the
                # content hash), so this is a resumed or repeated fetch.
                self.skipped_existing += 1
                return target
            target.write_bytes(result.content)
            self.saved += 1
        except OSError as exc:
            self.errors += 1
            self.log.error(
                "download.failed",
                f"could not write {target}: {exc}",
                url=url, path=str(target), error=str(exc),
            )
        return target


class OutputWriter:
    """
    Writes the final dataset in the exact 22-column schema.

    CSV specifics chosen for RFC 4180 conformance and Excel compatibility:

    * ``newline=""`` on the file handle plus ``lineterminator="\\r\\n"`` so the
      csv module - not the OS - decides the line ending,
    * ``QUOTE_MINIMAL``, which quotes exactly the fields that contain a comma,
      quote or newline, and
    * plain UTF-8 (no BOM) as the brief specifies.
    """

    def __init__(self, output_dir: str | os.PathLike[str], log: Any) -> None:
        self.output_dir = Path(output_dir)
        self.log = log
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def write_csv(self, rows: Sequence[DocumentRow], filename: str = "output.csv") -> Path:
        """Write *rows* to ``output.csv`` and return the path."""
        path = self.output_dir / filename
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=list(COLUMNS),
                lineterminator="\r\n",
                quoting=csv.QUOTE_MINIMAL,
                # Any attribute that is not a schema column is a bug, not
                # something to silently drop.
                extrasaction="raise",
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(row.to_output_dict())
        self.log.info(
            "output.written",
            f"wrote {len(rows)} row(s) to {path}",
            path=str(path), rows=len(rows), format="csv",
        )
        return path

    def write_xlsx(self, rows: Sequence[DocumentRow], filename: str = "output.xlsx") -> Path | None:
        """
        Write *rows* to ``output.xlsx`` with the data on the first sheet.

        Returns ``None`` (with an ERROR logged) when openpyxl is not installed,
        rather than failing the run: the CSV is the primary artefact and a
        missing optional dependency should not discard a completed crawl.

        Every cell is written as text. That is intentional: Excel would otherwise
        reinterpret ``2026-03-01`` as a date and re-render it in the local
        locale, and a policy number like ``00123.045`` as a float - both of which
        would violate the schema's formatting conventions on the way out.
        """
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Alignment, Font
            from openpyxl.utils import get_column_letter
        except ImportError:
            self.log.error(
                "output.xlsx_unavailable",
                "openpyxl is not installed, so output.xlsx was skipped "
                "(install it with 'pip install openpyxl'); output.csv is unaffected",
            )
            return None

        path = self.output_dir / filename
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "policy_documents"
        sheet.append(list(COLUMNS))
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        for row in rows:
            record = row.to_output_dict()
            sheet.append([record[column] for column in COLUMNS])

        # Force text formatting and set readable column widths.
        for index, column in enumerate(COLUMNS, start=1):
            letter = get_column_letter(index)
            width = {
                "document_title": 52, "document_url": 60, "source_page_url": 46,
                "discovery_path": 70, "notes": 60, "payer_name": 30,
                "content_hash_sha256": 20, "state_or_region": 16,
                "extraction_method": 22,
            }.get(column, 16)
            sheet.column_dimensions[letter].width = width
            for cell in sheet[letter][1:]:
                cell.number_format = "@"
                cell.alignment = Alignment(vertical="top")
        sheet.freeze_panes = "A2"
        # Autofilter over the whole table makes the sheet usable for review.
        sheet.auto_filter.ref = sheet.dimensions

        workbook.save(path)
        self.log.info(
            "output.written",
            f"wrote {len(rows)} row(s) to {path}",
            path=str(path), rows=len(rows), format="xlsx",
        )
        return path

    def write(
        self,
        rows: Sequence[DocumentRow],
        output_format: str = "csv",
        also_xlsx: bool = False,
    ) -> list[Path]:
        """
        Write the dataset in the configured format(s) and return the paths written.

        The CSV is always produced when ``output_format == "csv"``; ``also_xlsx``
        adds the workbook alongside it, which is convenient when a reviewer wants
        the spreadsheet and a pipeline wants the CSV.
        """
        written: list[Path] = []
        if output_format == "xlsx":
            path = self.write_xlsx(rows)
            if path is not None:
                written.append(path)
            else:
                # Fall back to CSV so a run always produces a dataset.
                written.append(self.write_csv(rows))
        else:
            written.append(self.write_csv(rows))
            if also_xlsx:
                path = self.write_xlsx(rows)
                if path is not None:
                    written.append(path)
        return written

    def write_validation_report(
        self, problems: Sequence[tuple[str, list[str]]], filename: str = "schema_problems.txt"
    ) -> Path | None:
        """
        Write any schema-validation complaints next to the dataset.

        Rows are still emitted (a flagged row is more useful to a reviewer than a
        dropped one), but the problems are recorded explicitly so nothing is
        quietly wrong.
        """
        if not problems:
            return None
        path = self.output_dir / filename
        lines = [
            "Schema validation problems for this run.",
            "Rows are still present in the output; each line below names the",
            "document_url and what did not conform.",
            "",
        ]
        for url, issues in problems:
            lines.append(url)
            lines.extend(f"    - {issue}" for issue in issues)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.log.warn(
            "schema.invalid",
            f"{len(problems)} row(s) had schema problems; details in {path}",
            path=str(path), rows=len(problems),
        )
        return path


def prepare_rows(rows: Iterable[DocumentRow]) -> tuple[list[DocumentRow], list[tuple[str, list[str]]]]:
    """
    Normalise and validate every row just before writing.

    Returns ``(rows, problems)``. Normalisation is idempotent, so this is safe on
    rows that came back from the resume checkpoint, and it is the last gate before
    the dataset is written - which is exactly where the schema should be enforced.
    """
    prepared: list[DocumentRow] = []
    problems: list[tuple[str, list[str]]] = []
    for row in rows:
        normalise_row(row)
        issues = validate_row(row)
        if issues:
            problems.append((str(row.document_url), issues))
        prepared.append(row)
    return prepared, problems
