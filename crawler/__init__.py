"""
Payer Policy Document Discovery crawler.

A polite, resumable crawler that discovers publicly available provider and
medical policy documents on health-payer websites and emits them as a 22-column
dataset (see :mod:`crawler.schema` for the authoritative column list).

Module map:

* :mod:`crawler.main`           - CLI entrypoint, config merge, orchestration
* :mod:`crawler.config`         - config loading/validation, CLI precedence
* :mod:`crawler.seeds`          - payer seed list loading
* :mod:`crawler.robots`         - robots.txt fetch + full rule parsing
* :mod:`crawler.fetcher`        - rate-limited, retrying async HTTP
* :mod:`crawler.discovery`      - sitemap/index crawl + enumeration fallback
* :mod:`crawler.extractor`      - HTML/PDF parsing into schema rows
* :mod:`crawler.dedupe`         - near-duplicate collapsing rule
* :mod:`crawler.schema`         - 22-column model, enums, validation
* :mod:`crawler.storage`        - document downloads, CSV/XLSX writers
* :mod:`crawler.state`          - SQLite/JSON checkpoint for resume
* :mod:`crawler.mailer`         - optional Gmail delivery
* :mod:`crawler.logging_setup`  - structured JSON-lines + human logs

Run it with::

    python -m crawler.main --config config.yaml
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
