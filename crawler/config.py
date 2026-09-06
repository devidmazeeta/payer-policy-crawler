"""
Configuration loading, merging and validation.

The config file (YAML or JSON) is the single source of truth for how a run
behaves. Every value is overridable from the command line, and the precedence
order - implemented in :func:`build_config` and documented in README.md - is:

    CLI argument  >  config file value  >  built-in default

Validation is deliberately fail-fast: a typo in ``per_domain_rate_limit_per_sec``
should stop the run in the first millisecond with a readable message, not
manifest three minutes later as an accidental hammering of a payer's website.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

try:  # PyYAML is the documented dependency; JSON configs work without it.
    import yaml
except ImportError:  # pragma: no cover - exercised only in minimal installs
    yaml = None  # type: ignore[assignment]


class ConfigError(ValueError):
    """Raised for a missing, unparseable or invalid configuration value."""


# ---------------------------------------------------------------------------
# Section dataclasses. The defaults below ARE the "built-in default" tier of the
# precedence order, so each one is chosen to be safe and polite rather than fast.
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class CrawlConfig:
    """Crawl budget, politeness and retry knobs."""

    #: Hard cap on emitted rows per payer. Keeps a single sprawling payer (UHC
    #: publishes thousands of policies) from consuming the whole run budget.
    max_docs_per_payer: int = 50
    #: Seed list of payers to crawl; see payer_seed_list.csv for the columns.
    payer_seed_csv: str = "payer_seed_list.csv"
    #: 1-based inclusive selection over the seed CSV: "1-10", "3,5,7", or "1-3,8".
    payer_range: str = "1-10"
    #: Maximum simultaneous in-flight HTTP requests across the entire run.
    concurrency_cap: int = 8
    #: Requests per second allowed *per host*. Enforced by a token bucket, so
    #: 1.0 means one request every second to that host no matter how many
    #: coroutines want it. robots.txt Crawl-delay always overrides this downward.
    per_domain_rate_limit_per_sec: float = 1.0
    #: Number of retries after the first attempt (so 3 == up to 4 total tries).
    retry_limit: int = 3
    #: First backoff sleep in seconds; doubles each retry under "exponential".
    backoff_base_seconds: float = 1.5
    #: "exponential" (base * 2**attempt, jittered) or "fixed" (base, jittered).
    backoff_strategy: str = "exponential"
    #: Per-request timeout covering connect + read.
    request_timeout_seconds: int = 30
    #: Descriptive UA with a contact address, as required by the brief. Payer
    #: WAF teams should be able to identify and reach us from a log line alone.
    user_agent: str = (
        "PayerPolicyCrawler/1.0 (+contact: devidmazeetagf@gmail.com; "
        "purpose: public policy document discovery research)"
    )
    #: Upper bound on index/category pages crawled per payer (frontier budget).
    max_index_pages_per_payer: int = 40
    #: Upper bound on sitemap documents (including nested indexes) fetched per payer.
    max_sitemaps_per_payer: int = 12
    #: Upper bound on candidates tried by the robots-blocked enumeration fallback.
    max_enumeration_candidates: int = 60
    #: Follow HTTP redirects (needed: the hint host is only a starting point and
    #: most payers redirect to a regional or provider subdomain).
    follow_redirects: bool = True
    #: Extra hosts we are willing to leave the seed host for, beyond the
    #: registrable-domain match that is always allowed.
    allowed_extra_hosts: list[str] = field(default_factory=list)
    #: Optional outbound proxy. Off by default: the brief forbids paid
    #: proxies/APIs unless they are disable-able and documented.
    proxy_url: str = ""
    #: Cap on a single downloaded document, to avoid pulling a 500MB manual.
    max_download_bytes: int = 40 * 1024 * 1024


@dataclass(slots=True)
class LoggingConfig:
    """Where and how logs are written (see logging_setup.py)."""

    #: Dedicated folder, kept separate from code and output.
    log_dir: str = "logs/"
    log_level: str = "INFO"
    #: Mirror the human-readable log to stdout as well as to disk.
    console: bool = True
    #: Size at which the human-readable rotating handler rolls over.
    rotate_max_bytes: int = 10 * 1024 * 1024
    rotate_backup_count: int = 5


@dataclass(slots=True)
class OutputConfig:
    """Where the dataset and the downloaded documents land."""

    output_dir: str = "output/"
    #: "csv" or "xlsx". Both carry the identical 22 columns in identical order.
    output_format: str = "csv"
    #: Root for downloads/<payer_name>/<file>; one subfolder per payer.
    downloads_dir: str = "downloads/"
    #: Save every fetched document to disk. Turning this off keeps the dataset
    #: identical but skips the bytes on disk (useful for a quick re-run).
    save_downloads: bool = True
    #: Write the companion output.xlsx alongside output.csv regardless of format.
    also_write_xlsx: bool = False


@dataclass(slots=True)
class EmailConfig:
    """Optional delivery of the finished dataset. Defaults to fully off."""

    #: MUST default to false: with this off the run needs no credentials at all.
    enabled: bool = False
    recipients: list[str] = field(default_factory=list)
    #: "gmail_api" (OAuth, google-api-python-client) or "smtp" (app password).
    smtp_or_gmail_api: str = "gmail_api"
    subject_template: str = "Payer Policy Crawl Output - {run_id}"
    #: Sender address. Read from $CRAWLER_SMTP_USER when left blank.
    sender: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    #: Path to the OAuth client secrets / stored token for the Gmail API path.
    gmail_credentials_file: str = "credentials.json"
    gmail_token_file: str = "token.json"


@dataclass(slots=True)
class ResumeConfig:
    """Checkpoint location and resume behaviour."""

    #: SQLite file (a ``.json`` suffix selects the JSON backend instead).
    state_file: str = "state/checkpoint.db"
    #: When false, the checkpoint is wiped at startup and the run restarts clean.
    enabled: bool = True


@dataclass(slots=True)
class AppConfig:
    """Fully merged, validated configuration for one crawl run."""

    crawl: CrawlConfig = field(default_factory=CrawlConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    resume: ResumeConfig = field(default_factory=ResumeConfig)

    #: Absolute path of the config file this object was built from ("" for
    #: pure-default runs). Recorded in the run log for reproducibility.
    config_path: str = ""
    #: Project root that all relative paths in this config resolve against.
    base_dir: str = "."

    # -- path helpers --------------------------------------------------------
    def resolve(self, relative: str) -> Path:
        """Resolve a config path against :attr:`base_dir` (absolute paths pass through)."""
        candidate = Path(relative).expanduser()
        if candidate.is_absolute():
            return candidate
        return (Path(self.base_dir) / candidate).resolve()

    @property
    def log_dir(self) -> Path:
        return self.resolve(self.logging.log_dir)

    @property
    def output_dir(self) -> Path:
        return self.resolve(self.output.output_dir)

    @property
    def downloads_dir(self) -> Path:
        return self.resolve(self.output.downloads_dir)

    @property
    def state_path(self) -> Path:
        return self.resolve(self.resume.state_file)

    @property
    def payer_csv_path(self) -> Path:
        return self.resolve(self.crawl.payer_seed_csv)

    def to_dict(self) -> dict[str, Any]:
        """Serialise for the run-config log record (used to reproduce a run)."""
        return _dataclass_to_dict(self)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def load_config_file(path: str | os.PathLike[str]) -> dict[str, Any]:
    """
    Parse a YAML or JSON config file into a plain nested dict.

    The extension picks the parser; a ``.yaml``/``.yml`` file without PyYAML
    installed produces an actionable error rather than a stack trace.
    """
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise ConfigError(f"config file not found: {file_path}")
    text = file_path.read_text(encoding="utf-8")
    suffix = file_path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        if yaml is None:
            raise ConfigError(
                f"{file_path} is YAML but PyYAML is not installed. "
                "Run 'pip install -r requirements.txt' or supply a .json config."
            )
        data = yaml.safe_load(text)
    elif suffix == ".json":
        data = json.loads(text)
    else:
        raise ConfigError(f"unsupported config extension {suffix!r}; use .yaml, .yml or .json")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{file_path}: top level of a config file must be a mapping")
    return data


def _coerce(value: Any, target_type: Any, dotted_name: str) -> Any:
    """
    Coerce one scalar/list config value to the type declared on the dataclass.

    YAML gives us real ints/floats/bools, but JSON-from-CLI and environment
    overrides arrive as strings, so booleans get the usual truthy-word treatment
    and numbers are parsed explicitly to produce a clear error on garbage.
    """
    if target_type in (int, "int"):
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{dotted_name}: expected an integer, got {value!r}") from None
    if target_type in (float, "float"):
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{dotted_name}: expected a number, got {value!r}") from None
    if target_type in (bool, "bool"):
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"true", "yes", "y", "1", "on"}:
            return True
        if text in {"false", "no", "n", "0", "off"}:
            return False
        raise ConfigError(f"{dotted_name}: expected a boolean, got {value!r}")
    if target_type in (str, "str"):
        return "" if value is None else str(value)
    if "list" in str(target_type):
        if value is None:
            return []
        if isinstance(value, str):
            # Accept "a@b.com, c@d.com" from the CLI as well as a YAML list.
            return [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value]
        raise ConfigError(f"{dotted_name}: expected a list, got {value!r}")
    return value


def _apply_mapping(section: Any, values: Mapping[str, Any], prefix: str) -> None:
    """
    Overlay *values* onto a section dataclass instance, coercing and rejecting
    unknown keys (an unknown key is almost always a typo that would otherwise be
    silently ignored, e.g. ``concurrency_camp``).
    """
    known = {f.name: f.type for f in fields(section)}
    for key, value in values.items():
        dotted = f"{prefix}.{key}" if prefix else str(key)
        if key not in known:
            raise ConfigError(
                f"unknown config key {dotted!r}. Valid keys in this section: "
                + ", ".join(sorted(known))
            )
        if value is None:
            continue
        setattr(section, key, _coerce(value, known[key], dotted))


def _dataclass_to_dict(obj: Any) -> dict[str, Any]:
    """Recursively convert nested dataclasses to plain JSON-serialisable dicts."""
    result: dict[str, Any] = {}
    for f in fields(obj):
        value = getattr(obj, f.name)
        result[f.name] = _dataclass_to_dict(value) if is_dataclass(value) else value
    return result


# ---------------------------------------------------------------------------
# Merging + validation
# ---------------------------------------------------------------------------
#: Maps a CLI destination name onto the ``section.field`` it overrides. Adding a
#: CLI flag means adding one line here and one add_argument() in main.py.
CLI_TO_CONFIG: dict[str, tuple[str, str]] = {
    "max_docs_per_payer": ("crawl", "max_docs_per_payer"),
    "payer_csv": ("crawl", "payer_seed_csv"),
    "payer_range": ("crawl", "payer_range"),
    "concurrency_cap": ("crawl", "concurrency_cap"),
    "rate_limit": ("crawl", "per_domain_rate_limit_per_sec"),
    "retry_limit": ("crawl", "retry_limit"),
    "backoff_base": ("crawl", "backoff_base_seconds"),
    "backoff_strategy": ("crawl", "backoff_strategy"),
    "timeout": ("crawl", "request_timeout_seconds"),
    "user_agent": ("crawl", "user_agent"),
    "max_index_pages": ("crawl", "max_index_pages_per_payer"),
    "proxy_url": ("crawl", "proxy_url"),
    "log_dir": ("logging", "log_dir"),
    "log_level": ("logging", "log_level"),
    "output_dir": ("output", "output_dir"),
    "output_format": ("output", "output_format"),
    "downloads_dir": ("output", "downloads_dir"),
    "save_downloads": ("output", "save_downloads"),
    "email_enabled": ("email", "enabled"),
    "email_recipients": ("email", "recipients"),
    "email_transport": ("email", "smtp_or_gmail_api"),
    "state_file": ("resume", "state_file"),
    "resume_enabled": ("resume", "enabled"),
}


def build_config(
    config_path: str | os.PathLike[str] | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
    base_dir: str | os.PathLike[str] | None = None,
) -> AppConfig:
    """
    Build the effective configuration for a run.

    Layers, applied in order so that later layers win:

    1. the dataclass defaults above (built-in defaults),
    2. the sections found in *config_path*, if given,
    3. *cli_overrides* - a mapping of ``argparse`` dest names to values, with
       ``None`` entries ignored so that "flag not passed" never clobbers the file.

    The result is validated before it is returned, so callers can rely on it.
    """
    config = AppConfig()
    config.base_dir = str(Path(base_dir).resolve() if base_dir else Path.cwd())

    if config_path:
        raw = load_config_file(config_path)
        config.config_path = str(Path(config_path).expanduser().resolve())
        sections = {f.name: getattr(config, f.name) for f in fields(config)
                    if is_dataclass(getattr(config, f.name))}
        for key, value in raw.items():
            if key in sections:
                if value is None:
                    continue
                if not isinstance(value, Mapping):
                    raise ConfigError(f"config section {key!r} must be a mapping")
                _apply_mapping(sections[key], value, key)
            elif key in {"config_path", "base_dir"}:
                continue  # informational only; never taken from the file
            else:
                raise ConfigError(
                    f"unknown config section {key!r}. Valid sections: "
                    + ", ".join(sorted(sections))
                )

    for dest, value in (cli_overrides or {}).items():
        if value is None:
            continue  # flag absent -> lower-precedence layer keeps its value
        target = CLI_TO_CONFIG.get(dest)
        if target is None:
            continue  # CLI-only argument (e.g. --config, --dry-run)
        section_name, field_name = target
        section = getattr(config, section_name)
        declared = {f.name: f.type for f in fields(section)}[field_name]
        setattr(section, field_name, _coerce(value, declared, f"--{dest.replace('_', '-')}"))

    validate_config(config)
    return config


def parse_payer_range(spec: str, total: int) -> list[int]:
    """
    Expand a ``payer_range`` spec into sorted 0-based indexes into the seed list.

    Accepts comma-separated singles and inclusive ranges over 1-based positions:
    ``"1-10"``, ``"3,5,7"``, ``"1-3,8"``, or ``"all"``. Out-of-bounds positions
    are an error rather than a silent no-op, because a range that quietly selects
    nothing looks identical to "every payer blocked us".
    """
    text = str(spec or "").strip().lower()
    if not text or text in {"all", "*"}:
        return list(range(total))
    selected: set[int] = set()
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", chunk)
        if match:
            start, end = int(match.group(1)), int(match.group(2))
            if start > end:
                raise ConfigError(f"payer_range {chunk!r}: start is after end")
            positions = range(start, end + 1)
        elif chunk.isdigit():
            positions = range(int(chunk), int(chunk) + 1)
        else:
            raise ConfigError(
                f"payer_range {chunk!r} is not a number or an N-M range (e.g. '1-5' or '3,5,7')"
            )
        for position in positions:
            if position < 1 or position > total:
                raise ConfigError(
                    f"payer_range position {position} is outside 1-{total} "
                    f"(the seed list has {total} payers)"
                )
            selected.add(position - 1)
    if not selected:
        raise ConfigError(f"payer_range {spec!r} selected no payers")
    return sorted(selected)


def _require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def validate_config(config: AppConfig) -> None:
    """
    Validate the merged config, raising :class:`ConfigError` listing every problem.

    All problems are collected before raising so that a badly edited config is
    fixed in one pass instead of one error per run.
    """
    errors: list[str] = []
    crawl = config.crawl

    _require(crawl.max_docs_per_payer > 0,
             "crawl.max_docs_per_payer must be >= 1", errors)
    _require(crawl.concurrency_cap > 0,
             "crawl.concurrency_cap must be >= 1", errors)
    _require(crawl.concurrency_cap <= 64,
             "crawl.concurrency_cap > 64 is not polite; lower it or crawl fewer payers", errors)
    _require(crawl.per_domain_rate_limit_per_sec > 0,
             "crawl.per_domain_rate_limit_per_sec must be > 0", errors)
    _require(crawl.per_domain_rate_limit_per_sec <= 10,
             "crawl.per_domain_rate_limit_per_sec > 10 req/s per host is not polite", errors)
    _require(crawl.retry_limit >= 0,
             "crawl.retry_limit must be >= 0", errors)
    _require(crawl.backoff_base_seconds > 0,
             "crawl.backoff_base_seconds must be > 0", errors)
    _require(crawl.backoff_strategy in {"exponential", "fixed"},
             f"crawl.backoff_strategy must be 'exponential' or 'fixed', "
             f"got {crawl.backoff_strategy!r}", errors)
    _require(crawl.request_timeout_seconds > 0,
             "crawl.request_timeout_seconds must be > 0", errors)
    _require(bool(crawl.user_agent.strip()),
             "crawl.user_agent is required", errors)
    # The brief requires a *descriptive* UA carrying a contact point.
    _require(bool(re.search(r"[\w.+-]+@[\w.-]+\.\w+|https?://", crawl.user_agent)),
             "crawl.user_agent must include a contact email or URL, e.g. "
             "'MyCrawler/1.0 (+contact: me@example.com)'", errors)
    _require(crawl.max_index_pages_per_payer > 0,
             "crawl.max_index_pages_per_payer must be >= 1", errors)
    _require(crawl.max_sitemaps_per_payer > 0,
             "crawl.max_sitemaps_per_payer must be >= 1", errors)
    _require(crawl.max_enumeration_candidates >= 0,
             "crawl.max_enumeration_candidates must be >= 0", errors)
    _require(crawl.max_download_bytes > 0,
             "crawl.max_download_bytes must be > 0", errors)

    if not config.payer_csv_path.is_file():
        errors.append(
            f"crawl.payer_seed_csv not found: {config.payer_csv_path} "
            "(set it in the config file or pass --payer-csv)"
        )
    try:
        parse_payer_range(crawl.payer_range, 10)
    except ConfigError as exc:
        # Validated again against the real row count once the CSV is loaded; this
        # early check just catches syntactically broken specs.
        if "outside 1-10" not in str(exc):
            errors.append(str(exc))

    level = str(config.logging.log_level).upper()
    _require(level in {"DEBUG", "INFO", "WARNING", "WARN", "ERROR", "CRITICAL"},
             f"logging.log_level {config.logging.log_level!r} is not a valid level", errors)
    config.logging.log_level = level
    _require(bool(str(config.logging.log_dir).strip()),
             "logging.log_dir is required", errors)
    _require(config.logging.rotate_max_bytes > 0,
             "logging.rotate_max_bytes must be > 0", errors)
    _require(config.logging.rotate_backup_count >= 0,
             "logging.rotate_backup_count must be >= 0", errors)

    fmt = str(config.output.output_format).lower()
    _require(fmt in {"csv", "xlsx"},
             f"output.output_format must be 'csv' or 'xlsx', got "
             f"{config.output.output_format!r}", errors)
    config.output.output_format = fmt
    _require(bool(str(config.output.output_dir).strip()),
             "output.output_dir is required", errors)
    _require(bool(str(config.output.downloads_dir).strip()),
             "output.downloads_dir is required", errors)

    email = config.email
    if email.enabled:
        # Only validated when enabled: with email off the run must not require
        # any credentials or recipients at all.
        _require(bool(email.recipients),
                 "email.enabled is true but email.recipients is empty", errors)
        for address in email.recipients:
            _require(bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", address)),
                     f"email.recipients contains an invalid address: {address!r}", errors)
        transport = str(email.smtp_or_gmail_api).lower()
        _require(transport in {"gmail_api", "smtp"},
                 f"email.smtp_or_gmail_api must be 'gmail_api' or 'smtp', "
                 f"got {email.smtp_or_gmail_api!r}", errors)
        email.smtp_or_gmail_api = transport
        _require("{run_id}" in email.subject_template or bool(email.subject_template.strip()),
                 "email.subject_template must not be empty", errors)
        _require(email.smtp_port > 0, "email.smtp_port must be > 0", errors)

    _require(bool(str(config.resume.state_file).strip()),
             "resume.state_file is required", errors)

    if errors:
        bullet_list = "\n".join(f"  - {message}" for message in errors)
        source = config.config_path or "built-in defaults"
        raise ConfigError(f"invalid configuration ({source}):\n{bullet_list}")


def describe_precedence() -> str:
    """One-line reminder of the precedence order, printed by ``--show-config``."""
    return "precedence: CLI argument > config file value > built-in default"


def iter_config_fields() -> Iterable[tuple[str, str, Any]]:
    """
    Yield ``(section, field, default)`` for every configurable value.

    Used by ``--show-config`` and by the test that keeps config.example.yaml in
    sync with the dataclasses, so a newly added field cannot go undocumented.
    """
    defaults = AppConfig()
    for section_field in fields(defaults):
        section = getattr(defaults, section_field.name)
        if not is_dataclass(section):
            continue
        for value_field in fields(section):
            yield section_field.name, value_field.name, getattr(section, value_field.name)
