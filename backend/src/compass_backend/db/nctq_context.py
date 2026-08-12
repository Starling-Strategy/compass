"""Read-only repository fetching NCTQ context snippets for the answer layer.

Returns curated rationales and exemplars first, then chatbot-ready
publications, ordered by topic match strength and recency. Cap at the
caller-supplied limit (default 2). Pure read-only: never writes.

Schema notes (verified against staging compass.* on 2026-05-28):

- ``nctq_rationales`` keys topics in ``topic`` (Title Case, e.g.
  "General Salary"); has ``position`` (stance) and ``rationale_text``
  (supporting prose). No ``title``/``body``/``rationale_id`` columns.
  ``source_url`` is frequently NULL — we skip rows without a URL so the
  ``NctqSnippet.url`` non-empty contract holds.
- ``nctq_exemplar_policies`` keys topics in ``topic`` (Title Case); has
  ``district_name`` and ``description``. Uses ``id`` for primary key.
- ``nctq_publications`` matches on ``tags`` / ``ai_tags`` (Title Case
  arrays). Only ``for_chatbot=true`` and ``url IS NOT NULL`` are eligible.

Topic keys arrive kebab-case (e.g. ``general-salary``). We expand each
to its Title Case display form before querying so the repo stays
agnostic to where the key originated.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from compass_backend.answer_layer.nctq_context import NctqPublicationHit, NctqSnippet
from compass_backend.config import Settings
from compass_backend.db._base import RepositoryBase
from compass_backend.db._pool import ChatPoolHolder
from compass_backend.db._turn_connection import acquire_turn_or_pool


class NctqContextRepository(RepositoryBase):
    """Fetches sealed NCTQ context snippets for the answer layer.

    Keeps its own ``_acquire`` override because the pool is stored under
    ``_pool_or_holder`` (a test pins that attribute); ``_table`` comes from
    ``RepositoryBase``.
    """

    def __init__(
        self,
        source: Settings,
        *,
        pool: asyncpg.Pool | ChatPoolHolder,
    ) -> None:
        self._settings = source
        self._pool_or_holder = pool

    async def fetch_snippets(
        self,
        topic_keys: tuple[str, ...],
        *,
        limit: int = 2,
    ) -> tuple[NctqSnippet, ...]:
        """Return up to ``limit`` snippets for the given topic keys.

        Selection order, then cap at ``limit``:
          1. ``nctq_rationales`` matching any topic key (skips rows
             without ``source_url`` because ``NctqSnippet.url`` must be
             non-empty).
          2. ``nctq_exemplar_policies`` matching any topic key.
          3. ``nctq_publications`` (``for_chatbot=true``) tagged with
             any topic key, ordered by ``published_date`` DESC.
        """

        if not topic_keys or limit <= 0:
            return ()

        topic_candidates = _expand_topic_candidates(topic_keys)
        if not topic_candidates:
            return ()

        async with self._acquire() as conn:
            rationale_rows = await conn.fetch(
                f"""
                SELECT
                    r.id::text AS id,
                    r.topic,
                    r.subtopic,
                    r.position,
                    r.rationale_text,
                    r.source_title,
                    r.source_url
                FROM {self._table("nctq_rationales")} r
                WHERE r.active = true
                  AND r.topic = ANY($1::text[])
                  AND r.source_url IS NOT NULL
                  AND r.source_url <> ''
                ORDER BY r.sort_order NULLS LAST, r.id
                LIMIT $2
                """,
                topic_candidates,
                limit,
            )
            exemplar_rows = await conn.fetch(
                f"""
                SELECT
                    e.id::text AS id,
                    e.topic,
                    e.subtopic,
                    e.district_name,
                    e.description,
                    e.source_url
                FROM {self._table("nctq_exemplar_policies")} e
                WHERE e.active = true
                  AND e.topic = ANY($1::text[])
                  AND e.source_url IS NOT NULL
                  AND e.source_url <> ''
                ORDER BY e.sort_order NULLS LAST, e.id
                LIMIT $2
                """,
                topic_candidates,
                limit,
            )
            publication_rows = await conn.fetch(
                f"""
                SELECT
                    p.publication_id,
                    p.title,
                    coalesce(p.summary, '') AS summary,
                    p.url,
                    p.key_points,
                    p.published_date
                FROM {self._table("nctq_publications")} p
                WHERE p.for_chatbot = true
                  AND p.url IS NOT NULL
                  AND p.url <> ''
                  AND (
                      p.ai_tags && $1::text[]
                      OR p.tags && $1::text[]
                  )
                ORDER BY p.published_date DESC NULLS LAST
                LIMIT $2
                """,
                topic_candidates,
                limit,
            )

        snippets: list[NctqSnippet] = []
        for row in rationale_rows:
            title = _rationale_title(row)
            summary = _first_sentence(row["rationale_text"]) or row["position"] or ""
            if not title or not summary:
                continue
            snippets.append(
                NctqSnippet(
                    source_kind="rationale",
                    title=title,
                    url=row["source_url"],
                    summary_line=summary,
                )
            )
        for row in exemplar_rows:
            title = _exemplar_title(row)
            summary = _first_sentence(row["description"])
            if not title or not summary:
                continue
            snippets.append(
                NctqSnippet(
                    source_kind="exemplar",
                    title=title,
                    url=row["source_url"],
                    summary_line=summary,
                )
            )
        for row in publication_rows:
            key_points = row["key_points"] or []
            summary = _first_sentence(row["summary"])
            if not row["title"] or not summary:
                continue
            snippets.append(
                NctqSnippet(
                    source_kind="publication",
                    title=row["title"],
                    url=row["url"],
                    summary_line=summary,
                    key_point=key_points[0] if key_points else None,
                )
            )

        return tuple(snippets[:limit])

    async def search_publications(
        self,
        query: str,
        *,
        limit: int = 3,
    ) -> tuple[NctqPublicationHit, ...]:
        """Return up to ``limit`` chatbot-ready NCTQ publications for ``query``.

        ``query`` is the planner-authored topic phrase (``PublicationPlan.
        publication_query``) — a TYPED field, never the raw user prose. The
        ``publication`` route uses this to cite NCTQ's published writing about
        a topic the curated 8-topic policy-guidance library doesn't cover.

        Matching (read-only):
          - only ``for_chatbot = true`` rows with a non-empty ``url`` are
            eligible;
          - the phrase matches against ``title`` / ``summary`` via ``ILIKE``,
            applying BOTH the hyphen and space variants (so "four day school
            week" finds a "four-day-school-week" title and vice versa);
          - it also matches when the phrase's tag-key forms intersect the
            ``tags`` / ``ai_tags`` arrays.

        Ordering: title hits before summary hits before tag-only hits, then
        most recent first. Titles, URLs, and summaries are returned VERBATIM.
        """

        cleaned = (query or "").strip()
        if not cleaned or limit <= 0:
            return ()

        # Corpus hyphenation is inconsistent ("four-day school week" mixes a
        # hyphen with spaces), so the all-space / all-hyphen variants both miss
        # it. Normalise hyphens -> spaces (and lowercase) on BOTH the query and
        # the matched columns, then compare on that space-normalised form.
        norm_pat = f"%{cleaned.lower().replace('-', ' ')}%"
        tag_keys = _publication_tag_keys(cleaned)

        async with self._acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT
                    p.publication_id::text AS publication_id,
                    p.title,
                    p.url,
                    coalesce(p.summary, '') AS summary
                FROM {self._table("nctq_publications")} p
                WHERE p.for_chatbot = true
                  AND p.url IS NOT NULL
                  AND p.url <> ''
                  AND (
                      replace(lower(p.title), '-', ' ') LIKE $1
                      OR replace(lower(coalesce(p.summary, '')), '-', ' ') LIKE $1
                      OR p.tags && $2::text[]
                      OR p.ai_tags && $2::text[]
                  )
                ORDER BY
                    CASE
                        WHEN replace(lower(p.title), '-', ' ') LIKE $1 THEN 0
                        WHEN replace(lower(coalesce(p.summary, '')), '-', ' ') LIKE $1 THEN 1
                        ELSE 2
                    END,
                    p.published_date DESC NULLS LAST
                LIMIT $3
                """,
                norm_pat,
                tag_keys,
                limit,
            )

        hits: list[NctqPublicationHit] = []
        for row in rows:
            if not row["title"] or not row["url"]:
                continue
            hits.append(
                NctqPublicationHit(
                    publication_id=row["publication_id"],
                    title=row["title"],
                    url=row["url"],
                    summary=row["summary"] or "",
                )
            )
        return tuple(hits)

    @asynccontextmanager
    async def _acquire(self) -> AsyncIterator[asyncpg.Connection]:
        """Yield a connection: the per-turn connection if free, else pool."""

        async with acquire_turn_or_pool(self._pool_or_holder) as conn:
            yield conn


def _expand_topic_candidates(topic_keys: tuple[str, ...]) -> list[str]:
    """Expand kebab-case topic keys to the candidate strings stored in
    ``compass.nctq_*`` tables.

    For each key we emit:
      - the original key (so callers passing already-canonical strings
        continue to work),
      - the Title Case form (``general-salary`` -> ``General Salary``),
        which is how ``nctq_rationales`` / ``nctq_exemplar_policies``
        and most publication tags store the topic.
    """

    candidates: list[str] = []
    seen: set[str] = set()
    for key in topic_keys:
        cleaned = (key or "").strip()
        if not cleaned:
            continue
        forms = {cleaned, _kebab_to_title_case(cleaned)}
        for form in forms:
            if form and form not in seen:
                seen.add(form)
                candidates.append(form)
    return candidates


def _publication_tag_keys(query: str) -> list[str]:
    """Candidate ``tags`` / ``ai_tags`` array values for a publication phrase.

    ``nctq_publications`` stores tags as Title Case arrays (e.g.
    ``"Four-Day School Week"``). We emit the phrase verbatim plus its Title
    Case forms in both hyphen and space variants so an array-overlap (`&&`)
    match works regardless of which separator the stored tag uses. Pure string
    work — no prose dispatch (it reads only the typed ``query``).
    """

    cleaned = (query or "").strip()
    if not cleaned:
        return []
    space_form = cleaned.replace("-", " ")
    hyphen_form = cleaned.replace(" ", "-")
    candidates = {
        cleaned,
        space_form,
        hyphen_form,
        _kebab_to_title_case(space_form),
        _kebab_to_title_case(hyphen_form),
    }
    return [value for value in candidates if value]


def _kebab_to_title_case(value: str) -> str:
    parts = [part for part in value.replace("_", "-").split("-") if part]
    return " ".join(part[:1].upper() + part[1:].lower() for part in parts)


def _rationale_title(row: asyncpg.Record) -> str:
    """Compose a presentable title for a rationale row.

    Falls back through ``source_title``, ``topic + subtopic``, then
    ``topic`` alone.
    """

    source_title = (row["source_title"] or "").strip()
    topic = (row["topic"] or "").strip()
    subtopic = (row["subtopic"] or "").strip()
    if source_title and subtopic:
        return f"{source_title}: {subtopic}"
    if source_title:
        return source_title
    if topic and subtopic:
        return f"{topic}: {subtopic}"
    return topic


def _exemplar_title(row: asyncpg.Record) -> str:
    district = (row["district_name"] or "").strip()
    subtopic = (row["subtopic"] or "").strip()
    topic = (row["topic"] or "").strip()
    if district and subtopic:
        return f"{district} — {subtopic}"
    if district and topic:
        return f"{district} — {topic}"
    if district:
        return district
    return topic or subtopic


def _first_sentence(text: str | None) -> str:
    """Return the first sentence-like chunk of `text` for snippet summary.

    Stops at the earliest of:
      - first ``". "`` (period + space) — the common sentence break
      - first newline (paragraph or line boundary)

    Heuristic, not a parser: it can truncate on abbreviation+space
    sequences like ``"Mr. "`` and ``"e.g., "``. Curated NCTQ rationale
    and exemplar bodies are short enough that the truncation is usually
    harmless; if a row needs a longer summary line, push the canonical
    one-line summary into a dedicated column.
    """

    if not text:
        return ""
    cleaned = text.strip()
    if not cleaned:
        return ""
    period = cleaned.find(". ")
    newline = cleaned.find("\n")
    if period == -1 and newline == -1:
        return cleaned
    if period == -1:
        return cleaned[:newline].strip()
    if newline == -1:
        return cleaned[: period + 1].strip()
    return cleaned[: min(period + 1, newline)].strip()


__all__ = ["NctqContextRepository"]
