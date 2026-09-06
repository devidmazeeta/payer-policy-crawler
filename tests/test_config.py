"""
Configuration tests: the precedence order, validation, and payer-range parsing.

The precedence rule (CLI > config file > built-in default) is the one the brief
states explicitly, so it is tested at every layer boundary rather than assumed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crawler.config import (
    AppConfig,
    ConfigError,
    build_config,
    iter_config_fields,
    load_config_file,
    parse_payer_range,
    validate_config,
)
from crawler.seeds import load_payers, select_payers

MINIMAL_SEED_CSV = (
    "payer_name,payer_alias,hint_host\n"
    "Payer One,P1,one.example.com\n"
    "Payer Two,P2,two.example.com\n"
    "Payer Three,P3,three.example.com\n"
)


@pytest.fixture
def seed_csv(tmp_path: Path) -> Path:
    path = tmp_path / "payers.csv"
    path.write_text(MINIMAL_SEED_CSV, encoding="utf-8")
    return path


def write_yaml(tmp_path: Path, body: str, seed_csv: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        body + f"\n  payer_seed_csv: \"{seed_csv.as_posix()}\"\n", encoding="utf-8"
    )
    return path


# ---------------------------------------------------------------------------
# Precedence: CLI > file > default
# ---------------------------------------------------------------------------
def test_defaults_apply_when_nothing_is_given(tmp_path, seed_csv):
    config = build_config(cli_overrides={"payer_csv": str(seed_csv)}, base_dir=tmp_path)
    assert config.crawl.max_docs_per_payer == 50
    assert config.crawl.concurrency_cap == 8
    assert config.output.output_format == "csv"
    assert config.email.enabled is False


def test_config_file_overrides_the_default(tmp_path, seed_csv):
    path = write_yaml(tmp_path, "crawl:\n  max_docs_per_payer: 7", seed_csv)
    config = build_config(config_path=path, base_dir=tmp_path)
    assert config.crawl.max_docs_per_payer == 7


def test_cli_overrides_the_config_file(tmp_path, seed_csv):
    path = write_yaml(tmp_path, "crawl:\n  max_docs_per_payer: 7", seed_csv)
    config = build_config(
        config_path=path,
        cli_overrides={"max_docs_per_payer": 3},
        base_dir=tmp_path,
    )
    assert config.crawl.max_docs_per_payer == 3


def test_absent_cli_flag_does_not_clobber_the_file_value(tmp_path, seed_csv):
    """An unpassed flag arrives as None and must be invisible to the merge."""
    path = write_yaml(tmp_path, "crawl:\n  max_docs_per_payer: 7", seed_csv)
    config = build_config(
        config_path=path,
        cli_overrides={"max_docs_per_payer": None, "payer_range": None},
        base_dir=tmp_path,
    )
    assert config.crawl.max_docs_per_payer == 7


def test_cli_only_arguments_are_ignored_by_the_merge(tmp_path, seed_csv):
    config = build_config(
        cli_overrides={"payer_csv": str(seed_csv), "dry_run": True, "config": "x.yaml"},
        base_dir=tmp_path,
    )
    assert isinstance(config, AppConfig)


def test_every_documented_cli_flag_maps_to_a_real_config_field():
    """Guards against a CLI flag that silently does nothing."""
    from crawler.config import CLI_TO_CONFIG

    known = {(section, field) for section, field, _ in iter_config_fields()}
    for dest, target in CLI_TO_CONFIG.items():
        assert target in known, f"--{dest} maps to unknown config field {target}"


# ---------------------------------------------------------------------------
# File formats and parsing
# ---------------------------------------------------------------------------
def test_json_config_is_supported(tmp_path, seed_csv):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"crawl": {"max_docs_per_payer": 11,
                              "payer_seed_csv": str(seed_csv)}}),
        encoding="utf-8",
    )
    config = build_config(config_path=path, base_dir=tmp_path)
    assert config.crawl.max_docs_per_payer == 11


def test_missing_config_file_is_a_clear_error(tmp_path):
    with pytest.raises(ConfigError, match="config file not found"):
        load_config_file(tmp_path / "nope.yaml")


def test_unsupported_extension_is_rejected(tmp_path):
    path = tmp_path / "config.ini"
    path.write_text("[x]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unsupported config extension"):
        load_config_file(path)


def test_unknown_section_is_rejected(tmp_path, seed_csv):
    path = tmp_path / "config.yaml"
    path.write_text("crawler:\n  max_docs_per_payer: 5\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown config section"):
        build_config(config_path=path, base_dir=tmp_path)


def test_unknown_key_is_rejected_because_it_is_almost_always_a_typo(tmp_path, seed_csv):
    path = tmp_path / "config.yaml"
    path.write_text("crawl:\n  concurrency_camp: 5\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown config key"):
        build_config(config_path=path, base_dir=tmp_path)


def test_string_values_are_coerced_to_the_declared_type(tmp_path, seed_csv):
    path = write_yaml(
        tmp_path,
        'crawl:\n  max_docs_per_payer: "9"\n  per_domain_rate_limit_per_sec: "0.5"',
        seed_csv,
    )
    config = build_config(config_path=path, base_dir=tmp_path)
    assert config.crawl.max_docs_per_payer == 9
    assert config.crawl.per_domain_rate_limit_per_sec == 0.5


def test_comma_separated_recipients_from_the_cli_become_a_list(tmp_path, seed_csv):
    config = build_config(
        cli_overrides={
            "payer_csv": str(seed_csv),
            "email_recipients": "a@example.com, b@example.com",
        },
        base_dir=tmp_path,
    )
    assert config.email.recipients == ["a@example.com", "b@example.com"]


def test_non_numeric_value_for_an_int_field_is_reported(tmp_path, seed_csv):
    path = write_yaml(tmp_path, 'crawl:\n  max_docs_per_payer: "many"', seed_csv)
    with pytest.raises(ConfigError, match="expected an integer"):
        build_config(config_path=path, base_dir=tmp_path)


# ---------------------------------------------------------------------------
# Validation (fail fast, and report every problem at once)
# ---------------------------------------------------------------------------
def test_validation_collects_all_problems_in_one_message(tmp_path, seed_csv):
    config = AppConfig()
    config.base_dir = str(tmp_path)
    config.crawl.payer_seed_csv = str(seed_csv)
    config.crawl.max_docs_per_payer = 0
    config.crawl.concurrency_cap = 0
    config.crawl.backoff_strategy = "random"
    with pytest.raises(ConfigError) as excinfo:
        validate_config(config)
    message = str(excinfo.value)
    assert "max_docs_per_payer" in message
    assert "concurrency_cap" in message
    assert "backoff_strategy" in message


def test_user_agent_must_carry_a_contact_point(tmp_path, seed_csv):
    """The brief requires a descriptive UA with a contact email or URL."""
    config = AppConfig()
    config.base_dir = str(tmp_path)
    config.crawl.payer_seed_csv = str(seed_csv)
    config.crawl.user_agent = "bot"
    with pytest.raises(ConfigError, match="contact email or URL"):
        validate_config(config)


def test_impolite_rate_limit_is_rejected(tmp_path, seed_csv):
    config = AppConfig()
    config.base_dir = str(tmp_path)
    config.crawl.payer_seed_csv = str(seed_csv)
    config.crawl.per_domain_rate_limit_per_sec = 50.0
    with pytest.raises(ConfigError, match="not polite"):
        validate_config(config)


def test_impolite_concurrency_is_rejected(tmp_path, seed_csv):
    config = AppConfig()
    config.base_dir = str(tmp_path)
    config.crawl.payer_seed_csv = str(seed_csv)
    config.crawl.concurrency_cap = 200
    with pytest.raises(ConfigError, match="not polite"):
        validate_config(config)


def test_missing_seed_csv_is_reported(tmp_path):
    config = AppConfig()
    config.base_dir = str(tmp_path)
    config.crawl.payer_seed_csv = "does-not-exist.csv"
    with pytest.raises(ConfigError, match="payer_seed_csv not found"):
        validate_config(config)


def test_email_validation_only_applies_when_enabled(tmp_path, seed_csv):
    """With email off, the run must need no recipients and no credentials."""
    config = AppConfig()
    config.base_dir = str(tmp_path)
    config.crawl.payer_seed_csv = str(seed_csv)
    config.email.enabled = False
    config.email.recipients = []
    validate_config(config)  # must not raise

    config.email.enabled = True
    with pytest.raises(ConfigError, match="recipients is empty"):
        validate_config(config)


def test_invalid_email_address_is_reported(tmp_path, seed_csv):
    config = AppConfig()
    config.base_dir = str(tmp_path)
    config.crawl.payer_seed_csv = str(seed_csv)
    config.email.enabled = True
    config.email.recipients = ["not-an-address"]
    with pytest.raises(ConfigError, match="invalid address"):
        validate_config(config)


def test_invalid_log_level_is_reported(tmp_path, seed_csv):
    config = AppConfig()
    config.base_dir = str(tmp_path)
    config.crawl.payer_seed_csv = str(seed_csv)
    config.logging.log_level = "CHATTY"
    with pytest.raises(ConfigError, match="not a valid level"):
        validate_config(config)


def test_paths_resolve_against_base_dir(tmp_path, seed_csv):
    config = build_config(cli_overrides={"payer_csv": str(seed_csv)}, base_dir=tmp_path)
    assert config.log_dir == (tmp_path / "logs").resolve()
    assert config.output_dir == (tmp_path / "output").resolve()
    assert config.state_path == (tmp_path / "state" / "checkpoint.db").resolve()


# ---------------------------------------------------------------------------
# payer_range
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "spec,total,expected",
    [
        ("1-10", 10, list(range(10))),
        ("1-5", 10, [0, 1, 2, 3, 4]),
        ("3,5,7", 10, [2, 4, 6]),
        ("1-3,8", 10, [0, 1, 2, 7]),
        ("2", 10, [1]),
        ("all", 4, [0, 1, 2, 3]),
        ("", 3, [0, 1, 2]),
        ("2-2", 5, [1]),
        ("3,3,3", 5, [2]),          # duplicates collapse
        ("5,1", 5, [0, 4]),         # output is sorted
    ],
)
def test_parse_payer_range(spec, total, expected):
    assert parse_payer_range(spec, total) == expected


def test_out_of_bounds_range_is_an_error_not_a_silent_no_op():
    """A range that quietly selects nothing looks like 'every payer blocked us'."""
    with pytest.raises(ConfigError, match="outside 1-3"):
        parse_payer_range("1-9", 3)


def test_reversed_range_is_rejected():
    with pytest.raises(ConfigError, match="start is after end"):
        parse_payer_range("5-2", 10)


def test_garbage_range_is_rejected():
    with pytest.raises(ConfigError, match="not a number or an N-M range"):
        parse_payer_range("first-five", 10)


def test_select_payers_applies_the_range(seed_csv):
    payers = load_payers(seed_csv)
    assert len(payers) == 3
    selected = select_payers(payers, "1,3")
    assert [payer.payer_alias for payer in selected] == ["P1", "P3"]


# ---------------------------------------------------------------------------
# config.example.yaml stays in sync with the dataclasses
# ---------------------------------------------------------------------------
def test_example_config_documents_every_field():
    """A newly added config field must not go undocumented."""
    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    text = example.read_text(encoding="utf-8")
    missing = [
        f"{section}.{field}"
        for section, field, _ in iter_config_fields()
        if f"{field}:" not in text
    ]
    assert not missing, f"config.example.yaml is missing: {missing}"


def test_shipped_config_yaml_loads_and_validates():
    """The config the README tells you to run must actually be valid."""
    root = Path(__file__).resolve().parents[1]
    config = build_config(config_path=root / "config.yaml", base_dir=root)
    assert config.crawl.max_docs_per_payer > 0
    assert config.email.enabled is False  # must ship with email off
