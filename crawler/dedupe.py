"""
Near-duplicate collapsing.

The schema requires ``document_url`` to be unique across the whole output file,
and the brief additionally requires near-duplicates - the same document at two
URLs, or dated revisions of one document - to be collapsed under a *documented*
rule. This module is that rule, implemented in one place so NOTES.md can
describe it accurately.

THE RULE, applied in this order (earlier tiers are more certain):

  Tier 0 - **Exact URL** (after canonicalisation): the same normalised URL is the
           same row. Guarantees the uniqueness constraint absolutely.

  Tier 1 - **Identical bytes, same payer**: equal ``content_hash_sha256``
           (200 rows only) means literally the same file served at two URLs.
           Scoped to one payer on purpose: two different payers publishing the
           same vendor document (MCG/InterQual criteria, a CMS form) are two
           genuine findings, not a duplicate.

  Tier 2 - **Same document identity**: same payer + same ``policy_number`` +
           same ``document_type``. A policy number is a payer-assigned unique
           identifier, so two documents sharing one are revisions of the same
           policy.

  Tier 3 - **Same normalised title**: same payer + same ``document_type`` +
           same title reduced to a comparison key (lowercased, version/date/
           revision noise stripped, punctuation dropped) + same ``file_type``.
           This is the tier that catches ``policy-cs123-2025.pdf`` versus
           ``policy-cs123-2026.pdf``.

WINNER SELECTION within a cluster, in order:
  1. the most recent ``effective_date``, then the most recent
     ``last_updated_date`` (the current revision is what a user wants);
  2. higher ``confidence_score`` (better-evidenced row);
  3. HTTP 200 over any non-200 (a working URL beats a broken one);
  4. ``pdf`` over ``html`` (the canonical artefact rather than a viewer page);
  5. the shorter, shallower URL (the stable/canonical location);
  6. lexicographic URL order, purely so the outcome is deterministic.

The loser is not discarded silently: the winner's ``notes`` records how many
duplicates it absorbed and the URL of the closest one, so the collapse is
visible and reversible from the output plus the log.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .schema import DocumentRow, clean_text

#: Version / revision / date noise stripped from a title before comparison.
#: Order matters: the longer, more specific patterns run first.
_TITLE_NOISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Trailing or embedded dates in any common written form.
    re.compile(r"\b(?:january|february|march|april|may|june|july|august|september"
               r"|october|november|december)\s+\d{1,2}?,?\s*(?:19|20)\d{2}\b", re.IGNORECASE),
    re.compile(r"\b\d{1,2}[/\-.]\d{1,2}[/\-.](?:19|20)?\d{2}\b"),
    re.compile(r"\b(?:19|20)\d{2}[-/]\d{1,2}(?:[-/]\d{1,2})?\b"),
    # Effective/revised/updated labels together with whatever follows them.
    re.compile(r"\b(?:effective|revised|reviewed|updated|last\s+updated|version|ver\.?"
               r"|rev\.?|revision)\b\s*[:#-]?\s*[\w./-]*", re.IGNORECASE),
    # Quarter / bare year markers.
    re.compile(r"\bq[1-4]\s*(?:19|20)\d{2}\b", re.IGNORECASE),
    re.compile(r"\b(?:19|20)\d{2}\b"),
    # File-format and copy noise payers append to titles.
    re.compile(r"\b(?:pdf|final|draft|copy|clean|redline|track\s*changes)\b", re.IGNORECASE),
)

#: Words that carry no distinguishing power in a policy title.
_TITLE_STOPWORDS = frozenset(
    {"the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "policy",
     "policies", "guideline", "guidelines", "medical", "clinical", "coverage"}
)


def title_key(title: str) -> str:
    """
    Reduce a document title to a comparison key.

    Lowercases, strips version/date/revision noise, drops punctuation and
    stopwords, and sorts nothing - word order is meaningful. Returns ``""`` for a
    title with no distinguishing content left, which makes tier 3 skip it
    (an empty key must never merge unrelated documents).

    ``"Medical Policy: Bariatric Surgery (Effective 03/01/2026)"`` and
    ``"Bariatric Surgery Medical Policy - Revised 2025"`` both reduce to
    ``"bariatric surgery"``.
    """
    text = clean_text(title).lower()
    if not text:
        return ""
    for pattern in _TITLE_NOISE_PATTERNS:
        text = pattern.sub(" ", text)
    text = re.sub(r"[^a-z0-9\s]+", " ", text)
    words = [word for word in text.split() if word and word not in _TITLE_STOPWORDS]
    # Two words is the minimum that can meaningfully identify a document; below
    # that the key is too generic to merge on.
    if len(words) < 2:
        return ""
    return " ".join(words)


def _url_sort_key(row: DocumentRow) -> tuple[int, int, str]:
    """Prefer shallower, shorter, then lexicographically-first URLs."""
    url = str(row.document_url)
    return url.count("/"), len(url), url


def _recency_key(row: DocumentRow) -> tuple[str, str]:
    """
    Sortable recency for a row: effective date first, then last-updated.

    ISO-8601 dates sort correctly as strings, and an empty string sorts before
    any real date - which is what we want, since a row with no printed date
    should lose to one that has one.
    """
    return str(row.effective_date or ""), str(row.last_updated_date or "")


def _file_type_rank(row: DocumentRow) -> int:
    """Prefer the canonical artefact: PDF, then Office formats, then HTML."""
    return {"pdf": 0, "docx": 1, "doc": 1, "xlsx": 2, "xls": 2, "html": 3}.get(
        str(row.file_type), 4
    )


def choose_winner(cluster: Sequence[DocumentRow]) -> DocumentRow:
    """
    Pick the surviving row from a duplicate cluster, per the documented order.

    Implemented as a single sort key so the outcome is total and deterministic:
    the same input always yields the same winner, which matters for a dataset
    that a reviewer may diff between runs.
    """

    def key(row: DocumentRow) -> tuple:
        effective, updated = _recency_key(row)
        return (
            # Negated recency: sort ascending, so "larger date" must sort first.
            _negate_date(effective),
            _negate_date(updated),
            -float(row.confidence_score or 0),
            0 if str(row.http_status) == "200" else 1,
            _file_type_rank(row),
            *_url_sort_key(row),
        )

    return sorted(cluster, key=key)[0]


def _negate_date(value: str) -> str:
    """
    Invert an ISO date so that ascending string sort puts the newest first.

    Digits are complemented (``9 - d``) which reverses the ordering, and an empty
    date maps to ``"~"`` - greater than any digit - so undated rows sort last.
    """
    if not value:
        return "~"
    return "".join(str(9 - int(char)) if char.isdigit() else char for char in value)


@dataclass(slots=True)
class DedupeReport:
    """Per-run de-duplication accounting, logged and summarised in NOTES terms."""

    kept: int = 0
    collapsed: int = 0
    #: ``collapsed`` broken down by which tier caught it, for the log record.
    by_tier: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    #: ``payer_name -> number collapsed``, so payer.done can report its own count.
    by_payer: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "kept": self.kept,
            "collapsed": self.collapsed,
            "by_tier": dict(self.by_tier),
        }


def _cluster_keys(row: DocumentRow) -> list[tuple[str, tuple]]:
    """
    Build the merge keys a row participates in, strongest tier first.

    A row can carry several keys; :func:`dedupe_rows` unions rows that share
    *any* key, so a chain (A shares a hash with B, B shares a title with C)
    collapses to a single cluster.
    """
    keys: list[tuple[str, tuple]] = []
    payer = str(row.payer_name).lower()
    digest = str(row.content_hash_sha256)
    if digest and str(row.http_status) == "200":
        # Scoped to the payer, like the tiers below. Two DIFFERENT payers serving
        # byte-identical bytes is common in this domain - shared vendor criteria
        # (MCG, InterQual), CMS forms, jointly published drug lists - and each
        # payer publishing that document is a distinct, wanted finding. Keying on
        # the digest alone would silently delete one payer's contribution.
        keys.append(("content_hash", (payer, digest)))
    policy_number = str(row.policy_number).strip().upper()
    if policy_number:
        keys.append(("policy_number", (payer, policy_number, str(row.document_type))))
    key = title_key(str(row.document_title))
    if key:
        keys.append(("title", (payer, str(row.document_type), key, str(row.file_type))))
    return keys


class _UnionFind:
    """
    Minimal union-find used to merge duplicate clusters transitively.

    Needed because duplicate relationships chain: if A and B are byte-identical
    and B and C share a normalised title, all three are one document and must
    collapse to one row - which a simple dict-of-groups would miss.
    """

    def __init__(self) -> None:
        self._parent: dict[int, int] = {}

    def find(self, item: int) -> int:
        parent = self._parent.setdefault(item, item)
        while parent != item:
            item, parent = parent, self._parent.setdefault(parent, parent)
        return item

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self._parent[right_root] = left_root


def dedupe_rows(
    rows: Iterable[DocumentRow], log: Any = None
) -> tuple[list[DocumentRow], DedupeReport]:
    """
    Collapse near-duplicates and guarantee ``document_url`` uniqueness.

    Returns ``(kept_rows, report)``. Input order is preserved among survivors, so
    the output stays grouped by payer in seed order rather than being reshuffled
    by the de-duplication itself.
    """
    report = DedupeReport()
    rows = list(rows)
    if not rows:
        return [], report

    # ---- Tier 0: exact (canonicalised) URL --------------------------------
    # Done first and separately because it is the one constraint the schema
    # states outright, and because it shrinks the work for the later tiers.
    from .discovery import normalise_url  # local import avoids a cycle

    by_url: dict[str, list[DocumentRow]] = {}
    order: list[str] = []
    for row in rows:
        key = normalise_url(str(row.document_url))
        if key not in by_url:
            by_url[key] = []
            order.append(key)
        by_url[key].append(row)

    url_unique: list[DocumentRow] = []
    for key in order:
        cluster = by_url[key]
        if len(cluster) == 1:
            url_unique.append(cluster[0])
            continue
        winner = choose_winner(cluster)
        report.collapsed += len(cluster) - 1
        report.by_tier["exact_url"] += len(cluster) - 1
        report.by_payer[str(winner.payer_name)] += len(cluster) - 1
        _annotate_winner(winner, cluster, "exact_url")
        url_unique.append(winner)

    # ---- Tiers 1-3: content hash, policy number, normalised title ---------
    union = _UnionFind()
    key_owner: dict[tuple[str, tuple], int] = {}
    tier_for_index: dict[int, str] = {}
    for index, row in enumerate(url_unique):
        union.find(index)
        for tier, key in _cluster_keys(row):
            owner = key_owner.get((tier, key))
            if owner is None:
                key_owner[(tier, key)] = index
            else:
                union.union(owner, index)
                # Record the strongest tier that caused this merge.
                strength = {"content_hash": 0, "policy_number": 1, "title": 2}
                existing = tier_for_index.get(union.find(index))
                if existing is None or strength[tier] < strength[existing]:
                    tier_for_index[union.find(index)] = tier

    clusters: dict[int, list[int]] = defaultdict(list)
    for index in range(len(url_unique)):
        clusters[union.find(index)].append(index)

    kept: list[DocumentRow] = []
    survivors: set[int] = set()
    for root, members in clusters.items():
        cluster_rows = [url_unique[index] for index in members]
        if len(cluster_rows) == 1:
            survivors.add(members[0])
            continue
        winner = choose_winner(cluster_rows)
        winner_index = members[cluster_rows.index(winner)]
        survivors.add(winner_index)
        tier = tier_for_index.get(root, "title")
        report.collapsed += len(cluster_rows) - 1
        report.by_tier[tier] += len(cluster_rows) - 1
        report.by_payer[str(winner.payer_name)] += len(cluster_rows) - 1
        _annotate_winner(winner, cluster_rows, tier)
        if log is not None:
            log.info(
                "dedupe.collapsed",
                f"collapsed {len(cluster_rows) - 1} near-duplicate(s) into "
                f"{winner.document_url} (rule: {tier})",
                payer=str(winner.payer_name),
                rule=tier,
                kept_url=str(winner.document_url),
                dropped_urls=[str(row.document_url) for row in cluster_rows
                              if row is not winner][:5],
            )

    # Preserve the original ordering of the survivors.
    for index, row in enumerate(url_unique):
        if index in survivors:
            kept.append(row)

    report.kept = len(kept)
    return kept, report


def _annotate_winner(
    winner: DocumentRow, cluster: Sequence[DocumentRow], tier: str
) -> None:
    """
    Record the collapse in the winner's ``notes``.

    Keeping the count and one example URL means a reviewer reading output.csv can
    see that a merge happened and go verify it, instead of having to trust that
    the missing URL was really a duplicate.
    """
    dropped = [row for row in cluster if row is not winner]
    if not dropped:
        return
    example = str(dropped[0].document_url)
    detail = (
        f"collapsed {len(dropped)} near-duplicate URL(s) under the "
        f"'{tier}' rule (e.g. {example})"
    )
    winner.notes = clean_text(f"{winner.notes}; {detail}" if winner.notes else detail)[:1000]


def rule_description() -> str:
    """
    The de-duplication rule as prose, for NOTES.md and ``--explain-dedupe``.

    Kept next to the implementation so the documentation cannot drift away from
    the behaviour.
    """
    return (
        "Near-duplicate collapsing rule (applied in order, strongest first):\n"
        "  0. exact canonicalised URL (scheme/host lowercased, fragment and "
        "tracking parameters dropped, index.html and trailing slash removed, "
        "query parameters sorted);\n"
        "  1. identical content_hash_sha256 among HTTP 200 rows OF THE SAME PAYER "
        "(a byte-identical file served at two URLs). Scoped per payer because two "
        "payers publishing the same vendor document (MCG/InterQual criteria, a CMS "
        "form) are two genuine findings, not a duplicate;\n"
        "  2. same payer + policy_number + document_type (a payer-assigned "
        "policy number identifies one policy across its revisions);\n"
        "  3. same payer + document_type + file_type + normalised title, where "
        "normalisation lowercases the title and strips dates, version/revision "
        "markers, punctuation and stopwords.\n"
        "Clusters are merged transitively (union-find), so a chain of relations "
        "collapses to one row. The survivor is chosen by: newest effective_date, "
        "then newest last_updated_date, then higher confidence_score, then HTTP "
        "200 over non-200, then pdf over html, then the shorter/shallower URL, "
        "then lexicographic URL order for determinism. The survivor's notes "
        "column records how many duplicates it absorbed and an example URL."
    )
