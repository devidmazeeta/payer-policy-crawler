"""
CLI and end-to-end orchestration tests.

The end-to-end test runs the real ``main()`` against a simulated two-payer world
served by a patched fetcher, and asserts on the delivered artefacts: output.csv,
the log files, the summary table, and the checkpoint. It also covers the two
behaviours that are easy to get wrong and hard to notice - failure isolation
(one payer crashing must not lose the others) and resume (a second run must not
duplicate rows).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import httpx
import pytest

from crawler.main import (
    EXIT_CONFIG,
    EXIT_OK,
    build_parser,
    main,
    render_summary,
)
from crawler.schema import COLUMNS
from crawler.seeds import Payer

SEED_CSV = (
    "payer_id,payer_name,payer_alias,hint_host,default_state_or_region,"
    "default_line_of_business,seed_paths\n"
    "1,Payer Alpha,ALPHA,alpha.test,NJ,Commercial,/provider/medical-policies\n"
    "2,Payer Beta,BETA,beta.test,TN,Medicare,/provider/policies\n"
)

PDF = b"%PDF-1.7\nBariatric Surgery Medical Policy\nPolicy Number: CS100\n%%EOF"

INDEX_HTML = """<html><head><title>Medical Policies</title></head><body>
<h1>Medical Policies</h1>
<a href="/assets/policy/bariatric-surgery.pdf">Bariatric Surgery Medical Policy</a>
<a href="/assets/policy/cardiac-imaging.pdf">Cardiac Imaging Medical Policy</a>
</body></html>
"""


def handler(request: httpx.Request) -> httpx.Response:
    """Serve two small, well-behaved payer sites."""
    path = request.url.path
    if path == "/robots.txt":
        return httpx.Response(
            200,
            text=f"User-agent: *\nDisallow: /private/\n"
                 f"Sitemap: https://{request.url.host}/sitemap.xml\n",
            headers={"content-type": "text/plain"},
        )
    if path == "/sitemap.xml":
        return httpx.Response(
            200,
            text='<?xml version="1.0"?><urlset><url>'
                 f"<loc>https://{request.url.host}/provider/medical-policies</loc>"
                 "<lastmod>2026-02-01</lastmod></url></urlset>",
            headers={"content-type": "application/xml"},
        )
    if path in ("/", "/provider/medical-policies", "/provider/policies"):
        return httpx.Response(200, text=INDEX_HTML,
                              headers={"content-type": "text/html"})
    if path.startswith("/assets/policy/"):
        return httpx.Response(200, content=PDF,
                              headers={"content-type": "application/pdf"})
    return httpx.Response(404, text="not found")


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    """A throwaway project directory with a seed list and a config file."""
    (tmp_path / "payer_seed_list.csv").write_text(SEED_CSV, encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        "crawl:\n"
        "  max_docs_per_payer: 3\n"
        "  payer_seed_csv: payer_seed_list.csv\n"
        "  payer_range: 1-2\n"
        "  concurrency_cap: 4\n"
        # 10 req/s is the highest the validator accepts as polite. No real host
        # is touched (MockTransport), so this only affects test wall-clock.
        "  per_domain_rate_limit_per_sec: 10.0\n"
        "  retry_limit: 1\n"
        "  backoff_base_seconds: 0.001\n"
        '  user_agent: "TestCrawler/1.0 (+contact: test@example.com)"\n'
        "logging:\n"
        "  log_dir: logs/\n"
        "  console: false\n"
        "output:\n"
        "  output_dir: output/\n"
        "  downloads_dir: downloads/\n"
        "resume:\n"
        "  state_file: state/checkpoint.db\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def mock_http(monkeypatch):
    """Patch Fetcher.__aenter__ so the real client is replaced by MockTransport."""
    from crawler.fetcher import Fetcher

    original = Fetcher.__aenter__

    async def patched(self):
        await original(self)
        await self._client.aclose()
        self._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
            headers={"User-Agent": self.config.crawl.user_agent},
        )
        return self

    monkeypatch.setattr(Fetcher, "__aenter__", patched)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
def test_config_flags_default_to_none_so_they_cannot_outrank_the_file():
    """The mechanism that implements CLI > file > default."""
    parser = build_parser()
    args = parser.parse_args([])
    for dest in ("max_docs_per_payer", "payer_range", "payer_csv", "concurrency_cap",
                 "rate_limit", "retry_limit", "output_format", "log_level",
                 "state_file", "email_enabled", "resume_enabled", "save_downloads"):
        assert getattr(args, dest) is None, dest


def test_documented_command_line_parses():
    parser = build_parser()
    args = parser.parse_args([
        "--config", "config.yaml", "--max-docs-per-payer", "20",
        "--payer-range", "1-5", "--payer-csv", "payer_seed_list.csv",
    ])
    assert args.config == "config.yaml"
    assert args.max_docs_per_payer == 20
    assert args.payer_range == "1-5"


def test_negative_flags_are_explicit_constants():
    parser = build_parser()
    args = parser.parse_args(["--no-resume", "--no-downloads"])
    assert args.resume_enabled is False
    assert args.save_downloads is False


# ---------------------------------------------------------------------------
# Informational modes (no fetching)
# ---------------------------------------------------------------------------
def test_dry_run_validates_and_exits_cleanly(project, capsys):
    assert main(["--config", "config.yaml", "--dry-run"]) == EXIT_OK
    output = capsys.readouterr().out
    assert "Payer Alpha" in output
    assert "Payer Beta" in output
    assert "22 columns" in output
    assert "nothing was fetched" in output
    # Nothing may have been written.
    assert not (project / "output" / "output.csv").exists()


def test_show_config_prints_the_merged_configuration(project, capsys):
    assert main(["--config", "config.yaml", "--show-config",
                 "--max-docs-per-payer", "9"]) == EXIT_OK
    output = capsys.readouterr().out
    payload = json.loads(output.split("\n#")[0])
    # The CLI override must be visible in the merged result.
    assert payload["crawl"]["max_docs_per_payer"] == 9
    assert "precedence" in output


def test_explain_dedupe_prints_the_rule(capsys):
    assert main(["--explain-dedupe"]) == EXIT_OK
    output = capsys.readouterr().out
    assert "content_hash_sha256" in output
    assert "policy_number" in output


def test_bad_config_fails_fast_with_a_readable_message(project, capsys):
    (project / "bad.yaml").write_text("crawl:\n  concurrency_cap: 0\n", encoding="utf-8")
    assert main(["--config", "bad.yaml"]) == EXIT_CONFIG
    assert "concurrency_cap" in capsys.readouterr().err


def test_missing_config_file_is_an_error(project, capsys):
    assert main(["--config", "nope.yaml"]) == EXIT_CONFIG
    assert "not found" in capsys.readouterr().err


def test_invalid_payer_range_is_an_error(project, capsys):
    assert main(["--config", "config.yaml", "--payer-range", "1-99"]) == EXIT_CONFIG
    assert "payer_range" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------
def read_output(project: Path) -> list[dict[str, str]]:
    path = project / "output" / "output.csv"
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_single_command_run_produces_every_deliverable(project, mock_http, capsys):
    """python -m crawler.main --config config.yaml, end to end."""
    exit_code = main(["--config", "config.yaml"])
    assert exit_code == EXIT_OK

    # 1. the dataset
    output_path = project / "output" / "output.csv"
    assert output_path.is_file()
    rows = read_output(project)
    assert rows
    with output_path.open("r", encoding="utf-8", newline="") as handle:
        assert list(csv.DictReader(handle).fieldnames or []) == list(COLUMNS)

    # 2. per-payer document downloads
    downloads = project / "downloads"
    assert downloads.is_dir()
    assert any(child.is_dir() for child in downloads.iterdir())

    # 3. the logs (both flavours) and the summary sidecar
    logs = project / "logs"
    assert list(logs.glob("*.jsonl"))
    assert list(logs.glob("*.log"))
    assert (logs / "summary-latest.txt").is_file()

    # 4. the checkpoint
    assert (project / "state" / "checkpoint.db").is_file()

    # 5. the summary table on stdout
    output = capsys.readouterr().out
    assert "Per-payer summary" in output
    assert "attempted" in output
    assert "Payer Alpha" in output


def test_every_output_row_is_schema_valid(project, mock_http):
    from crawler.schema import DocumentRow, validate_row

    main(["--config", "config.yaml"])
    for record in read_output(project):
        row = DocumentRow(**{key: value for key, value in record.items()
                             if key in COLUMNS})
        assert validate_row(row) == [], f"{record['document_url']}: {validate_row(row)}"


def test_document_url_is_unique_across_the_whole_file(project, mock_http):
    main(["--config", "config.yaml"])
    urls = [record["document_url"] for record in read_output(project)]
    assert len(urls) == len(set(urls))


def test_both_payers_appear_and_the_budget_is_respected(project, mock_http):
    main(["--config", "config.yaml"])
    rows = read_output(project)
    by_payer: dict[str, int] = {}
    for record in rows:
        by_payer[record["payer_name"]] = by_payer.get(record["payer_name"], 0) + 1
    assert set(by_payer) == {"Payer Alpha", "Payer Beta"}
    for count in by_payer.values():
        assert count <= 3   # max_docs_per_payer


def test_cli_override_beats_the_config_file_end_to_end(project, mock_http):
    main(["--config", "config.yaml", "--max-docs-per-payer", "1"])
    rows = read_output(project)
    by_payer: dict[str, int] = {}
    for record in rows:
        by_payer[record["payer_name"]] = by_payer.get(record["payer_name"], 0) + 1
    for count in by_payer.values():
        assert count <= 1


def test_payer_range_selects_a_subset(project, mock_http):
    main(["--config", "config.yaml", "--payer-range", "2"])
    assert {record["payer_name"] for record in read_output(project)} == {"Payer Beta"}


def test_required_log_events_are_all_emitted(project, mock_http):
    main(["--config", "config.yaml"])
    jsonl = sorted((project / "logs").glob("*.jsonl"))[-1]
    events = {json.loads(line)["event"]
              for line in jsonl.read_text(encoding="utf-8").splitlines() if line.strip()}
    for required in ("payer.start", "robots.loaded", "sitemap.parsed",
                     "doc.found", "payer.done"):
        assert required in events, f"missing {required}"


def test_payer_done_carries_the_required_field_set(project, mock_http):
    main(["--config", "config.yaml"])
    jsonl = sorted((project / "logs").glob("*.jsonl"))[-1]
    records = [json.loads(line) for line
               in jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    done = [record for record in records if record["event"] == "payer.done"]
    assert done
    for record in done:
        for field in ("payer", "attempted", "found", "failed", "skipped_robots",
                      "dupes_collapsed", "elapsed_s"):
            assert field in record, field


def test_resume_does_not_re_emit_rows(project, mock_http):
    """Second run with the same command must not duplicate anything."""
    main(["--config", "config.yaml"])
    first = read_output(project)
    main(["--config", "config.yaml"])
    second = read_output(project)

    assert len(second) == len(first)
    urls = [record["document_url"] for record in second]
    assert len(urls) == len(set(urls))


def test_no_resume_starts_from_scratch(project, mock_http):
    main(["--config", "config.yaml"])
    baseline = len(read_output(project))
    main(["--config", "config.yaml", "--no-resume"])
    assert len(read_output(project)) == baseline


def test_one_payer_crashing_does_not_lose_the_others(project, mock_http, monkeypatch):
    """Failure isolation: the run continues and reports the failure."""
    from crawler.discovery import PayerCrawler

    original_run = PayerCrawler.run

    async def flaky_run(self):
        if self.payer.payer_name == "Payer Alpha":
            raise RuntimeError("simulated crawler explosion")
        return await original_run(self)

    monkeypatch.setattr(PayerCrawler, "run", flaky_run)
    exit_code = main(["--config", "config.yaml"])

    assert exit_code == EXIT_OK
    payers = {record["payer_name"] for record in read_output(project)}
    assert "Payer Beta" in payers     # the healthy payer still delivered

    jsonl = sorted((project / "logs").glob("*.jsonl"))[-1]
    text = jsonl.read_text(encoding="utf-8")
    assert "payer.error" in text
    assert "simulated crawler explosion" in text


def test_xlsx_can_be_produced_alongside_the_csv(project, mock_http):
    pytest.importorskip("openpyxl")
    main(["--config", "config.yaml", "--also-xlsx"])
    assert (project / "output" / "output.csv").is_file()
    assert (project / "output" / "output.xlsx").is_file()


def test_run_needs_no_credentials_when_email_is_off(project, mock_http, monkeypatch):
    from crawler.mailer import ENV_SMTP_PASSWORD, ENV_SMTP_USER

    monkeypatch.delenv(ENV_SMTP_USER, raising=False)
    monkeypatch.delenv(ENV_SMTP_PASSWORD, raising=False)
    assert main(["--config", "config.yaml"]) == EXIT_OK


# ---------------------------------------------------------------------------
# Summary rendering
# ---------------------------------------------------------------------------
def make_payer(name: str, position: int) -> Payer:
    return Payer(payer_name=name, hint_host=f"{name.lower()}.test", position=position)


def test_summary_distinguishes_blocked_from_publishes_nothing():
    """The distinction the brief calls out as scoring-relevant."""
    payers = [make_payer("Blocked Payer", 1), make_payer("Empty Payer", 2)]
    stats = {
        "Blocked Payer": {"attempted": 4, "found": 0, "failed": 4,
                          "skipped_robots": 0, "blocked": True,
                          "block_evidence": "HTTP 403 / cloudflare", "elapsed_s": 3.0},
        "Empty Payer": {"attempted": 6, "found": 0, "failed": 0,
                        "skipped_robots": 0, "blocked": False, "elapsed_s": 5.0},
    }
    table = render_summary(stats, payers)
    assert "BLOCKED" in table
    assert "nothing publishable found" in table


def test_summary_flags_robots_limited_payers():
    payers = [make_payer("Robots Payer", 1)]
    stats = {"Robots Payer": {"attempted": 0, "found": 0, "failed": 0,
                              "skipped_robots": 12, "blocked": False,
                              "elapsed_s": 2.0}}
    assert "robots-disallowed" in render_summary(stats, payers)


def test_summary_totals_are_correct():
    payers = [make_payer("A", 1), make_payer("B", 2)]
    stats = {
        "A": {"attempted": 10, "found": 7, "failed": 3, "skipped_robots": 1,
              "dupes_collapsed": 2, "elapsed_s": 1.0},
        "B": {"attempted": 5, "found": 4, "failed": 1, "skipped_robots": 0,
              "dupes_collapsed": 1, "elapsed_s": 2.0},
    }
    table = render_summary(stats, payers)
    assert "TOTAL" in table
    total_line = [line for line in table.splitlines() if "TOTAL" in line][0]
    assert "15" in total_line   # attempted
    assert "11" in total_line   # found


def test_summary_includes_checkpointed_payers_outside_this_selection():
    """Their rows are in the file, so they belong in the table."""
    payers = [make_payer("A", 1)]
    stats = {
        "A": {"attempted": 1, "found": 1, "failed": 0, "skipped_robots": 0,
              "elapsed_s": 1.0},
        "Z From A Previous Run": {"attempted": 3, "found": 3, "failed": 0,
                                  "skipped_robots": 0, "elapsed_s": 1.0},
    }
    assert "Z From A Previous Run" in render_summary(stats, payers)
