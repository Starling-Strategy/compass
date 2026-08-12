"""Evidence retrieval via Vespa Cloud with PostgreSQL fallback.

Queries Nathan's PiedPiper Vespa instance (passage schema, BM25 ranking)
then applies lightweight client-side re-ranking for k-diversity slicing.
"""
import logging
import math
import os
import re
from collections import Counter
from contextlib import nullcontext as _nullcontext

import psycopg2
import psycopg2.extras

try:
    import httpx
except ImportError:
    httpx = None

try:
    import logfire
except ImportError:
    logfire = None

from typing import Protocol
from prediction_pipeline.config import Config
from prediction_pipeline.db import resolve_doc_ids, resolve_single_doc_id
from prediction_pipeline.models import Chunk, QuestionContext

logger = logging.getLogger(__name__)

_SALARY_QUERY_RE = re.compile(
    r"(salary|base\s*pay|annual\s*(?:base\s*)?salary|salary\s*schedule|"
    r"step\s*\d|lane\s*\d|maximum\s*(?:annual\s*)?salary|stipend|compensation\s*schedule)",
    re.IGNORECASE,
)


def build_search_query(question: QuestionContext, ay_id: int = 25) -> str:
    """Build a query from question text + academic year + coding guidance.

    Guidance text contains domain-specific terms (e.g. "contract work year",
    "salary schedule") that dramatically improve retrieval precision.
    """
    parts = [question.q_text]
    ay_year = f"20{ay_id - 1:02d}-20{ay_id:02d}" if ay_id < 100 else str(ay_id)
    parts.append(f"Academic year {ay_year}")
    if question.coding_guidance:
        parts.append(question.coding_guidance[:500])
    if question.focus_terms:
        parts.append(" ".join(question.focus_terms))
    if question.target_sections:
        parts.append(" ".join(question.target_sections))
    if question.terminology_context:
        parts.append(question.terminology_context[:300])
    return " ".join(parts)


def get_pg_connection(config: Config):
    """Same as db.py — duplicated to avoid circular imports."""
    return psycopg2.connect(host=config.pg_host, port=config.pg_port,
                            database=config.pg_database, user=config.pg_user,
                            password=config.pg_password)


_STOP_WORDS = frozenset("the a an is are was were be been being in on at to for of and or not with by from".split())
_WORD_RE = re.compile(r"[a-z0-9]+")


def _bm25_rank_chunks(
    query: str,
    chunks: list[Chunk],
    top_n: int = 10,
) -> list[Chunk]:
    """Rank chunks by BM25 text matching against the query.

    Fast (no API calls). Vespa's server-side hybrid ranking already handled
    semantic matching at the document level; this selects the best chunks
    within those documents using lexical overlap.
    """
    n = len(chunks)
    if n <= top_n:
        return chunks

    q_terms = [w for w in _WORD_RE.findall(query.lower()) if w not in _STOP_WORDS and len(w) > 2]
    q_counts = Counter(q_terms)
    if not q_terms:
        return chunks[:top_n]

    df: Counter = Counter()
    chunk_tokens: list[Counter] = []
    for c in chunks:
        combined = c.text + " " + (c.summary or "")
        tokens = [w for w in _WORD_RE.findall(combined.lower()) if w not in _STOP_WORDS and len(w) > 2]
        ct = Counter(tokens)
        chunk_tokens.append(ct)
        for t in set(tokens):
            df[t] += 1

    k1, b = 1.5, 0.75
    avg_len = max(1, sum(sum(ct.values()) for ct in chunk_tokens) / max(1, n))

    max_vespa_score = max((c.score for c in chunks), default=1.0) or 1.0

    scored: list[tuple[float, int]] = []
    for i, tf in enumerate(chunk_tokens):
        doc_len = sum(tf.values()) or 1
        bm25 = 0.0
        for term, qf in q_counts.items():
            if tf[term] == 0:
                continue
            idf = math.log((n - df[term] + 0.5) / (df[term] + 0.5) + 1)
            tf_norm = (tf[term] * (k1 + 1)) / (tf[term] + k1 * (1 - b + b * doc_len / avg_len))
            bm25 += idf * tf_norm * qf
        vespa_boost = chunks[i].score / max_vespa_score * 0.3
        scored.append((bm25 + vespa_boost, i))

    scored.sort(key=lambda x: -x[0])

    seen: set[str] = set()
    selected: list[Chunk] = []
    for _, idx in scored:
        c = chunks[idx]
        prefix = c.text[:200].strip().lower()
        if prefix in seen:
            continue
        seen.add(prefix)
        selected.append(c)
        if len(selected) >= top_n:
            break

    return selected


class BaseRetriever(Protocol):
    def retrieve(self, district_id: int, ay_id: int, query: str, top_k: int) -> list[Chunk]: ...


class VespaRetriever:
    """BM25 retrieval via Nathan's PiedPiper Vespa instance.

    Schema: `passage` — each record is one pre-chunked passage with fields:
      passage_text, doc_id, doc_title, document_type, section_heading,
      page_number, chunk_index, char_start, char_end, academic_years

    Rank profile: `bm25` — BM25 across passage_text + section_heading + doc_title.
    No client-side embedding generation needed.
    """
    def __init__(self, url: str, api_key: str | None = None,
                 cert_path: str | None = None, key_path: str | None = None,
                 config: Config | None = None):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.cert_path = cert_path
        self.key_path = key_path
        self._config = config
        self._client = None
        self._id_mapping: dict[str, str] = {}

    def _get_client(self):
        if self._client is None:
            kwargs = {"base_url": self.url, "timeout": 30.0}
            if self.cert_path and self.key_path:
                cert = os.path.expanduser(self.cert_path)
                key = os.path.expanduser(self.key_path)
                kwargs["cert"] = (cert, key)
            elif self.api_key:
                kwargs["headers"] = {"Authorization": f"Bearer {self.api_key}"}
            self._client = httpx.Client(**kwargs)
        return self._client

    def _bm25_search(self, district_id: int, ay_id: int,
                     query_text: str, n_hits: int = 15,
                     salary_boost: bool = False) -> list[dict]:
        """BM25 search on PiedPiper passage schema, filtered by district + AY.

        If salary_boost=True, widens retrieval with an OR clause for
        salary_schedule documents so they appear even if BM25 doesn't rank
        them in the top results.
        """
        client = self._get_client()

        where_clause = (
            f"district_id = {district_id} "
            f"and academic_years contains '{ay_id}' "
            "and userQuery()"
        )
        if salary_boost:
            where_clause = (
                f"district_id = {district_id} "
                f"and academic_years contains '{ay_id}' "
                "and (userQuery() or document_type contains 'salary_schedule')"
            )

        yql = (
            "select doc_id, doc_title, document_type, passage_text, "
            "section_heading, page_number, chunk_index, char_start, char_end "
            f"from passage where {where_clause} "
            f"limit {n_hits}"
        )

        body = {
            "yql": yql,
            "query": query_text[:1000],
            "ranking": "bm25",
            "hits": n_hits,
            "timeout": "10s",
        }

        import time as _time
        for attempt in range(3):
            try:
                resp = client.post("/search/", json=body)
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if attempt < 2 and (status is None or status >= 500):
                    logger.warning("Vespa search attempt %s failed (%s), retrying...", attempt + 1, status or type(exc).__name__)
                    _time.sleep(2 ** attempt)
                    continue
                logger.exception("Vespa BM25 search failed after %s attempts", attempt + 1)
                return []

        hits = data.get("root", {}).get("children", [])
        total = data.get("root", {}).get("fields", {}).get("totalCount", len(hits))
        logger.info("Vespa BM25 returned %s passages (total=%s) for d=%s", len(hits), total, district_id)
        return hits

    def _extract_chunks(self, hits: list[dict], query_text: str) -> list[Chunk]:
        """Convert Vespa passage hits to Chunk objects.

        Each hit IS one passage (not a parent doc with chunk arrays).
        For salary queries, boost passages from salary_schedule docs.
        """
        is_salary = bool(_SALARY_QUERY_RE.search(query_text))
        salary_chunks = []
        other_chunks = []

        for hit in hits:
            fields = hit.get("fields", {})
            raw_doc_id = str(fields.get("doc_id", ""))
            doc_id = resolve_single_doc_id(raw_doc_id, self._id_mapping) or raw_doc_id
            doc_name = fields.get("doc_title", "") or ""
            doc_type = fields.get("document_type", "") or ""
            relevance = hit.get("relevance", 0.0)

            # passage_text may have a [Summary: ...] suffix — split it out
            # Strip Vespa <hi></hi> highlight tags to get clean text
            passage = fields.get("passage_text", "") or ""
            passage = passage.replace("<hi>", "").replace("</hi>", "")
            summary = ""
            if "[Summary:" in passage:
                parts = passage.rsplit("[Summary:", 1)
                passage = parts[0].rstrip()
                summary = parts[1].rstrip("]").strip()

            chunk = Chunk(
                doc_id=doc_id,
                doc_name=doc_name,
                text=passage,
                summary=summary,
                page_number=fields.get("page_number"),
                section_heading=fields.get("section_heading"),
                score=relevance,
            )
            if is_salary and "salary" in doc_type.lower():
                salary_chunks.append(chunk)
            else:
                other_chunks.append(chunk)

        if salary_chunks:
            logger.info("Salary boost: %s passages from salary docs promoted", len(salary_chunks))
            return salary_chunks + other_chunks
        return other_chunks

    def retrieve(self, district_id: int, ay_id: int, query: str, top_k: int) -> list[Chunk]:
        with logfire.span("vespa_retrieve", district_id=district_id, top_k=top_k) if logfire else _nullcontext():
            if self._config:
                self._id_mapping = resolve_doc_ids(self._config, district_id)

            is_salary = bool(_SALARY_QUERY_RE.search(query))
            n_hits = max(top_k, 25) if is_salary else max(top_k, 15)
            hits = self._bm25_search(district_id, ay_id, query,
                                     n_hits=n_hits, salary_boost=is_salary)

            if not hits:
                logger.warning("Vespa BM25 returned 0 passages for d=%s", district_id)
                return []

            all_chunks = self._extract_chunks(hits, query)
            logger.info("Extracted %s passages from Vespa", len(all_chunks))

            ranked = _bm25_rank_chunks(query, all_chunks, top_n=top_k)
            logger.info("Returning top %s passages after re-rank", len(ranked))
            return ranked


class PostgresRetriever:
    """Fallback: reads full document text from silver.district_documents.

    Caches all docs per district. Uses lightweight BM25 on doc_name + lead
    text to rank documents per-query, so salary questions surface salary
    schedules and evaluation questions surface evaluation docs.
    """
    _PREVIEW_CHARS = 3000

    def __init__(self, config: Config):
        self.config = config
        self._district_cache: dict[int, list[Chunk]] = {}

    def _load_district_docs(self, district_id: int) -> list[Chunk]:
        if district_id in self._district_cache:
            return self._district_cache[district_id]
        sql = """
            SELECT doc_id::text, doc_name, full_text, ai_confidence
            FROM silver.district_documents
            WHERE district_id = %s AND source_pipeline = 'docpipe'
              AND extraction_status = 'success' AND full_text IS NOT NULL
            ORDER BY ai_confidence DESC NULLS LAST, length(full_text) DESC, doc_name
        """
        with get_pg_connection(self.config) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (district_id,))
                rows = cur.fetchall()
        chunks = [
            Chunk(doc_id=str(row["doc_id"]), doc_name=row["doc_name"] or "",
                  text=row["full_text"] or "", score=float(row["ai_confidence"] or 0))
            for row in rows
        ]
        logger.info("Cached %s docs for district %s", len(chunks), district_id)
        self._district_cache[district_id] = chunks
        return chunks

    _GUARANTEED_SLOTS = 3

    def _rank_for_query(self, docs: list[Chunk], query: str, top_k: int) -> list[Chunk]:
        """Hybrid ranking: guarantee the top docs by confidence+length
        (preserving large CBAs), then fill remaining slots via BM25 on
        doc_name + lead text to surface query-relevant smaller docs."""
        if len(docs) <= top_k:
            return docs

        guaranteed = docs[:min(self._GUARANTEED_SLOTS, top_k)]
        guaranteed_ids = {id(d) for d in guaranteed}
        remaining_slots = top_k - len(guaranteed)
        if remaining_slots <= 0:
            return guaranteed

        candidates = [d for d in docs if id(d) not in guaranteed_ids]
        q_terms = [w for w in _WORD_RE.findall(query.lower())
                    if w not in _STOP_WORDS and len(w) > 2]
        if not q_terms:
            return guaranteed + candidates[:remaining_slots]

        q_counts = Counter(q_terms)
        n = len(candidates)

        doc_tokens: list[Counter] = []
        df: Counter = Counter()
        for doc in candidates:
            preview = (doc.doc_name + " " + doc.text[:self._PREVIEW_CHARS]).lower()
            tokens = [w for w in _WORD_RE.findall(preview)
                      if w not in _STOP_WORDS and len(w) > 2]
            ct = Counter(tokens)
            doc_tokens.append(ct)
            for t in set(tokens):
                df[t] += 1

        k1, b = 1.5, 0.75
        avg_len = max(1, sum(sum(ct.values()) for ct in doc_tokens) / max(1, n))

        scored: list[tuple[float, int]] = []
        for i, tf in enumerate(doc_tokens):
            doc_len = sum(tf.values()) or 1
            bm25 = 0.0
            for term, qf in q_counts.items():
                if tf[term] == 0:
                    continue
                idf = math.log((n - df[term] + 0.5) / (df[term] + 0.5) + 1)
                tf_norm = (tf[term] * (k1 + 1)) / (tf[term] + k1 * (1 - b + b * doc_len / avg_len))
                bm25 += idf * tf_norm * qf
            conf_boost = candidates[i].score * 0.2
            scored.append((bm25 + conf_boost, i))

        scored.sort(key=lambda x: (-x[0], x[1]))
        bm25_picks = [candidates[idx] for _, idx in scored[:remaining_slots]]
        return guaranteed + bm25_picks

    def retrieve(self, district_id: int, ay_id: int, query: str, top_k: int) -> list[Chunk]:
        with logfire.span("postgres_retrieve", district_id=district_id, top_k=top_k) if logfire else _nullcontext():
            all_docs = self._load_district_docs(district_id)
            result = self._rank_for_query(all_docs, query, top_k)
            logger.info("Postgres: returning %s/%s docs for d=%s", len(result), len(all_docs), district_id)
            return result


def get_retriever(config: Config) -> BaseRetriever:
    """Factory: Vespa if configured, PostgreSQL fallback otherwise."""
    api_key = config.vespa_api_key or os.environ.get("VESPA_API_KEY")
    has_mtls = config.vespa_cert_path and config.vespa_key_path
    if config.vespa_url and (api_key or has_mtls):
        logger.info("Using VespaRetriever at %s", config.vespa_url)
        return VespaRetriever(
            config.vespa_url,
            api_key=api_key,
            cert_path=config.vespa_cert_path,
            key_path=config.vespa_key_path,
            config=config,
        )
    logger.info("Using PostgresRetriever (Vespa not configured)")
    return PostgresRetriever(config)
