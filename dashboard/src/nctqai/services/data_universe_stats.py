"""Data Universe stats — queries against the compass schema for data coverage.

Shows what data Compass has access to: districts, questions, answers,
publications, NCES districts, and when each dataset was last synced.
"""

import logging

from nctqai.db import COMPASS_SCHEMA, run_sql

logger = logging.getLogger(__name__)


def get_summary_stats() -> dict:
    """Top-level KPI counts: districts, questions, answers, source documents, academic years."""
    sql = f"""
        SELECT
            (SELECT COUNT(*) FROM {COMPASS_SCHEMA}.district_profiles) AS district_count,
            (SELECT COUNT(DISTINCT state) FROM {COMPASS_SCHEMA}.district_profiles) AS state_count,
            (SELECT COUNT(*) FROM {COMPASS_SCHEMA}.policy_questions) AS question_count,
            (SELECT COUNT(*) FROM {COMPASS_SCHEMA}.policy_answers) AS answer_count,
            (SELECT COUNT(*) FROM {COMPASS_SCHEMA}.navigator_sources) AS source_document_count,
            (SELECT COUNT(DISTINCT academic_year) FROM {COMPASS_SCHEMA}.policy_answers) AS year_count,
            (SELECT MIN(academic_year) FROM {COMPASS_SCHEMA}.policy_answers) AS earliest_academic_year,
            (SELECT COUNT(DISTINCT topic_name) FROM {COMPASS_SCHEMA}.policy_questions) AS topic_count
    """
    rows = run_sql(sql)
    return rows[0] if rows else {}


def get_publication_stats() -> dict:
    """Current chatbot publication counts and summary coverage.

    The summary column is added dynamically by the backfill script.
    Compass is the only runtime schema; legacy schemas are not consulted.
    """
    empty = {"total": 0, "summarized": 0, "coverage_pct": 0}
    tbl_check = """
        SELECT COUNT(*) AS has_tbl
        FROM information_schema.tables
        WHERE table_schema = %s AND table_name = 'nctq_publications'
    """
    tbl_rows = run_sql(tbl_check, (COMPASS_SCHEMA,))
    has_table = (tbl_rows[0]["has_tbl"] > 0) if tbl_rows else False
    if not has_table:
        logger.warning("%s.nctq_publications is missing", COMPASS_SCHEMA)
        return empty

    col_check = """
        SELECT COUNT(*) AS has_col
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = 'nctq_publications'
          AND column_name = 'summary'
    """
    col_rows = run_sql(col_check, (COMPASS_SCHEMA,))
    has_summary = (col_rows[0]["has_col"] > 0) if col_rows else False

    if has_summary:
        sql = f"""
            SELECT
                COUNT(*) FILTER (WHERE for_chatbot IS TRUE) AS total,
                SUM(
                    CASE
                        WHEN for_chatbot IS TRUE
                         AND summary IS NOT NULL
                         AND summary != ''
                        THEN 1 ELSE 0
                    END
                ) AS summarized
            FROM {COMPASS_SCHEMA}.nctq_publications
        """
    else:
        logger.warning("%s.nctq_publications.summary is missing", COMPASS_SCHEMA)
        sql = f"""
            SELECT COUNT(*) FILTER (WHERE for_chatbot IS TRUE) AS total, 0 AS summarized
            FROM {COMPASS_SCHEMA}.nctq_publications
        """
    rows = run_sql(sql)
    stats = rows[0] if rows else {}
    total = stats.get("total", 0) or 0
    summarized = stats.get("summarized", 0) or 0
    return {
        "total": total,
        "summarized": summarized,
        "coverage_pct": round(summarized / total * 100) if total else 0,
    }



def get_nces_stats() -> dict:
    """NCES district count and linked coverage."""
    sql = f"""
        SELECT
            (SELECT COUNT(*) FROM {COMPASS_SCHEMA}.nces_districts) AS nces_total,
            (SELECT COUNT(*) FROM {COMPASS_SCHEMA}.navigator_nces_link) AS linked_count
    """
    rows = run_sql(sql)
    return rows[0] if rows else {}


def get_districts_by_state() -> list[dict]:
    """District count per state, sorted by count descending."""
    sql = f"""
        SELECT state, COUNT(*) AS count
        FROM {COMPASS_SCHEMA}.district_profiles
        GROUP BY state
        ORDER BY count DESC
    """
    return run_sql(sql)


def get_questions_by_topic() -> list[dict]:
    """Question count per topic, sorted by count descending."""
    sql = f"""
        SELECT topic_name AS topic, COUNT(*) AS count
        FROM {COMPASS_SCHEMA}.policy_questions
        GROUP BY topic_name
        ORDER BY count DESC
    """
    return run_sql(sql)


def get_answers_by_year() -> list[dict]:
    """Total reviewed policy answers per academic year.

    Counts every reviewed row, including specific values as well as the
    'Issue not addressed', 'Not applicable for district', and 'Unavailable'
    outcomes, since each one represents a district and question that an analyst
    reviewed for that year.
    """
    sql = f"""
        SELECT academic_year, COUNT(*) AS count
        FROM {COMPASS_SCHEMA}.policy_answers
        WHERE academic_year IS NOT NULL
        GROUP BY academic_year
        ORDER BY academic_year DESC
    """
    return run_sql(sql)

def get_sync_timestamps() -> dict:
    """Last sync timestamps from each data source."""
    sql = f"""
        SELECT
            (SELECT MAX(synced_at) FROM {COMPASS_SCHEMA}.navigator_answers) AS policy_data_synced,
            (SELECT MAX(synced_at) FROM {COMPASS_SCHEMA}.navigator_sources) AS sources_synced,
            (SELECT MAX(synced_at) FROM {COMPASS_SCHEMA}.nctq_publications) AS publications_synced,
            (SELECT MAX(imported_at) FROM {COMPASS_SCHEMA}.nces_districts) AS nces_imported
    """
    rows = run_sql(sql)
    return rows[0] if rows else {}


def _table_exists(table_name: str) -> bool:
    sql = f"""
        SELECT COUNT(*) AS has_tbl
        FROM information_schema.tables
        WHERE table_schema = %s AND table_name = '{table_name}'
    """
    rows = run_sql(sql, (COMPASS_SCHEMA,))
    return bool(rows and rows[0].get("has_tbl", 0) > 0)


def get_pathfinder_content_stats() -> dict:
    """Pathfinder WordPress cache counts and freshness."""
    empty = {
        "total": 0,
        "topic_pages": 0,
        "pages": 0,
        "articles": 0,
        "synced_at": None,
        "source_modified_at": None,
    }
    if not _table_exists("pathfinder_content_items"):
        return empty

    sql = f"""
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE post_type = 'tcd-topic') AS topic_pages,
            COUNT(*) FILTER (WHERE post_type = 'page') AS pages,
            COUNT(*) FILTER (WHERE post_type = 'article') AS articles,
            MAX(synced_at) AS synced_at,
            MAX(modified_gmt) AS source_modified_at
        FROM {COMPASS_SCHEMA}.pathfinder_content_items
        WHERE active IS TRUE
    """
    rows = run_sql(sql)
    return rows[0] if rows else empty
