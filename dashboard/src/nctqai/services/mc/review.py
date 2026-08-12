"""Review services for the Metric Calculator.

Handles loading full question review context and writing approve/reject decisions.
Reads use v_latest_suggested_answers (source filtering baked in).
Writes target the real silver.suggested_answers table with explicit source filter.
"""

import json
import logging
import re

from nctqai.db import BRONZE_SCHEMA, SILVER_SCHEMA, run_sql, run_sql_write
from nctqai.models.mc import AnswerHold, QuestionReviewData
from nctqai.services.mc.constants import D_FILTER as _D_FILTER
from nctqai.services.mc.constants import Q_FILTER as _Q_FILTER
from nctqai.services.mc.constants import SOURCE_FILTER_BARE as _SOURCE_FILTER_BARE

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _log_review_action(
    district_id: int,
    ay_id: int,
    q_id: int,
    action: str,
    reviewed_by: str,
    rejection_reason: str | None = None,
    decision_note: str | None = None,
    footnote: str | None = None,
    page_ref: str | None = None,
) -> None:
    """Append to audit log. Failures are logged but don't break the review."""
    try:
        run_sql_write(
            f"""INSERT INTO {SILVER_SCHEMA}.review_audit_log
               (district_id, ay_id, q_id, action, reviewed_by,
                rejection_reason, decision_note, footnote, page_ref)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (district_id, ay_id, q_id, action, reviewed_by,
             rejection_reason, decision_note, footnote, page_ref),
        )
    except Exception:
        logger.exception("Failed to write audit log for %s/%s/%s", district_id, ay_id, q_id)


def _enrich_citations(citations):
    """Add display metadata to citations by looking up district_documents.

    Citations may have UUID doc_ids (newer predictions) or filename doc_ids
    (older predictions). For non-UUID doc_ids, we use the citation's own
    doc_name field to look up the document.
    """
    if not citations or not isinstance(citations, list):
        return citations

    # Collect lookup keys: UUID doc_ids and doc_name values for non-UUID cites
    uuid_ids = []
    doc_names = []
    for c in citations:
        if not isinstance(c, dict):
            continue
        doc_id = str(c.get("doc_id", ""))
        if _UUID_RE.match(doc_id):
            uuid_ids.append(doc_id)
        elif c.get("doc_name"):
            doc_names.append(c["doc_name"])

    if not uuid_ids and not doc_names:
        return citations

    doc_meta = {}

    # Look up by UUID
    if uuid_ids:
        unique = list(set(uuid_ids))
        ph = ",".join(["%s"] * len(unique))
        rows = run_sql(
            f"""SELECT dd.doc_id::text, dd.src_id, dd.doc_name, dd.src_type,
                       dd.ai_document_type, dd.ai_ay_ids, dd.human_ay_ids,
                       dd.ai_title, ds.src_disp_name
                FROM {SILVER_SCHEMA}.district_documents dd
                LEFT JOIN {BRONZE_SCHEMA}.district_sources ds ON ds.src_id = dd.src_id
                WHERE dd.doc_id::text IN ({ph})""",
            tuple(unique),
        )
        doc_meta.update({r["doc_id"]: r for r in rows})

    # Look up by doc_name for non-UUID citations
    if doc_names:
        unique = list(set(doc_names))
        ph = ",".join(["%s"] * len(unique))
        rows = run_sql(
            f"""SELECT dd.doc_id::text, dd.src_id, dd.doc_name, dd.src_type,
                       dd.ai_document_type, dd.ai_ay_ids, dd.human_ay_ids,
                       dd.ai_title, ds.src_disp_name
                FROM {SILVER_SCHEMA}.district_documents dd
                LEFT JOIN {BRONZE_SCHEMA}.district_sources ds ON ds.src_id = dd.src_id
                WHERE dd.doc_name IN ({ph})""",
            tuple(unique),
        )
        # Key by doc_name so we can look up from citation's doc_name field
        doc_meta.update({r["doc_name"]: r for r in rows})

    for cite in citations:
        if isinstance(cite, dict):
            doc_id = str(cite.get("doc_id", ""))
            # Try UUID match first, then doc_name match
            meta = doc_meta.get(doc_id) or doc_meta.get(cite.get("doc_name", ""))
            if meta:
                if meta.get("doc_name"):
                    cite["_doc_filename"] = meta["doc_name"]
                # _src_name is the original NCTQ-provided display title (src_disp_name)
                # from bronze.district_sources — e.g. "Contract - BTU CBA 2024-2025 (File, ID 20681)"
                if meta.get("src_disp_name"):
                    cite["_src_name"] = meta["src_disp_name"]
                if meta.get("ai_title"):
                    cite["_ai_title"] = meta["ai_title"]
                if meta.get("ai_document_type"):
                    cite["_doc_type"] = meta["ai_document_type"]
                if meta.get("src_id"):
                    cite["_src_id"] = meta["src_id"]
                if meta.get("src_type"):
                    cite["_src_type"] = meta["src_type"]
                ay_ids = meta.get("human_ay_ids") or meta.get("ai_ay_ids")
                if ay_ids:
                    cite["_doc_ay_ids"] = list(ay_ids) if not isinstance(ay_ids, list) else ay_ids

    return citations


def get_active_hold(district_id: int, ay_id: int, q_id: int) -> AnswerHold | None:
    """Return the active hold for an answer, or None."""
    rows = run_sql(
        f"""SELECT district_id, ay_id, q_id, held_by, hold_reason,
                  held_at::text as held_at
           FROM {SILVER_SCHEMA}.answer_holds
           WHERE district_id = %s AND ay_id = %s AND q_id = %s AND is_active = TRUE""",
        (district_id, ay_id, q_id),
    )
    if not rows:
        return None
    return AnswerHold(**rows[0])


def place_hold(
    district_id: int, ay_id: int, q_id: int, held_by: str, hold_reason: str
) -> bool:
    """Place a quality hold on an answer. Returns True if inserted (not a duplicate)."""
    count = run_sql_write(
        f"""INSERT INTO {SILVER_SCHEMA}.answer_holds (district_id, ay_id, q_id, held_by, hold_reason)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (district_id, ay_id, q_id) WHERE is_active = TRUE
           DO NOTHING""",
        (district_id, ay_id, q_id, held_by, hold_reason),
    )
    if count > 0:
        _log_review_action(district_id, ay_id, q_id, "hold_placed", held_by,
                           decision_note=hold_reason)
    return count > 0


def release_hold(district_id: int, ay_id: int, q_id: int, released_by: str) -> bool:
    """Release the active hold on an answer. Returns True if a hold was released."""
    count = run_sql_write(
        f"""UPDATE {SILVER_SCHEMA}.answer_holds
           SET is_active = FALSE, released_by = %s, released_at = NOW()
           WHERE district_id = %s AND ay_id = %s AND q_id = %s AND is_active = TRUE""",
        (released_by, district_id, ay_id, q_id),
    )
    if count > 0:
        _log_review_action(district_id, ay_id, q_id, "hold_released", released_by)
    return count > 0


def is_answer_held(district_id: int, ay_id: int, q_id: int) -> bool:
    """Quick check whether an answer has an active hold."""
    rows = run_sql(
        f"""SELECT 1 FROM {SILVER_SCHEMA}.answer_holds
           WHERE district_id = %s AND ay_id = %s AND q_id = %s AND is_active = TRUE""",
        (district_id, ay_id, q_id),
    )
    return bool(rows)


def get_question_review(
    district_id: int, ay_id: int, q_id: int
) -> QuestionReviewData | None:
    """Load full question review context from the database.

    Joins suggested_answers with questions, district, subpolicy, and policy
    to build the complete review page data. Also calculates prev/next
    question IDs for navigation within the same policy.
    """
    sql = f"""
        SELECT sa.q_id, bsq.q_num, sa.district_id, sa.ay_id, sa.run_id,
               d.district_name, d.district_state as state,
               q.q_text, q.q_ans_type, q.effective_valid_options as valid_options,
               sa.suggested_answer, sa.confidence, sa.reasoning,
               sa.is_ina, sa.citations_json,
               sa.entropy, sa.agreement_pct,
               sa.n_unique_answers, sa.n_predictions,
               sa.footnote, sa.page_ref,
               sa.status, sa.rejection_reason, sa.decision_note,
               sa.reviewed_by, sa.reviewed_at::text as reviewed_at,
               sa.created_at::text as predicted_at,
               sp.dsubpolicy_name as subpolicy_name, sp.dsubpol_id,
               p.dpolicy_name as policy_name, p.dpol_id
        FROM {SILVER_SCHEMA}.v_latest_suggested_answers sa
        JOIN {SILVER_SCHEMA}.questions q ON q.q_id = sa.q_id
        LEFT JOIN {BRONZE_SCHEMA}.subpolicy_question bsq ON bsq.q_id = q.q_id
        JOIN {BRONZE_SCHEMA}.district d ON d.district_id = sa.district_id
        JOIN {SILVER_SCHEMA}.districts sd ON sd.district_id = d.district_id
        JOIN {BRONZE_SCHEMA}.subpolicy sp ON sp.dsubpol_id = q.dsubpol_id
        JOIN {BRONZE_SCHEMA}.policy p ON p.dpol_id = sp.dpol_id
        WHERE sa.district_id = %s AND sa.ay_id = %s AND sa.q_id = %s
          AND {_Q_FILTER} AND {_D_FILTER}
    """
    rows = run_sql(sql, (district_id, ay_id, q_id))
    if not rows:
        return None

    row = rows[0]
    dpol_id = row["dpol_id"]

    # Parse citations_json and enrich with ai_title from district_documents
    citations = row.get("citations_json")
    if isinstance(citations, str):
        try:
            citations = json.loads(citations)
        except (json.JSONDecodeError, TypeError):
            citations = None
    citations = _enrich_citations(citations)

    # Parse valid_options
    valid_options = row.get("valid_options")
    if isinstance(valid_options, str):
        try:
            valid_options = json.loads(valid_options)
        except (json.JSONDecodeError, TypeError):
            valid_options = None

    # Get all q_ids in same policy for this district/ay (for prev/next nav)
    nav_sql = f"""
        SELECT sa.q_id
        FROM {SILVER_SCHEMA}.v_latest_suggested_answers sa
        JOIN {SILVER_SCHEMA}.districts sd ON sd.district_id = sa.district_id
        JOIN {SILVER_SCHEMA}.questions q ON q.q_id = sa.q_id
        JOIN {BRONZE_SCHEMA}.subpolicy sp ON sp.dsubpol_id = q.dsubpol_id
        WHERE sa.district_id = %s AND sa.ay_id = %s AND sp.dpol_id = %s
          AND {_Q_FILTER} AND {_D_FILTER}
        ORDER BY sp.dsubpolicy_name, sa.q_id
    """
    nav_rows = run_sql(nav_sql, (district_id, ay_id, dpol_id))
    q_ids = [r["q_id"] for r in nav_rows]

    prev_q_id = None
    next_q_id = None
    position = None
    total_in_policy = len(q_ids)

    if q_id in q_ids:
        idx = q_ids.index(q_id)
        position = idx + 1
        if idx > 0:
            prev_q_id = q_ids[idx - 1]
        if idx < len(q_ids) - 1:
            next_q_id = q_ids[idx + 1]

    # Check for active hold
    hold = get_active_hold(district_id, ay_id, q_id)

    return QuestionReviewData(
        district_id=row["district_id"],
        ay_id=row["ay_id"],
        q_id=row["q_id"],
        q_num=row["q_num"],
        district_name=row["district_name"],
        state=row["state"],
        policy_name=row["policy_name"],
        subpolicy_name=row["subpolicy_name"],
        dpol_id=dpol_id,
        q_text=row["q_text"],
        q_ans_type=row["q_ans_type"],
        valid_options=valid_options,
        suggested_answer=row["suggested_answer"] or "No answer",
        confidence=float(row["confidence"]) if row["confidence"] is not None else 0,
        reasoning=row["reasoning"],
        is_ina=bool(row["is_ina"]) if row["is_ina"] is not None else False,
        citations_json=citations,
        entropy=float(row["entropy"]) if row["entropy"] is not None else 0,
        agreement_pct=float(row["agreement_pct"]) if row["agreement_pct"] is not None else 0,
        n_unique_answers=int(row["n_unique_answers"]) if row["n_unique_answers"] is not None else 0,
        n_predictions=int(row["n_predictions"]) if row["n_predictions"] is not None else 0,
        footnote=row.get("footnote"),
        page_ref=row.get("page_ref"),
        status=row["status"] or "unreviewed",
        rejection_reason=row["rejection_reason"],
        decision_note=row["decision_note"],
        reviewed_by=row["reviewed_by"],
        reviewed_at=row["reviewed_at"],
        predicted_at=row["predicted_at"],
        run_id=row["run_id"],
        hold=hold,
        prev_q_id=prev_q_id,
        next_q_id=next_q_id,
        position=position,
        total_in_policy=total_in_policy,
    )


def approve_answer(
    district_id: int,
    ay_id: int,
    q_id: int,
    reviewed_by: str,
    decision_note: str | None = None,
    footnote: str | None = None,
    page_ref: str | None = None,
    run_id: str | None = None,
) -> bool:
    """Mark a suggested answer as approved.

    Returns True if a row was updated.
    """
    sql = f"""
        UPDATE {SILVER_SCHEMA}.suggested_answers
        SET status = 'approved', reviewed_by = %s, reviewed_at = NOW(),
            decision_note = %s, footnote = %s, page_ref = %s
        WHERE district_id = %s AND ay_id = %s AND q_id = %s AND {_SOURCE_FILTER_BARE}
    """
    params: list = [reviewed_by, decision_note, footnote, page_ref, district_id, ay_id, q_id]
    if run_id:
        sql += " AND run_id = %s"
        params.append(run_id)
    count = run_sql_write(sql, tuple(params))
    if count > 0:
        _log_review_action(
            district_id, ay_id, q_id, "approved", reviewed_by,
            decision_note=decision_note, footnote=footnote, page_ref=page_ref,
        )
    return count > 0


def reject_answer(
    district_id: int,
    ay_id: int,
    q_id: int,
    reviewed_by: str,
    rejection_reason: str,
    decision_note: str | None = None,
    footnote: str | None = None,
    page_ref: str | None = None,
    run_id: str | None = None,
) -> bool:
    """Mark a suggested answer as rejected (incorrect).

    Returns True if a row was updated.
    """
    sql = f"""
        UPDATE {SILVER_SCHEMA}.suggested_answers
        SET status = 'incorrect', reviewed_by = %s, reviewed_at = NOW(),
            rejection_reason = %s, decision_note = %s, footnote = %s, page_ref = %s
        WHERE district_id = %s AND ay_id = %s AND q_id = %s AND {_SOURCE_FILTER_BARE}
    """
    params: list = [reviewed_by, rejection_reason, decision_note, footnote, page_ref, district_id, ay_id, q_id]
    if run_id:
        sql += " AND run_id = %s"
        params.append(run_id)
    count = run_sql_write(sql, tuple(params))
    if count > 0:
        _log_review_action(
            district_id, ay_id, q_id, "incorrect", reviewed_by,
            rejection_reason=rejection_reason, decision_note=decision_note,
            footnote=footnote, page_ref=page_ref,
        )
    return count > 0


def reset_answer(district_id: int, ay_id: int, q_id: int, reviewed_by: str, run_id: str | None = None) -> bool:
    """Reset a suggested answer back to unreviewed state.

    Preserves who performed the reset and when for audit purposes.
    Returns True if a row was updated.
    """
    decision_note = f"Reset by {reviewed_by}"
    sql = f"""
        UPDATE {SILVER_SCHEMA}.suggested_answers
        SET status = 'unreviewed', reviewed_by = %s, reviewed_at = NOW(),
            rejection_reason = NULL, decision_note = %s,
            footnote = NULL, page_ref = NULL
        WHERE district_id = %s AND ay_id = %s AND q_id = %s AND {_SOURCE_FILTER_BARE}
    """
    params: list = [reviewed_by, decision_note, district_id, ay_id, q_id]
    if run_id:
        sql += " AND run_id = %s"
        params.append(run_id)
    count = run_sql_write(sql, tuple(params))
    if count > 0:
        _log_review_action(
            district_id, ay_id, q_id, "unreviewed", reviewed_by,
            decision_note=decision_note,
        )
    return count > 0


def update_note(
    district_id: int,
    ay_id: int,
    q_id: int,
    reviewed_by: str,
    decision_note: str | None = None,
    footnote: str | None = None,
    page_ref: str | None = None,
    run_id: str | None = None,
) -> bool:
    """Update the note fields on an already-reviewed answer without changing status.

    Allows analysts to edit their comment after accepting or rejecting without
    needing to reset the review decision. Returns True if a row was updated.
    """
    sql = f"""
        UPDATE {SILVER_SCHEMA}.suggested_answers
        SET decision_note = %s, footnote = %s, page_ref = %s
        WHERE district_id = %s AND ay_id = %s AND q_id = %s
          AND status IN ('approved', 'incorrect')
          AND {_SOURCE_FILTER_BARE}
    """
    params: list = [decision_note, footnote, page_ref, district_id, ay_id, q_id]
    if run_id:
        sql += " AND run_id = %s"
        params.append(run_id)
    count = run_sql_write(sql, tuple(params))
    if count > 0:
        _log_review_action(
            district_id, ay_id, q_id, "note_updated", reviewed_by,
            decision_note=decision_note, footnote=footnote, page_ref=page_ref,
        )
    return count > 0
