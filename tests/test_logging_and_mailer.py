"""
Logging and mailer tests.

Logging: the JSON-lines records must match the shape the case study specifies,
including the required event names and the exact field set on ``payer.done``.

Mailer: the important property is that email is *inert* when disabled and never
fails the run when enabled but broken. No test here sends real mail.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crawler import logging_setup as ev
from crawler.logging_setup import (
    finalise_latest,
    new_run_id,
    setup_logging,
    shutdown_logging,
)
from crawler.mailer import ENV_SMTP_PASSWORD, ENV_SMTP_USER, send_output


@pytest.fixture
def run_logger(tmp_path: Path):
    """A real logger writing into tmp_path; torn down after each test."""
    logger = setup_logging(tmp_path / "logs", log_level="DEBUG",
                           run_id="test-run", console=False)
    yield logger
    shutdown_logging()


def read_jsonl(tmp_path: Path, run_id: str = "test-run") -> list[dict]:
    path = tmp_path / "logs" / f"{run_id}.jsonl"
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Log record shape
# ---------------------------------------------------------------------------
def test_run_id_is_sortable_and_unique():
    first, second = new_run_id(), new_run_id()
    assert first != second
    assert len(first.split("-")) == 2
    assert first.split("-")[0].endswith("Z")


def test_both_a_json_and_a_human_log_are_written(tmp_path, run_logger):
    run_logger.info("test.event", "hello")
    shutdown_logging()
    assert (tmp_path / "logs" / "test-run.jsonl").is_file()
    assert (tmp_path / "logs" / "test-run.log").is_file()


def test_each_json_line_is_one_object_with_the_required_keys(tmp_path, run_logger):
    run_logger.info(ev.EV_DOC_FOUND, "found a doc", url="https://e.com/a.pdf")
    shutdown_logging()
    records = read_jsonl(tmp_path)
    assert len(records) == 1
    record = records[0]
    # The shape from the case study example.
    assert list(record)[:3] == ["ts", "lvl", "event"]
    assert record["event"] == "doc.found"
    assert record["lvl"] == "INFO"
    assert record["url"] == "https://e.com/a.pdf"
    assert record["ts"].endswith("Z")
    assert len(record["ts"]) == 20


def test_levels_are_rendered_as_info_warn_error(tmp_path, run_logger):
    run_logger.info("a")
    run_logger.warn("b")
    run_logger.error("c")
    shutdown_logging()
    assert [record["lvl"] for record in read_jsonl(tmp_path)] == ["INFO", "WARN", "ERROR"]


def test_event_specific_fields_become_top_level_keys(tmp_path, run_logger):
    run_logger.info(ev.EV_PAYER_DONE, "done", payer="UHC", attempted=10, found=7,
                    failed=3, skipped_robots=2, dupes_collapsed=1, elapsed_s=42.5)
    shutdown_logging()
    record = read_jsonl(tmp_path)[0]
    # Exactly the field set the brief requires on payer.done.
    for field in ("payer", "attempted", "found", "failed", "skipped_robots",
                  "dupes_collapsed", "elapsed_s"):
        assert field in record, field
    assert record["found"] == 7
    assert record["elapsed_s"] == 42.5


def test_none_valued_fields_are_omitted_rather_than_written_as_null(tmp_path, run_logger):
    run_logger.info("test.event", "msg", present="yes", absent=None)
    shutdown_logging()
    record = read_jsonl(tmp_path)[0]
    assert record["present"] == "yes"
    assert "absent" not in record


def test_bound_fields_are_added_to_every_record(tmp_path, run_logger):
    bound = run_logger.bind(payer="Horizon BCBSNJ")
    bound.info(ev.EV_ROBOTS_LOADED, "loaded", rules=12)
    bound.warn(ev.EV_ROBOTS_BLOCKED, "blocked", url="https://e.com/search")
    shutdown_logging()
    records = read_jsonl(tmp_path)
    assert all(record["payer"] == "Horizon BCBSNJ" for record in records)


def test_every_required_event_name_is_defined():
    """The brief lists these as the minimum event set."""
    required = {"payer.start", "robots.loaded", "sitemap.parsed", "robots.blocked",
                "doc.found", "fetch.failed", "payer.done"}
    defined = {ev.EV_PAYER_START, ev.EV_ROBOTS_LOADED, ev.EV_SITEMAP_PARSED,
               ev.EV_ROBOTS_BLOCKED, ev.EV_DOC_FOUND, ev.EV_FETCH_FAILED,
               ev.EV_PAYER_DONE}
    assert defined == required


def test_run_id_is_stamped_on_every_record(tmp_path, run_logger):
    run_logger.info("test.event", "msg")
    shutdown_logging()
    assert read_jsonl(tmp_path)[0]["run_id"] == "test-run"


def test_exception_details_are_captured(tmp_path, run_logger):
    try:
        raise ValueError("something broke")
    except ValueError:
        run_logger.error("payer.error", "crawl failed", exc_info=True, payer="UHC")
    shutdown_logging()
    record = read_jsonl(tmp_path)[0]
    assert "ValueError" in record["exc"]
    assert "something broke" in record["exc"]


def test_human_log_is_readable(tmp_path, run_logger):
    run_logger.bind(payer="UHC").info(ev.EV_DOC_FOUND, "Bariatric Surgery Policy")
    shutdown_logging()
    text = (tmp_path / "logs" / "test-run.log").read_text(encoding="utf-8")
    assert "doc.found" in text
    assert "UHC" in text
    assert "Bariatric Surgery Policy" in text


def test_latest_pointers_are_created(tmp_path, run_logger):
    run_logger.info("test.event", "msg")
    shutdown_logging()
    finalise_latest(tmp_path / "logs", "test-run")
    # Symlink on POSIX, plain copy on Windows; either way it must resolve.
    assert (tmp_path / "logs" / "latest.jsonl").exists()
    assert (tmp_path / "logs" / "latest.log").exists()


def test_sidecar_files_land_in_the_log_folder(tmp_path, run_logger):
    """The run summary table is a deliverable and lives beside the logs."""
    path = run_logger.write_sidecar("summary-test-run.txt", "attempted found failed\n")
    assert path.is_file()
    assert path.parent == tmp_path / "logs"
    assert "attempted" in path.read_text(encoding="utf-8")


def test_re_running_setup_does_not_duplicate_handlers(tmp_path):
    setup_logging(tmp_path / "logs", run_id="r1", console=False)
    logger = setup_logging(tmp_path / "logs", run_id="r2", console=False)
    logger.info("test.event", "once")
    shutdown_logging()
    assert len(read_jsonl(tmp_path, "r2")) == 1


def test_log_level_filters_debug_records(tmp_path):
    logger = setup_logging(tmp_path / "logs", log_level="INFO", run_id="lvl",
                           console=False)
    logger.debug("noise.event", "should not appear")
    logger.info("real.event", "should appear")
    shutdown_logging()
    events = [record["event"] for record in read_jsonl(tmp_path, "lvl")]
    assert events == ["real.event"]


# ---------------------------------------------------------------------------
# Mailer
# ---------------------------------------------------------------------------
def test_disabled_email_is_completely_inert(config, log, tmp_path, monkeypatch):
    """With email off the run must need no credentials and take no action."""
    monkeypatch.delenv(ENV_SMTP_USER, raising=False)
    monkeypatch.delenv(ENV_SMTP_PASSWORD, raising=False)
    config.email.enabled = False
    attachment = tmp_path / "output.csv"
    attachment.write_text("payer_name\n", encoding="utf-8")

    assert send_output(config, log, [attachment], "run-1") is False
    assert any(event == "email.disabled" for _, event, _ in log.records)
    assert not any(event == "email.failed" for _, event, _ in log.records)


def test_enabled_email_without_recipients_logs_an_error_and_returns_false(
    config, log, tmp_path
):
    config.email.enabled = True
    config.email.recipients = []
    attachment = tmp_path / "output.csv"
    attachment.write_text("payer_name\n", encoding="utf-8")

    assert send_output(config, log, [attachment], "run-1") is False
    failures = [fields for level, event, fields in log.records if event == "email.failed"]
    assert failures
    assert failures[0]["reason"] == "no_recipients"


def test_missing_attachment_is_reported_not_raised(config, log, tmp_path):
    config.email.enabled = True
    config.email.recipients = ["someone@example.com"]
    assert send_output(config, log, [tmp_path / "does-not-exist.csv"], "run-1") is False
    assert any(event == "email.failed" for _, event, _ in log.records)


def test_send_failure_never_raises_into_the_crawl(config, log, tmp_path, monkeypatch):
    """A delivery failure must log an ERROR but not fail the run."""
    config.email.enabled = True
    config.email.smtp_or_gmail_api = "smtp"
    config.email.recipients = ["someone@example.com"]
    monkeypatch.setenv(ENV_SMTP_USER, "sender@example.com")
    monkeypatch.setenv(ENV_SMTP_PASSWORD, "app-password-here")

    def explode(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr("smtplib.SMTP", explode)
    attachment = tmp_path / "output.csv"
    attachment.write_text("payer_name\n", encoding="utf-8")

    result = send_output(config, log, [attachment], "run-1")   # must not raise
    assert result is False
    assert any(event == "email.failed" for _, event, _ in log.records)


def test_missing_smtp_password_is_reported_clearly(config, log, tmp_path, monkeypatch):
    config.email.enabled = True
    config.email.smtp_or_gmail_api = "smtp"
    config.email.recipients = ["someone@example.com"]
    monkeypatch.setenv(ENV_SMTP_USER, "sender@example.com")
    monkeypatch.delenv(ENV_SMTP_PASSWORD, raising=False)
    attachment = tmp_path / "output.csv"
    attachment.write_text("payer_name\n", encoding="utf-8")

    assert send_output(config, log, [attachment], "run-1") is False
    messages = " ".join(fields["message"] for _, event, fields in log.records
                        if event == "email.failed")
    assert ENV_SMTP_PASSWORD in messages


def test_password_is_never_written_to_a_log(config, log, tmp_path, monkeypatch):
    """Defensive: SMTP libraries sometimes echo the credential in an exception."""
    secret = "super-secret-app-password"
    config.email.enabled = True
    config.email.smtp_or_gmail_api = "smtp"
    config.email.recipients = ["someone@example.com"]
    monkeypatch.setenv(ENV_SMTP_USER, "sender@example.com")
    monkeypatch.setenv(ENV_SMTP_PASSWORD, secret)

    def leaky(*args, **kwargs):
        raise OSError(f"auth failed for password {secret}")

    monkeypatch.setattr("smtplib.SMTP", leaky)
    attachment = tmp_path / "output.csv"
    attachment.write_text("payer_name\n", encoding="utf-8")

    send_output(config, log, [attachment], "run-1")
    logged = json.dumps([fields for _, _, fields in log.records])
    assert secret not in logged
    assert "***redacted***" in logged


def test_successful_send_builds_a_message_with_the_attachment(
    config, log, tmp_path, monkeypatch
):
    config.email.enabled = True
    config.email.smtp_or_gmail_api = "smtp"
    config.email.recipients = ["a@example.com", "b@example.com"]
    config.email.subject_template = "Payer Policy Crawl Output - {run_id}"
    monkeypatch.setenv(ENV_SMTP_USER, "sender@example.com")
    monkeypatch.setenv(ENV_SMTP_PASSWORD, "app-password")

    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["host"] = host
            sent["port"] = port

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def ehlo(self):
            pass

        def starttls(self, context=None):
            sent["tls"] = True

        def login(self, user, password):
            sent["user"] = user

        def send_message(self, message):
            sent["message"] = message

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    attachment = tmp_path / "output.csv"
    attachment.write_text("payer_name,document_url\n", encoding="utf-8")

    assert send_output(config, log, [attachment], "run-42",
                       summary_text="1 UHC 5 found") is True
    assert sent["tls"] is True
    assert sent["port"] == 587
    message = sent["message"]
    assert message["Subject"] == "Payer Policy Crawl Output - run-42"
    assert "a@example.com" in message["To"]
    attachments = [part.get_filename() for part in message.iter_attachments()]
    assert "output.csv" in attachments
    assert any(event == "email.sent" for _, event, _ in log.records)
