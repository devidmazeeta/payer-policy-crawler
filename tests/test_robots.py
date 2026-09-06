"""
robots.txt parsing and access-decision tests.

Compliance is the part of this project where a bug is not just a wrong number in
a spreadsheet - it means fetching something a site asked us not to. So the rules
are tested directly, including the awkward cases (wildcards, ``$`` anchors,
Allow/Disallow precedence, group selection, malformed files).
"""

from __future__ import annotations

import pytest

from crawler.robots import RobotsFile, parse_robots

UA = "PayerPolicyCrawler/1.0 (+contact: test@example.com)"


def test_parses_all_directives_not_just_sitemaps():
    """The brief requires full parsing: Allow, Disallow AND Crawl-delay."""
    robots = parse_robots(
        """
        User-agent: *
        Disallow: /search
        Disallow: /private/
        Allow: /private/public-policies/
        Crawl-delay: 2

        Sitemap: https://example.com/sitemap.xml
        Sitemap: https://example.com/sitemap-policies.xml
        """,
        host="example.com",
    )
    assert len(robots.groups) == 1
    group = robots.groups[0]
    assert len(group.rules) == 3
    assert group.crawl_delay == 2.0
    assert robots.sitemaps == [
        "https://example.com/sitemap.xml",
        "https://example.com/sitemap-policies.xml",
    ]


def test_disallow_blocks_and_reports_the_matched_rule():
    robots = parse_robots("User-agent: *\nDisallow: /search\n", host="example.com")
    decision = robots.can_fetch(UA, "https://example.com/search?q=policy")
    assert not decision.allowed
    # The matched rule is the evidence we quote in the log and in notes.
    assert decision.matched_rule == "Disallow: /search"


def test_allowed_path_is_permitted():
    robots = parse_robots("User-agent: *\nDisallow: /search\n", host="example.com")
    assert robots.can_fetch(UA, "https://example.com/policies/medical.pdf").allowed


def test_longest_match_wins_so_allow_carveout_is_honoured():
    """A narrow Allow inside a broad Disallow is the common real-world shape."""
    robots = parse_robots(
        "User-agent: *\nDisallow: /docs/\nAllow: /docs/public/\n", host="example.com"
    )
    assert not robots.can_fetch(UA, "https://example.com/docs/secret.pdf").allowed
    assert robots.can_fetch(UA, "https://example.com/docs/public/policy.pdf").allowed


def test_allow_wins_an_equal_length_tie():
    robots = parse_robots(
        "User-agent: *\nDisallow: /x/\nAllow: /x/\n", host="example.com"
    )
    assert robots.can_fetch(UA, "https://example.com/x/file.pdf").allowed


def test_wildcard_pattern_matching():
    robots = parse_robots(
        "User-agent: *\nDisallow: /*/search\nDisallow: /*?sessionid=\n",
        host="example.com",
    )
    assert not robots.can_fetch(UA, "https://example.com/provider/search").allowed
    assert not robots.can_fetch(UA, "https://example.com/a/b?sessionid=42").allowed
    assert robots.can_fetch(UA, "https://example.com/provider/policies").allowed


def test_dollar_anchor_matches_only_at_end_of_path():
    robots = parse_robots("User-agent: *\nDisallow: /*.pdf$\n", host="example.com")
    assert not robots.can_fetch(UA, "https://example.com/a/policy.pdf").allowed
    # The anchor means a .pdf followed by more path is NOT matched.
    assert robots.can_fetch(UA, "https://example.com/a/policy.pdf.html").allowed


def test_empty_disallow_value_allows_everything():
    robots = parse_robots("User-agent: *\nDisallow:\n", host="example.com")
    assert robots.can_fetch(UA, "https://example.com/anything").allowed


def test_most_specific_user_agent_group_wins():
    """A group naming our crawler overrides the wildcard group deliberately."""
    robots = parse_robots(
        """
        User-agent: *
        Disallow: /

        User-agent: PayerPolicyCrawler
        Disallow: /admin
        """,
        host="example.com",
    )
    assert robots.can_fetch(UA, "https://example.com/policies").allowed
    assert not robots.can_fetch(UA, "https://example.com/admin").allowed
    # A different crawler still gets the wildcard group.
    assert not robots.can_fetch("SomeOtherBot/2.0", "https://example.com/policies").allowed


def test_consecutive_user_agent_lines_share_one_rule_block():
    robots = parse_robots(
        """
        User-agent: BotA
        User-agent: BotB
        Disallow: /nope
        """,
        host="example.com",
    )
    assert len(robots.groups) == 1
    assert robots.groups[0].agents == ["BotA", "BotB"]
    assert not robots.can_fetch("BotA/1.0", "https://example.com/nope").allowed
    assert not robots.can_fetch("BotB/1.0", "https://example.com/nope").allowed


def test_comments_blank_lines_and_crlf_are_tolerated():
    robots = parse_robots(
        "# site rules\r\n"
        "User-agent: *\r\n"
        "\r\n"
        "Disallow: /admin  # staff only\r\n"
        "Crawl-delay: 1.5\r\n",
        host="example.com",
    )
    assert not robots.can_fetch(UA, "https://example.com/admin").allowed
    assert robots.groups[0].crawl_delay == 1.5


def test_bom_prefixed_file_parses():
    robots = parse_robots(chr(0xFEFF) + "User-agent: *\nDisallow: /x\n", host="example.com")
    assert not robots.can_fetch(UA, "https://example.com/x").allowed


def test_percent_encoded_and_decoded_paths_compare_equal():
    robots = parse_robots("User-agent: *\nDisallow: /Provider Policies\n",
                          host="example.com")
    assert not robots.can_fetch(UA, "https://example.com/Provider%20Policies/a.pdf").allowed


def test_rules_before_any_user_agent_line_are_treated_as_wildcard():
    """Malformed but common; the lenient reading is also the safe one."""
    robots = parse_robots("Disallow: /secret\n", host="example.com")
    assert not robots.can_fetch(UA, "https://example.com/secret").allowed


def test_unknown_directives_are_recorded_not_silently_dropped():
    robots = parse_robots(
        "User-agent: *\nDisallow: /x\nHost: example.com\nClean-param: sid\ngarbage line\n",
        host="example.com",
    )
    assert robots.unknown_directives
    assert any("garbage" in entry for entry in robots.unknown_directives)


def test_crawl_delay_is_capped_to_keep_a_run_finite():
    robots = parse_robots("User-agent: *\nCrawl-delay: 3600\n", host="example.com")
    assert robots.crawl_delay_for(UA) == 30.0


def test_invalid_crawl_delay_is_ignored():
    robots = parse_robots("User-agent: *\nCrawl-delay: soon\n", host="example.com")
    assert robots.crawl_delay_for(UA) is None


# ---------------------------------------------------------------------------
# Fail-open / fail-closed policy for a robots.txt we could not read
# ---------------------------------------------------------------------------
def test_missing_robots_txt_means_unrestricted():
    robots = RobotsFile(host="example.com", status=404, fetched=False, default_allow=True,
                        note="robots.txt not present (404)")
    assert robots.can_fetch(UA, "https://example.com/anything").allowed


def test_unreadable_robots_txt_means_stay_out():
    """A 5xx or transport failure must not be read as permission."""
    robots = RobotsFile(host="example.com", status=503, fetched=False, default_allow=False,
                        note="robots.txt server error 503; assuming disallowed")
    decision = robots.can_fetch(UA, "https://example.com/anything")
    assert not decision.allowed
    assert "assuming disallowed" in decision.reason


def test_decision_is_truthy_for_convenience():
    robots = parse_robots("User-agent: *\nDisallow: /x\n", host="example.com")
    assert bool(robots.can_fetch(UA, "https://example.com/ok"))
    assert not bool(robots.can_fetch(UA, "https://example.com/x"))


def test_summary_reports_counts_for_the_log_record():
    robots = parse_robots(
        "User-agent: *\nDisallow: /a\nDisallow: /b\nCrawl-delay: 1\n"
        "Sitemap: https://example.com/s.xml\n",
        host="example.com",
    )
    summary = robots.summary()
    assert summary["rules"] == 2
    assert summary["sitemaps"] == 1
    assert summary["crawl_delay"] == 1.0
