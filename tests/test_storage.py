"""
Storage tests: download naming/sanitisation and the CSV/XLSX writers.

The writer tests read the file back with the ``csv`` module rather than by
string matching, so they verify the delivered artefact rather than the code's
intent - including that the header is exactly the 22 columns in order and that
no internal bookkeeping column leaks in.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from crawler.fetcher import FetchResult
from crawler.schema import COLUMNS
from crawler.storage import (
    Downloader,
    OutputWriter,
    prepare_rows,
    sanitize_dirname,
    sanitize_filename,
)

from .conftest import make_row

PDF = b"%PDF-1.7 policy content"


# ---------------------------------------------------------------------------
# Filename sanitisation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("bariatric-surgery", "bariatric-surgery"),
        ("policy: CS123/045", "policy_CS123_045"),
        ("a<b>c:d\"e|f?g*h", "a_b_c_d_e_f_g_h"),
        ("spaces  everywhere", "spaces_everywhere"),
        ("Provider%20Policies", "Provider_Policies"),   # percent-decoded first
        ("....", "document"),
        ("", "document"),
    ],
)
def test_sanitize_filename(raw, expected):
    assert sanitize_filename(raw) == expected


def test_windows_reserved_device_names_are_escaped():
    """A file literally called con.pdf cannot be created on Windows."""
    assert sanitize_filename("con") == "file_con"
    assert sanitize_filename("nul.pdf").startswith("file_")


def test_filename_length_is_capped_to_keep_paths_short():
    assert len(sanitize_filename("x" * 400)) <= 90


def test_control_characters_are_stripped():
    assert "\n" not in sanitize_filename("policy\nname")
    assert "\x00" not in sanitize_filename("policy\x00name")


def test_sanitize_dirname_is_shorter():
    assert len(sanitize_dirname("y" * 200)) <= 60


# ---------------------------------------------------------------------------
# Downloader
# ---------------------------------------------------------------------------
def test_documents_land_in_a_per_payer_subfolder(tmp_path, log):
    downloader = Downloader(tmp_path / "downloads", log)
    result = FetchResult(url="https://e.com/p/bariatric.pdf", status=200, content=PDF,
                         headers={"content-type": "application/pdf"})
    path = downloader.save("Test Health Plan", result.url, result, "pdf")

    assert path.is_file()
    assert path.parent.name == "Test_Health_Plan"
    assert path.parent.parent == tmp_path / "downloads"
    assert path.read_bytes() == PDF
    assert downloader.saved == 1


def test_filename_carries_the_stem_and_a_content_hash_prefix(tmp_path, log):
    """Same bytes -> same path (idempotent); different bytes -> no collision."""
    downloader = Downloader(tmp_path / "downloads", log)
    result = FetchResult(url="https://e.com/p/bariatric.pdf", status=200, content=PDF,
                         headers={"content-type": "application/pdf"})
    path = downloader.save("Payer", result.url, result, "pdf")
    assert path.name.startswith("bariatric_")
    assert path.suffix == ".pdf"
    assert result.sha256[:10] in path.name


def test_same_filename_different_content_does_not_collide(tmp_path, log):
    downloader = Downloader(tmp_path / "downloads", log)
    first = FetchResult(url="https://e.com/a/policy.pdf", status=200, content=b"%PDF-1 one",
                        headers={"content-type": "application/pdf"})
    second = FetchResult(url="https://e.com/b/policy.pdf", status=200, content=b"%PDF-1 two",
                         headers={"content-type": "application/pdf"})
    path_one = downloader.save("Payer", first.url, first, "pdf")
    path_two = downloader.save("Payer", second.url, second, "pdf")
    assert path_one != path_two
    assert path_one.read_bytes() != path_two.read_bytes()


def test_re_saving_identical_bytes_is_skipped(tmp_path, log):
    downloader = Downloader(tmp_path / "downloads", log)
    result = FetchResult(url="https://e.com/p.pdf", status=200, content=PDF,
                         headers={"content-type": "application/pdf"})
    downloader.save("Payer", result.url, result, "pdf")
    downloader.save("Payer", result.url, result, "pdf")
    assert downloader.saved == 1
    assert downloader.skipped_existing == 1


def test_extensionless_cms_route_gets_a_slug_from_the_path(tmp_path, log):
    downloader = Downloader(tmp_path / "downloads", log)
    result = FetchResult(url="https://e.com/content/dam/policy/12345", status=200,
                         content=PDF, headers={"content-type": "application/pdf"})
    path = downloader.save("Payer", result.url, result, "pdf")
    assert path.suffix == ".pdf"
    assert "policy" in path.name


def test_disabled_downloader_writes_nothing_but_still_reports_a_path(tmp_path, log):
    downloader = Downloader(tmp_path / "downloads", log, enabled=False)
    result = FetchResult(url="https://e.com/p.pdf", status=200, content=PDF,
                         headers={"content-type": "application/pdf"})
    path = downloader.save("Payer", result.url, result, "pdf")
    assert not path.exists()
    assert downloader.saved == 0


def test_file_extension_follows_the_detected_file_type(tmp_path, log):
    downloader = Downloader(tmp_path / "downloads", log)
    for file_type, suffix in (("pdf", ".pdf"), ("html", ".html"), ("xlsx", ".xlsx"),
                              ("docx", ".docx"), ("other", ".bin")):
        result = FetchResult(url=f"https://e.com/doc-{file_type}", status=200,
                            content=b"body-" + file_type.encode())
        path = downloader.save("Payer", result.url, result, file_type)
        assert path.suffix == suffix


# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------
def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def test_csv_header_is_exactly_the_22_columns_in_order(tmp_path, log):
    writer = OutputWriter(tmp_path / "output", log)
    path = writer.write_csv([make_row()])
    header, _ = read_csv(path)
    assert header == list(COLUMNS)


def test_csv_contains_no_internal_bookkeeping_columns(tmp_path, log):
    row = make_row()
    row.local_path = r"C:\downloads\payer\policy.pdf"
    row.content_text = "extracted text"
    writer = OutputWriter(tmp_path / "output", log)
    path = writer.write_csv([row])
    header, records = read_csv(path)
    assert "local_path" not in header
    assert "content_text" not in header
    assert len(header) == 22
    assert r"C:\downloads" not in path.read_text(encoding="utf-8")
    assert len(records) == 1


def test_csv_is_utf8_with_crlf_line_endings(tmp_path, log):
    """RFC 4180 specifies CRLF; the csv module (not the OS) must decide."""
    writer = OutputWriter(tmp_path / "output", log)
    path = writer.write_csv([make_row(document_title="Policy for Munchen Clinic")])
    raw = path.read_bytes()
    assert raw.startswith(b"payer_name,")
    assert b"\r\n" in raw
    # No BOM: the brief asks for plain UTF-8.
    assert not raw.startswith(b"\xef\xbb\xbf")
    path.read_text(encoding="utf-8")  # must decode cleanly


def test_fields_with_commas_and_quotes_round_trip(tmp_path, log):
    tricky = 'Bariatric Surgery, Adult ("Sleeve") Policy'
    writer = OutputWriter(tmp_path / "output", log)
    path = writer.write_csv([make_row(document_title=tricky)])
    _, records = read_csv(path)
    assert records[0]["document_title"] == tricky


def test_notes_containing_a_pipe_and_semicolon_round_trip(tmp_path, log):
    note = "collapsed 2 near-duplicate URL(s); rule: title|hash"
    writer = OutputWriter(tmp_path / "output", log)
    path = writer.write_csv([make_row(notes=note)])
    _, records = read_csv(path)
    assert records[0]["notes"] == note


def test_empty_optional_fields_are_written_as_empty_strings(tmp_path, log):
    """Never "None", "null", "N/A", "-" or "TBD"."""
    row = make_row(policy_number="", effective_date="", last_updated_date="",
                   source_page_url="", state_or_region="")
    writer = OutputWriter(tmp_path / "output", log)
    path = writer.write_csv([row])
    _, records = read_csv(path)
    for field in ("policy_number", "effective_date", "last_updated_date",
                  "source_page_url", "state_or_region"):
        assert records[0][field] == ""
    text = path.read_text(encoding="utf-8")
    for forbidden in (",None,", ",null,", ",N/A,", ",TBD,"):
        assert forbidden not in text


def test_writing_zero_rows_still_produces_a_header(tmp_path, log):
    """An empty run must produce a well-formed, readable file."""
    writer = OutputWriter(tmp_path / "output", log)
    path = writer.write_csv([])
    header, records = read_csv(path)
    assert header == list(COLUMNS)
    assert records == []


def test_output_directory_is_created(tmp_path, log):
    writer = OutputWriter(tmp_path / "nested" / "output", log)
    path = writer.write_csv([make_row()])
    assert path.is_file()


# ---------------------------------------------------------------------------
# XLSX writer
# ---------------------------------------------------------------------------
def test_xlsx_has_the_data_on_the_first_sheet(tmp_path, log):
    openpyxl = pytest.importorskip("openpyxl")
    writer = OutputWriter(tmp_path / "output", log)
    path = writer.write_xlsx([make_row(), make_row(document_url="https://e.com/b.pdf")])
    assert path is not None

    workbook = openpyxl.load_workbook(path)
    sheet = workbook.worksheets[0]
    header = [cell.value for cell in sheet[1]]
    assert header == list(COLUMNS)
    assert sheet.max_row == 3  # header + 2 rows


def test_xlsx_cells_are_text_so_excel_cannot_reformat_them(tmp_path, log):
    """Excel would otherwise re-render 2026-03-01 in the local locale."""
    openpyxl = pytest.importorskip("openpyxl")
    writer = OutputWriter(tmp_path / "output", log)
    path = writer.write_xlsx([make_row(effective_date="2026-03-01",
                                       policy_number="00123.045")])
    sheet = openpyxl.load_workbook(path).worksheets[0]
    values = {cell.value for cell in sheet[2]}
    assert "2026-03-01" in values
    assert "00123.045" in values
    for cell in sheet[2]:
        assert cell.number_format == "@"


def test_write_dispatches_on_the_configured_format(tmp_path, log):
    writer = OutputWriter(tmp_path / "output", log)
    paths = writer.write([make_row()], output_format="csv")
    assert [path.name for path in paths] == ["output.csv"]

    paths = writer.write([make_row()], output_format="csv", also_xlsx=True)
    assert sorted(path.name for path in paths) == ["output.csv", "output.xlsx"]


# ---------------------------------------------------------------------------
# prepare_rows and the validation report
# ---------------------------------------------------------------------------
def test_prepare_rows_normalises_and_reports_problems():
    good = make_row()
    bad = make_row(document_url="")
    prepared, problems = prepare_rows([good, bad])
    assert len(prepared) == 2   # a flagged row is still delivered
    assert len(problems) == 1
    assert "document_url is required" in " ".join(problems[0][1])


def test_validation_report_is_written_when_there_are_problems(tmp_path, log):
    writer = OutputWriter(tmp_path / "output", log)
    path = writer.write_validation_report([("https://e.com/a.pdf", ["bad thing"])])
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "https://e.com/a.pdf" in text
    assert "bad thing" in text


def test_no_validation_report_when_everything_is_clean(tmp_path, log):
    writer = OutputWriter(tmp_path / "output", log)
    assert writer.write_validation_report([]) is None
