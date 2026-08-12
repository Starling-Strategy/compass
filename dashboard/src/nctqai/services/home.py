"""Home page service — cross-section KPI aggregates.

Three queries, one per dashboard section.
"""

from nctqai.db import COMPASS_SCHEMA, SILVER_SCHEMA, run_sql
from nctqai.services.mc.constants import D_FILTER
from nctqai.services.mc.constants import Q_FILTER


def get_mc_stats(ay_id: int = 25) -> dict:
    """Metric Calculator KPIs from suggested_answers."""
    sql = f"""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN sa.status IN ('approved', 'accepted') THEN 1 ELSE 0 END) as accepted,
               SUM(CASE WHEN sa.status IN ('incorrect', 'rejected') THEN 1 ELSE 0 END) as rejected,
               SUM(CASE WHEN sa.status = 'unreviewed' THEN 1 ELSE 0 END) as unreviewed,
               COUNT(DISTINCT sa.district_id) as districts
        FROM {SILVER_SCHEMA}.v_latest_suggested_answers sa
        JOIN {SILVER_SCHEMA}.districts sd ON sd.district_id = sa.district_id
        JOIN {SILVER_SCHEMA}.questions q ON q.q_id = sa.q_id
        WHERE {Q_FILTER} AND {D_FILTER} AND sa.ay_id = %s
    """
    rows = run_sql(sql, (ay_id,))
    return rows[0] if rows else {
        "total": 0, "accepted": 0, "rejected": 0, "unreviewed": 0, "districts": 0,
    }


def get_docs_stats() -> dict:
    """Documents KPIs from district_documents."""
    sql = f"""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN extraction_status = 'success' THEN 1 ELSE 0 END) as processed,
               SUM(CASE WHEN extraction_status = 'failed' THEN 1 ELSE 0 END) as failed
        FROM {SILVER_SCHEMA}.district_documents
        WHERE source_pipeline = 'docpipe'
    """
    rows = run_sql(sql)
    return rows[0] if rows else {"total": 0, "processed": 0, "failed": 0}


def get_pa_stats() -> dict:
    """Compass KPIs from chat_sessions + chat_messages."""
    sql = f"""
        SELECT COUNT(DISTINCT s.session_id) as conversations,
               COUNT(m.id) as messages
        FROM {COMPASS_SCHEMA}.chat_sessions s
        LEFT JOIN {COMPASS_SCHEMA}.chat_messages m ON m.session_id = s.session_id
    """
    rows = run_sql(sql)
    return rows[0] if rows else {"conversations": 0, "messages": 0}
