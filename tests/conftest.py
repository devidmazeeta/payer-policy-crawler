"""
Shared pytest fixtures.

Deliberately offline: nothing in the test suite makes a network request. The
fetcher tests drive a fake transport, and the discovery tests use canned HTML
and sitemap strings, so the suite is fast, deterministic and safe to run in CI.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# Make the project root importable when pytest is invoked from elsewhere.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from crawler.config import AppConfig  # noqa: E402
from crawler.schema import DocumentRow, normalise_row, utc_now_iso  # noqa: E402
from crawler.seeds import Payer  # noqa: E402


class RecordingLog:
    """
    Test double for :class:`crawler.logging_setup.RunLogger`.

    Records every event so a test can assert that, for example, a
    ``robots.blocked`` record was emitted, without touching the filesystem.
    """

    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, Any]]] = []
        self.run_id = "test-run"

    def _record(self, level: str, event: str, message: str, fields: dict[str, Any]) -> None:
        self.records.append((level, event, {"message": message, **fields}))

    def debug(self, event: str, message: str = "", **fields: Any) -> None:
        self._record("DEBUG", event, message, fields)

    def info(self, event: str, message: str = "", **fields: Any) -> None:
        self._record("INFO", event, message, fields)

    def warn(self, event: str, message: str = "", **fields: Any) -> None:
        self._record("WARN", event, message, fields)

    def error(self, event: str, message: str = "", exc_info: bool = False, **fields: Any) -> None:
        self._record("ERROR", event, message, fields)

    def bind(self, **fields: Any) -> "RecordingLog":
        # Bound fields are irrelevant to the assertions, so the same object is
        # returned; this keeps the double simple while matching the interface.
        return self

    def write_sidecar(self, filename: str, text: str) -> Path:
        return Path(filename)

    def events(self, name: str) -> list[dict[str, Any]]:
        """All recorded field dicts for events named *name*."""
        return [fields for _, event, fields in self.records if event == name]

    def event_names(self) -> list[str]:
        return [event for _, event, _ in self.records]


@pytest.fixture
def log() -> RecordingLog:
    """A recording logger for tests that need to assert on emitted events."""
    return RecordingLog()


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    """
    A valid :class:`AppConfig` with all paths pointed inside ``tmp_path``.

    Rates are set high because no real requests are made; keeping them low would
    only make the suite slow.
    """
    cfg = AppConfig()
    cfg.base_dir = str(tmp_path)
    cfg.crawl.per_domain_rate_limit_per_sec = 1000.0
    cfg.crawl.concurrency_cap = 4
    cfg.crawl.retry_limit = 2
    cfg.crawl.backoff_base_seconds = 0.001
    cfg.crawl.max_docs_per_payer = 5
    cfg.crawl.user_agent = "TestCrawler/1.0 (+contact: test@example.com)"
    cfg.output.save_downloads = False
    cfg.resume.state_file = str(tmp_path / "state" / "checkpoint.db")
    cfg.logging.log_dir = str(tmp_path / "logs")
    cfg.output.output_dir = str(tmp_path / "output")
    cfg.output.downloads_dir = str(tmp_path / "downloads")
    return cfg


@pytest.fixture
def payer() -> Payer:
    """A representative payer seed row."""
    return Payer(
        payer_name="Test Health Plan",
        payer_alias="THP",
        hint_host="example-payer.com",
        default_state_or_region="NJ",
        default_line_of_business="Commercial",
        seed_paths=["/providers/medical-policies"],
        position=1,
    )


def make_row(**overrides: Any) -> DocumentRow:
    """
    Build a schema-valid :class:`DocumentRow`, overriding any field.

    Used throughout the dedupe and schema tests so each test states only the
    fields it actually cares about.
    """
    defaults: dict[str, Any] = {
        "payer_name": "Test Health Plan",
        "payer_alias": "THP",
        "state_or_region": "NJ",
        "line_of_business": "Commercial",
        "document_title": "Bariatric Surgery Medical Policy",
        "document_type": "medical_policy",
        "document_url": "https://example-payer.com/policies/bariatric.pdf",
        "source_page_url": "https://example-payer.com/policies",
        "discovery_path": "seed:example-payer.com > robots.txt > sitemap.xml",
        "file_type": "pdf",
        "policy_number": "MP-001",
        "effective_date": "2026-01-01",
        "last_updated_date": "2026-01-15",
        "http_status": 200,
        "content_hash_sha256": "a" * 64,
        "file_size_bytes": 12345,
        "requires_auth": "N",
        "render_mode": "static",
        "extraction_method": "sitemap|pdf_text",
        "confidence_score": 0.90,
        "scrape_timestamp_utc": utc_now_iso(),
        "notes": "",
    }
    defaults.update(overrides)
    return normalise_row(DocumentRow(**defaults))
