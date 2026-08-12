"""Policy-level services for the Metric Calculator.

Joins v_latest_suggested_answers -> questions -> subpolicy -> policy for policy-level
review progress.
"""

from nctqai.db import BRONZE_SCHEMA, SILVER_SCHEMA, run_sql
from nctqai.models.mc import PolicyProgress
from nctqai.services.mc.constants import Q_FILTER as _Q_FILTER


def get_policy_progress(district_id: int, ay_id: int) -> list[PolicyProgress]:
    """Get policy-level review progress for a district in a given AY.

    Join path: v_latest_suggested_answers -> questions -> subpolicy -> policy.
    """
    sql = f"""
        SELECT p.dpol_id, p.dpolicy_name as policy_name,
               COUNT(*)::int as total,
               SUM(CASE WHEN sa.status IN ('approved','accepted') THEN 1 ELSE 0 END)::int as accepted,
               SUM(CASE WHEN sa.status IN ('incorrect','rejected') THEN 1 ELSE 0 END)::int as rejected,
               SUM(CASE WHEN sa.status = 'unreviewed' THEN 1 ELSE 0 END)::int as unreviewed
        FROM {SILVER_SCHEMA}.v_latest_suggested_answers sa
        JOIN {SILVER_SCHEMA}.questions q ON q.q_id = sa.q_id
        JOIN {BRONZE_SCHEMA}.subpolicy sp ON sp.dsubpol_id = q.dsubpol_id
        JOIN {BRONZE_SCHEMA}.policy p ON p.dpol_id = sp.dpol_id
        WHERE {_Q_FILTER} AND sa.district_id = %s AND sa.ay_id = %s
        GROUP BY p.dpol_id, p.dpolicy_name
        ORDER BY p.dpolicy_name
    """
    rows = run_sql(sql, (district_id, ay_id))
    return [PolicyProgress(**row) for row in rows]


def get_policy_name(dpol_id: int) -> str:
    """Get the display name for a single policy."""
    sql = f"SELECT dpolicy_name FROM {BRONZE_SCHEMA}.policy WHERE dpol_id = %s"
    rows = run_sql(sql, (dpol_id,))
    if rows:
        return rows[0]["dpolicy_name"]
    return f"Policy {dpol_id}"


def get_policies_with_counts() -> list[dict]:
    """Get all policies with subpolicy and question counts (active high-priority only)."""
    sql = f"""
        SELECT p.dpol_id, p.dpolicy_name,
               COUNT(DISTINCT sp.dsubpol_id) as subpolicy_count,
               COUNT(DISTINCT sq.q_id) as question_count
        FROM {BRONZE_SCHEMA}.policy p
        JOIN {BRONZE_SCHEMA}.subpolicy sp ON sp.dpol_id = p.dpol_id
        JOIN {BRONZE_SCHEMA}.subpolicy_question sq ON sq.dsubpol_id = sp.dsubpol_id
        JOIN {SILVER_SCHEMA}.questions q ON q.q_id = sq.q_id
        WHERE {_Q_FILTER}
        GROUP BY p.dpol_id, p.dpolicy_name
        ORDER BY p.dpolicy_name
    """
    return run_sql(sql)


def get_policy_detail(dpol_id: int) -> dict | None:
    """Get single policy info by dpol_id."""
    sql = f"SELECT dpol_id, dpolicy_name FROM {BRONZE_SCHEMA}.policy WHERE dpol_id = %s"
    rows = run_sql(sql, (dpol_id,))
    return rows[0] if rows else None


def get_subpolicies(dpol_id: int) -> list[dict]:
    """Get subpolicies with question counts for a policy (active high-priority only)."""
    sql = f"""
        SELECT sp.dsubpol_id, sp.dsubpolicy_name,
               COUNT(DISTINCT sq.q_id) as question_count
        FROM {BRONZE_SCHEMA}.subpolicy sp
        JOIN {BRONZE_SCHEMA}.subpolicy_question sq ON sq.dsubpol_id = sp.dsubpol_id
        JOIN {SILVER_SCHEMA}.questions q ON q.q_id = sq.q_id
        WHERE sp.dpol_id = %s AND {_Q_FILTER}
        GROUP BY sp.dsubpol_id, sp.dsubpolicy_name
        ORDER BY sp.dsubpolicy_name
    """
    return run_sql(sql, (dpol_id,))
