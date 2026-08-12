"""Document data services for the NCTQ.ai dashboard.

All SQL for /docs and /docs/{src_id} pages lives here.
Only shows documents processed by the docpipe pipeline (source_pipeline = 'docpipe').

Ported from document_pipeline.dashboard.services.documents — adapted to use nctqai.db.
"""

from nctqai.db import BRONZE_SCHEMA, SILVER_SCHEMA, run_sql
from nctqai.models.docs import DocumentDetail, DocumentStats, DocumentSummary
from nctqai.services.mc.constants import D_FILTER as _D_FILTER

# Only show docs processed by our pipeline
_PIPELINE_FILTER = "dd.source_pipeline = 'docpipe'"


def get_document_stats(district_id: int | None = None, ay: int | None = None, doc_type: str | None = None) -> DocumentStats:
    """Aggregate stats for the KPI health cards row."""
    where = f"WHERE {_PIPELINE_FILTER}"
    params: list = []
    if district_id:
        where += " AND dd.district_id = %s"
        params.append(district_id)
    if ay:
        where += " AND COALESCE(dd.human_ay_ids, dd.ai_ay_ids) && ARRAY[%s]::int[]"
        params.append(ay)
    if doc_type:
        where += " AND COALESCE(dd.ai_document_type, dd.src_type) = %s"
        params.append(doc_type)

    sql = f"""
        SELECT
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE dd.extraction_status = 'success') as success,
            COUNT(*) FILTER (WHERE dd.extraction_status = 'failed') as failed,
            COUNT(*) FILTER (WHERE dd.extraction_status = 'pending' OR dd.extraction_status IS NULL) as pending,
            AVG(dd.ai_confidence) FILTER (WHERE dd.ai_confidence IS NOT NULL) as avg_confidence,
            COUNT(*) FILTER (WHERE dd.ai_readability = 'good') as readability_good,
            COUNT(*) FILTER (WHERE dd.ai_readability = 'fair') as readability_fair,
            COUNT(*) FILTER (WHERE dd.ai_readability = 'poor') as readability_poor,
            COUNT(*) FILTER (WHERE dd.human_ay_ids IS NOT NULL AND dd.ai_ay_ids IS NOT NULL
                             AND dd.human_ay_ids = dd.ai_ay_ids) as ay_exact_match,
            COUNT(*) FILTER (WHERE dd.ai_ay_ids IS NOT NULL AND dd.ai_ay_ids != '{{}}') as ay_total_enriched,
            COUNT(*) FILTER (WHERE dd.src_type = dd.ai_document_type) as type_agree,
            COUNT(*) FILTER (WHERE dd.ai_document_type IS NOT NULL) as type_total
        FROM {SILVER_SCHEMA}.district_documents dd
        {where}
    """
    rows = run_sql(sql, tuple(params))
    if not rows:
        return DocumentStats()

    row = rows[0]
    return DocumentStats(
        total=int(row["total"]),
        success=int(row["success"]),
        failed=int(row["failed"]),
        pending=int(row["pending"]),
        avg_confidence=float(row["avg_confidence"]) if row["avg_confidence"] is not None else None,
        readability_good=int(row["readability_good"]),
        readability_fair=int(row["readability_fair"]),
        readability_poor=int(row["readability_poor"]),
        ay_exact_match=int(row["ay_exact_match"]),
        ay_total_enriched=int(row["ay_total_enriched"]),
        type_agree=int(row["type_agree"]),
        type_total=int(row["type_total"]),
    )


def get_documents(
    district_id: int | None = None,
    status: str | None = None,
    search: str | None = None,
    sort: str = "src_id_desc",
    ay: int | None = None,
    doc_type: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[DocumentSummary]:
    """Get document list for the table on /docs."""
    where = f"WHERE {_PIPELINE_FILTER}"
    params: list = []

    if district_id:
        where += " AND dd.district_id = %s"
        params.append(district_id)
    if ay:
        where += " AND COALESCE(dd.human_ay_ids, dd.ai_ay_ids) && ARRAY[%s]::int[]"
        params.append(ay)
    if status and status != "all":
        where += " AND dd.extraction_status = %s"
        params.append(status)
    if doc_type:
        where += " AND COALESCE(dd.ai_document_type, dd.src_type) = %s"
        params.append(doc_type)
    if search:
        where += " AND (dd.ai_title ILIKE %s OR dd.doc_name ILIKE %s)"
        params.extend([f"%{search}%", f"%{search}%"])

    sort_map = {
        "src_id_desc": "dd.src_id DESC",
        "src_id_asc": "dd.src_id ASC",
        "district": "d.district_name ASC, dd.src_id DESC",
        "confidence_desc": "dd.ai_confidence DESC NULLS LAST",
        "confidence_asc": "dd.ai_confidence ASC NULLS LAST",
        "text_length_desc": "dd.text_length DESC",
        "title": "dd.ai_title ASC NULLS LAST",
    }
    order = sort_map.get(sort, "dd.src_id DESC")

    sql = f"""
        SELECT
            dd.src_id,
            dd.district_id,
            d.district_name,
            dd.ai_title,
            dd.doc_name as src_name,
            dd.src_type,
            dd.ai_document_type,
            dd.human_ay_ids,
            dd.ai_ay_ids,
            dd.ai_readability,
            dd.ai_confidence,
            dd.text_length,
            dd.extraction_status
        FROM {SILVER_SCHEMA}.district_documents dd
        JOIN {BRONZE_SCHEMA}.district d ON dd.district_id = d.district_id
        JOIN {SILVER_SCHEMA}.districts sd ON sd.district_id = dd.district_id
        {where} AND {_D_FILTER}
        ORDER BY {order}
        LIMIT %s OFFSET %s
    """
    params.extend([limit, offset])

    rows = run_sql(sql, tuple(params))

    results = []
    for row in rows:
        results.append(
            DocumentSummary(
                src_id=row["src_id"],
                district_id=row["district_id"],
                district_name=row["district_name"],
                ai_title=row["ai_title"],
                src_name=row["src_name"] or "",
                src_type=row["src_type"],
                ai_document_type=row["ai_document_type"],
                human_ay_ids=list(row["human_ay_ids"]) if row["human_ay_ids"] else [],
                ai_ay_ids=list(row["ai_ay_ids"]) if row["ai_ay_ids"] else None,
                ai_readability=row["ai_readability"],
                ai_confidence=float(row["ai_confidence"]) if row["ai_confidence"] is not None else None,
                text_length=int(row["text_length"] or 0),
                extraction_status=row["extraction_status"],
            )
        )
    return results


def get_document_detail(src_id: int) -> DocumentDetail | None:
    """Get full document detail for the /docs/{src_id} detail page."""
    sql = f"""
        SELECT
            dd.src_id,
            dd.district_id,
            d.district_name,
            dd.doc_name as src_name,
            dd.src_link,
            dd.src_type,
            dd.valid_from::text,
            dd.valid_to::text,
            dd.extraction_status,
            dd.extraction_error,
            dd.full_text,
            dd.text_length,
            dd.text_hash,
            dd.human_ay_ids,
            dd.ai_ay_ids,
            dd.effective_ay_ids,
            dd.ai_title,
            dd.ai_summary,
            dd.ai_document_type,
            dd.ai_temporal_class,
            dd.ai_readability,
            dd.ai_confidence
        FROM {SILVER_SCHEMA}.district_documents dd
        JOIN {BRONZE_SCHEMA}.district d ON dd.district_id = d.district_id
        JOIN {SILVER_SCHEMA}.districts sd ON sd.district_id = dd.district_id
        WHERE dd.src_id = %s AND {_D_FILTER}
    """
    rows = run_sql(sql, (src_id,))
    if not rows:
        return None

    row = rows[0]
    return DocumentDetail(
        src_id=row["src_id"],
        district_id=row["district_id"],
        district_name=row["district_name"],
        src_name=row["src_name"] or "",
        src_link=row["src_link"] or "",
        src_type=row["src_type"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        extraction_status=row["extraction_status"],
        extraction_error=row["extraction_error"],
        full_text=row["full_text"],
        text_length=int(row["text_length"] or 0),
        text_hash=row["text_hash"],
        human_ay_ids=list(row["human_ay_ids"]) if row["human_ay_ids"] else [],
        ai_ay_ids=list(row["ai_ay_ids"]) if row["ai_ay_ids"] else None,
        effective_ay_ids=list(row["effective_ay_ids"]) if row["effective_ay_ids"] else [],
        ai_title=row["ai_title"],
        ai_summary=row["ai_summary"],
        ai_document_type=row["ai_document_type"],
        ai_temporal_class=row["ai_temporal_class"],
        ai_readability=row["ai_readability"],
        ai_confidence=float(row["ai_confidence"]) if row["ai_confidence"] is not None else None,
    )


def get_district_options() -> list[tuple[int, str]]:
    """Get (district_id, name) pairs for the filter dropdown.

    Only includes districts that have docs processed by the docpipe pipeline.
    """
    sql = f"""
        SELECT DISTINCT dd.district_id, d.district_name
        FROM {SILVER_SCHEMA}.district_documents dd
        JOIN {BRONZE_SCHEMA}.district d ON dd.district_id = d.district_id
        JOIN {SILVER_SCHEMA}.districts sd ON sd.district_id = dd.district_id
        WHERE {_PIPELINE_FILTER} AND {_D_FILTER}
        ORDER BY d.district_name
    """
    rows = run_sql(sql, ())
    return [(int(row["district_id"]), row["district_name"]) for row in rows]


def get_doc_type_options() -> list[str]:
    """Get distinct document types for the filter dropdown.

    Uses COALESCE(ai_document_type, src_type) to match what the analyst sees.
    Returns sorted alphabetically.
    """
    sql = f"""
        SELECT DISTINCT COALESCE(dd.ai_document_type, dd.src_type) AS doc_type
        FROM {SILVER_SCHEMA}.district_documents dd
        WHERE {_PIPELINE_FILTER}
          AND COALESCE(dd.ai_document_type, dd.src_type) IS NOT NULL
        ORDER BY doc_type
    """
    rows = run_sql(sql, ())
    return [row["doc_type"] for row in rows]


def get_ay_options() -> list[int]:
    """Get distinct effective AY values from documents for the filter dropdown.

    Uses COALESCE(human_ay_ids, ai_ay_ids) to match what the analyst sees.
    Returns sorted descending (most recent first).
    """
    sql = f"""
        SELECT DISTINCT unnest(COALESCE(dd.human_ay_ids, dd.ai_ay_ids)) AS ay
        FROM {SILVER_SCHEMA}.district_documents dd
        WHERE {_PIPELINE_FILTER}
          AND COALESCE(dd.human_ay_ids, dd.ai_ay_ids) IS NOT NULL
        ORDER BY ay DESC
    """
    rows = run_sql(sql, ())
    return [int(row["ay"]) for row in rows]
