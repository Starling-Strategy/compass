"""District-level services for the Metric Calculator.

Queries v_latest_suggested_answers joined with bronze.district for review stats.
"""

from nctqai.db import BRONZE_SCHEMA, SILVER_SCHEMA, run_sql
from nctqai.models.mc import DistrictWithReviewStats
from nctqai.services.mc.constants import D_FILTER as _D_FILTER
from nctqai.services.mc.constants import Q_FILTER as _Q_FILTER


def get_all_districts(ay_id: int) -> list[DistrictWithReviewStats]:
    """Get all districts with review progress counts for a given academic year."""
    sql = f"""
        SELECT d.district_id, d.district_name, d.district_state as state,
               COALESCE(d.sample_30_flag, false) as is_priority,
               SUM(CASE WHEN sa.status IN ('approved','accepted') THEN 1 ELSE 0 END)::int as accepted,
               SUM(CASE WHEN sa.status IN ('incorrect','rejected') THEN 1 ELSE 0 END)::int as rejected,
               SUM(CASE WHEN sa.status = 'unreviewed' THEN 1 ELSE 0 END)::int as unreviewed
        FROM {SILVER_SCHEMA}.v_latest_suggested_answers sa
        JOIN {BRONZE_SCHEMA}.district d ON d.district_id = sa.district_id
        JOIN {SILVER_SCHEMA}.districts sd ON sd.district_id = d.district_id
        JOIN {SILVER_SCHEMA}.questions q ON q.q_id = sa.q_id
        WHERE {_Q_FILTER} AND {_D_FILTER} AND sa.ay_id = %s
        GROUP BY d.district_id, d.district_name, d.district_state, d.sample_30_flag
        ORDER BY d.district_name
    """
    rows = run_sql(sql, (ay_id,))
    return [DistrictWithReviewStats(**row) for row in rows]


def get_state_options(ay_id: int) -> list[str]:
    """Get distinct states that have districts with predictions for a given AY."""
    sql = f"""
        SELECT DISTINCT d.district_state as state
        FROM {SILVER_SCHEMA}.v_latest_suggested_answers sa
        JOIN {BRONZE_SCHEMA}.district d ON d.district_id = sa.district_id
        JOIN {SILVER_SCHEMA}.districts sd ON sd.district_id = d.district_id
        JOIN {SILVER_SCHEMA}.questions q ON q.q_id = sa.q_id
        WHERE {_Q_FILTER} AND {_D_FILTER} AND sa.ay_id = %s AND d.district_state IS NOT NULL
        ORDER BY d.district_state
    """
    rows = run_sql(sql, (ay_id,))
    return [row["state"] for row in rows]


def get_district_info(district_id: int) -> dict:
    """Get district name and state."""
    sql = f"""
        SELECT d.district_name, d.district_state as state
        FROM {BRONZE_SCHEMA}.district d
        JOIN {SILVER_SCHEMA}.districts sd ON sd.district_id = d.district_id
        WHERE d.district_id = %s AND {_D_FILTER}
    """
    rows = run_sql(sql, (district_id,))
    if rows:
        return {"district_name": rows[0]["district_name"], "state": rows[0]["state"]}
    return {"district_name": f"District {district_id}", "state": None}
