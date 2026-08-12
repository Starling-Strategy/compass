"""Feed district documents into Nathan's PiedPiper Vespa instance.

Loads documents from silver.district_documents, chunks them, generates
Gemini summaries, and feeds each chunk as a `passage` record to Vespa.

Usage:
    # Dry run one district
    PYTHONPATH=src python src/document_pipeline/feed_piedpiper.py --district 26 --dry-run

    # Live feed one district
    PYTHONPATH=src python src/document_pipeline/feed_piedpiper.py --district 26

    # Backfill all missing districts
    PYTHONPATH=src python src/document_pipeline/feed_piedpiper.py --all --backfill

    # Limit docs per district (for testing)
    PYTHONPATH=src python src/document_pipeline/feed_piedpiper.py --district 26 --limit 3
"""

import argparse
import asyncio
import logging
import os
import re
import sys
import time

import httpx
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

from document_pipeline.chunker import chunk_document
from document_pipeline.config import Config
from document_pipeline.db import get_pg_connection
from document_pipeline.summarize import summarize_chunks

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Concurrency limits
DOC_CONCURRENCY = 8       # docs processed in parallel
FEED_CONCURRENCY = 32     # concurrent Vespa PUT requests

# Strip control characters that Vespa's JSON parser rejects
_ILLEGAL_CHARS = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f"
    "\ufdd0-\ufdef\ufffe\uffff"
    "\U0001fffe\U0001ffff\U0002fffe\U0002ffff"
    "\U0003fffe\U0003ffff\U0004fffe\U0004ffff"
    "\U0005fffe\U0005ffff\U0006fffe\U0006ffff"
    "\U0007fffe\U0007ffff\U0008fffe\U0008ffff"
    "\U0009fffe\U0009ffff\U000afffe\U000affff"
    "\U000bfffe\U000bffff\U000cfffe\U000cffff"
    "\U000dfffe\U000dffff\U000efffe\U000effff"
    "\U000ffffe\U000fffff\U0010fffe\U0010ffff]"
)


def _sanitize(text: str) -> str:
    """Strip control characters that Vespa's parser rejects."""
    return _ILLEGAL_CHARS.sub("", text)


def _load_documents(config: Config, district_id: int | None, limit: int = 0) -> list[dict]:
    """Load processable documents from silver.district_documents."""
    params: list = []
    where = ""
    if district_id:
        where = "AND district_id = %s"
        params.append(district_id)

    limit_clause = ""
    if limit > 0:
        limit_clause = "LIMIT %s"
        params.append(limit)

    sql = f"""
        SELECT doc_id, district_id, doc_name, full_text,
               ai_document_type, ai_title, ai_summary, effective_ay_ids
        FROM silver.district_documents
        WHERE extraction_status = 'success'
          AND full_text IS NOT NULL
          AND text_length > 200
          {where}
        ORDER BY district_id, doc_name
        {limit_clause}
    """
    with get_pg_connection(config) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def _get_vespa_district_ids(client: httpx.Client) -> set[int]:
    """Query Vespa for distinct district_ids already indexed."""
    # Use a grouping query to get unique district_ids
    yql = (
        "select district_id from passage where true "
        "| all(group(district_id) each(output(count())))"
    )
    body = {"yql": yql, "hits": 0}
    try:
        resp = client.post("/search/", json=body)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.exception("Failed to query Vespa for indexed districts")
        return set()

    # Parse grouping results
    district_ids: set[int] = set()
    root = data.get("root", {})
    for child in root.get("children", []):
        if "children" in child:
            for group_list in child.get("children", []):
                if "children" in group_list:
                    for group in group_list.get("children", []):
                        val = group.get("value")
                        if val is not None:
                            district_ids.add(int(val))
    logger.info("Vespa has %s districts already indexed", len(district_ids))
    return district_ids


def _get_all_district_ids(config: Config) -> list[int]:
    """Get all district_ids that have processable documents in silver."""
    sql = """
        SELECT DISTINCT district_id
        FROM silver.district_documents
        WHERE extraction_status = 'success'
          AND full_text IS NOT NULL
          AND text_length > 200
        ORDER BY district_id
    """
    with get_pg_connection(config) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return [row[0] for row in cur.fetchall()]


def _build_vespa_client(config: Config) -> httpx.AsyncClient:
    """Create an async httpx client with mTLS for PiedPiper Vespa."""
    vespa_url = (
        config.vespa_url
        or os.environ.get("PREDICTOR_VESPA_URL")
        or os.environ.get("VESPA_URL", "")
    )
    cert_path = (
        config.vespa_cert_path
        or os.environ.get("PREDICTOR_VESPA_CERT_PATH")
        or os.environ.get("VESPA_CERT_PATH", "")
    )
    key_path = (
        config.vespa_key_path
        or os.environ.get("PREDICTOR_VESPA_KEY_PATH")
        or os.environ.get("VESPA_KEY_PATH", "")
    )

    if not vespa_url:
        raise ValueError("Set PREDICTOR_VESPA_URL or DOCPIPE_VESPA_URL")
    if not cert_path or not key_path:
        raise ValueError("Set PREDICTOR_VESPA_CERT_PATH and PREDICTOR_VESPA_KEY_PATH")

    cert = os.path.expanduser(cert_path)
    key = os.path.expanduser(key_path)
    return httpx.AsyncClient(
        base_url=vespa_url.rstrip("/"),
        cert=(cert, key),
        timeout=30.0,
    )


def _build_sync_vespa_client(config: Config) -> httpx.Client:
    """Create a sync httpx client with mTLS (for pre-flight queries)."""
    vespa_url = (
        config.vespa_url
        or os.environ.get("PREDICTOR_VESPA_URL")
        or os.environ.get("VESPA_URL", "")
    )
    cert_path = (
        config.vespa_cert_path
        or os.environ.get("PREDICTOR_VESPA_CERT_PATH")
        or os.environ.get("VESPA_CERT_PATH", "")
    )
    key_path = (
        config.vespa_key_path
        or os.environ.get("PREDICTOR_VESPA_KEY_PATH")
        or os.environ.get("VESPA_KEY_PATH", "")
    )

    cert = os.path.expanduser(cert_path)
    key = os.path.expanduser(key_path)
    return httpx.Client(
        base_url=vespa_url.rstrip("/"),
        cert=(cert, key),
        timeout=30.0,
    )


async def _feed_passage(
    client: httpx.AsyncClient,
    passage_id: str,
    fields: dict,
    sem: asyncio.Semaphore,
    dry_run: bool,
) -> bool:
    """PUT one passage record to Vespa. Returns True on success."""
    if dry_run:
        logger.debug("  [DRY RUN] Would feed passage %s", passage_id)
        return True

    async with sem:
        url = f"/document/v1/passage/passage/docid/{passage_id}"
        for attempt in range(3):
            try:
                resp = await client.post(url, json={"fields": fields})
                if resp.status_code in (200, 201):
                    return True
                logger.warning(
                    "Vespa POST %s returned %s: %s",
                    passage_id, resp.status_code, resp.text[:200],
                )
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                logger.error("Vespa POST %s failed: %s", passage_id, e)
                return False
    return False


async def _process_document(
    doc: dict,
    vespa_client: httpx.AsyncClient,
    feed_sem: asyncio.Semaphore,
    dry_run: bool,
) -> tuple[int, int]:
    """Process one document: chunk → summarize → feed. Returns (fed, errors)."""
    doc_id = str(doc["doc_id"])
    doc_name = doc["doc_name"] or ""
    doc_type = doc["ai_document_type"] or "other"
    district_id = doc["district_id"]
    full_text = doc["full_text"]
    ay_ids = doc.get("effective_ay_ids") or []

    # Quality gate: warn if no AY IDs (passages won't be findable by prediction pipeline)
    if not ay_ids:
        logger.warning("  Doc %s (%s): no effective_ay_ids — passages won't match AY filter",
                        doc_id[:8], doc_name[:40])

    # 1. Chunk
    result = chunk_document(full_text, target_size=2048, overlap=200)
    if not result.chunks:
        logger.warning("  No chunks for doc %s (%s)", doc_id[:8], doc_name)
        return 0, 0

    chunk_texts = [c.text for c in result.chunks]
    logger.info(
        "  Doc %s (%s): %s chunks, %s chars",
        doc_id[:8], doc_name[:40], len(result.chunks), result.total_chars,
    )

    # 2. Summarize
    summaries, errors = await summarize_chunks(chunk_texts, doc_name, doc_type)
    error_count = sum(1 for e in errors if e)
    if error_count:
        logger.warning("  %s/%s chunk summaries failed (using truncated text as fallback)",
                        error_count, len(chunk_texts))

    # 3. Feed to Vespa
    fed = 0
    feed_errors = 0
    tasks = []
    for chunk, summary in zip(result.chunks, summaries):
        passage_id = f"{doc_id}_{chunk.index}"
        passage_text = _sanitize(f"{chunk.text} [Summary: {summary}]")

        fields = {
            "doc_id": doc_id,
            "doc_title": _sanitize(doc_name),
            "document_type": doc_type,
            "district_id": district_id,
            "academic_years": [str(ay) for ay in ay_ids],
            "passage_text": passage_text,
            "section_heading": _sanitize(chunk.heading or ""),
            "page_number": chunk.page_hint,
            "chunk_index": chunk.index,
            "char_start": chunk.char_start,
            "char_end": chunk.char_end,
        }
        tasks.append(_feed_passage(vespa_client, passage_id, fields, feed_sem, dry_run))

    results = await asyncio.gather(*tasks)
    fed = sum(1 for r in results if r)
    feed_errors = sum(1 for r in results if not r)

    return fed, feed_errors


async def process_district(
    config: Config,
    district_id: int,
    dry_run: bool,
    limit: int = 0,
) -> tuple[int, int, int]:
    """Process all documents for one district. Returns (docs, passages_fed, errors)."""
    docs = _load_documents(config, district_id, limit)
    if not docs:
        logger.info("District %s: no documents to process", district_id)
        return 0, 0, 0

    # Quality stats
    docs_no_ay = sum(1 for d in docs if not d.get("effective_ay_ids"))
    docs_no_type = sum(1 for d in docs if not d.get("ai_document_type"))
    logger.info("District %s: %s documents to process (no AY: %s, no type: %s)",
                district_id, len(docs), docs_no_ay, docs_no_type)

    vespa_client = _build_vespa_client(config)
    feed_sem = asyncio.Semaphore(FEED_CONCURRENCY)
    doc_sem = asyncio.Semaphore(DOC_CONCURRENCY)

    total_fed = 0
    total_errors = 0

    async def _bounded_process(doc):
        async with doc_sem:
            return await _process_document(doc, vespa_client, feed_sem, dry_run)

    tasks = [_bounded_process(doc) for doc in docs]
    results = await asyncio.gather(*tasks)

    await vespa_client.aclose()

    for fed, errors in results:
        total_fed += fed
        total_errors += errors

    return len(docs), total_fed, total_errors


async def run(args: argparse.Namespace) -> None:
    """Main entry point."""
    config = Config()
    dry_run = args.dry_run

    if dry_run:
        logger.info("=== DRY RUN MODE (no Vespa writes) ===")

    if args.all:
        all_districts = _get_all_district_ids(config)
        logger.info("Found %s districts with documents in silver", len(all_districts))

        if args.backfill:
            sync_client = _build_sync_vespa_client(config)
            indexed = _get_vespa_district_ids(sync_client)
            sync_client.close()
            districts = [d for d in all_districts if d not in indexed]
            logger.info("Backfill: %s districts not yet in Vespa", len(districts))
        else:
            districts = all_districts
    elif args.district:
        districts = [args.district]
    else:
        logger.error("Provide --district <id> or --all")
        sys.exit(1)

    grand_docs = 0
    grand_fed = 0
    grand_errors = 0
    t0 = time.time()

    for i, district_id in enumerate(districts, 1):
        logger.info(
            "--- District %s (%s/%s) ---",
            district_id, i, len(districts),
        )
        docs, fed, errors = await process_district(
            config, district_id, dry_run, limit=args.limit,
        )
        grand_docs += docs
        grand_fed += fed
        grand_errors += errors
        logger.info(
            "District %s done: %s docs, %s passages fed, %s errors",
            district_id, docs, fed, errors,
        )

    elapsed = time.time() - t0
    logger.info(
        "=== COMPLETE: %s districts, %s docs, %s passages fed, %s errors (%.0fs) ===",
        len(districts), grand_docs, grand_fed, grand_errors, elapsed,
    )


def main():
    parser = argparse.ArgumentParser(description="Feed documents to PiedPiper Vespa")
    parser.add_argument("--district", type=int, help="District ID to process")
    parser.add_argument("--all", action="store_true", help="Process all districts")
    parser.add_argument("--backfill", action="store_true", help="Skip districts already in Vespa")
    parser.add_argument("--dry-run", action="store_true", default=False, help="Process but don't feed")
    parser.add_argument("--limit", type=int, default=0, help="Max docs per district")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
