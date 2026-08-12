"""Suggested answers / question-level services for the Metric Calculator.

Fetches questions for a district+policy combination, grouped by subpolicy.
"""

from collections import defaultdict

from nctqai.db import BRONZE_SCHEMA, SILVER_SCHEMA, run_sql
from nctqai.models.mc import QuestionRow, SubpolicyGroup
from nctqai.services.mc.constants import Q_FILTER as _Q_FILTER


def get_policy_questions(
    district_id: int,
    ay_id: int,
    dpol_id: int,
    status_filter: str | None = None,
) -> list[SubpolicyGroup]:
    """Get questions for a district+policy, grouped by subpolicy.

    Args:
        district_id: District to filter by.
        ay_id: Academic year ID.
        dpol_id: Policy ID to filter by.
        status_filter: Optional status filter ('approved', 'rejected', 'unreviewed').
    """
    where_extra = ""
    params: list = [district_id, ay_id, dpol_id]

    if status_filter and status_filter != "all":
        if status_filter in ("accepted", "approved"):
            where_extra = " AND sa.status IN ('approved', 'accepted')"
        elif status_filter in ("rejected", "incorrect"):
            where_extra = " AND sa.status IN ('incorrect', 'rejected')"
        elif status_filter == "unreviewed":
            where_extra = " AND sa.status = 'unreviewed'"
        elif status_filter == "needs_review":
            where_extra = (
                " AND sa.status NOT IN ('approved', 'accepted', 'incorrect', 'rejected')"
                f" AND NOT EXISTS (SELECT 1 FROM {SILVER_SCHEMA}.answer_holds ah"
                " WHERE ah.district_id = sa.district_id AND ah.ay_id = sa.ay_id"
                " AND ah.q_id = sa.q_id AND ah.is_active = TRUE)"
            )

    sql = f"""
        SELECT sa.q_id, bsq.q_num, q.q_text, sa.suggested_answer, sa.status,
               sa.agreement_pct, sa.n_predictions, sa.is_ina,
               sp.dsubpolicy_name as subpolicy_name,
               (ah.id IS NOT NULL) as is_on_hold
        FROM {SILVER_SCHEMA}.v_latest_suggested_answers sa
        JOIN {SILVER_SCHEMA}.questions q ON q.q_id = sa.q_id
        LEFT JOIN {BRONZE_SCHEMA}.subpolicy_question bsq ON bsq.q_id = q.q_id
        LEFT JOIN {BRONZE_SCHEMA}.question bq ON bq.q_id = q.q_id
        JOIN {BRONZE_SCHEMA}.subpolicy sp ON sp.dsubpol_id = q.dsubpol_id
        LEFT JOIN {SILVER_SCHEMA}.answer_holds ah
            ON ah.district_id = sa.district_id AND ah.ay_id = sa.ay_id
            AND ah.q_id = sa.q_id AND ah.is_active = TRUE
        WHERE sa.district_id = %s AND sa.ay_id = %s AND sp.dpol_id = %s
          AND {_Q_FILTER}
          {where_extra}
        ORDER BY sp.dsubpolicy_sort, bq.q_sort, sa.q_id
    """
    rows = run_sql(sql, tuple(params))

    # Group into SubpolicyGroup objects
    groups: dict[str, list[QuestionRow]] = defaultdict(list)
    for row in rows:
        subpol = row["subpolicy_name"] or "Uncategorized"
        groups[subpol].append(
            QuestionRow(
                q_id=row["q_id"],
                q_num=row["q_num"],
                q_text=row["q_text"],
                suggested_answer=row["suggested_answer"],
                status=row["status"] or "unreviewed",
                agreement_pct=float(row["agreement_pct"]) if row["agreement_pct"] is not None else None,
                n_predictions=int(row["n_predictions"]) if row["n_predictions"] is not None else None,
                is_ina=bool(row["is_ina"]) if row["is_ina"] is not None else False,
                is_on_hold=bool(row.get("is_on_hold")),
            )
        )

    return [
        SubpolicyGroup(subpolicy_name=name, questions=qs)
        for name, qs in groups.items()
    ]


def get_policy_question_counts(district_id: int, ay_id: int, dpol_id: int) -> dict:
    """Get question counts per status for a district+policy (unfiltered)."""
    sql = f"""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN sa.status IN ('approved','accepted') THEN 1 ELSE 0 END)::int as accepted,
            SUM(CASE WHEN sa.status IN ('incorrect','rejected') THEN 1 ELSE 0 END)::int as rejected,
            SUM(CASE WHEN sa.status = 'unreviewed' THEN 1 ELSE 0 END)::int as unreviewed,
            SUM(CASE WHEN sa.status NOT IN ('approved', 'accepted', 'incorrect', 'rejected')
                      AND NOT EXISTS (SELECT 1 FROM {SILVER_SCHEMA}.answer_holds ah
                          WHERE ah.district_id = sa.district_id AND ah.ay_id = sa.ay_id
                          AND ah.q_id = sa.q_id AND ah.is_active = TRUE)
                 THEN 1 ELSE 0 END)::int as needs_review
        FROM {SILVER_SCHEMA}.v_latest_suggested_answers sa
        JOIN {SILVER_SCHEMA}.questions q ON q.q_id = sa.q_id
        JOIN {BRONZE_SCHEMA}.subpolicy sp ON sp.dsubpol_id = q.dsubpol_id
        WHERE {_Q_FILTER}
          AND sa.district_id = %s AND sa.ay_id = %s AND sp.dpol_id = %s
    """
    rows = run_sql(sql, (district_id, ay_id, dpol_id))
    if rows:
        return {k: (v or 0) for k, v in rows[0].items()}
    return {"total": 0, "accepted": 0, "rejected": 0, "unreviewed": 0, "needs_review": 0}


def get_subtopic_counts(
    district_id: int, ay_id: int, dpol_id: int
) -> dict[str, dict]:
    """Get review counts per subtopic for progress indicators.

    Returns dict keyed by subpolicy_name with values:
        {"total": int, "reviewed": int, "accepted": int, "rejected": int}
    """
    sql = f"""
        SELECT sp.dsubpolicy_name as subpolicy_name,
               COUNT(*) as total,
               COUNT(*) FILTER (WHERE sa.status IN ('approved','accepted','incorrect','rejected')) as reviewed,
               COUNT(*) FILTER (WHERE sa.status IN ('approved','accepted')) as accepted,
               COUNT(*) FILTER (WHERE sa.status IN ('incorrect','rejected')) as rejected
        FROM {SILVER_SCHEMA}.v_latest_suggested_answers sa
        JOIN {SILVER_SCHEMA}.questions q ON q.q_id = sa.q_id
        JOIN {BRONZE_SCHEMA}.subpolicy sp ON sp.dsubpol_id = q.dsubpol_id
        WHERE sa.district_id = %s AND sa.ay_id = %s AND sp.dpol_id = %s
          AND {_Q_FILTER}
        GROUP BY sp.dsubpolicy_name, sp.dsubpolicy_sort
        ORDER BY sp.dsubpolicy_sort
    """
    rows = run_sql(sql, (district_id, ay_id, dpol_id))
    return {
        (row["subpolicy_name"] or "Uncategorized"): {
            "total": row["total"] or 0,
            "reviewed": row["reviewed"] or 0,
            "accepted": row["accepted"] or 0,
            "rejected": row["rejected"] or 0,
        }
        for row in rows
    }
