"""
robots.txt fetching and full rule parsing.

The brief is explicit that we must parse *all* directives - every ``Allow``,
``Disallow`` and ``Crawl-delay``, not just the ``Sitemap:`` lines - and never
fetch a Disallowed path. Python's stdlib ``urllib.robotparser`` is not good
enough here: it does not expose ``Crawl-delay`` per group reliably, it does not
implement ``$`` anchoring, and it gives us no way to report *which* rule
matched, which we need as evidence in the ``robots.blocked`` log records and in
the ``notes`` column.

So this module implements RFC 9309 semantics directly:

* group selection by most-specific matching ``User-agent`` token, falling back
  to ``*``;
* ``*`` wildcard and ``$`` end-anchor support in paths;
* longest-match-wins between a competing ``Allow`` and ``Disallow``, with
  ``Allow`` winning an exact-length tie (the documented Google/RFC behaviour);
* an empty ``Disallow:`` value meaning "allow everything";
* a missing or 4xx robots.txt meaning "unrestricted", while a 5xx or transport
  failure is treated conservatively as "assume disallowed" so a flaky server
  never tricks us into crawling something it wanted protected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import unquote, urljoin, urlsplit

from .fetcher import Fetcher, FetchResult

#: Directives we understand. Anything else (``Host``, ``Clean-param``, vendor
#: extensions) is recorded in ``unknown_directives`` for the log but ignored.
_KNOWN_DIRECTIVES = {"user-agent", "allow", "disallow", "crawl-delay", "sitemap",
                     "request-rate", "visit-time", "noindex"}

#: UTF-8 BOM, which several payers prepend to robots.txt. Spelled with chr()
#: so this source file stays pure ASCII.
_BOM = chr(0xFEFF)


@dataclass(slots=True)
class Rule:
    """
    One ``Allow`` or ``Disallow`` path pattern.

    ``raw`` keeps the pattern exactly as it appeared in the file so we can quote
    it verbatim as evidence, e.g. ``Disallow: /*/search*``.
    """

    allow: bool
    raw: str
    pattern: re.Pattern[str]
    #: Specificity for longest-match-wins: the length of the literal path text.
    length: int

    def matches(self, path: str) -> bool:
        return bool(self.pattern.match(path))

    def as_text(self) -> str:
        return f"{'Allow' if self.allow else 'Disallow'}: {self.raw}"


@dataclass(slots=True)
class RobotsGroup:
    """All directives belonging to one ``User-agent`` group."""

    agents: list[str] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    crawl_delay: float | None = None
    request_rate: str = ""


@dataclass(slots=True)
class RobotsDecision:
    """
    The outcome of an access check, with the reason attached.

    Carrying the matched rule (rather than just a bool) is what lets the
    ``robots.blocked`` event and the ``notes`` column say *why* a URL was
    skipped, which is the difference between a reviewer trusting the run and
    having to take our word for it.
    """

    allowed: bool
    reason: str = ""
    matched_rule: str = ""

    def __bool__(self) -> bool:
        return self.allowed


def _compile_pattern(raw: str) -> re.Pattern[str]:
    """
    Compile a robots path pattern into a regex anchored at the start of the path.

    ``*`` matches any run of characters, a trailing ``$`` anchors the end of the
    path, and every other character is matched literally. Percent-encoded octets
    are left alone here; :func:`_normalise_path` unquotes the *URL* side so that
    ``/a%20b`` and ``/a b`` compare equal.
    """
    pattern_parts: list[str] = []
    anchored_end = raw.endswith("$")
    body = raw[:-1] if anchored_end else raw
    for char in body:
        if char == "*":
            pattern_parts.append(".*")
        else:
            pattern_parts.append(re.escape(char))
    regex = "".join(pattern_parts)
    if anchored_end:
        regex += r"\Z"
    return re.compile(regex, re.IGNORECASE)


def _normalise_path(url_or_path: str) -> str:
    """
    Reduce a URL (or bare path) to the path+query string robots rules match on.

    Percent-decoding makes ``/Provider%20Policies`` and ``/Provider Policies``
    equivalent, and a missing path becomes ``/`` as RFC 9309 requires.
    """
    if "://" in url_or_path:
        split = urlsplit(url_or_path)
        path = split.path or "/"
        if split.query:
            path = f"{path}?{split.query}"
    else:
        path = url_or_path or "/"
    if not path.startswith("/"):
        path = "/" + path
    try:
        return unquote(path)
    except (UnicodeDecodeError, ValueError):
        return path


class RobotsFile:
    """
    A parsed robots.txt for one host.

    Instances are cheap and immutable in practice; :class:`RobotsCache` keeps one
    per host for the whole run so we never re-fetch the same robots.txt.
    """

    def __init__(
        self,
        host: str,
        *,
        groups: list[RobotsGroup] | None = None,
        sitemaps: list[str] | None = None,
        status: int = 0,
        fetched: bool = False,
        default_allow: bool = True,
        note: str = "",
        unknown_directives: list[str] | None = None,
        raw_line_count: int = 0,
    ) -> None:
        self.host = host
        self.groups = groups or []
        #: Sitemap URLs, which are host-global in robots.txt (not per group).
        self.sitemaps = sitemaps or []
        self.status = status
        self.fetched = fetched
        #: What to do when no robots.txt could be parsed. True when the file is
        #: absent (404 == unrestricted); False when the server erred (5xx), where
        #: the conservative reading is "stay out".
        self.default_allow = default_allow
        self.note = note
        self.unknown_directives = unknown_directives or []
        self.raw_line_count = raw_line_count

    # -- group selection -----------------------------------------------------
    def group_for(self, user_agent: str) -> RobotsGroup | None:
        """
        Select the applicable group for *user_agent* per RFC 9309.

        The most specific match wins: the longest ``User-agent`` token that is a
        case-insensitive substring of our UA. ``*`` is the fallback and is only
        used when no named group matches - a site that names us specifically has
        overridden its own wildcard group on purpose.
        """
        ua = user_agent.lower()
        best: RobotsGroup | None = None
        best_length = -1
        wildcard: RobotsGroup | None = None
        for group in self.groups:
            for agent in group.agents:
                token = agent.lower().strip()
                if token == "*":
                    if wildcard is None:
                        wildcard = group
                    continue
                if token and token in ua and len(token) > best_length:
                    best, best_length = group, len(token)
        return best or wildcard

    # -- the access decision -------------------------------------------------
    def can_fetch(self, user_agent: str, url: str) -> RobotsDecision:
        """
        Decide whether *user_agent* may fetch *url*.

        Longest-match-wins across the group's rules; on an equal-length tie the
        ``Allow`` wins, which is the behaviour every major crawler implements and
        the one site owners expect when they write a broad ``Disallow`` plus a
        narrow ``Allow`` carve-out.
        """
        if not self.fetched:
            return RobotsDecision(
                allowed=self.default_allow,
                reason=self.note or ("no robots.txt found" if self.default_allow
                                     else "robots.txt unreadable; assuming disallowed"),
            )
        group = self.group_for(user_agent)
        if group is None or not group.rules:
            return RobotsDecision(True, reason="no applicable robots.txt rules")

        path = _normalise_path(url)
        winner: Rule | None = None
        for rule in group.rules:
            if not rule.matches(path):
                continue
            if winner is None or rule.length > winner.length:
                winner = rule
            elif rule.length == winner.length and rule.allow and not winner.allow:
                winner = rule  # Allow breaks the tie.

        if winner is None:
            return RobotsDecision(True, reason="no matching robots.txt rule")
        if winner.allow:
            return RobotsDecision(True, reason="explicitly allowed",
                                  matched_rule=winner.as_text())
        return RobotsDecision(False, reason="disallowed by robots.txt",
                              matched_rule=winner.as_text())

    def crawl_delay_for(self, user_agent: str) -> float | None:
        """Return the applicable ``Crawl-delay`` in seconds, or ``None``."""
        group = self.group_for(user_agent)
        return group.crawl_delay if group else None

    def summary(self) -> dict[str, object]:
        """Compact dict for the ``robots.loaded`` log record."""
        rule_count = sum(len(group.rules) for group in self.groups)
        return {
            "host": self.host,
            "status": self.status,
            "groups": len(self.groups),
            "rules": rule_count,
            "sitemaps": len(self.sitemaps),
            "crawl_delay": self.crawl_delay_for("*"),
            "note": self.note,
        }


def parse_robots(text: str, host: str = "", status: int = 200) -> RobotsFile:
    """
    Parse robots.txt *text* into a :class:`RobotsFile`.

    Handles the real-world messiness: CRLF line endings, ``#`` comments (both
    whole-line and trailing), BOMs, blank lines splitting groups, several
    consecutive ``User-agent`` lines sharing one rule block, and directives with
    missing or malformed values. Unparseable lines are counted and reported
    rather than silently dropped, because a robots.txt we misread is a
    compliance risk, not a cosmetic bug.
    """
    groups: list[RobotsGroup] = []
    sitemaps: list[str] = []
    unknown: list[str] = []
    current: RobotsGroup | None = None
    # True while we are reading consecutive User-agent lines that all share the
    # rule block that follows them.
    accumulating_agents = False
    line_count = 0

    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line_count += 1
        line = raw_line.lstrip(_BOM).strip()
        if not line or line.startswith("#"):
            continue
        # Strip trailing comments, e.g. "Disallow: /admin  # staff only".
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            unknown.append(line[:120])
            continue

        directive, _, value = line.partition(":")
        directive = directive.strip().lower()
        value = value.strip()

        if directive not in _KNOWN_DIRECTIVES:
            unknown.append(line[:120])
            continue

        if directive == "user-agent":
            if not value:
                continue
            if current is None or not accumulating_agents:
                current = RobotsGroup()
                groups.append(current)
                accumulating_agents = True
            current.agents.append(value)
            continue

        if directive == "sitemap":
            # Host-global directive: valid even outside any group.
            if value:
                sitemaps.append(value)
            continue

        # Every remaining directive belongs to a group. A file that opens with
        # rules and no User-agent line is malformed; the lenient reading (and
        # the safe one) is to treat those rules as belonging to "*".
        if current is None:
            current = RobotsGroup(agents=["*"])
            groups.append(current)
        accumulating_agents = False

        if directive in {"allow", "disallow"}:
            if directive == "disallow" and value == "":
                # "Disallow:" with an empty value explicitly allows everything.
                continue
            if not value:
                continue
            current.rules.append(
                Rule(
                    allow=(directive == "allow"),
                    raw=value,
                    pattern=_compile_pattern(_normalise_path(value)),
                    # Specificity ignores the wildcard/anchor metacharacters so
                    # "/a/b/c" beats "/a/*" as a reviewer would expect.
                    length=len(_normalise_path(value).replace("*", "").rstrip("$")),
                )
            )
        elif directive == "crawl-delay":
            try:
                delay = float(value)
                if delay > 0:
                    # Cap at 30s: a few payers publish absurd delays (300s+)
                    # that would stall the run. Documented in NOTES.md as a
                    # deliberate, disclosed deviation.
                    current.crawl_delay = min(delay, 30.0)
            except ValueError:
                unknown.append(line[:120])
        elif directive == "request-rate":
            current.request_rate = value
        # "noindex" is an indexing hint, not an access rule; we record and ignore.

    return RobotsFile(
        host=host,
        groups=groups,
        sitemaps=sitemaps,
        status=status,
        fetched=True,
        default_allow=True,
        unknown_directives=unknown,
        raw_line_count=line_count,
    )


class RobotsCache:
    """
    Per-host robots.txt cache for one run.

    Guarantees at most one robots.txt request per host and applies each host's
    ``Crawl-delay`` to the fetcher's token bucket the moment it is parsed, so the
    very next request to that host is already correctly paced.
    """

    def __init__(self, fetcher: Fetcher, log, user_agent: str) -> None:
        self.fetcher = fetcher
        self.log = log
        self.user_agent = user_agent
        self._cache: dict[str, RobotsFile] = {}

    async def get(self, url_or_host: str, *, payer: str = "") -> RobotsFile:
        """
        Return the (cached) :class:`RobotsFile` governing *url_or_host*.

        Emits one ``robots.loaded`` record per host on first fetch, including the
        rule and sitemap counts, so the log alone shows what we were working from.
        """
        host = (urlsplit(url_or_host).hostname
                if "://" in url_or_host else url_or_host.split("/")[0]) or url_or_host
        host = host.lower()
        if host in self._cache:
            return self._cache[host]

        scheme = urlsplit(url_or_host).scheme if "://" in url_or_host else "https"
        robots_url = f"{scheme or 'https'}://{host}/robots.txt"
        result = await self.fetcher.get(robots_url, use_cache=True, context="robots")
        robots = self._interpret(host, robots_url, result)
        self._cache[host] = robots

        delay = robots.crawl_delay_for(self.user_agent)
        if delay:
            await self.fetcher.apply_crawl_delay(host, delay)

        self.log.info(
            "robots.loaded",
            f"{robots_url} -> HTTP {result.status}; "
            f"{sum(len(g.rules) for g in robots.groups)} rule(s), "
            f"{len(robots.sitemaps)} sitemap(s)"
            + (f", crawl-delay {delay}s" if delay else ""),
            payer=payer or None,
            url=robots_url,
            **robots.summary(),
        )
        if robots.unknown_directives:
            self.log.debug(
                "robots.unknown_directives",
                f"{host}: {len(robots.unknown_directives)} unrecognised line(s)",
                payer=payer or None, host=host,
                samples=robots.unknown_directives[:5],
            )
        return robots

    def _interpret(self, host: str, robots_url: str, result: FetchResult) -> RobotsFile:
        """
        Turn a robots.txt fetch into a :class:`RobotsFile`, including the
        fail-open / fail-closed policy for the non-200 cases.
        """
        if result.status == 200 and result.content:
            kind = result.sniff_kind()
            if kind in {"html", "xml"}:
                # Some payers answer /robots.txt with their 200 marketing page.
                # Parsing that as robots rules would be nonsense, so treat it as
                # "no robots.txt" (unrestricted) and say so in the note.
                return RobotsFile(
                    host=host, status=result.status, fetched=False, default_allow=True,
                    note="robots.txt returned HTML, not a robots file; treating as absent",
                )
            robots = parse_robots(result.text(), host=host, status=result.status)
            return robots

        if result.status in {404, 410}:
            return RobotsFile(host=host, status=result.status, fetched=False,
                              default_allow=True, note="robots.txt not present (404)")
        if result.status in {401, 403}:
            # The site refuses to show its own rules. We cannot know what is
            # permitted, so we stay out and record it as a block with evidence.
            return RobotsFile(host=host, status=result.status, fetched=False,
                              default_allow=False,
                              note=f"robots.txt returned HTTP {result.status}; "
                                   "access to rules refused, assuming disallowed")
        if result.status == 0:
            return RobotsFile(host=host, status=0, fetched=False, default_allow=False,
                              note=f"robots.txt unreachable ({result.error[:120]}); "
                                   "assuming disallowed")
        if 500 <= result.status < 600:
            return RobotsFile(host=host, status=result.status, fetched=False,
                              default_allow=False,
                              note=f"robots.txt server error {result.status}; "
                                   "assuming disallowed")
        # 3xx that did not resolve, 2xx with an empty body, or anything exotic.
        return RobotsFile(host=host, status=result.status, fetched=False, default_allow=True,
                          note=f"robots.txt returned HTTP {result.status} with no usable "
                               "rules; treating as absent")

    async def can_fetch(self, url: str, *, payer: str = "") -> RobotsDecision:
        """
        Convenience check used on every candidate URL before it is requested.

        This is the single choke point that enforces "never fetch a Disallowed
        path": :mod:`crawler.discovery` routes every URL through it, so there is
        exactly one place to audit for compliance.
        """
        robots = await self.get(url, payer=payer)
        return robots.can_fetch(self.user_agent, url)

    async def sitemaps_for(self, url_or_host: str, *, payer: str = "") -> list[str]:
        """Declared sitemap URLs for the host, de-duplicated, order preserved."""
        robots = await self.get(url_or_host, payer=payer)
        seen: list[str] = []
        for entry in robots.sitemaps:
            absolute = urljoin(f"https://{robots.host}/", entry.strip())
            if absolute not in seen:
                seen.append(absolute)
        return seen

    def known_hosts(self) -> Iterable[str]:
        """Hosts whose robots.txt has been fetched (used by the run summary)."""
        return tuple(self._cache)
