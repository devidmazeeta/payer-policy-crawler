"""
Checkpointing and resume.

Requirement: killing the process mid-run and restarting with the same command
must continue rather than start over, and must not re-emit rows that were
already collected.

Design: a single SQLite file (``resume.state_file``) holding three tables -

* ``runs``     - one row per run, for provenance and for the resume banner;
* ``payers``   - per-payer status (``pending`` / ``in_progress`` / ``done`` /
                 ``failed`` / ``blocked``) plus the counters needed to rebuild
                 the summary table after a resume;
* ``rows``     - every emitted dataset row, keyed by its canonicalised
                 ``document_url``.

Storing the *rows* (not just progress markers) in the checkpoint is what makes
the "do not re-emit" guarantee real: the final output is assembled from the
checkpoint, so a run that was interrupted after payer 7 still writes a complete
ten-payer dataset when it resumes, and a document already collected is never
fetched or written twice.

SQLite is used in its default (serialised) threading mode with a short busy
timeout, and every write is committed immediately - a checkpoint that loses the
last few seconds of work on a hard kill would defeat its own purpose. A
``.json`` extension on ``state_file`` selects the JSON backend instead, which is
slower and less crash-safe but trivially inspectable by hand.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import fields
from pathlib import Path
from typing import Any, Iterable, Sequence

from .schema import COLUMNS, DocumentRow, normalise_row

#: Status values a payer can hold in the checkpoint.
STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"

#: Statuses that mean "do not crawl this payer again on resume".
TERMINAL_STATUSES = frozenset({STATUS_DONE, STATUS_BLOCKED})

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    config_json  TEXT,
    argv         TEXT
);

CREATE TABLE IF NOT EXISTS payers (
    payer_name       TEXT PRIMARY KEY,
    status           TEXT NOT NULL,
    run_id           TEXT,
    attempted        INTEGER DEFAULT 0,
    found            INTEGER DEFAULT 0,
    failed           INTEGER DEFAULT 0,
    skipped_robots   INTEGER DEFAULT 0,
    dupes_collapsed  INTEGER DEFAULT 0,
    elapsed_s        REAL DEFAULT 0,
    blocked          INTEGER DEFAULT 0,
    block_evidence   TEXT DEFAULT '',
    stats_json       TEXT,
    updated_at       TEXT
);

-- One row per emitted dataset record. url_key is the canonicalised URL, which
-- is what enforces "never emit the same document twice" across resumed runs.
CREATE TABLE IF NOT EXISTS rows (
    url_key     TEXT PRIMARY KEY,
    payer_name  TEXT NOT NULL,
    run_id      TEXT,
    row_json    TEXT NOT NULL,
    created_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_rows_payer ON rows (payer_name);

-- Pages/URLs already visited, so a resumed run does not re-request them.
CREATE TABLE IF NOT EXISTS visited (
    url_key     TEXT PRIMARY KEY,
    payer_name  TEXT,
    kind        TEXT,
    created_at  TEXT
);
"""


def _now() -> str:
    """UTC timestamp for checkpoint bookkeeping."""
    from .schema import utc_now_iso

    return utc_now_iso()


def _row_to_json(row: DocumentRow) -> str:
    """
    Serialise a row, including the internal (non-schema) bookkeeping fields.

    ``local_path`` is preserved so a resumed run knows the document is already on
    disk; ``content_text`` is dropped because it can be megabytes per row and is
    only needed during extraction.
    """
    payload = {name: getattr(row, name) for name in COLUMNS}
    payload["local_path"] = row.local_path
    return json.dumps(payload, ensure_ascii=False)


def _row_from_json(text: str) -> DocumentRow:
    """Rehydrate a row from the checkpoint, tolerating older/newer field sets."""
    payload = json.loads(text)
    known = {field.name for field in fields(DocumentRow)}
    row = DocumentRow(**{key: value for key, value in payload.items() if key in known})
    # Re-normalise: idempotent, and it protects against a checkpoint written by
    # an older version of the schema conventions.
    return normalise_row(row)


class Checkpoint:
    """
    Resume state for a run, backed by SQLite (or JSON when the path ends ``.json``).

    Use as a context manager so the connection is always closed::

        with Checkpoint(config.state_path, enabled=True) as checkpoint:
            ...
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        enabled: bool = True,
        log: Any = None,
    ) -> None:
        self.path = Path(path)
        self.enabled = enabled
        self.log = log
        self._json_mode = self.path.suffix.lower() == ".json"
        self._connection: sqlite3.Connection | None = None
        self._json_state: dict[str, Any] = {"runs": {}, "payers": {}, "rows": {}, "visited": {}}

        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.enabled:
            # Resume disabled: start from a clean slate so the run is a true
            # from-scratch crawl, as the operator asked.
            self._reset()
        if self._json_mode:
            self._load_json()
        else:
            self._open_sqlite()

    # -- lifecycle -----------------------------------------------------------
    def __enter__(self) -> "Checkpoint":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def _reset(self) -> None:
        """Delete the checkpoint file (used when ``resume.enabled`` is false)."""
        try:
            if self.path.exists():
                self.path.unlink()
        except OSError:
            pass

    def _open_sqlite(self) -> None:
        self._connection = sqlite3.connect(self.path, timeout=30.0)
        self._connection.row_factory = sqlite3.Row
        # WAL keeps readers unblocked and survives a hard process kill better
        # than the default rollback journal.
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.executescript(_SCHEMA_SQL)
        self._connection.commit()

    def _load_json(self) -> None:
        if self.path.exists():
            try:
                self._json_state = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                # A corrupt checkpoint must not stop the run; we start fresh and
                # say so, which is better than crashing on someone's resume.
                if self.log is not None:
                    self.log.warn(
                        "resume.corrupt",
                        f"{self.path} could not be parsed; starting from scratch",
                        path=str(self.path),
                    )
                self._json_state = {"runs": {}, "payers": {}, "rows": {}, "visited": {}}
        for key in ("runs", "payers", "rows", "visited"):
            self._json_state.setdefault(key, {})

    def _flush_json(self) -> None:
        """Write the JSON checkpoint atomically (temp file + replace)."""
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self._json_state, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        temporary.replace(self.path)

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.commit()
                self._connection.close()
            except sqlite3.Error:
                pass
            self._connection = None
        elif self._json_mode:
            try:
                self._flush_json()
            except OSError:
                pass

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        """
        Run a statement, retrying briefly on a transient lock.

        A locked database is a normal, recoverable condition when several
        coroutines commit at once; retrying beats propagating an exception into
        the crawl loop.
        """
        assert self._connection is not None
        for attempt in range(5):
            try:
                cursor = self._connection.execute(sql, params)
                self._connection.commit()
                return cursor
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 4:
                    raise
                time.sleep(0.2 * (attempt + 1))
        raise sqlite3.OperationalError("checkpoint write failed after retries")

    # -- run bookkeeping ----------------------------------------------------
    def start_run(self, run_id: str, config_dict: dict[str, Any], argv: str = "") -> None:
        """Record the start of a run, for provenance in the checkpoint file."""
        payload = json.dumps(config_dict, ensure_ascii=False, default=str)
        if self._json_mode:
            self._json_state["runs"][run_id] = {
                "started_at": _now(), "finished_at": None,
                "config_json": payload, "argv": argv,
            }
            self._flush_json()
            return
        self._execute(
            "INSERT OR REPLACE INTO runs (run_id, started_at, config_json, argv) "
            "VALUES (?, ?, ?, ?)",
            (run_id, _now(), payload, argv),
        )

    def finish_run(self, run_id: str) -> None:
        """Mark the run finished."""
        if self._json_mode:
            entry = self._json_state["runs"].setdefault(run_id, {})
            entry["finished_at"] = _now()
            self._flush_json()
            return
        self._execute("UPDATE runs SET finished_at = ? WHERE run_id = ?", (_now(), run_id))

    def previous_runs(self) -> int:
        """How many runs this checkpoint has already seen (drives the resume banner)."""
        if self._json_mode:
            return len(self._json_state["runs"])
        assert self._connection is not None
        cursor = self._connection.execute("SELECT COUNT(*) AS total FROM runs")
        return int(cursor.fetchone()["total"])

    # -- payer status -------------------------------------------------------
    def payer_status(self, payer_name: str) -> str:
        """Current status of *payer_name*, or ``pending`` if never seen."""
        if self._json_mode:
            entry = self._json_state["payers"].get(payer_name)
            return str(entry.get("status")) if entry else STATUS_PENDING
        assert self._connection is not None
        cursor = self._connection.execute(
            "SELECT status FROM payers WHERE payer_name = ?", (payer_name,)
        )
        record = cursor.fetchone()
        return str(record["status"]) if record else STATUS_PENDING

    def is_payer_complete(self, payer_name: str) -> bool:
        """
        True when *payer_name* need not be crawled again.

        ``failed`` is deliberately *not* terminal: a payer that died on a
        transient error should be retried on the next run, whereas one that
        finished or was hard-blocked should not.
        """
        return self.payer_status(payer_name) in TERMINAL_STATUSES

    def mark_payer(
        self, payer_name: str, status: str, run_id: str = "", stats: Any = None
    ) -> None:
        """Upsert a payer's status and counters."""
        stats_fields: dict[str, Any] = {}
        if stats is not None:
            stats_fields = stats.as_log_fields() if hasattr(stats, "as_log_fields") else dict(stats)
        payload = json.dumps(stats_fields, ensure_ascii=False, default=str)
        values = (
            payer_name, status, run_id,
            int(stats_fields.get("attempted", 0) or 0),
            int(stats_fields.get("found", 0) or 0),
            int(stats_fields.get("failed", 0) or 0),
            int(stats_fields.get("skipped_robots", 0) or 0),
            int(stats_fields.get("dupes_collapsed", 0) or 0),
            float(stats_fields.get("elapsed_s", 0) or 0),
            1 if stats_fields.get("blocked") else 0,
            str(stats_fields.get("block_evidence") or ""),
            payload, _now(),
        )
        if self._json_mode:
            self._json_state["payers"][payer_name] = {
                "status": status, "run_id": run_id, "stats": stats_fields,
                "updated_at": _now(),
            }
            self._flush_json()
            return
        self._execute(
            "INSERT OR REPLACE INTO payers (payer_name, status, run_id, attempted, found, "
            "failed, skipped_robots, dupes_collapsed, elapsed_s, blocked, block_evidence, "
            "stats_json, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )

    def payer_stats(self) -> dict[str, dict[str, Any]]:
        """
        Every payer's recorded stats, for rebuilding the summary after a resume.

        Returns ``{payer_name: {status, attempted, found, failed, skipped_robots,
        dupes_collapsed, elapsed_s, blocked, block_evidence}}``.
        """
        result: dict[str, dict[str, Any]] = {}
        if self._json_mode:
            for name, entry in self._json_state["payers"].items():
                stats = dict(entry.get("stats") or {})
                stats["status"] = entry.get("status", STATUS_PENDING)
                result[name] = stats
            return result
        assert self._connection is not None
        cursor = self._connection.execute("SELECT * FROM payers")
        for record in cursor.fetchall():
            stats = json.loads(record["stats_json"] or "{}")
            stats.update(
                {
                    "status": record["status"],
                    "attempted": record["attempted"],
                    "found": record["found"],
                    "failed": record["failed"],
                    "skipped_robots": record["skipped_robots"],
                    "dupes_collapsed": record["dupes_collapsed"],
                    "elapsed_s": record["elapsed_s"],
                    "blocked": bool(record["blocked"]),
                    "block_evidence": record["block_evidence"] or "",
                }
            )
            result[record["payer_name"]] = stats
        return result

    # -- rows ---------------------------------------------------------------
    def has_row(self, url_key: str) -> bool:
        """True when a row for this canonicalised URL is already checkpointed."""
        if self._json_mode:
            return url_key in self._json_state["rows"]
        assert self._connection is not None
        cursor = self._connection.execute(
            "SELECT 1 FROM rows WHERE url_key = ? LIMIT 1", (url_key,)
        )
        return cursor.fetchone() is not None

    def save_rows(self, rows: Iterable[DocumentRow], run_id: str = "") -> int:
        """
        Persist *rows*, ignoring any whose URL is already stored.

        Returns the number newly written. ``INSERT OR IGNORE`` on the primary key
        is what makes this idempotent, and therefore what makes a resumed run
        unable to duplicate a row it already had.
        """
        from .discovery import normalise_url  # local import avoids a cycle

        written = 0
        if self._json_mode:
            for row in rows:
                key = normalise_url(str(row.document_url))
                if key in self._json_state["rows"]:
                    continue
                self._json_state["rows"][key] = {
                    "payer_name": str(row.payer_name),
                    "run_id": run_id,
                    "row_json": _row_to_json(row),
                    "created_at": _now(),
                }
                written += 1
            if written:
                self._flush_json()
            return written

        assert self._connection is not None
        payload = []
        for row in rows:
            payload.append(
                (
                    normalise_url(str(row.document_url)),
                    str(row.payer_name),
                    run_id,
                    _row_to_json(row),
                    _now(),
                )
            )
        if not payload:
            return 0
        for attempt in range(5):
            try:
                cursor = self._connection.executemany(
                    "INSERT OR IGNORE INTO rows (url_key, payer_name, run_id, row_json, "
                    "created_at) VALUES (?,?,?,?,?)",
                    payload,
                )
                self._connection.commit()
                return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 4:
                    raise
                time.sleep(0.2 * (attempt + 1))
        return 0

    def load_rows(self, payer_name: str | None = None) -> list[DocumentRow]:
        """
        Load checkpointed rows, optionally for one payer.

        This is how the final dataset is assembled: the writer reads the union of
        everything ever collected under this checkpoint, so an interrupted run
        contributes its work to the eventual output.
        """
        rows: list[DocumentRow] = []
        if self._json_mode:
            for entry in self._json_state["rows"].values():
                if payer_name and entry.get("payer_name") != payer_name:
                    continue
                try:
                    rows.append(_row_from_json(entry["row_json"]))
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
            return rows
        assert self._connection is not None
        if payer_name:
            cursor = self._connection.execute(
                "SELECT row_json FROM rows WHERE payer_name = ? ORDER BY rowid", (payer_name,)
            )
        else:
            cursor = self._connection.execute("SELECT row_json FROM rows ORDER BY rowid")
        for record in cursor.fetchall():
            try:
                rows.append(_row_from_json(record["row_json"]))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        return rows

    def known_row_urls(self, payer_name: str | None = None) -> set[str]:
        """
        Canonicalised URLs already collected, so a resumed crawl can skip them.

        Handed to the per-payer crawler as its initial ``seen_urls`` set, which
        means an interrupted payer resumes mid-list instead of re-fetching
        everything it had already downloaded.
        """
        if self._json_mode:
            return {
                key for key, entry in self._json_state["rows"].items()
                if not payer_name or entry.get("payer_name") == payer_name
            }
        assert self._connection is not None
        if payer_name:
            cursor = self._connection.execute(
                "SELECT url_key FROM rows WHERE payer_name = ?", (payer_name,)
            )
        else:
            cursor = self._connection.execute("SELECT url_key FROM rows")
        return {str(record["url_key"]) for record in cursor.fetchall()}

    def row_count(self) -> int:
        """Total checkpointed rows (used in the resume banner)."""
        if self._json_mode:
            return len(self._json_state["rows"])
        assert self._connection is not None
        cursor = self._connection.execute("SELECT COUNT(*) AS total FROM rows")
        return int(cursor.fetchone()["total"])

    # -- visited URLs -------------------------------------------------------
    def mark_visited(self, url_key: str, payer_name: str = "", kind: str = "") -> None:
        """Record that a URL was already requested in a previous run segment."""
        if self._json_mode:
            self._json_state["visited"][url_key] = {
                "payer_name": payer_name, "kind": kind, "created_at": _now(),
            }
            return
        try:
            self._execute(
                "INSERT OR IGNORE INTO visited (url_key, payer_name, kind, created_at) "
                "VALUES (?,?,?,?)",
                (url_key, payer_name, kind, _now()),
            )
        except sqlite3.Error:
            pass  # Visited tracking is an optimisation, never a correctness need.

    def visited_urls(self, payer_name: str) -> set[str]:
        """Previously requested URLs for *payer_name*."""
        if self._json_mode:
            return {
                key for key, entry in self._json_state["visited"].items()
                if entry.get("payer_name") == payer_name
            }
        assert self._connection is not None
        cursor = self._connection.execute(
            "SELECT url_key FROM visited WHERE payer_name = ?", (payer_name,)
        )
        return {str(record["url_key"]) for record in cursor.fetchall()}

    # -- reporting ----------------------------------------------------------
    def resume_banner(self) -> str:
        """
        One-line description of what is being resumed, or ``""`` on a fresh start.

        Printed and logged at startup so it is never a surprise that a run is
        continuing from earlier work rather than crawling from scratch.
        """
        runs = self.previous_runs()
        if runs == 0:
            return ""
        stats = self.payer_stats()
        done = sum(1 for entry in stats.values()
                   if entry.get("status") in TERMINAL_STATUSES)
        return (
            f"resuming from {self.path.name}: {runs} previous run(s), "
            f"{done} payer(s) already complete, {self.row_count()} row(s) collected"
        )
