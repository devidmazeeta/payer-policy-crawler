"""
Checkpoint / resume tests.

The requirement being tested: killing the process and restarting with the same
command must continue rather than restart, and must not re-emit rows that were
already collected. Both the SQLite and JSON backends are exercised.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from crawler.discovery import normalise_url
from crawler.state import (
    STATUS_BLOCKED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_IN_PROGRESS,
    STATUS_PENDING,
    Checkpoint,
)

from .conftest import make_row


@pytest.fixture(params=["checkpoint.db", "checkpoint.json"])
def state_path(request, tmp_path: Path) -> Path:
    """Run every test against both backends; the contract is identical."""
    return tmp_path / "state" / request.param


def test_creates_its_parent_directory(state_path):
    with Checkpoint(state_path) as checkpoint:
        checkpoint.start_run("run-1", {"crawl": {}})
    assert state_path.parent.is_dir()


def test_payer_starts_pending(state_path):
    with Checkpoint(state_path) as checkpoint:
        assert checkpoint.payer_status("Payer One") == STATUS_PENDING
        assert not checkpoint.is_payer_complete("Payer One")


def test_done_payer_is_complete_and_skipped_on_resume(state_path):
    with Checkpoint(state_path) as checkpoint:
        checkpoint.mark_payer("Payer One", STATUS_DONE, "run-1")
    with Checkpoint(state_path) as reopened:
        assert reopened.payer_status("Payer One") == STATUS_DONE
        assert reopened.is_payer_complete("Payer One")


def test_blocked_payer_is_terminal(state_path):
    """A hard block will not resolve itself on a retry, so do not re-crawl."""
    with Checkpoint(state_path) as checkpoint:
        checkpoint.mark_payer("Payer One", STATUS_BLOCKED, "run-1")
        assert checkpoint.is_payer_complete("Payer One")


def test_failed_payer_is_retried_on_the_next_run(state_path):
    """A transient crash SHOULD be retried, unlike a block."""
    with Checkpoint(state_path) as checkpoint:
        checkpoint.mark_payer("Payer One", STATUS_FAILED, "run-1")
        assert not checkpoint.is_payer_complete("Payer One")


def test_in_progress_payer_is_retried(state_path):
    """A payer interrupted mid-crawl must be resumed, not treated as finished."""
    with Checkpoint(state_path) as checkpoint:
        checkpoint.mark_payer("Payer One", STATUS_IN_PROGRESS, "run-1")
        assert not checkpoint.is_payer_complete("Payer One")


def test_rows_survive_a_restart(state_path):
    rows = [make_row(document_url=f"https://e.com/p/{n}.pdf",
                     policy_number=f"MP-{n}") for n in range(3)]
    with Checkpoint(state_path) as checkpoint:
        assert checkpoint.save_rows(rows, "run-1") == 3
    with Checkpoint(state_path) as reopened:
        loaded = reopened.load_rows()
        assert len(loaded) == 3
        assert {row.document_url for row in loaded} == {row.document_url for row in rows}


def test_saving_the_same_url_twice_does_not_duplicate_it(state_path):
    """The core 'do not re-emit' guarantee."""
    row = make_row(document_url="https://e.com/p/a.pdf")
    with Checkpoint(state_path) as checkpoint:
        checkpoint.save_rows([row], "run-1")
        checkpoint.save_rows([row], "run-2")
        assert checkpoint.row_count() == 1
        assert len(checkpoint.load_rows()) == 1


def test_url_variants_are_treated_as_the_same_row(state_path):
    """Canonicalisation happens on the way into the checkpoint, too."""
    with Checkpoint(state_path) as checkpoint:
        checkpoint.save_rows([make_row(document_url="https://e.com/p/a.pdf")], "r1")
        checkpoint.save_rows(
            [make_row(document_url="https://E.com/p/a.pdf?utm_source=x")], "r2"
        )
        assert checkpoint.row_count() == 1


def test_known_row_urls_seeds_a_resumed_crawler(state_path):
    """Handed to PayerCrawler.seen_urls so a resumed payer skips what it has."""
    rows = [make_row(document_url=f"https://e.com/p/{n}.pdf", policy_number=f"P{n}")
            for n in range(3)]
    with Checkpoint(state_path) as checkpoint:
        checkpoint.save_rows(rows, "run-1")
        known = checkpoint.known_row_urls("Test Health Plan")
        assert len(known) == 3
        assert normalise_url("https://e.com/p/1.pdf") in known
        assert checkpoint.known_row_urls("Some Other Payer") == set()


def test_rows_can_be_loaded_per_payer(state_path):
    with Checkpoint(state_path) as checkpoint:
        checkpoint.save_rows([
            make_row(payer_name="Payer A", document_url="https://a.com/1.pdf"),
            make_row(payer_name="Payer B", document_url="https://b.com/1.pdf"),
        ], "run-1")
        assert len(checkpoint.load_rows("Payer A")) == 1
        assert len(checkpoint.load_rows()) == 2


def test_loaded_rows_are_schema_valid_after_rehydration(state_path):
    from crawler.schema import validate_row

    with Checkpoint(state_path) as checkpoint:
        checkpoint.save_rows([make_row()], "run-1")
        for row in checkpoint.load_rows():
            assert validate_row(row) == []


def test_local_path_survives_but_content_text_does_not(state_path):
    """local_path is useful on resume; content_text is large and disposable."""
    row = make_row()
    row.local_path = r"C:\downloads\thp\policy.pdf"
    row.content_text = "x" * 10000
    with Checkpoint(state_path) as checkpoint:
        checkpoint.save_rows([row], "run-1")
        loaded = checkpoint.load_rows()[0]
    assert loaded.local_path == row.local_path
    assert loaded.content_text == ""


def test_payer_stats_round_trip_for_the_summary_table(state_path):
    from crawler.discovery import PayerStats

    stats = PayerStats(payer="Payer One", attempted=10, found=7, failed=3,
                       skipped_robots=2, dupes_collapsed=1, elapsed_s=12.5)
    with Checkpoint(state_path) as checkpoint:
        checkpoint.mark_payer("Payer One", STATUS_DONE, "run-1", stats)
    with Checkpoint(state_path) as reopened:
        entry = reopened.payer_stats()["Payer One"]
        assert entry["attempted"] == 10
        assert entry["found"] == 7
        assert entry["failed"] == 3
        assert entry["skipped_robots"] == 2
        assert entry["status"] == STATUS_DONE


def test_block_evidence_is_persisted(state_path):
    """The summary must be able to say 'blocked' rather than 'found nothing'."""
    from crawler.discovery import PayerStats

    stats = PayerStats(payer="Payer One", blocked=True,
                       block_evidence="HTTP 403 / cloudflare at index")
    with Checkpoint(state_path) as checkpoint:
        checkpoint.mark_payer("Payer One", STATUS_BLOCKED, "run-1", stats)
    with Checkpoint(state_path) as reopened:
        entry = reopened.payer_stats()["Payer One"]
        assert entry["blocked"] is True
        assert "403" in entry["block_evidence"]


def test_disabling_resume_wipes_the_checkpoint(state_path):
    with Checkpoint(state_path) as checkpoint:
        checkpoint.save_rows([make_row()], "run-1")
        checkpoint.mark_payer("Payer One", STATUS_DONE, "run-1")
    with Checkpoint(state_path, enabled=False) as fresh:
        assert fresh.row_count() == 0
        assert fresh.payer_status("Payer One") == STATUS_PENDING


def test_resume_banner_is_empty_on_a_first_run(state_path):
    with Checkpoint(state_path) as checkpoint:
        assert checkpoint.resume_banner() == ""


def test_resume_banner_describes_prior_work(state_path):
    with Checkpoint(state_path) as checkpoint:
        checkpoint.start_run("run-1", {})
        checkpoint.mark_payer("Payer One", STATUS_DONE, "run-1")
        checkpoint.save_rows([make_row()], "run-1")
    with Checkpoint(state_path) as reopened:
        banner = reopened.resume_banner()
        assert "resuming" in banner
        assert "1 payer(s) already complete" in banner


def test_run_provenance_is_recorded(state_path):
    with Checkpoint(state_path) as checkpoint:
        checkpoint.start_run("run-1", {"crawl": {"max_docs_per_payer": 5}},
                             argv="python -m crawler.main --config config.yaml")
        checkpoint.finish_run("run-1")
        assert checkpoint.previous_runs() == 1


def test_visited_urls_are_tracked_per_payer(state_path):
    with Checkpoint(state_path) as checkpoint:
        checkpoint.mark_visited("https://e.com/a", "Payer A", "index")
        checkpoint.mark_visited("https://e.com/b", "Payer B", "index")
        assert checkpoint.visited_urls("Payer A") == {"https://e.com/a"}


def test_corrupt_json_checkpoint_starts_fresh_instead_of_crashing(tmp_path, log):
    """A broken checkpoint must not stop a run."""
    path = tmp_path / "checkpoint.json"
    path.write_text("{not json at all", encoding="utf-8")
    with Checkpoint(path, log=log) as checkpoint:
        assert checkpoint.row_count() == 0
    assert any(event == "resume.corrupt" for _, event, _ in log.records)


def test_saving_an_empty_row_list_is_a_no_op(state_path):
    with Checkpoint(state_path) as checkpoint:
        assert checkpoint.save_rows([], "run-1") == 0
        assert checkpoint.row_count() == 0


def test_full_resume_scenario(tmp_path):
    """
    End-to-end simulation of the documented resume behaviour.

    Run 1 completes two payers and is then killed. Run 2 must skip both, keep
    their rows, and add only the third payer's.
    """
    path = tmp_path / "state" / "checkpoint.db"

    with Checkpoint(path) as run_one:
        run_one.start_run("run-1", {})
        for name in ("Payer A", "Payer B"):
            run_one.save_rows(
                [make_row(payer_name=name,
                          document_url=f"https://{name[-1].lower()}.com/p.pdf")],
                "run-1",
            )
            run_one.mark_payer(name, STATUS_DONE, "run-1")
        run_one.mark_payer("Payer C", STATUS_IN_PROGRESS, "run-1")
        # process killed here

    with Checkpoint(path) as run_two:
        run_two.start_run("run-2", {})
        assert run_two.is_payer_complete("Payer A")
        assert run_two.is_payer_complete("Payer B")
        assert not run_two.is_payer_complete("Payer C")

        run_two.save_rows(
            [make_row(payer_name="Payer C", document_url="https://c.com/p.pdf")],
            "run-2",
        )
        run_two.mark_payer("Payer C", STATUS_DONE, "run-2")

        # The final dataset is assembled from the checkpoint, so it covers all
        # three payers with no duplicates.
        rows = run_two.load_rows()
        assert len(rows) == 3
        assert {row.payer_name for row in rows} == {"Payer A", "Payer B", "Payer C"}
        urls = [str(row.document_url) for row in rows]
        assert len(urls) == len(set(urls))
