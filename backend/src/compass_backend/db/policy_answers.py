"""Read-only DB repository for deterministic policy answer execution."""

from __future__ import annotations

import asyncpg

from compass_backend.artifacts import CitationSource
from compass_backend.config import Settings, settings
from compass_backend.db._base import RepositoryBase
from compass_backend.db._pool import ChatPoolHolder
from compass_backend.db.rows import MetricAnswerRow


# Answer values that mean "no real answer" — a source document backs a value, so
# a row carrying one of these stays uncited (the fallback document attach skips
# it). Mirrors the no-answer literals the execution layer already recognises
# (execution/operations.py, ranking.py, lookup.py); kept local so the db layer
# does not import upward from execution/.
_NO_ANSWER_VALUES = frozenset(
    {
        "ina",
        "n/a",
        "na",
        "none",
        "unavailable",
        "not available",
        "issue not addressed",
        "not addressed",
    }
)


def _value_is_no_answer(value: object) -> bool:
    """Return True when ``value`` is empty or an "Issue not addressed" marker."""

    if value is None:
        return True
    text = str(value).strip().casefold()
    return text == "" or text in _NO_ANSWER_VALUES


class ReadOnlyPolicyAnswerRepository(RepositoryBase):
    """Fetch resolved policy answer rows from Compass navigator tables.

    Borrows a connection from the shared chat pool wired by
    ``create_app_from_settings``. Tests inject a synthetic pool via
    ``FakeChatPool`` from ``compass_backend.tests._chat_pool_fixtures``.
    """

    def __init__(
        self,
        source: Settings = settings,
        *,
        pool: asyncpg.Pool | ChatPoolHolder,
    ) -> None:
        self._settings = source
        self._pool = pool

    async def fetch_metric_answer_rows(
        self,
        *,
        metric_id: int,
        academic_year: str,
    ) -> list[MetricAnswerRow]:
        """Fetch covered-district answer rows for one metric and year."""

        sql = f"""
            SELECT
                answer.id AS answer_id,
                answer.district_id,
                district.name AS district_name,
                district.state_abbrev AS state,
                answer.metric_id,
                metric.name AS metric_name,
                answer.value,
                answer.ay_range AS academic_year
            FROM {self._table("navigator_answers")} answer
            INNER JOIN {self._table("navigator_districts")} district
                ON district.district_id = answer.district_id
            INNER JOIN {self._table("navigator_metrics")} metric
                ON metric.metric_id = answer.metric_id
            WHERE answer.metric_id = $1
              AND answer.ay_range = $2
            ORDER BY district.name, answer.id
        """
        async with self._acquire() as conn:
            answer_rows = await conn.fetch(sql, metric_id, academic_year)

        rows_by_district: dict[int, MetricAnswerRow] = {}
        for row in answer_rows:
            data = dict(row)
            candidate = MetricAnswerRow(
                answer_id=data["answer_id"],
                district_id=data["district_id"],
                district_name=data["district_name"],
                state=data["state"],
                metric_id=data["metric_id"],
                metric_name=data["metric_name"],
                value=data["value"],
                academic_year=data["academic_year"],
            )
            existing = rows_by_district.get(candidate.district_id)
            if existing is None or (candidate.answer_id or 0) > (
                existing.answer_id or 0
            ):
                rows_by_district[candidate.district_id] = candidate

        rows = sorted(
            rows_by_district.values(),
            key=lambda answer: answer.district_name.casefold(),
        )

        await self._attach_answer_citations(rows)
        await self._attach_document_sources(rows)
        return rows

    async def list_available_academic_years(
        self,
        *,
        metric_id: int,
        district_ids: set[int],
    ) -> list[str]:
        """Return available years for one metric and selected districts."""

        if not district_ids:
            return []
        sql = f"""
            SELECT DISTINCT ay_range AS academic_year
            FROM {self._table("navigator_answers")}
            WHERE metric_id = $1
              AND district_id = ANY($2::int[])
            ORDER BY split_part(ay_range, ' - ', 1)::int, ay_range
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(sql, metric_id, sorted(district_ids))
        return [str(row["academic_year"]) for row in rows]

    async def fetch_metric_answer_rows_for_year(
        self,
        *,
        metric_id: int,
        academic_year: str,
        district_ids: set[int],
    ) -> list[MetricAnswerRow]:
        """Fetch selected-district answer rows for one metric and year."""

        return await self.fetch_metric_answer_rows_for_years(
            metric_id=metric_id,
            academic_years=[academic_year],
            district_ids=district_ids,
        )

    async def fetch_metric_answer_rows_for_years(
        self,
        *,
        metric_id: int,
        academic_years: list[str],
        district_ids: set[int],
    ) -> list[MetricAnswerRow]:
        """Fetch selected-district answer rows for one metric and year list."""

        if not academic_years or not district_ids:
            return []
        sql = f"""
            SELECT
                answer.id AS answer_id,
                answer.district_id,
                district.name AS district_name,
                district.state_abbrev AS state,
                answer.metric_id,
                metric.name AS metric_name,
                answer.value,
                answer.ay_range AS academic_year
            FROM {self._table("navigator_answers")} answer
            INNER JOIN {self._table("navigator_districts")} district
                ON district.district_id = answer.district_id
            INNER JOIN {self._table("navigator_metrics")} metric
                ON metric.metric_id = answer.metric_id
            WHERE answer.metric_id = $1
              AND answer.ay_range = ANY($2::text[])
              AND answer.district_id = ANY($3::int[])
            ORDER BY
                split_part(answer.ay_range, ' - ', 1)::int,
                district.name,
                answer.id
        """
        async with self._acquire() as conn:
            answer_rows = await conn.fetch(
                sql,
                metric_id,
                academic_years,
                sorted(district_ids),
            )

        rows_by_identity: dict[tuple[int, str], MetricAnswerRow] = {}
        for row in answer_rows:
            candidate = MetricAnswerRow(**dict(row))
            identity = (candidate.district_id, candidate.academic_year)
            existing = rows_by_identity.get(identity)
            if existing is None or (candidate.answer_id or 0) > (
                existing.answer_id or 0
            ):
                rows_by_identity[identity] = candidate

        year_order = {year: index for index, year in enumerate(academic_years)}
        rows = sorted(
            rows_by_identity.values(),
            key=lambda answer: (
                year_order.get(answer.academic_year, len(year_order)),
                answer.district_name.casefold(),
            ),
        )
        await self._attach_answer_citations(rows)
        await self._attach_document_sources(rows)
        return rows

    async def fetch_reviewed_district_ids(
        self,
        *,
        academic_year: str,
        district_ids: set[int],
    ) -> set[int]:
        """Return selected district IDs with any Compass answer row for one year."""

        if not district_ids:
            return set()
        sql = f"""
            SELECT DISTINCT district_id
            FROM {self._table("navigator_answers")}
            WHERE ay_range = $1
              AND district_id = ANY($2::int[])
            ORDER BY district_id
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(sql, academic_year, sorted(district_ids))
        return {int(row["district_id"]) for row in rows}

    async def fetch_recent_metric_answer_rows(
        self,
        *,
        metric_id: int,
        before_academic_year: str,
        district_ids: set[int],
    ) -> list[MetricAnswerRow]:
        """Fetch latest prior-year metric rows for selected districts."""

        if not district_ids:
            return []
        sql = f"""
            SELECT DISTINCT ON (answer.district_id)
                answer.id AS answer_id,
                answer.district_id,
                district.name AS district_name,
                district.state_abbrev AS state,
                answer.metric_id,
                metric.name AS metric_name,
                answer.value,
                answer.ay_range AS academic_year
            FROM {self._table("navigator_answers")} answer
            INNER JOIN {self._table("navigator_districts")} district
                ON district.district_id = answer.district_id
            INNER JOIN {self._table("navigator_metrics")} metric
                ON metric.metric_id = answer.metric_id
            WHERE answer.metric_id = $1
              AND answer.district_id = ANY($2::int[])
              AND split_part(answer.ay_range, ' - ', 1)::int <
                  split_part($3, ' - ', 1)::int
            ORDER BY
                answer.district_id,
                split_part(answer.ay_range, ' - ', 1)::int DESC,
                answer.id DESC
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(sql, metric_id, sorted(district_ids), before_academic_year)
        return [MetricAnswerRow(**dict(row)) for row in rows]

    async def _attach_answer_citations(
        self,
        rows: list[MetricAnswerRow],
    ) -> None:
        """Attach answer-level citations from Compass navigator evidence."""

        answer_ids = sorted({row.answer_id for row in rows if row.answer_id is not None})
        if not answer_ids:
            return

        sql = f"""
            SELECT
                answer_id,
                citation_text AS source_name,
                citation_link AS source_url,
                citation_order
            FROM {self._table("navigator_citations")}
            WHERE answer_id = ANY($1::int[])
            ORDER BY answer_id, citation_order, id
        """
        async with self._acquire() as conn:
            citation_rows = await conn.fetch(sql, answer_ids)

        row_by_answer_id = {row.answer_id: row for row in rows}
        for citation_row in citation_rows:
            data = dict(citation_row)
            row = row_by_answer_id.get(data["answer_id"])
            if row is None:
                continue
            row.citations.append(
                CitationSource(
                    source_name=data["source_name"],
                    source_url=data["source_url"],
                    academic_year=row.academic_year,
                    district_id=row.district_id,
                    citation_order=data["citation_order"],
                )
            )

    async def _attach_document_sources(
        self,
        rows: list[MetricAnswerRow],
    ) -> None:
        """Attach district/year source documents when answer evidence is absent.

        Two gates make a fallback document a citation that genuinely backs the
        claim (docs/compass_concepts/06-accuracy-citation.md, CITE-R2):

        1. The row must carry a real answer value — a source backs a *value*, so
           an "Issue not addressed" / no-answer row stays uncited (#1733).
        2. The document must be TOPIC-RELEVANT — its document_type must be one
           that genuinely backs this metric's topic, derived from real citation
           usage in ``compass.topic_source_document_types`` (migration 151). This
           drops the over-attachment that put the district's whole filing cabinet
           on a covered-but-uncited row — e.g. an "Annual Calendar" on a
           health-premium "Yes" (calendars back workdays, never benefits; the
           citation graph draws that line). A document is attached at document
           level (no page) because the page-level inline citation is the thing
           that's missing, not the document.

        A covered row whose district has no document of a topic-relevant type
        carries no fallback citation — the honest CITE-R9 outcome ("no source
        record exists -> no citation"), strictly better than an off-topic one.

        This method is called per metric (one ``metric_id`` per row set), so the
        topic filter scopes by the metrics present in the rows.
        """

        rows_without_answer_citations = [
            row
            for row in rows
            if not row.citations and not _value_is_no_answer(row.value)
        ]
        district_ids = sorted({row.district_id for row in rows_without_answer_citations})
        if not district_ids:
            return
        metric_ids = sorted({row.metric_id for row in rows_without_answer_citations})

        sql = f"""
            SELECT
                district_id,
                title AS source_name,
                source_link AS source_url,
                document_type AS source_document_type,
                valid_year_range AS source_academic_year,
                source_id AS citation_order
            FROM {self._table("navigator_sources")}
            WHERE district_id = ANY($1::int[])
              AND document_type IN (
                  SELECT tst.document_type
                  FROM {self._table("topic_source_document_types")} tst
                  JOIN {self._table("navigator_metrics")} m
                      ON m.topic = tst.topic
                  WHERE m.metric_id = ANY($2::int[])
              )
            ORDER BY district_id, valid_year_range DESC, source_id
        """
        async with self._acquire() as conn:
            source_rows = await conn.fetch(sql, district_ids, metric_ids)

        sources_by_district: dict[int, list[CitationSource]] = {}
        for row in source_rows:
            data = dict(row)
            sources_by_district.setdefault(data["district_id"], []).append(
                CitationSource(
                    source_name=data["source_name"],
                    source_url=data["source_url"],
                    document_type=data["source_document_type"],
                    academic_year=data["source_academic_year"],
                    district_id=data["district_id"],
                    citation_order=data["citation_order"],
                )
            )

        for row in rows_without_answer_citations:
            for source in sources_by_district.get(row.district_id, []):
                if _year_ranges_overlap(source.academic_year, row.academic_year):
                    row.citations.append(source)


def _year_ranges_overlap(left: str | None, right: str) -> bool:
    """Return whether two academic-year strings overlap."""

    left_range = _parse_year_range(left)
    right_range = _parse_year_range(right)
    if left_range is None or right_range is None:
        return False
    left_start, left_end = left_range
    right_start, right_end = right_range
    return left_start <= right_end and right_start <= left_end


def _parse_year_range(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    parts = [part.strip() for part in value.split("-")]
    if len(parts) != 2:
        return None
    try:
        start = int(parts[0])
        end = int(parts[1])
    except ValueError:
        return None
    return (start, end)
