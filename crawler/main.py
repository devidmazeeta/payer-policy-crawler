"""
CLI entrypoint: argument parsing, config merge, run orchestration, reporting.

Single command to run the whole project::

    python -m crawler.main --config config.yaml

with overrides available for every config value, e.g.::

    python -m crawler.main --config config.yaml --max-docs-per-payer 20 \\
        --payer-range 1-5 --payer-csv payer_seed_list.csv

Orchestration shape:

* payers are crawled concurrently, but bounded - ``crawl.concurrency_cap`` caps
  total in-flight requests globally and each host has its own token bucket, so
  adding payers adds parallelism only across *different* hosts;
* each payer runs inside :func:`_run_payer`, which catches everything: one
  payer's crash is logged, recorded as ``failed`` in the checkpoint, and the run
  continues. Failure isolation is the point;
* rows are checkpointed as each payer finishes, so a kill at any moment loses at
  most the payer that was in flight;
* the final dataset is assembled from the checkpoint (not just from this
  process's memory), which is what makes a resumed run produce a complete file.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from . import logging_setup as ev
from .config import AppConfig, ConfigError, build_config, describe_precedence
from .dedupe import dedupe_rows, rule_description
from .discovery import PayerCrawler, normalise_url
from .fetcher import Fetcher
from .logging_setup import finalise_latest, new_run_id, setup_logging, shutdown_logging
from .mailer import send_output
from .robots import RobotsCache
from .schema import COLUMNS, DocumentRow
from .seeds import Payer, describe_payers, load_payers, select_payers
from .state import STATUS_BLOCKED, STATUS_DONE, STATUS_FAILED, STATUS_IN_PROGRESS, Checkpoint
from .storage import Downloader, OutputWriter, prepare_rows

#: Exit codes. 0 success, 1 configuration/usage error, 2 the run produced no
#: rows at all (worth a distinct code for CI), 130 interrupted.
EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_EMPTY = 2
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    """
    Build the CLI.

    Every ``--flag`` here that maps to a config value defaults to ``None``, which
    is what implements the precedence rule: a flag left unset is invisible to the
    merge, so the config file (and then the built-in default) keeps its value.
    Never give these argparse defaults - that would silently outrank the file.
    """
    parser = argparse.ArgumentParser(
        prog="python -m crawler.main",
        description="Payer Policy Document Discovery - crawl public payer sites for "
                    "provider/medical policy documents.",
        epilog=describe_precedence(),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--config", default=None, metavar="PATH",
        help="path to config.yaml / config.json (all values overridable below)",
    )

    crawl = parser.add_argument_group("crawl")
    crawl.add_argument("--max-docs-per-payer", type=int, default=None,
                       help="cap on emitted documents per payer")
    crawl.add_argument("--payer-csv", default=None, metavar="PATH",
                       help="payer seed CSV")
    crawl.add_argument("--payer-range", default=None, metavar="SPEC",
                       help="1-based selection over the seed list, e.g. '1-5' or '3,5,7'")
    crawl.add_argument("--concurrency-cap", type=int, default=None,
                       help="max simultaneous in-flight HTTP requests")
    crawl.add_argument("--rate-limit", type=float, default=None, metavar="REQ_PER_SEC",
                       help="per-domain rate limit in requests/second")
    crawl.add_argument("--retry-limit", type=int, default=None,
                       help="retries after the first attempt")
    crawl.add_argument("--backoff-base", type=float, default=None, metavar="SECONDS",
                       help="first backoff sleep; doubles per retry when exponential")
    crawl.add_argument("--backoff-strategy", choices=["exponential", "fixed"], default=None,
                       help="retry backoff strategy")
    crawl.add_argument("--timeout", type=int, default=None, metavar="SECONDS",
                       help="per-request timeout")
    crawl.add_argument("--user-agent", default=None,
                       help="User-Agent string (must contain a contact email or URL)")
    crawl.add_argument("--max-index-pages", type=int, default=None,
                       help="cap on index/category pages crawled per payer")
    crawl.add_argument("--proxy-url", default=None,
                       help="optional outbound proxy; disabled by default")

    logging_group = parser.add_argument_group("logging")
    logging_group.add_argument("--log-dir", default=None, help="log output folder")
    logging_group.add_argument(
        "--log-level", default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], help="log level",
    )
    logging_group.add_argument("--quiet", action="store_true",
                               help="do not mirror the human log to stdout")

    output = parser.add_argument_group("output")
    output.add_argument("--output-dir", default=None, help="dataset output folder")
    output.add_argument("--output-format", choices=["csv", "xlsx"], default=None,
                        help="dataset format")
    output.add_argument("--downloads-dir", default=None,
                        help="root for downloads/<payer>/ document copies")
    output.add_argument("--no-downloads", dest="save_downloads", action="store_const",
                        const=False, default=None,
                        help="identify and hash documents but do not save copies")
    output.add_argument("--also-xlsx", action="store_true",
                        help="write output.xlsx alongside output.csv")
    output.add_argument("--strict-schema", action="store_true",
                        help="exit non-zero if any row fails schema validation")

    email = parser.add_argument_group("email (optional, off by default)")
    email.add_argument("--email-enabled", dest="email_enabled", action="store_const",
                       const=True, default=None,
                       help="email the finished dataset (needs credentials; see README)")
    email.add_argument("--email-recipients", default=None, metavar="ADDR[,ADDR]",
                       help="comma-separated recipients")
    email.add_argument("--email-transport", choices=["gmail_api", "smtp"], default=None,
                       help="delivery transport")

    resume = parser.add_argument_group("resume")
    resume.add_argument("--state-file", default=None,
                        help="checkpoint path (.db for SQLite, .json for JSON)")
    resume.add_argument("--no-resume", dest="resume_enabled", action="store_const",
                        const=False, default=None,
                        help="wipe the checkpoint and crawl from scratch")

    informational = parser.add_argument_group("informational")
    informational.add_argument("--dry-run", action="store_true",
                               help="validate config and seed list, list payers, exit")
    informational.add_argument("--show-config", action="store_true",
                               help="print the merged configuration and exit")
    informational.add_argument("--explain-dedupe", action="store_true",
                               help="print the near-duplicate collapsing rule and exit")
    return parser


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def render_summary(stats_by_payer: dict[str, dict[str, Any]], payers: Sequence[Payer]) -> str:
    """
    Render the per-payer attempted/found/failed/skipped table.

    The ``outcome`` column is the important one: it is what makes the log
    self-explanatory about *why* a payer is empty. ``BLOCKED`` means automated
    access was refused (with evidence in the log); ``no documents`` means we were
    allowed in and found nothing publishable; ``ROBOTS`` means robots.txt put the
    relevant paths out of bounds.
    """
    header = (
        f"{'#':>3}  {'payer':<34} {'attempted':>9} {'found':>6} {'failed':>7} "
        f"{'skipped':>8} {'dupes':>6} {'elapsed':>8}  outcome"
    )
    lines = [header, "-" * len(header)]
    totals = {"attempted": 0, "found": 0, "failed": 0,
              "skipped_robots": 0, "dupes_collapsed": 0}

    ordered = list(payers) or []
    seen: set[str] = set()
    for payer in ordered:
        stats = stats_by_payer.get(payer.payer_name, {})
        seen.add(payer.payer_name)
        lines.append(_summary_line(payer.position, payer.payer_name, stats))
        for key in totals:
            totals[key] += int(stats.get(key, 0) or 0)
    # Payers present in the checkpoint but not in this run's selection still
    # belong in the table, because their rows are in the output file.
    position = len(ordered)
    for name, stats in stats_by_payer.items():
        if name in seen:
            continue
        position += 1
        lines.append(_summary_line(position, name, stats))
        for key in totals:
            totals[key] += int(stats.get(key, 0) or 0)

    lines.append("-" * len(header))
    lines.append(
        f"{'':>3}  {'TOTAL':<34} {totals['attempted']:>9} {totals['found']:>6} "
        f"{totals['failed']:>7} {totals['skipped_robots']:>8} "
        f"{totals['dupes_collapsed']:>6} {'':>8}"
    )
    return "\n".join(lines)


def _summary_line(position: int, payer_name: str, stats: dict[str, Any]) -> str:
    """Format one row of the summary table, including the outcome verdict."""
    found = int(stats.get("found", 0) or 0)
    status = str(stats.get("status", "") or "")
    blocked = bool(stats.get("blocked"))
    skipped = int(stats.get("skipped_robots", 0) or 0)

    if blocked and found == 0:
        outcome = "BLOCKED - automated access refused (see log for evidence)"
    elif blocked:
        outcome = f"partial - {found} found before being blocked"
    elif status == STATUS_FAILED:
        outcome = "FAILED - crawl error, see log"
    elif found == 0 and skipped > 0:
        outcome = f"no documents - {skipped} candidate(s) robots-disallowed"
    elif found == 0:
        outcome = "no documents - crawled successfully, nothing publishable found"
    else:
        outcome = "ok"
    return (
        f"{position:>3}  {payer_name[:34]:<34} "
        f"{int(stats.get('attempted', 0) or 0):>9} {found:>6} "
        f"{int(stats.get('failed', 0) or 0):>7} {skipped:>8} "
        f"{int(stats.get('dupes_collapsed', 0) or 0):>6} "
        f"{float(stats.get('elapsed_s', 0) or 0):>7.1f}s  {outcome}"
    )


# ---------------------------------------------------------------------------
# Per-payer execution
# ---------------------------------------------------------------------------
async def _run_payer(
    payer: Payer,
    config: AppConfig,
    fetcher: Fetcher,
    robots: RobotsCache,
    downloader: Downloader,
    log: Any,
    checkpoint: Checkpoint,
    semaphore: asyncio.Semaphore,
) -> tuple[Payer, list[DocumentRow], dict[str, Any]]:
    """
    Crawl one payer with full failure isolation.

    Returns ``(payer, rows, stats_dict)``. Any exception is caught, logged with a
    traceback and turned into a ``failed`` status - the run continues with the
    other payers, which is the behaviour the brief asks for.

    *semaphore* bounds how many payers are in flight at once. It is separate from
    the fetcher's request cap: a small number of payers each trickling requests
    to their own host is exactly the shape of polite parallelism we want.
    """
    async with semaphore:
        if checkpoint.is_payer_complete(payer.payer_name):
            stats = checkpoint.payer_stats().get(payer.payer_name, {})
            log.info(
                ev.EV_RESUME,
                f"{payer.payer_name} already complete in the checkpoint "
                f"({stats.get('found', 0)} row(s)); skipping",
                payer=payer.payer_name, found=stats.get("found", 0),
            )
            return payer, [], stats

        checkpoint.mark_payer(payer.payer_name, STATUS_IN_PROGRESS, log.run_id)
        crawler = PayerCrawler(
            payer=payer, config=config, fetcher=fetcher, robots=robots,
            downloader=downloader, log=log, state=checkpoint,
        )
        # Seed the crawler with URLs already collected in a previous run segment
        # so a resumed payer does not re-fetch what it already has.
        already = checkpoint.known_row_urls(payer.payer_name)
        if already:
            crawler.seen_urls.update(already)
            log.info(
                ev.EV_RESUME,
                f"{payer.payer_name}: skipping {len(already)} URL(s) already collected",
                payer=payer.payer_name, known_urls=len(already),
            )

        try:
            rows, stats = await crawler.run()
        except asyncio.CancelledError:
            checkpoint.mark_payer(payer.payer_name, STATUS_FAILED, log.run_id)
            raise
        except Exception as exc:
            log.error(
                "payer.error",
                f"{payer.payer_name} crawl failed: {type(exc).__name__}: {exc}",
                payer=payer.payer_name, error=str(exc), exc_info=True,
            )
            # Partial rows are still worth keeping: they were genuinely found.
            partial = list(crawler.rows)
            crawler.stats.elapsed_s = crawler.stats.elapsed_s or 0.0
            fields = crawler.stats.as_log_fields()
            fields["status"] = STATUS_FAILED
            if partial:
                checkpoint.save_rows(partial, log.run_id)
            checkpoint.mark_payer(payer.payer_name, STATUS_FAILED, log.run_id, crawler.stats)
            return payer, partial, fields

        # Persist immediately: a kill after this point cannot lose this payer.
        written = checkpoint.save_rows(rows, log.run_id)
        status = STATUS_BLOCKED if (stats.blocked and stats.found == 0) else STATUS_DONE
        checkpoint.mark_payer(payer.payer_name, status, log.run_id, stats)

        fields = stats.as_log_fields()
        fields["status"] = status
        log.info(
            ev.EV_PAYER_DONE,
            f"{payer.payer_name}: attempted={stats.attempted} found={stats.found} "
            f"failed={stats.failed} skipped_robots={stats.skipped_robots} "
            f"dupes_collapsed={stats.dupes_collapsed} elapsed_s={stats.elapsed_s:.1f}"
            + (" [BLOCKED]" if stats.blocked else ""),
            **fields,
            rows_checkpointed=written,
        )
        return payer, rows, fields


async def run_crawl(config: AppConfig, args: argparse.Namespace, log: Any) -> int:
    """
    Execute the whole run: crawl every selected payer, dedupe, write, email.

    Returns the process exit code. Everything that can be salvaged is salvaged -
    if the crawl is interrupted, the rows already checkpointed are still written
    out before returning.
    """
    started = time.monotonic()
    payers = select_payers(load_payers(config.payer_csv_path), config.crawl.payer_range)
    log.info(
        ev.EV_RUN_START,
        f"run {log.run_id}: {len(payers)} payer(s) selected "
        f"({config.crawl.payer_range}), max {config.crawl.max_docs_per_payer} doc(s) each",
        run_id=log.run_id, payers=[payer.payer_name for payer in payers],
        payer_count=len(payers), max_docs_per_payer=config.crawl.max_docs_per_payer,
        concurrency_cap=config.crawl.concurrency_cap,
        rate_limit_per_sec=config.crawl.per_domain_rate_limit_per_sec,
    )
    log.info(ev.EV_RUN_CONFIG, "effective configuration for this run",
             config=config.to_dict())

    writer = OutputWriter(config.output_dir, log)
    downloader = Downloader(config.downloads_dir, log, enabled=config.output.save_downloads)
    interrupted = False

    with Checkpoint(config.state_path, enabled=config.resume.enabled, log=log) as checkpoint:
        banner = checkpoint.resume_banner()
        if banner:
            log.info(ev.EV_RESUME, banner, path=str(config.state_path))
        checkpoint.start_run(log.run_id, config.to_dict(), " ".join(sys.argv))

        # Payer-level parallelism: enough to keep several hosts busy without
        # letting the frontier explode. Capped by the request concurrency too.
        payer_slots = max(1, min(len(payers), max(2, config.crawl.concurrency_cap // 2)))
        semaphore = asyncio.Semaphore(payer_slots)

        stats_by_payer: dict[str, dict[str, Any]] = {}
        async with Fetcher(config, log) as fetcher:
            robots = RobotsCache(fetcher, log, config.crawl.user_agent)
            tasks = [
                asyncio.create_task(
                    _run_payer(payer, config, fetcher, robots, downloader,
                               log, checkpoint, semaphore),
                    name=f"payer:{payer.payer_name}",
                )
                for payer in payers
            ]
            try:
                for coroutine in asyncio.as_completed(tasks):
                    payer, _rows, fields = await coroutine
                    stats_by_payer[payer.payer_name] = fields
            except (asyncio.CancelledError, KeyboardInterrupt):
                interrupted = True
                log.warn(
                    "run.interrupted",
                    "run interrupted; writing what has been collected so far. "
                    "Re-run the same command to resume.",
                )
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        # ---- assemble the dataset from the checkpoint ---------------------
        # Reading from the checkpoint rather than from memory is what makes a
        # resumed run emit a complete file: rows collected by earlier segments
        # are included exactly once.
        all_rows = checkpoint.load_rows()
        log.info(
            "output.assembling",
            f"assembling {len(all_rows)} checkpointed row(s) from {config.state_path.name}",
            rows=len(all_rows), state_file=str(config.state_path),
        )

        deduped, dedupe_report = dedupe_rows(all_rows, log)
        # Fold the collapse counts back into the per-payer stats so the summary
        # table's "dupes" column reflects the final dataset, not just what each
        # payer noticed on its own.
        for payer_name, count in dedupe_report.by_payer.items():
            entry = stats_by_payer.setdefault(payer_name, {})
            entry["dupes_collapsed"] = int(entry.get("dupes_collapsed", 0) or 0) + count
        if dedupe_report.collapsed:
            log.info(
                ev.EV_DEDUPE,
                f"collapsed {dedupe_report.collapsed} near-duplicate(s); "
                f"{dedupe_report.kept} row(s) remain",
                **dedupe_report.as_log_fields(),
            )

        prepared, problems = prepare_rows(deduped)
        # document_url uniqueness is the schema's one hard cross-row constraint;
        # assert it after dedupe so a bug there cannot ship a malformed file.
        urls = [str(row.document_url) for row in prepared]
        if len(urls) != len(set(urls)):
            duplicates = {url for url in urls if urls.count(url) > 1}
            log.error(
                "output.duplicate_urls",
                f"{len(duplicates)} document_url value(s) are not unique after dedupe",
                examples=sorted(duplicates)[:5],
            )
        if problems:
            writer.write_validation_report(problems)

        paths = writer.write(
            prepared,
            output_format=config.output.output_format,
            also_xlsx=config.output.also_write_xlsx or bool(args.also_xlsx),
        )

        # ---- summary table (a deliverable, so it goes to disk and stdout) ---
        checkpoint_stats = checkpoint.payer_stats()
        for payer_name, entry in checkpoint_stats.items():
            merged = dict(entry)
            merged.update(stats_by_payer.get(payer_name, {}))
            # The checkpoint's status is authoritative for payers this process
            # skipped, but a live run's counters are more current.
            merged.setdefault("status", entry.get("status", ""))
            stats_by_payer[payer_name] = merged

        summary = render_summary(stats_by_payer, payers)
        summary_path = log.write_sidecar(f"summary-{log.run_id}.txt", summary + "\n")
        log.write_sidecar("summary-latest.txt", summary + "\n")

        elapsed = time.monotonic() - started
        log.info(
            ev.EV_RUN_DONE,
            f"run {log.run_id} finished in {elapsed:.1f}s: {len(prepared)} row(s) "
            f"written to {', '.join(path.name for path in paths)}",
            run_id=log.run_id, rows=len(prepared), elapsed_s=round(elapsed, 2),
            outputs=[str(path) for path in paths],
            summary_path=str(summary_path),
            dupes_collapsed=dedupe_report.collapsed,
            schema_problems=len(problems),
            downloads_saved=downloader.saved,
            fetch_stats=fetcher.stats,
            interrupted=interrupted,
        )
        checkpoint.finish_run(log.run_id)

    # Printed (not just logged) because the summary table is part of the
    # deliverable and must be visible on stdout per the brief.
    print()
    print(f"Per-payer summary for run {log.run_id}")
    print(summary)
    print()
    print(f"Dataset : {', '.join(str(path) for path in paths)}")
    print(f"Logs    : {config.log_dir}  ({log.run_id}.jsonl, {log.run_id}.log)")
    print(f"Summary : {summary_path}")
    if problems:
        print(f"Schema  : {len(problems)} row(s) flagged; see "
              f"{config.output_dir / 'schema_problems.txt'}")

    # Email last, so a delivery failure can never cost us the dataset.
    send_output(config, log, paths, log.run_id, summary_text=summary)

    if interrupted:
        return EXIT_INTERRUPTED
    if args.strict_schema and problems:
        return EXIT_CONFIG
    return EXIT_OK if prepared else EXIT_EMPTY


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """
    Make Ctrl-C / SIGTERM cancel the run cleanly so the checkpoint is flushed.

    ``add_signal_handler`` is POSIX-only; on Windows the default
    ``KeyboardInterrupt`` path is used instead, which the run loop already
    handles. Either way the checkpoint has been committed as each payer
    finished, so an abrupt kill still resumes correctly.
    """
    for signal_name in ("SIGINT", "SIGTERM"):
        handler = getattr(signal, signal_name, None)
        if handler is None:
            continue
        try:
            loop.add_signal_handler(handler, lambda: _cancel_all(loop))
        except (NotImplementedError, RuntimeError):
            pass


def _cancel_all(loop: asyncio.AbstractEventLoop) -> None:
    """Cancel every outstanding task, triggering the interrupted-run path."""
    for task in asyncio.all_tasks(loop):
        task.cancel()


def main(argv: Sequence[str] | None = None) -> int:
    """
    Entry point. Returns a process exit code; never raises for a user error.

    Order of operations matters here: the config is built and validated *before*
    logging is configured, so a bad config fails fast with a plain message
    instead of creating an empty log folder first.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.explain_dedupe:
        print(rule_description())
        return EXIT_OK

    try:
        config = build_config(
            config_path=args.config,
            cli_overrides=vars(args),
            base_dir=Path.cwd(),
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(f"\n{describe_precedence()}", file=sys.stderr)
        return EXIT_CONFIG

    if args.quiet:
        config.logging.console = False
    if args.also_xlsx:
        config.output.also_write_xlsx = True

    if args.show_config:
        import json as _json

        print(_json.dumps(config.to_dict(), indent=2, default=str))
        print(f"\n# {describe_precedence()}")
        print(f"# config file: {config.config_path or '(none; built-in defaults)'}")
        return EXIT_OK

    try:
        payers = select_payers(load_payers(config.payer_csv_path), config.crawl.payer_range)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    if args.dry_run:
        print(f"config    : {config.config_path or '(built-in defaults)'}")
        print(f"seed list : {config.payer_csv_path}")
        print(f"selection : {config.crawl.payer_range} -> {len(payers)} payer(s)")
        print(f"budget    : {config.crawl.max_docs_per_payer} doc(s)/payer, "
              f"concurrency {config.crawl.concurrency_cap}, "
              f"{config.crawl.per_domain_rate_limit_per_sec} req/s per host")
        print(f"outputs   : {config.output_dir} ({config.output.output_format}), "
              f"downloads {config.downloads_dir}")
        print(f"logs      : {config.log_dir} (level {config.logging.log_level})")
        print(f"state     : {config.state_path} "
              f"(resume {'on' if config.resume.enabled else 'off'})")
        print(f"email     : {'on' if config.email.enabled else 'off'}")
        print(f"schema    : {len(COLUMNS)} columns")
        print()
        print(describe_payers(payers))
        print("\ndry run: nothing was fetched.")
        return EXIT_OK

    run_id = new_run_id()
    log = setup_logging(
        config.log_dir,
        log_level=config.logging.log_level,
        run_id=run_id,
        console=config.logging.console,
        rotate_max_bytes=config.logging.rotate_max_bytes,
        rotate_backup_count=config.logging.rotate_backup_count,
    )

    exit_code = EXIT_OK
    try:
        async def _main() -> int:
            _install_signal_handlers(asyncio.get_running_loop())
            return await run_crawl(config, args, log)

        exit_code = asyncio.run(_main())
    except KeyboardInterrupt:
        log.warn("run.interrupted",
                 "interrupted by user; re-run the same command to resume")
        exit_code = EXIT_INTERRUPTED
    except ConfigError as exc:
        log.error("run.config_error", str(exc))
        print(f"error: {exc}", file=sys.stderr)
        exit_code = EXIT_CONFIG
    except Exception as exc:
        log.error("run.failed", f"unhandled {type(exc).__name__}: {exc}", exc_info=True)
        print(f"error: run failed: {exc}", file=sys.stderr)
        exit_code = EXIT_CONFIG
    finally:
        # finalise_latest + shutdown must happen even on a crash, or the log of
        # the failure is the thing we lose.
        finalise_latest(config.log_dir, run_id)
        shutdown_logging()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
