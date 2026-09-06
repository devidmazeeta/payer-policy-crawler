"""
Structured (JSON-lines) plus human-readable logging.

Two sinks are configured for every run, both under ``logging.log_dir``:

* ``{run_id}.jsonl`` - one JSON object per line, the machine-readable audit
  trail. Shape matches the case study example exactly::

      {"ts": "2026-08-19T09:41:12Z", "lvl": "INFO", "event": "doc.found", ...}

* ``{run_id}.log`` - a plain rotating text log at INFO level for quick manual
  review, plus ``latest.log`` / ``latest.jsonl`` copies so "show me the last
  run" never requires knowing the run id.

Nothing in this project uses ``print`` for operational output; the run summary
table is the single exception and it is emitted through the logger *and* to
stdout because it is a deliverable.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Event names the brief requires. Kept as constants so a typo in a call site is
#: a NameError at import time instead of an event that silently never appears.
EV_PAYER_START = "payer.start"
EV_ROBOTS_LOADED = "robots.loaded"
EV_SITEMAP_PARSED = "sitemap.parsed"
EV_ROBOTS_BLOCKED = "robots.blocked"
EV_DOC_FOUND = "doc.found"
EV_FETCH_FAILED = "fetch.failed"
EV_PAYER_DONE = "payer.done"

# Supporting events used by the surrounding machinery.
EV_RUN_START = "run.start"
EV_RUN_CONFIG = "run.config"
EV_RUN_DONE = "run.done"
EV_INDEX_CRAWLED = "index.crawled"
EV_ENUMERATION = "enumeration.attempted"
EV_DEDUPE = "dedupe.collapsed"
EV_OUTPUT_WRITTEN = "output.written"
EV_EMAIL_SENT = "email.sent"
EV_EMAIL_FAILED = "email.failed"
EV_RESUME = "resume.loaded"
EV_SCHEMA_INVALID = "schema.invalid"
EV_BLOCKED = "payer.blocked"

#: Standard LogRecord attributes, so the JSON formatter can tell our custom
#: ``extra`` fields apart from the machinery's own attributes.
_RESERVED_RECORD_ATTRS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
        "pathname", "process", "processName", "relativeCreated", "stack_info",
        "thread", "threadName", "taskName", "getMessage",
    }
)

#: Level names are abbreviated to match the case study's "WARN" spelling.
_LEVEL_ALIASES = {"WARNING": "WARN", "CRITICAL": "ERROR"}


def new_run_id() -> str:
    """
    Build a sortable, collision-resistant run id, e.g. ``20260906T142211Z-3f9a1c``.

    The timestamp prefix makes ``ls logs/`` chronological; the random suffix
    keeps two runs started in the same second from sharing a log file.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


class JsonLinesFormatter(logging.Formatter):
    """
    Render a :class:`logging.LogRecord` as a single-line JSON object.

    Key order is fixed (``ts``, ``lvl``, ``event``, then event-specific fields)
    so the output diffs cleanly and is pleasant to read with ``jq``. Any keyword
    passed via ``extra=`` becomes a top-level field; ``event`` defaults to the
    logger's message when a call site omits it, so no record is ever shapeless.
    """

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def format(self, record: logging.LogRecord) -> str:
        level = _LEVEL_ALIASES.get(record.levelname, record.levelname)
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "lvl": level,
            "event": getattr(record, "event", record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_ATTRS or key.startswith("_") or key == "event":
                continue
            payload[key] = _jsonable(value)
        # A human message that is not just the event name is worth keeping.
        message = record.getMessage()
        if message and message != payload["event"] and "msg_text" not in payload:
            payload["msg_text"] = message
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)[-2000:]
        payload.setdefault("run_id", self.run_id)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _jsonable(value: Any) -> Any:
    """Best-effort conversion of an ``extra`` value into something JSON can hold."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Path):
        return str(value)
    return str(value)


class HumanFormatter(logging.Formatter):
    """
    Compact, aligned text format for the rotating human log and the console.

    Example::

        14:22:11 INFO  doc.found        uhc | https://.../policy.pdf
    """

    def __init__(self) -> None:
        super().__init__(datefmt="%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        event = str(getattr(record, "event", ""))
        payer = getattr(record, "payer", "")
        message = record.getMessage()
        head = f"{self.formatTime(record, self.datefmt)} {record.levelname:<5}"
        parts = [head]
        if event:
            parts.append(f"{event:<18}")
        if payer:
            parts.append(f"{payer} |")
        parts.append(message)
        line = " ".join(parts)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class RunLogger:
    """
    Thin wrapper around a :class:`logging.Logger` that enforces the event shape.

    Call sites use :meth:`event` (or the level shortcuts) and pass event-specific
    fields as keyword arguments, which keeps the JSON sink structured without
    every module having to remember the ``extra={...}`` incantation.
    """

    def __init__(self, logger: logging.Logger, run_id: str, log_dir: Path) -> None:
        self._logger = logger
        self.run_id = run_id
        self.log_dir = log_dir

    # -- core ---------------------------------------------------------------
    def event(self, level: int, event: str, message: str = "", **fields: Any) -> None:
        """Emit one structured record at *level*."""
        extra = {"event": event}
        extra.update({key: value for key, value in fields.items() if value is not None})
        self._logger.log(level, message or event, extra=extra, stacklevel=3)

    def debug(self, event: str, message: str = "", **fields: Any) -> None:
        self.event(logging.DEBUG, event, message, **fields)

    def info(self, event: str, message: str = "", **fields: Any) -> None:
        self.event(logging.INFO, event, message, **fields)

    def warn(self, event: str, message: str = "", **fields: Any) -> None:
        self.event(logging.WARNING, event, message, **fields)

    def error(self, event: str, message: str = "", exc_info: bool = False, **fields: Any) -> None:
        extra = {"event": event}
        extra.update({key: value for key, value in fields.items() if value is not None})
        self._logger.log(logging.ERROR, message or event, extra=extra,
                         exc_info=exc_info, stacklevel=2)

    # -- convenience --------------------------------------------------------
    def bind(self, **fields: Any) -> "BoundRunLogger":
        """Return a logger that adds *fields* to every record (e.g. the payer name)."""
        return BoundRunLogger(self, fields)

    @property
    def raw(self) -> logging.Logger:
        """Escape hatch for third-party code that wants a stdlib logger."""
        return self._logger

    def write_sidecar(self, filename: str, text: str) -> Path:
        """
        Write an extra artefact (e.g. the run summary table) into the log folder.

        Returned so the caller can report the path; the summary table is part of
        the deliverable, not just an operational detail.
        """
        path = self.log_dir / filename
        path.write_text(text, encoding="utf-8")
        return path


class BoundRunLogger:
    """A :class:`RunLogger` view with pre-bound fields (see :meth:`RunLogger.bind`)."""

    def __init__(self, parent: RunLogger, bound: dict[str, Any]) -> None:
        self._parent = parent
        self._bound = bound

    def _merge(self, fields: dict[str, Any]) -> dict[str, Any]:
        merged = dict(self._bound)
        merged.update(fields)
        return merged

    def debug(self, event: str, message: str = "", **fields: Any) -> None:
        self._parent.debug(event, message, **self._merge(fields))

    def info(self, event: str, message: str = "", **fields: Any) -> None:
        self._parent.info(event, message, **self._merge(fields))

    def warn(self, event: str, message: str = "", **fields: Any) -> None:
        self._parent.warn(event, message, **self._merge(fields))

    def error(self, event: str, message: str = "", exc_info: bool = False, **fields: Any) -> None:
        self._parent.error(event, message, exc_info=exc_info, **self._merge(fields))

    @property
    def run_id(self) -> str:
        return self._parent.run_id


def setup_logging(
    log_dir: str | os.PathLike[str],
    log_level: str = "INFO",
    run_id: str | None = None,
    *,
    console: bool = True,
    rotate_max_bytes: int = 10 * 1024 * 1024,
    rotate_backup_count: int = 5,
) -> RunLogger:
    """
    Configure the run's logging and return the :class:`RunLogger` facade.

    Handlers installed on the ``crawler`` logger (never the root logger, so an
    embedding application keeps control of its own logging):

    1. ``{run_id}.jsonl`` - JSON-lines at the configured level (the audit trail).
    2. ``{run_id}.log`` - human text via ``RotatingFileHandler``.
    3. stdout - the same human format, when *console* is true.

    ``latest.log`` / ``latest.jsonl`` are refreshed at the end of the run by
    :func:`finalise_latest`; on POSIX they are symlinks, and on Windows - where
    symlink creation needs elevation - they are plain copies.
    """
    run_id = run_id or new_run_id()
    directory = Path(log_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, str(log_level).upper().replace("WARN", "WARNING"), logging.INFO)

    logger = logging.getLogger("crawler")
    logger.setLevel(min(level, logging.INFO))
    logger.propagate = False
    # Re-running setup in the same process (tests, notebooks) must not stack
    # duplicate handlers on the logger.
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)

    json_handler = logging.FileHandler(directory / f"{run_id}.jsonl", encoding="utf-8")
    json_handler.setFormatter(JsonLinesFormatter(run_id))
    json_handler.setLevel(level)
    logger.addHandler(json_handler)

    text_handler = logging.handlers.RotatingFileHandler(
        directory / f"{run_id}.log",
        maxBytes=rotate_max_bytes,
        backupCount=rotate_backup_count,
        encoding="utf-8",
    )
    text_handler.setFormatter(HumanFormatter())
    text_handler.setLevel(max(level, logging.INFO))
    logger.addHandler(text_handler)

    if console:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(HumanFormatter())
        stream.setLevel(max(level, logging.INFO))
        logger.addHandler(stream)

    return RunLogger(logger, run_id, directory)


def finalise_latest(log_dir: str | os.PathLike[str], run_id: str) -> None:
    """
    Point ``latest.log`` / ``latest.jsonl`` at this run's files.

    Symlink first (cheap, obviously current); fall back to a copy on Windows or
    any filesystem that refuses symlinks. Failure here is never fatal - it is a
    convenience, and losing it must not fail a completed crawl.
    """
    directory = Path(log_dir).expanduser()
    for suffix in (".log", ".jsonl"):
        source = directory / f"{run_id}{suffix}"
        if not source.exists():
            continue
        target = directory / f"latest{suffix}"
        try:
            if target.exists() or target.is_symlink():
                target.unlink()
            target.symlink_to(source.name)
        except (OSError, NotImplementedError):
            try:
                shutil.copyfile(source, target)
            except OSError:
                pass


def shutdown_logging() -> None:
    """Flush and detach handlers; called from a ``finally`` so logs survive a crash."""
    logger = logging.getLogger("crawler")
    for handler in list(logger.handlers):
        try:
            handler.flush()
            handler.close()
        except (OSError, ValueError):
            pass
        logger.removeHandler(handler)
