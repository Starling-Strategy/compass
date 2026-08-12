"""Questions reference service — cross-district question stats.

Provides question listing with aggregated metrics across all districts,
and per-district breakdown for a single question.
"""

from nctqai.db import BRONZE_SCHEMA, SILVER_SCHEMA, run_sql
from nctqai.services.mc.constants import D_FILTER as _D_FILTER
from nctqai.services.mc.constants import Q_FILTER as _Q_FILTER


def get_questions_list(
    ay_id: int = 25,
    search: str | None = None,
    ans_type: str | None = None,
) -> list[dict]:
    """Get all questions with cross-district stats.

    Returns q_id, q_text, q_ans_type, districts_answered, acceptance_rate.
    """
    conditions = ["sa.ay_id = %s", _Q_FILTER, _D_FILTER]
    params: list = [ay_id]

    if search:
        conditions.append("q.q_text ILIKE %s")
        params.append(f"%{search}%")

    if ans_type:
        conditions.append("q.q_ans_type = %s")
        params.append(ans_type)

    where = " AND ".join(conditions)

    sql = f"""
        SELECT q.q_id, bsq.q_num, q.q_text, q.q_ans_type,
               COUNT(DISTINCT sa.district_id) as districts_answered,
               COUNT(*) as total_answers,
               SUM(CASE WHEN sa.status IN ('approved', 'accepted') THEN 1 ELSE 0 END) as accepted_count,
               SUM(CASE WHEN sa.status IN ('incorrect', 'rejected') THEN 1 ELSE 0 END) as rejected_count
        FROM {SILVER_SCHEMA}.questions q
        JOIN {SILVER_SCHEMA}.v_latest_suggested_answers sa ON sa.q_id = q.q_id
        JOIN {SILVER_SCHEMA}.districts sd ON sd.district_id = sa.district_id
        LEFT JOIN {BRONZE_SCHEMA}.subpolicy_question bsq ON bsq.q_id = q.q_id
        WHERE {where}
        GROUP BY q.q_id, bsq.q_num, q.q_text, q.q_ans_type
        ORDER BY q.q_id
    """
    rows = run_sql(sql, tuple(params))

    # Calculate acceptance rate
    for r in rows:
        reviewed = r["accepted_count"] + r["rejected_count"]
        r["acceptance_rate"] = round(r["accepted_count"] / reviewed * 100, 1) if reviewed > 0 else None

    return rows


def get_question_cross_district(q_id: int, ay_id: int = 25) -> list[dict]:
    """Get per-district breakdown for one question.

    Returns district_name, state, suggested_answer, agreement_pct, status, is_ina.
    """
    sql = f"""
        SELECT d.district_id, d.district_name, d.district_state as state,
               sa.suggested_answer, sa.agreement_pct, sa.status,
               sa.is_ina, sa.n_predictions
        FROM {SILVER_SCHEMA}.v_latest_suggested_answers sa
        JOIN {BRONZE_SCHEMA}.district d ON d.district_id = sa.district_id
        JOIN {SILVER_SCHEMA}.districts sd ON sd.district_id = d.district_id
        JOIN {SILVER_SCHEMA}.questions q ON q.q_id = sa.q_id
        WHERE sa.q_id = %s AND sa.ay_id = %s
          AND {_Q_FILTER} AND {_D_FILTER}
        ORDER BY d.district_name
    """
    return run_sql(sql, (q_id, ay_id))


def get_question_text(q_id: int) -> dict | None:
    """Get question text and metadata."""
    sql = f"""SELECT q.q_id, bsq.q_num, q.q_text, q.q_ans_type
              FROM {SILVER_SCHEMA}.questions q
              LEFT JOIN {BRONZE_SCHEMA}.subpolicy_question bsq ON bsq.q_id = q.q_id
              WHERE q.q_id = %s"""
    rows = run_sql(sql, (q_id,))
    return rows[0] if rows else None
