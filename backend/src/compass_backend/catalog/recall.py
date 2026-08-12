"""Advisory catalog recall for official Compass candidate discovery.

This module deliberately does not grant execution authority. It gathers model-
safe candidate cards from governed catalog surfaces so planner/recognition and
future recall@k diagnostics can see what official things might match a phrase.
`CatalogResolver` remains the only layer that may approve IDs for execution.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from compass_backend.catalog.resolution import (
    CatalogAliasRecord,
    CatalogEntityType,
    CatalogRepository,
    DistrictCandidate,
    GlossaryTermCandidate,
    MetricCandidate,
    NCESFieldCandidate,
    SourceDocumentCandidate,
    TopicCandidate,
)
from compass_backend.reference import split_state_suffix


RecallEntityType = Literal[
    "metric",
    "metric_bundle",
    "district",
    "profile_field",
    "topic",
    "glossary_term",
    "source_document",
    "unsupported_concept",
]

RecallSourceMethod = Literal[
    "alias",
    "metric_search",
    "district_search",
    "profile_field_search",
    "topic_search",
    "glossary_search",
    "source_document_search",
    "unsupported_concept",
]
RecallBatchSource = Literal["direct", "ngram", "catalog_alias_overlap"]

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?")
_MAX_FANOUT_PHRASES = 14
_FANOUT_BATCH_LIMIT = 8
_PROMPT_EXPANSION_MIN_TOKENS = 3
# Largest n-gram window enumerated from the prompt. Higher catches longer exact
# phrases but multiplies candidate queries; 6 covers realistic metric names.
_MAX_NGRAM_SIZE = 6
# Fraction of an alias's roots that must appear in the query to count it as an
# overlap match. Lower = looser recall (more false matches); higher = stricter.
_ALIAS_OVERLAP_THRESHOLD = 0.67
_STOPWORDS = {
    "a",
    "about",
    "allow",
    "am",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "both",
    "by",
    "can",
    "compare",
    "cover",
    "district",
    "districts",
    "do",
    "does",
    "for",
    "from",
    "have",
    "highest",
    "how",
    "i",
    "in",
    "is",
    "many",
    "me",
    "more",
    "most",
    "of",
    "or",
    "provide",
    "require",
    "show",
    "similar",
    "state",
    "states",
    "ten",
    "than",
    "that",
    "the",
    "their",
    "those",
    "to",
    "what",
    "which",
    "with",
    "year",
}

_DEFAULT_ENTITY_TYPES: tuple[RecallEntityType, ...] = (
    "metric",
    "metric_bundle",
    "district",
    "profile_field",
    "topic",
    "glossary_term",
    "source_document",
    "unsupported_concept",
)

_ALIAS_ENTITY_TYPES: dict[RecallEntityType, CatalogEntityType] = {
    "metric": "metric",
    "metric_bundle": "metric_bundle",
    "district": "district",
    "profile_field": "nces_field",
    "topic": "topic",
    "glossary_term": "glossary_term",
    "unsupported_concept": "unsupported_concept",
}


class CandidateCard(BaseModel):
    """Model-safe summary of one official candidate.

    `debug_ref` and `metadata` are internal diagnostics. They may contain catalog
    row references, but they are not execution authority and are excluded from
    `to_model_context()`.
    """

    model_config = ConfigDict(extra="forbid")

    input_phrase: str = Field(min_length=1)
    entity_type: RecallEntityType
    label: str = Field(min_length=1)
    plain_definition: str = ""
    source_methods: list[RecallSourceMethod] = Field(default_factory=list)
    entity_ref: str | None = None
    debug_ref: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)

    def to_model_context(self) -> dict[str, object]:
        """Return the safe card shape for future model-facing Toolset output."""

        return self.model_dump(exclude={"entity_ref", "debug_ref", "metadata"})


class RecallBatch(BaseModel):
    """One advisory recall run for a phrase and entity-type set."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    source: RecallBatchSource = "direct"
    entity_types: list[RecallEntityType] = Field(default_factory=list)
    candidates: list[CandidateCard] = Field(default_factory=list)
    limit: int = Field(default=10, ge=1)

    def top(self, k: int) -> list[CandidateCard]:
        """Return the first k candidates without mutating the batch."""

        return self.candidates[: max(k, 0)]


class RecallReport(BaseModel):
    """Advisory recall evidence collected before final catalog resolution."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    batches: list[RecallBatch] = Field(default_factory=list)
    ranked_candidates: list[CandidateCard] | None = None

    @property
    def candidates(self) -> list[CandidateCard]:
        """Flatten candidates across all batches in report order."""

        if self.ranked_candidates is not None:
            return self.ranked_candidates
        return [candidate for batch in self.batches for candidate in batch.candidates]


class CatalogRecallService:
    """Gather official catalog candidates without approving execution IDs."""

    def __init__(
        self,
        repository: CatalogRepository,
    ) -> None:
        self._repository = repository

    async def recall(
        self,
        query: str,
        *,
        entity_types: Iterable[RecallEntityType] | None = None,
        limit: int = 10,
        states: set[str] | None = None,
        expand_prompt: bool = True,
    ) -> RecallReport:
        """Return advisory official candidates for a user/planner phrase."""

        requested = tuple(entity_types or _DEFAULT_ENTITY_TYPES)
        district_query, states = await self._district_state_scope(
            query,
            requested=requested,
            states=states,
            limit=limit,
        )
        direct_batch = await self._recall_batch(
            query,
            requested=requested,
            limit=limit,
            states=states,
            source="direct",
            origin_query=query,
            include_alias_cards=True,
            district_query=district_query,
        )
        batches = [direct_batch]
        if expand_prompt and _should_expand_prompt(query):
            alias_overlap_batch = await self._alias_overlap_batch(
                query,
                requested=requested,
                limit=min(limit, _FANOUT_BATCH_LIMIT),
            )
            if alias_overlap_batch.candidates:
                batches.append(alias_overlap_batch)
            seen_queries = {_normalized_query_value(query)}
            seen_queries.update(
                _normalized_query_value(batch.query)
                for batch in batches
            )
            for phrase in _prompt_ngram_queries(query):
                normalized_phrase = _normalized_query_value(phrase)
                if normalized_phrase in seen_queries:
                    continue
                seen_queries.add(normalized_phrase)
                batch = await self._recall_batch(
                    phrase,
                    requested=requested,
                    limit=min(limit, _FANOUT_BATCH_LIMIT),
                    states=states,
                    source="ngram",
                    origin_query=query,
                    include_alias_cards=False,
                )
                if batch.candidates:
                    batches.append(batch)
        return RecallReport(
            query=query,
            batches=batches,
            ranked_candidates=_rank_recall_candidates(query, batches, limit),
        )

    async def _district_state_scope(
        self,
        query: str,
        *,
        requested: tuple[RecallEntityType, ...],
        states: set[str] | None,
        limit: int,
    ) -> tuple[str | None, set[str] | None]:
        """Honor a typed trailing state qualifier in district recall (#1513).

        Compass renders districts as ``"Name, ST"`` (chip labels, clarify
        bullets), so that canonical handle must narrow recall to the named
        state instead of trigram-matching every same-name district
        ("Portland Public Schools, OR" must not resurface Portland ME). The
        suffix state UNIONs with any caller-supplied ``states`` filter and
        applies to every batch (direct and ngram fan-out); the remainder
        becomes the direct district search phrase. A zero-hit probe guards
        against false splits that match nothing ("Port Washington" → "Port" +
        WA): the raw phrase and caller states are kept so no candidates are
        lost. A false split whose remainder still matches a district under
        the wrong state filter is not detectable here — accepted residual,
        strictly stronger than the no-probe split in referent resolution.
        When the probe hits, the direct batch re-runs the same district
        lookup — one duplicate repository query per state-suffixed district
        phrase, accepted to keep batch assembly untouched.

        Returns ``(district_query, states)``; ``district_query`` is ``None``
        when no split applies (raw-phrase behavior, unchanged).
        """

        if "district" not in requested:
            return None, states
        remainder, suffix_states = split_state_suffix(query)
        if not suffix_states:
            return None, states
        merged = (states or set()) | suffix_states
        probe = await self._repository.search_district_candidates(
            remainder,
            states=merged,
            limit=limit,
        )
        if not probe:
            return None, states
        return remainder, merged

    async def _recall_batch(
        self,
        query: str,
        *,
        requested: tuple[RecallEntityType, ...],
        limit: int,
        source: RecallBatchSource,
        origin_query: str,
        include_alias_cards: bool,
        states: set[str] | None = None,
        district_query: str | None = None,
    ) -> RecallBatch:
        """Return one candidate batch for one phrase."""

        cards: list[CandidateCard] = []
        if include_alias_cards:
            await self._append_alias_cards(query, requested, cards)

        # The six per-entity-type lookups are independent reads, so fetch
        # them concurrently instead of awaiting one DB round trip after
        # another (#1080: this was the dominant per-batch latency).
        #
        # #1587: use asyncio.TaskGroup, not a bare asyncio.gather(). A bare
        # gather() with the default return_exceptions=False unwinds the parent
        # on the first child failure while the OTHER children keep running
        # detached -- still borrowing the per-turn DB connection
        # (db/_turn_connection.py). If the parent then leaves the
        # turn_connection scope and the pooled connection is recycled while a
        # detached sibling is mid-fetch, that is a use-after-release. TaskGroup
        # cancels the siblings on first failure and awaits their teardown
        # before propagating (as an ExceptionGroup), so no sibling outlives the
        # connection. Recall's callers catch bare ``except Exception`` (which
        # still catches ExceptionGroup) or don't catch at all, so the wrapped
        # type is safe here. We keep explicit, ordered task handles so the
        # concatenation order is identical to the previous version --
        # _dedupe_cards keeps the first card on a collision, so order matters.
        fetch_factories: list[Awaitable[list[CandidateCard]]] = []
        if "metric" in requested:
            fetch_factories.append(self._metric_cards(query, limit=limit))
        if "district" in requested:
            fetch_factories.append(
                self._district_cards(
                    district_query or query, states=states, limit=limit
                )
            )
        if "profile_field" in requested:
            fetch_factories.append(self._profile_field_cards(query, limit=limit))
        if "topic" in requested:
            fetch_factories.append(self._topic_cards(query, limit=limit))
        if "glossary_term" in requested:
            fetch_factories.append(self._glossary_cards(query, limit=limit))
        if "source_document" in requested:
            fetch_factories.append(
                self._source_document_cards(query, limit=limit)
            )
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(coro) for coro in fetch_factories]
        for task in tasks:
            cards.extend(task.result())
        return RecallBatch(
            query=query,
            source=source,
            entity_types=list(requested),
            candidates=[
                _decorate_recall_card(
                    card,
                    origin_query=origin_query,
                    # Ranking decision (#1513): when a state suffix was split
                    # off, district-search cards rank against the remainder —
                    # the string the repository actually matched — not the
                    # suffixed phrase. Alias and non-district cards keep the
                    # batch query.
                    matched_query=(
                        district_query
                        if district_query is not None
                        and "district_search" in card.source_methods
                        else query
                    ),
                    batch_source=source,
                )
                for card in _dedupe_cards(cards)[:limit]
            ],
            limit=limit,
        )

    async def _alias_overlap_batch(
        self,
        origin_query: str,
        *,
        requested: tuple[RecallEntityType, ...],
        limit: int,
    ) -> RecallBatch:
        """Return alias candidates whose governed alias text overlaps the prompt."""

        alias_types = {
            _ALIAS_ENTITY_TYPES[entity_type]
            for entity_type in requested
            if entity_type in _ALIAS_ENTITY_TYPES
        }
        if not alias_types:
            return RecallBatch(
                query=origin_query,
                source="catalog_alias_overlap",
                entity_types=list(requested),
                limit=limit,
            )
        fetcher = getattr(self._repository, "fetch_recall_aliases", None)
        if fetcher is None:
            return RecallBatch(
                query=origin_query,
                source="catalog_alias_overlap",
                entity_types=list(requested),
                limit=limit,
            )
        aliases = await fetcher(entity_types=alias_types)
        matched_aliases = [
            alias
            for alias in aliases
            if alias.active
            and alias.review_status == "approved"
            and alias.entity_type in alias_types
            and _alias_overlaps_query(alias.alias, origin_query)
        ]
        cards: list[CandidateCard] = []
        metric_aliases = [
            alias
            for alias in matched_aliases
            if alias.entity_type in {"metric", "metric_bundle"}
        ]
        for alias in matched_aliases:
            if alias.entity_type in {"metric", "metric_bundle"}:
                continue
            cards.append(_card_from_alias(alias.alias, alias))
        for alias in metric_aliases:
            cards.extend(
                await self._cards_from_metric_aliases(
                    alias.alias,
                    [alias],
                )
            )
        decorated = [
            _decorate_recall_card(
                card,
                origin_query=origin_query,
                matched_query=_matched_alias_query(card, origin_query),
                batch_source="catalog_alias_overlap",
            )
            for card in _dedupe_cards(cards)[:limit]
        ]
        return RecallBatch(
            query=origin_query,
            source="catalog_alias_overlap",
            entity_types=list(requested),
            candidates=decorated,
            limit=limit,
        )

    async def _append_alias_cards(
        self,
        query: str,
        requested: tuple[RecallEntityType, ...],
        cards: list[CandidateCard],
    ) -> None:
        alias_types = {
            _ALIAS_ENTITY_TYPES[entity_type]
            for entity_type in requested
            if entity_type in _ALIAS_ENTITY_TYPES
        }
        if not alias_types:
            return
        aliases = await self._repository.search_catalog_aliases(
            query,
            entity_types=alias_types,
        )
        active_aliases = [alias for alias in aliases if alias.active]
        metric_aliases = [
            alias
            for alias in active_aliases
            if alias.entity_type in {"metric", "metric_bundle"}
        ]
        cards.extend(await self._cards_from_metric_aliases(query, metric_aliases))
        cards.extend(
            _card_from_alias(query, alias)
            for alias in active_aliases
            if alias.entity_type not in {"metric", "metric_bundle"}
        )

    async def _cards_from_metric_aliases(
        self,
        query: str,
        aliases: list[CatalogAliasRecord],
    ) -> list[CandidateCard]:
        cards: list[CandidateCard] = []
        for alias in aliases:
            if alias.resolution_status == "ambiguous":
                candidates = _cards_from_metric_candidate_refs(query, alias)
                cards.extend(candidates or [_card_from_alias(query, alias)])
                continue
            if alias.entity_type == "metric_bundle":
                cards.append(_card_from_metric_bundle_alias(query, alias))
                continue
            metric_ids = _metric_ids_from_alias(alias)
            if not metric_ids:
                cards.append(_card_from_alias(query, alias))
                continue
            metrics = await self._repository.fetch_metrics_by_ids(metric_ids)
            cards.extend(
                _card_from_alias_metric(query, alias, metric)
                for metric in _ordered_metrics(metric_ids, metrics)
            )
            if not metrics:
                cards.append(_card_from_alias(query, alias))
        return cards

    async def _metric_cards(self, query: str, *, limit: int) -> list[CandidateCard]:
        candidates = await self._repository.search_metrics(query, limit=limit)
        return [_card_from_metric(query, candidate) for candidate in candidates]

    async def _district_cards(
        self,
        query: str,
        *,
        states: set[str] | None,
        limit: int,
    ) -> list[CandidateCard]:
        candidates = await self._repository.search_district_candidates(
            query,
            states=states,
            limit=limit,
        )
        return [_card_from_district(query, candidate) for candidate in candidates]

    async def _profile_field_cards(
        self,
        query: str,
        *,
        limit: int,
    ) -> list[CandidateCard]:
        candidates = await self._repository.search_nces_fields(query, limit=limit)
        return [_card_from_profile_field(query, candidate) for candidate in candidates]

    async def _topic_cards(self, query: str, *, limit: int) -> list[CandidateCard]:
        candidates = await self._repository.search_topics(query, limit=limit)
        return [_card_from_topic(query, candidate) for candidate in candidates]

    async def _glossary_cards(self, query: str, *, limit: int) -> list[CandidateCard]:
        candidates = await self._repository.search_glossary_terms(query, limit=limit)
        return [_card_from_glossary(query, candidate) for candidate in candidates]

    async def _source_document_cards(
        self,
        query: str,
        *,
        limit: int,
    ) -> list[CandidateCard]:
        candidates = await self._repository.search_source_documents(query, limit=limit)
        return [_card_from_source_document(query, candidate) for candidate in candidates]


def _decorate_recall_card(
    card: CandidateCard,
    *,
    origin_query: str,
    matched_query: str,
    batch_source: RecallBatchSource,
) -> CandidateCard:
    metadata = {
        **card.metadata,
        "origin_query": origin_query,
        "matched_query": matched_query,
        "batch_source": batch_source,
        "recall_matches": [
            {
                "origin_query": origin_query,
                "matched_query": matched_query,
                "batch_source": batch_source,
            }
        ],
    }
    return card.model_copy(update={"metadata": metadata})


def _matched_alias_query(card: CandidateCard, origin_query: str) -> str:
    alias = card.metadata.get("alias")
    if isinstance(alias, str) and alias.strip():
        return alias.strip()
    if card.input_phrase and card.input_phrase != origin_query:
        return card.input_phrase
    return origin_query


def _rank_recall_candidates(
    origin_query: str,
    batches: list[RecallBatch],
    limit: int,
) -> list[CandidateCard]:
    indexed: list[tuple[int, CandidateCard]] = []
    for batch in batches:
        for card in batch.candidates:
            indexed.append((len(indexed), card))

    deduped: dict[tuple[RecallEntityType, str], tuple[int, CandidateCard]] = {}
    for index, card in indexed:
        key = _card_key(card)
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = (index, card)
            continue
        existing_index, existing_card = existing
        preferred = _preferred_recall_card(
            origin_query,
            existing_card,
            card,
            existing_index=existing_index,
            card_index=index,
        )
        merged = _merge_recall_cards(preferred, card if preferred is existing_card else existing_card)
        deduped[key] = (min(existing_index, index), merged)

    ranked = sorted(
        deduped.values(),
        key=lambda item: _recall_rank_key(origin_query, item[1], item[0]),
    )
    return [card for _, card in ranked[: max(limit, 0)]]


def _preferred_recall_card(
    origin_query: str,
    left: CandidateCard,
    right: CandidateCard,
    *,
    existing_index: int,
    card_index: int,
) -> CandidateCard:
    left_key = _recall_rank_key(origin_query, left, existing_index)
    right_key = _recall_rank_key(origin_query, right, card_index)
    return left if left_key <= right_key else right


def _merge_recall_cards(
    preferred: CandidateCard,
    other: CandidateCard,
) -> CandidateCard:
    methods = [*preferred.source_methods]
    for method in other.source_methods:
        if method not in methods:
            methods.append(method)
    metadata = {**other.metadata, **preferred.metadata}
    matches: list[dict[str, object]] = []
    for card in (preferred, other):
        for match in _recall_matches(card):
            if match not in matches:
                matches.append(match)
    if matches:
        metadata["recall_matches"] = matches
    return preferred.model_copy(
        update={
            "source_methods": methods,
            "metadata": metadata,
        }
    )


def _recall_matches(card: CandidateCard) -> list[dict[str, object]]:
    raw = card.metadata.get("recall_matches")
    if isinstance(raw, list):
        return [
            match
            for match in raw
            if isinstance(match, dict)
        ]
    matched_query = card.metadata.get("matched_query")
    batch_source = card.metadata.get("batch_source")
    origin_query = card.metadata.get("origin_query")
    if (
        isinstance(matched_query, str)
        and isinstance(batch_source, str)
        and isinstance(origin_query, str)
    ):
        return [
            {
                "origin_query": origin_query,
                "matched_query": matched_query,
                "batch_source": batch_source,
            }
        ]
    return []


def _recall_rank_key(
    origin_query: str,
    card: CandidateCard,
    index: int,
) -> tuple[int, int, int, int, str, str, int]:
    methods = set(card.source_methods)
    batch_source = card.metadata.get("batch_source")
    matched_query = card.metadata.get("matched_query")
    matched_phrase = matched_query if isinstance(matched_query, str) else card.input_phrase
    method_rank = 0 if methods & {"alias", "unsupported_concept"} else 1
    source_rank = {
        "catalog_alias_overlap": 0,
        "ngram": 1,
        "direct": 2,
    }.get(batch_source, 3)
    multi_source_rank = 0 if len(methods) > 1 or len(_recall_matches(card)) > 1 else 1
    phrase_len = len(_query_tokens(matched_phrase))
    if _normalized_query_value(matched_phrase) == _normalized_query_value(origin_query):
        phrase_len += _PROMPT_EXPANSION_MIN_TOKENS
    return (
        method_rank,
        multi_source_rank,
        source_rank,
        phrase_len,
        card.entity_type,
        card.entity_ref or card.label.casefold(),
        index,
    )


def _card_key(card: CandidateCard) -> tuple[RecallEntityType, str]:
    return (card.entity_type, card.entity_ref or card.label.casefold())


def _should_expand_prompt(query: str) -> bool:
    content_roots = _content_roots(query)
    if len(content_roots) >= _PROMPT_EXPANSION_MIN_TOKENS:
        return True
    return "?" in query or "," in query


def _prompt_ngram_queries(query: str) -> list[str]:
    tokens = _query_tokens(query)
    phrases: dict[str, tuple[list[str], int]] = {}
    order = 0
    max_size = min(_MAX_NGRAM_SIZE, len(tokens))
    for size in range(1, max_size + 1):
        for index in range(0, len(tokens) - size + 1):
            window = tokens[index : index + size]
            if not _useful_phrase(window):
                continue
            phrase = " ".join(window)
            if phrase not in phrases:
                phrases[phrase] = (window, order)
                order += 1
            trimmed = _trim_stopwords(window)
            if trimmed and trimmed != window:
                trimmed_phrase = " ".join(trimmed)
                if trimmed_phrase not in phrases and _useful_phrase(trimmed):
                    phrases[trimmed_phrase] = (trimmed, order)
                    order += 1
    ranked = sorted(
        phrases.items(),
        key=lambda item: _phrase_sort_key(item[1][0], item[1][1]),
    )
    return [phrase for phrase, _ in ranked[:_MAX_FANOUT_PHRASES]]


def _phrase_sort_key(tokens: list[str], order: int) -> tuple[int, int, int, int]:
    roots = [_token_root(token) for token in tokens]
    content_count = sum(1 for root in roots if root not in _STOPWORDS)
    single_penalty = 1 if len(tokens) == 1 else 0
    content_target_penalty = 0 if 2 <= content_count <= 4 else 1
    stopword_count = len(tokens) - content_count
    return (
        single_penalty,
        content_target_penalty,
        stopword_count,
        order,
    )


def _useful_phrase(tokens: list[str]) -> bool:
    if not tokens:
        return False
    roots = [_token_root(token) for token in tokens if _token_root(token) not in _STOPWORDS]
    if not roots:
        return False
    if len(tokens) == 1:
        token = tokens[0]
        return len(token) >= 3 and not token.isdigit()
    return not all(token.isdigit() for token in tokens)


def _trim_stopwords(tokens: list[str]) -> list[str]:
    start = 0
    end = len(tokens)
    while start < end and _token_root(tokens[start]) in _STOPWORDS:
        start += 1
    while end > start and _token_root(tokens[end - 1]) in _STOPWORDS:
        end -= 1
    return tokens[start:end]


def _alias_overlaps_query(alias: str, query: str) -> bool:
    alias_roots = _content_roots(alias)
    query_roots = set(_content_roots(query))
    if not alias_roots or not query_roots:
        return False
    matches = sum(1 for root in alias_roots if root in query_roots)
    if len(alias_roots) == 1:
        root = alias_roots[0]
        return len(root) >= 3 and matches == 1
    if matches == len(alias_roots):
        return True
    if len(alias_roots) >= 3 and matches >= max(
        2, round(len(alias_roots) * _ALIAS_OVERLAP_THRESHOLD)
    ):
        return True
    return False


def _content_roots(value: str) -> list[str]:
    roots: list[str] = []
    for token in _query_tokens(value):
        root = _token_root(token)
        if root in _STOPWORDS or not root:
            continue
        roots.append(root)
    return roots


def _query_tokens(value: str) -> list[str]:
    normalized = value.casefold()
    return [match.group(0).strip("'") for match in _TOKEN_RE.finditer(normalized)]


def _token_root(token: str) -> str:
    root = token.casefold().strip("'")
    if root.endswith("'s"):
        root = root[:-2]
    special_roots = {
        "children": "child",
        "ell": "ell",
        "frpl": "frpl",
        "master": "master",
        "master's": "master",
        "masters": "master",
        "premiums": "premium",
        "salaries": "salary",
        "salary": "salary",
        "stipends": "stipend",
        "strikes": "strike",
        "teachers": "teacher",
        "workdays": "workday",
    }
    if root in special_roots:
        return special_roots[root]
    if root.startswith("observ"):
        return "observ"
    if root.startswith("formal"):
        return "formal"
    if root.endswith("ies") and len(root) > 4:
        return root[:-3] + "y"
    if root.endswith("ally") and len(root) > 6:
        return root[:-4]
    for suffix in ("ing", "ed", "es", "s"):
        if root.endswith(suffix) and len(root) > len(suffix) + 3:
            return root[: -len(suffix)]
    return root


def _normalized_query_value(value: str) -> str:
    return " ".join(_query_tokens(value))

def _card_from_alias(query: str, alias: CatalogAliasRecord) -> CandidateCard:
    entity_type = _recall_entity_type_from_alias(alias.entity_type)
    return CandidateCard(
        input_phrase=query,
        entity_type=entity_type,
        label=alias.alias,
        plain_definition=_alias_definition(alias),
        source_methods=_alias_source_methods(alias),
        entity_ref=_entity_ref_from_alias(alias),
        debug_ref=f"alias:{alias.entity_type}:{alias.normalized_alias}",
        metadata={
            "resolution_status": alias.resolution_status,
            "review_status": alias.review_status,
            "source": alias.source,
            "provenance": alias.provenance,
            "scenario_ids": alias.scenario_ids,
        },
    )


def _card_from_metric(query: str, candidate: MetricCandidate) -> CandidateCard:
    return CandidateCard(
        input_phrase=query,
        entity_type="metric",
        label=candidate.name,
        plain_definition=candidate.topic or "",
        source_methods=["metric_search"],
        entity_ref=f"metric:{candidate.metric_id}",
        debug_ref=f"metric:{candidate.metric_id}",
        metadata={
            "answer_type": candidate.answer_type,
            "topic": candidate.topic,
        },
    )


def _card_from_alias_metric(
    query: str,
    alias: CatalogAliasRecord,
    candidate: MetricCandidate,
) -> CandidateCard:
    return CandidateCard(
        input_phrase=query,
        entity_type="metric",
        label=candidate.name,
        plain_definition=candidate.topic or _alias_definition(alias),
        source_methods=["alias"],
        entity_ref=f"metric:{candidate.metric_id}",
        debug_ref=f"alias_metric:{alias.normalized_alias}:{candidate.metric_id}",
        metadata={
            "answer_type": candidate.answer_type,
            "topic": candidate.topic,
            "alias": alias.alias,
            "resolution_status": alias.resolution_status,
            "review_status": alias.review_status,
            "source": alias.source,
            "provenance": alias.provenance,
            "scenario_ids": alias.scenario_ids,
        },
    )


def _card_from_metric_bundle_alias(
    query: str,
    alias: CatalogAliasRecord,
) -> CandidateCard:
    metric_ids = _metric_ids_from_alias(alias)
    metric_count = len(metric_ids)
    definition = (
        f"Reviewed metric bundle with {metric_count} official metrics."
        if metric_count
        else _alias_definition(alias)
    )
    return CandidateCard(
        input_phrase=query,
        entity_type="metric_bundle",
        label=alias.alias,
        plain_definition=definition,
        source_methods=["alias"],
        entity_ref=_metric_bundle_entity_ref(metric_ids, alias),
        debug_ref=f"alias:{alias.entity_type}:{alias.normalized_alias}",
        metadata={
            "metric_count": metric_count,
            "resolution_status": alias.resolution_status,
            "review_status": alias.review_status,
            "source": alias.source,
            "provenance": alias.provenance,
            "scenario_ids": alias.scenario_ids,
        },
    )


def _cards_from_metric_candidate_refs(
    query: str,
    alias: CatalogAliasRecord,
) -> list[CandidateCard]:
    entity_type = _recall_entity_type_from_alias(alias.entity_type)
    cards: list[CandidateCard] = []
    for raw in alias.candidate_refs:
        label = raw.get("metric_name") or raw.get("name") or raw.get("label")
        if not isinstance(label, str) or not label:
            continue
        metric_id = raw.get("metric_id")
        entity_ref = _metric_entity_ref(metric_id)
        cards.append(
            CandidateCard(
                input_phrase=query,
                entity_type=entity_type,
                label=label,
                plain_definition=_alias_definition(alias),
                source_methods=["alias"],
                entity_ref=entity_ref,
                debug_ref=f"alias_candidate:{alias.entity_type}:{metric_id}",
                metadata={
                    "alias": alias.alias,
                    "resolution_status": alias.resolution_status,
                    "review_status": alias.review_status,
                    "source": alias.source,
                    "provenance": alias.provenance,
                    "scenario_ids": alias.scenario_ids,
                    "rankable": raw.get("rankable"),
                },
            )
        )
    return cards


def _card_from_district(query: str, candidate: DistrictCandidate) -> CandidateCard:
    location = ", ".join(part for part in (candidate.city, candidate.state) if part)
    return CandidateCard(
        input_phrase=query,
        entity_type="district",
        label=candidate.district_name,
        plain_definition=location,
        source_methods=["district_search"],
        entity_ref=f"district:{candidate.district_id}",
        debug_ref=f"district:{candidate.district_id}",
        metadata={
            "state": candidate.state,
            "match_reason": candidate.match_reason,
        },
    )


def _card_from_profile_field(
    query: str,
    candidate: NCESFieldCandidate,
) -> CandidateCard:
    return CandidateCard(
        input_phrase=query,
        entity_type="profile_field",
        label=candidate.label,
        plain_definition=candidate.description or "",
        source_methods=["profile_field_search"],
        entity_ref=f"profile_field:{candidate.field_key}",
        debug_ref=f"profile_field:{candidate.field_key}",
        metadata={"data_type": candidate.data_type},
    )


def _card_from_topic(query: str, candidate: TopicCandidate) -> CandidateCard:
    label = candidate.subtopic_name or candidate.topic_name
    definition = (
        candidate.topic_name
        if candidate.subtopic_name and candidate.subtopic_name != candidate.topic_name
        else ""
    )
    return CandidateCard(
        input_phrase=query,
        entity_type="topic",
        label=label,
        plain_definition=definition,
        source_methods=["topic_search"],
        entity_ref=f"topic:{candidate.topic_id}:{candidate.subtopic_id or ''}",
        debug_ref=f"topic:{candidate.topic_id}:{candidate.subtopic_id or ''}",
        metadata={"question_count": candidate.question_count},
    )


def _card_from_glossary(
    query: str,
    candidate: GlossaryTermCandidate,
) -> CandidateCard:
    return CandidateCard(
        input_phrase=query,
        entity_type="glossary_term",
        label=candidate.term,
        plain_definition=candidate.definition,
        source_methods=["glossary_search"],
        entity_ref=f"glossary:{candidate.term_id}",
        debug_ref=f"glossary:{candidate.term_id}",
        metadata={"context": candidate.context},
    )


def _card_from_source_document(
    query: str,
    candidate: SourceDocumentCandidate,
) -> CandidateCard:
    return CandidateCard(
        input_phrase=query,
        entity_type="source_document",
        label=candidate.title,
        plain_definition=candidate.document_type or "",
        source_methods=["source_document_search"],
        entity_ref=f"source_document:{candidate.source_id}",
        debug_ref=f"source_document:{candidate.source_id}",
        metadata={
            "district_id": candidate.district_id,
            "academic_year": candidate.academic_year,
        },
    )


def _recall_entity_type_from_alias(entity_type: CatalogEntityType) -> RecallEntityType:
    if entity_type == "nces_field":
        return "profile_field"
    if entity_type in {
        "metric",
        "metric_bundle",
        "district",
        "topic",
        "glossary_term",
        "unsupported_concept",
    }:
        return entity_type
    raise ValueError(f"Unsupported alias entity type for recall: {entity_type}")


def _alias_definition(alias: CatalogAliasRecord) -> str:
    if alias.entity_type == "unsupported_concept":
        message = alias.metadata.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
        return "Reviewed unsupported catalog concept."
    if alias.resolution_status == "approved":
        return "Approved catalog alias."
    if alias.resolution_status == "ambiguous":
        return "Reviewed ambiguous catalog alias; clarification may be required."
    if alias.resolution_status == "out_of_universe":
        return "Reviewed catalog blocker."
    return f"Reviewed catalog alias with status: {alias.resolution_status}."


def _alias_source_methods(alias: CatalogAliasRecord) -> list[RecallSourceMethod]:
    if alias.entity_type == "unsupported_concept":
        return ["alias", "unsupported_concept"]
    return ["alias"]


def _entity_ref_from_alias(alias: CatalogAliasRecord) -> str | None:
    canonical_id = alias.canonical_id.strip() if alias.canonical_id else ""
    if alias.entity_type == "metric":
        return _metric_entity_ref(canonical_id)
    if alias.entity_type == "metric_bundle":
        return _metric_bundle_entity_ref(_metric_ids_from_alias(alias), alias)
    if alias.entity_type == "district" and canonical_id:
        return f"district:{canonical_id}"
    if alias.entity_type == "nces_field" and canonical_id:
        return f"profile_field:{canonical_id}"
    if alias.entity_type == "topic" and canonical_id:
        return f"topic:{canonical_id}"
    if alias.entity_type == "glossary_term" and canonical_id:
        return f"glossary:{canonical_id}"
    if alias.entity_type == "unsupported_concept" and canonical_id:
        return f"unsupported_concept:{canonical_id}"
    return None


def _metric_entity_ref(value: object) -> str | None:
    try:
        metric_id = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f"metric:{metric_id}"


def _metric_bundle_entity_ref(
    metric_ids: list[int],
    alias: CatalogAliasRecord,
) -> str:
    if metric_ids:
        return "metric_bundle:" + ",".join(str(metric_id) for metric_id in metric_ids)
    return f"metric_bundle_alias:{alias.normalized_alias}"


def _metric_ids_from_alias(alias: CatalogAliasRecord) -> list[int]:
    values = alias.canonical_ids or (
        [alias.canonical_id] if alias.canonical_id else []
    )
    ids: list[int] = []
    for value in values:
        try:
            metric_id = int(value)
        except (TypeError, ValueError):
            continue
        ids.append(metric_id)
    return ids


def _ordered_metrics(
    metric_ids: list[int],
    metrics: list[MetricCandidate],
) -> list[MetricCandidate]:
    metrics_by_id = {metric.metric_id: metric for metric in metrics}
    return [
        metric
        for metric_id in metric_ids
        if (metric := metrics_by_id.get(metric_id)) is not None
    ]


def _dedupe_cards(cards: Iterable[CandidateCard]) -> list[CandidateCard]:
    deduped: dict[tuple[RecallEntityType, str], CandidateCard] = {}
    for card in cards:
        key = (card.entity_type, card.entity_ref or card.label.casefold())
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = card
            continue
        methods = [*existing.source_methods]
        for method in card.source_methods:
            if method not in methods:
                methods.append(method)
        deduped[key] = existing.model_copy(
            update={
                "source_methods": methods,
                "metadata": {**existing.metadata, **card.metadata},
            }
        )
    return list(deduped.values())


__all__ = [
    "CandidateCard",
    "CatalogRecallService",
    "RecallBatch",
    "RecallBatchSource",
    "RecallEntityType",
    "RecallReport",
    "RecallSourceMethod",
]
