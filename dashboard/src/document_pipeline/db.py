"""All database operations for the document pipeline.

Uses psycopg2 for PostgreSQL connections.
All SQL lives in this file — nowhere else in the pipeline.
"""

import logging

import psycopg2
import psycopg2.extras

from document_pipeline.config import Config
from document_pipeline.models import Document, normalize_bronze_doc_type

logger = logging.getLogger(__name__)


def get_pg_connection(config: Config):
    """Get a PostgreSQL connection from config."""
    return psycopg2.connect(
        host=config.pg_host,
        port=config.pg_port,
        database=config.pg_database,
        user=config.pg_user,
        password=config.pg_password,
    )




def load_sources(
    config: Config,
    district_id: int | None = None,
    limit: int = 0,
    process_all: bool = False,
    new_only: bool = False,
) -> list[Document]:
    """Load documents from bronze.district_sources with human academic year mappings.

    Args:
        config: Pipeline configuration (for DB connection).
        district_id: Filter to one district (required unless process_all=True)
        limit: Max documents (0 = no limit)
        process_all: Process all districts
        new_only: Only return sources not yet in silver.district_documents

    Returns:
        List of Document objects ready for processing.
    """
    if not district_id and not process_all:
        raise ValueError("Provide --district or --all")

    params = []

    where = ""
    if district_id:
        where = "AND bs.district_id = %s"
        params.append(district_id)

    new_only_join = ""
    new_only_filter = ""
    if new_only:
        new_only_join = "LEFT JOIN silver.district_documents sd ON bs.src_id = sd.src_id"
        new_only_filter = "AND sd.src_id IS NULL"

    limit_clause = ""
    if limit > 0:
        limit_clause = "LIMIT %s"
        params.append(limit)

    sql = f"""
        SELECT
            bs.src_id,
            bs.district_id::INTEGER as district_id,
            d.district_name,
            bs.src_name,
            bs.src_link,
            bs.src_type,
            bs.src_valid_from::DATE as valid_from,
            bs.src_valid_to::DATE as valid_to,
            COALESCE(
                ARRAY_AGG(DISTINCT sy.ay_id::INTEGER ORDER BY sy.ay_id::INTEGER)
                FILTER (WHERE sy.ay_id IS NOT NULL),
                ARRAY[]::INTEGER[]
            ) as human_ay_ids
        FROM bronze.district_sources bs
        JOIN bronze.district d ON bs.district_id = d.district_id
        LEFT JOIN bronze.district_source_yrs sy ON bs.src_id = sy.src_id
        {new_only_join}
        WHERE bs.src_link IS NOT NULL
          AND bs.src_link != ''
          {where}
          {new_only_filter}
        GROUP BY bs.src_id, bs.district_id, d.district_name,
                 bs.src_name, bs.src_link, bs.src_type,
                 bs.src_valid_from, bs.src_valid_to
        ORDER BY bs.src_id
        {limit_clause}
    """
    with get_pg_connection(config) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    if not rows:
        return []

    documents = []
    for row in rows:
        documents.append(
            Document(
                src_id=row["src_id"],
                district_id=row["district_id"],
                district_name=row["district_name"],
                src_name=row["src_name"] or "",
                src_link=row["src_link"],
                src_type=normalize_bronze_doc_type(row["src_type"]),
                valid_from=row["valid_from"],
                valid_to=row["valid_to"],
                human_ay_ids=list(row["human_ay_ids"]) if row["human_ay_ids"] else [],
            )
        )
    return documents


def get_reprocess_src_ids(config: Config, district_id: int | None = None) -> set[int]:
    """Get src_ids of documents that need (re)processing.

    Retriable statuses:
        failed, ocr_failed  - generic / OCR failures worth retrying
        timeout             - transient network issue, always worth retrying
        blocked             - 403; may succeed with different headers/timing
        corrupted           - re-fetch may get a fixed version of the file
    Non-retriable (excluded):
        dead_link, format_unsupported, test_record — not worth retrying
    Also includes success docs without AI enrichment.
    """
    params = []
    where = """WHERE src_id IS NOT NULL AND (
        extraction_status IN ('failed', 'ocr_failed', 'timeout', 'blocked', 'corrupted')
        OR (extraction_status = 'success' AND (ai_confidence IS NULL OR ai_confidence = 0))
    )"""
    if district_id:
        where += " AND district_id = %s"
        params.append(district_id)
    sql = f"SELECT src_id FROM silver.district_documents {where}"
    with get_pg_connection(config) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return {row[0] for row in cur.fetchall()}


def get_processed_src_ids(config: Config, district_id: int | None = None) -> set[int]:
    """Get src_ids of documents already successfully processed.

    A document is considered 'done' when it has:
      - extraction_status = 'success'
      - ai_confidence > 0 (enrichment completed)
    """
    params = []
    where = """WHERE source_pipeline = 'docpipe'
        AND extraction_status = 'success'
        AND ai_confidence IS NOT NULL AND ai_confidence > 0"""
    if district_id:
        where += " AND district_id = %s"
        params.append(district_id)
    sql = f"SELECT src_id FROM silver.district_documents {where}"
    with get_pg_connection(config) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return {row[0] for row in cur.fetchall()}


def save_document(doc: Document, config: Config) -> None:
    """Upsert a processed document into silver.district_documents.

    Uses src_id as the conflict key. Updates all fields on conflict.
    """
    sql = """
        INSERT INTO silver.district_documents (
            src_id, district_id, src_link, doc_name, src_type,
            valid_from, valid_to, source_pipeline, extraction_status,
            full_text, text_length, text_hash, extraction_error,
            human_ay_ids, ai_ay_ids, effective_ay_ids,
            ai_title, ai_summary, ai_document_type,
            ai_temporal_class, ai_readability, ai_confidence,
            ingested_at, last_updated
        ) VALUES (
            %(src_id)s, %(district_id)s, %(src_link)s, %(src_name)s, %(src_type)s,
            %(valid_from)s, %(valid_to)s, 'docpipe', %(extraction_status)s,
            %(full_text)s, %(text_length)s, %(text_hash)s, %(extraction_error)s,
            %(human_ay_ids)s, %(ai_ay_ids)s, %(effective_ay_ids)s,
            %(ai_title)s, %(ai_summary)s, %(ai_document_type)s,
            %(ai_temporal_class)s, %(ai_readability)s, %(ai_confidence)s,
            NOW(), NOW()
        )
        ON CONFLICT (src_id) WHERE src_id IS NOT NULL
        DO UPDATE SET
            source_pipeline = 'docpipe',
            extraction_status = EXCLUDED.extraction_status,
            full_text = EXCLUDED.full_text,
            text_length = EXCLUDED.text_length,
            text_hash = EXCLUDED.text_hash,
            extraction_error = EXCLUDED.extraction_error,
            human_ay_ids = EXCLUDED.human_ay_ids,
            ai_ay_ids = EXCLUDED.ai_ay_ids,
            effective_ay_ids = EXCLUDED.effective_ay_ids,
            ai_title = EXCLUDED.ai_title,
            ai_summary = EXCLUDED.ai_summary,
            ai_document_type = EXCLUDED.ai_document_type,
            ai_temporal_class = EXCLUDED.ai_temporal_class,
            ai_readability = EXCLUDED.ai_readability,
            ai_confidence = EXCLUDED.ai_confidence,
            last_updated = NOW(),
            -- Clear legacy columns so no stale data remains
            ai_effective_date = NULL,
            ai_expiration_date = NULL,
            ai_academic_years = NULL,
            ai_parties = NULL,
            enriched_at = NULL,
            http_status = NULL
    """
    params = {
        "src_id": doc.src_id,
        "district_id": doc.district_id,
        "src_link": doc.src_link,
        "src_name": doc.src_name,
        "src_type": doc.src_type.value,
        "valid_from": doc.valid_from,
        "valid_to": doc.valid_to,
        "extraction_status": doc.extraction_status,
        "full_text": (doc.full_text or "").replace("\x00", ""),
        "text_length": doc.text_length,
        "text_hash": doc.text_hash or "",
        "extraction_error": doc.extraction_error,
        "human_ay_ids": doc.human_ay_ids or [],
        "ai_ay_ids": doc.ai_ay_ids,
        "effective_ay_ids": doc.effective_ay_ids,
        "ai_title": doc.ai_title,
        "ai_summary": doc.ai_summary,
        "ai_document_type": doc.ai_document_type,
        "ai_temporal_class": doc.ai_temporal_class,
        "ai_readability": doc.ai_readability,
        "ai_confidence": doc.ai_confidence,
    }

    with get_pg_connection(config) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
