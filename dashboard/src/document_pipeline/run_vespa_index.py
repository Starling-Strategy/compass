"""Vespa indexing pipeline — CLI entry point.

Reads enriched documents from PostgreSQL, chunks, summarizes, embeds,
and feeds to Vespa Cloud.

Usage:
    # Deploy schema + index district 37
    PYTHONPATH=src python src/document_pipeline/run_vespa_index.py \\
        --district 37 --tenant <tenant> --vespa-token <token>

    # Backfill only missing documents (queries Vespa for existing doc_ids)
    PYTHONPATH=src python src/document_pipeline/run_vespa_index.py \\
        --all --backfill --skip-deploy --vespa-url <url>

    # Dry run (no Vespa, just chunk/summarize/embed)
    PYTHONPATH=src python src/document_pipeline/run_vespa_index.py \\
        --district 37 --dry-run

Pipeline stages (per document):
    1. Load enriched documents from silver.district_documents
    2. Chunk markdown text (CPU, instant)
    3. Summarize chunks via Flash-Lite (async, 16 concurrent)
    4. Embed chunks + doc_summary via embedding-001 (async, 8 concurrent batches)
    5. Feed to Vespa Cloud (async, 32 concurrent)

Stages 2-4 run as a pipeline: documents flow through all stages concurrently.
"""

import argparse
import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

from document_pipeline.chunker import chunk_document
from document_pipeline.config import Config
from document_pipeline.db import get_pg_connection
from document_pipeline.embed import embed_document_chunks
from document_pipeline.observability import setup as setup_observability
from document_pipeline.summarize import summarize_chunks
from document_pipeline.vespa_indexer import (
    VespaDocPayload,
    deploy_app,
    feed_documents,
    get_app_connection,
)

try:
    import logfire
except ImportError:
    logfire = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

PIPELINE_BUFFER = 8
ERROR_REPORT_PATH = "vespa_backfill_errors.json"


# ---------------------------------------------------------------------------
# Error tracking
# ---------------------------------------------------------------------------

@dataclass
class DocError:
    src_id: str | int
    district_id: int
    stage: str          # "chunk", "summarize", "embed", "feed", "process"
    error_type: str     # exception class name
    error_message: str
    doc_name: str = ""
    chunk_count: int = 0
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


@dataclass
class RunReport:
    started_at: str = ""
    finished_at: str = ""
    mode: str = "full"  # "full" or "backfill"
    districts_attempted: int = 0
    districts_succeeded: int = 0
    docs_in_postgres: int = 0
    docs_skipped_existing: int = 0
    docs_processed: int = 0
    docs_succeeded: int = 0
    docs_failed: int = 0
    total_chunks: int = 0
    feed_failures: int = 0
    errors: list[DocError] = field(default_factory=list)

    def add_error(self, **kwargs):
        self.errors.append(DocError(**kwargs))

    def summary(self) -> str:
        lines = [
            f"Mode: {self.mode}",
            f"Districts: {self.districts_succeeded}/{self.districts_attempted}",
            f"Docs: {self.docs_succeeded} ok, {self.docs_failed} failed "
            f"(of {self.docs_processed} processed, {self.docs_skipped_existing} skipped)",
            f"Chunks: {self.total_chunks}",
            f"Feed failures: {self.feed_failures}",
        ]
        if self.errors:
            from collections import Counter
            stage_counts = Counter(e.stage for e in self.errors)
            type_counts = Counter(e.error_type for e in self.errors)
            lines.append(f"Errors by stage: {dict(stage_counts)}")
            lines.append(f"Errors by type:  {dict(type_counts)}")
        return "\n".join(lines)

    def save(self, path: str = ERROR_REPORT_PATH):
        data = {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "mode": self.mode,
            "districts_attempted": self.districts_attempted,
            "districts_succeeded": self.districts_succeeded,
            "docs_in_postgres": self.docs_in_postgres,
            "docs_skipped_existing": self.docs_skipped_existing,
            "docs_processed": self.docs_processed,
            "docs_succeeded": self.docs_succeeded,
            "docs_failed": self.docs_failed,
            "total_chunks": self.total_chunks,
            "feed_failures": self.feed_failures,
            "error_count": len(self.errors),
            "errors": [asdict(e) for e in self.errors],
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        logger.info(f"Error report saved to {path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Index documents into Vespa Cloud")
    parser.add_argument("--district", type=int, help="District ID to index")
    parser.add_argument("--all", action="store_true", help="Index all districts")
    parser.add_argument("--backfill", action="store_true",
                        help="Only process documents missing from Vespa")
    parser.add_argument("--dry-run", action="store_true", help="Preview without feeding")
    parser.add_argument("--limit", type=int, default=0, help="Max documents (0 = all)")
    parser.add_argument("--chunk-size", type=int, default=2048, help="Target chunk size in chars")
    parser.add_argument("--chunk-overlap", type=int, default=200, help="Overlap between chunks")
    parser.add_argument("--skip-summaries", action="store_true", help="Skip chunk summarization")
    parser.add_argument("--schema-only", action="store_true", help="Deploy schema and exit")

    # Vespa Cloud
    parser.add_argument("--tenant", type=str, default="starling", help="Vespa Cloud tenant name")
    parser.add_argument("--app-name", type=str, default="nctq", help="Vespa Cloud app name")
    parser.add_argument("--vespa-token", type=str, default=None, help="Vespa Cloud data-plane token value")
    parser.add_argument("--vespa-token-id", type=str, default=None, help="Vespa Cloud token ID (from console)")
    parser.add_argument("--skip-deploy", action="store_true",
                        help="Skip deploy, connect via mTLS certs directly (for long runs)")
    parser.add_argument("--vespa-url", type=str, default=None, help="Vespa Cloud endpoint URL")
    parser.add_argument("--vespa-cert-dir", type=str, default=None,
                        help="Dir with data-plane-public-cert.pem and data-plane-private-key.pem")
    return parser.parse_args()


def get_district_ids(config: Config) -> list[int]:
    """Get all district IDs that have indexable documents."""
    sql = """
        SELECT DISTINCT district_id
        FROM silver.district_documents
        WHERE extraction_status = 'success' AND full_text IS NOT NULL AND text_length > 200
        ORDER BY district_id
    """
    with get_pg_connection(config) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return [row[0] for row in cur.fetchall()]


def get_vespa_doc_ids(vespa_app, district_id: int) -> set[str]:
    """Query Vespa for all doc_ids already indexed for a given district."""
    try:
        offset = 0
        batch = 400
        existing: set[str] = set()
        while True:
            resp = vespa_app.query(
                yql=f"select doc_id from district_doc where district_id = {district_id}",
                hits=batch,
                offset=offset,
            )
            hits = resp.json.get("root", {}).get("children", [])
            if not hits:
                break
            for h in hits:
                doc_id = h.get("fields", {}).get("doc_id")
                if doc_id is not None:
                    existing.add(str(doc_id))
            if len(hits) < batch:
                break
            offset += batch
        return existing
    except Exception as e:
        logger.warning(f"Failed to query Vespa for district {district_id}: {e}")
        return set()


def load_enriched_documents(config: Config, district_id: int | None, limit: int = 0) -> list[dict]:
    """Load documents for a single district (or all if district_id is None)."""
    import psycopg2.extras

    where_parts = [
        "extraction_status = 'success'",
        "full_text IS NOT NULL",
        "text_length > 200",
    ]
    if district_id:
        where_parts.append(f"district_id = {district_id}")

    where = " AND ".join(where_parts)
    limit_clause = f"LIMIT {limit}" if limit > 0 else ""

    sql = f"""
        SELECT doc_id, src_id, district_id, doc_name, src_type, full_text, text_length,
               ai_title, ai_summary, ai_document_type, effective_ay_ids,
               (SELECT district_name FROM bronze.district d
                WHERE d.district_id = dd.district_id LIMIT 1) as district_name
        FROM silver.district_documents dd
        WHERE {where}
        ORDER BY district_id, doc_id
        {limit_clause}
    """
    with get_pg_connection(config) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            return cur.fetchall()


async def process_document(
    row: dict,
    chunk_size: int,
    chunk_overlap: int,
    skip_summaries: bool,
    report: RunReport,
) -> VespaDocPayload | None:
    """Process one document through chunk -> summarize -> embed.

    Summarization and embedding run concurrently for the same document.
    Errors are tracked in the report rather than silently swallowed.
    """
    doc_uuid = str(row["doc_id"]) if row.get("doc_id") else str(row.get("src_id", "unknown"))
    district_id = row["district_id"]
    full_text = row["full_text"]
    doc_title = row.get("ai_title") or row.get("doc_name") or f"doc_{doc_uuid[:8]}"
    doc_type = row.get("ai_document_type") or row.get("src_type") or "policy"
    doc_summary = row.get("ai_summary") or doc_title
    doc_name = row.get("doc_name") or ""

    try:
        chunk_result = chunk_document(
            full_text, src_id=0, target_size=chunk_size, overlap=chunk_overlap,
        )
    except Exception as e:
        report.add_error(
            src_id=doc_uuid, district_id=district_id, stage="chunk",
            error_type=type(e).__name__, error_message=str(e), doc_name=doc_name,
        )
        return None

    if not chunk_result.chunks:
        report.add_error(
            src_id=doc_uuid, district_id=district_id, stage="chunk",
            error_type="EmptyChunks", error_message="no chunks produced",
            doc_name=doc_name,
        )
        return None

    chunk_texts = [c.text for c in chunk_result.chunks]
    chunk_headings = [c.heading for c in chunk_result.chunks]
    n_chunks = len(chunk_texts)

    # --- Summarize ---
    summary_errors: list[str] = []
    if skip_summaries:
        chunk_summaries = chunk_texts
    else:
        try:
            chunk_summaries, summary_errors = await summarize_chunks(
                chunk_texts, doc_title=doc_title, doc_type=doc_type,
            )
            failed_count = sum(1 for e in summary_errors if e)
            if failed_count:
                logger.warning(
                    f"  {doc_uuid[:12]}: {failed_count}/{n_chunks} chunk summaries failed"
                )
                for i, err in enumerate(summary_errors):
                    if err:
                        report.add_error(
                            src_id=doc_uuid, district_id=district_id, stage="summarize",
                            error_type="ChunkSummaryFailed",
                            error_message=f"chunk {i}: {err}",
                            doc_name=doc_name, chunk_count=n_chunks,
                        )
        except Exception as e:
            report.add_error(
                src_id=doc_uuid, district_id=district_id, stage="summarize",
                error_type=type(e).__name__, error_message=str(e),
                doc_name=doc_name, chunk_count=n_chunks,
            )
            return None

    # --- Embed ---
    try:
        chunk_embeddings, doc_embedding = await embed_document_chunks(
            chunk_texts, doc_summary=doc_summary,
        )
    except Exception as e:
        report.add_error(
            src_id=doc_uuid, district_id=district_id, stage="embed",
            error_type=type(e).__name__, error_message=str(e),
            doc_name=doc_name, chunk_count=n_chunks,
        )
        return None

    ay_ids = row.get("effective_ay_ids") or []
    if isinstance(ay_ids, str):
        ay_ids = [int(x) for x in ay_ids.strip("{}").split(",") if x.strip()]

    return VespaDocPayload(
        doc_id=doc_uuid,
        district_id=district_id,
        district_name=row.get("district_name") or "",
        ay_ids=ay_ids,
        doc_type=doc_type,
        doc_name=doc_name,
        doc_summary=doc_summary,
        full_text=full_text,
        chunks=chunk_texts,
        chunk_summaries=list(chunk_summaries),
        chunk_headings=chunk_headings,
        chunk_embeddings=chunk_embeddings,
        doc_embedding=doc_embedding,
    )


async def run_pipeline(
    rows: list[dict],
    vespa_app,
    args: argparse.Namespace,
    report: RunReport,
) -> tuple[int, int, int]:
    """Run the full pipeline: chunk -> summarize -> embed -> feed.

    Returns (total_docs, total_chunks, feed_failures).
    """
    sem = asyncio.Semaphore(PIPELINE_BUFFER)
    payloads: list[VespaDocPayload] = []
    total_chunks = 0
    process_failures = 0

    async def _process_with_limit(row: dict) -> VespaDocPayload | None:
        async with sem:
            return await process_document(
                row, args.chunk_size, args.chunk_overlap, args.skip_summaries,
                report=report,
            )

    logger.info(f"Processing {len(rows)} documents (buffer={PIPELINE_BUFFER})...")
    tasks = [_process_with_limit(row) for row in rows]

    for i, coro in enumerate(asyncio.as_completed(tasks)):
        try:
            payload = await coro
            if payload:
                payloads.append(payload)
                total_chunks += len(payload.chunks)
                logger.info(
                    f"  [{i+1}/{len(rows)}] src_id={payload.doc_id}: "
                    f"{len(payload.chunks)} chunks"
                )
            else:
                process_failures += 1
        except Exception as e:
            process_failures += 1
            logger.error(f"  [{i+1}/{len(rows)}] Unexpected error: {type(e).__name__}: {e}")
            report.add_error(
                src_id=0, district_id=0, stage="process",
                error_type=type(e).__name__, error_message=str(e),
            )

    logger.info(
        f"Processed: {len(payloads)} docs, {total_chunks} chunks, "
        f"{process_failures} failures"
    )

    if args.dry_run or vespa_app is None:
        logger.info("DRY RUN — skipping Vespa feed")
        for p in payloads[:5]:
            logger.info(
                f"  Would feed: doc_id={p.doc_id} district={p.district_id} "
                f"chunks={len(p.chunks)} embed_dim={len(p.doc_embedding)}"
            )
        if len(payloads) > 5:
            logger.info(f"  ... and {len(payloads) - 5} more")
        return len(payloads), total_chunks, 0

    succeeded, failed = await feed_documents(vespa_app, payloads)
    report.feed_failures += failed
    return len(payloads), total_chunks, failed


def main():
    setup_observability("vespa-indexer")
    args = parse_args()
    config = Config()

    if args.dry_run:
        config.dry_run = True
    if args.district:
        config.district_id = args.district

    report = RunReport(
        started_at=datetime.now(timezone.utc).isoformat(),
        mode="backfill" if args.backfill else "full",
    )

    vespa_token = args.vespa_token or os.environ.get("VESPA_TOKEN")
    vespa_token_id = args.vespa_token_id or os.environ.get("VESPA_TOKEN_ID")

    # --- Schema-only deploy ---
    if args.schema_only:
        app, _ = deploy_app(
            tenant=args.tenant,
            app_name=args.app_name,
            token_id=vespa_token_id,
        )
        logger.info("Schema deployed. Exiting.")
        return

    # --- Full pipeline ---
    if not args.district and not args.all:
        raise SystemExit("Provide --district <id> or --all")

    # Connect to Vespa (or None for dry run)
    vespa_app = None
    if not args.dry_run:
        if args.skip_deploy:
            from vespa.application import Vespa as VespaApp
            vespa_url = args.vespa_url or os.environ.get("VESPA_URL")
            cert_dir = args.vespa_cert_dir or os.path.expanduser(
                f"~/.vespa/{args.tenant}.{args.app_name}.default"
            )
            if not vespa_url:
                raise SystemExit("--vespa-url required with --skip-deploy")
            logger.info(f"Connecting to Vespa at {vespa_url} (skip-deploy, mTLS)")
            vespa_app = VespaApp(
                url=vespa_url,
                cert=f"{cert_dir}/data-plane-public-cert.pem",
                key=f"{cert_dir}/data-plane-private-key.pem",
            )
        elif vespa_token:
            logger.info("Deploying + connecting to Vespa Cloud...")
            vespa_app, vespa_cloud = deploy_app(
                tenant=args.tenant,
                app_name=args.app_name,
                token_id=vespa_token_id,
            )
        else:
            logger.info("No --vespa-token provided, will run as dry-run")
            args.dry_run = True

    # Build list of districts to process
    if args.district:
        district_ids = [args.district]
    else:
        logger.info("Fetching district list...")
        district_ids = get_district_ids(config)
        logger.info(f"Found {len(district_ids)} districts to index")

    report.districts_attempted = len(district_ids)
    grand_start = time.time()
    grand_docs = 0
    grand_chunks = 0
    grand_failures = 0

    for di, d_id in enumerate(district_ids, 1):
        d_start = time.time()
        logger.info(f"\n{'='*60}")
        logger.info(f"District {d_id} [{di}/{len(district_ids)}]")
        logger.info(f"{'='*60}")

        try:
            rows = load_enriched_documents(config, district_id=d_id, limit=args.limit)
        except Exception as e:
            logger.error(f"  Failed to load district {d_id}: {e}")
            report.add_error(
                src_id=0, district_id=d_id, stage="load",
                error_type=type(e).__name__, error_message=str(e),
            )
            grand_failures += 1
            continue

        if not rows:
            logger.info(f"  No documents, skipping")
            continue

        report.docs_in_postgres += len(rows)

        # --- Backfill: skip docs already in Vespa ---
        if args.backfill and vespa_app is not None:
            existing_ids = get_vespa_doc_ids(vespa_app, d_id)
            before = len(rows)
            # Match against both old-style (src_id) and new-style (doc_id UUID) keys
            rows = [
                r for r in rows
                if str(r.get("doc_id", "")) not in existing_ids
                and str(r.get("src_id", "")) not in existing_ids
            ]
            skipped = before - len(rows)
            report.docs_skipped_existing += skipped
            if skipped:
                logger.info(f"  Backfill: {skipped} already indexed, {len(rows)} remaining")
            if not rows:
                logger.info(f"  All docs already indexed, skipping district")
                report.districts_succeeded += 1
                continue

        logger.info(f"  Processing {len(rows)} documents")

        try:
            total_docs, total_chunks, feed_failures = asyncio.run(
                run_pipeline(rows, vespa_app, args, report=report)
            )
        except Exception as e:
            logger.error(f"  Pipeline failed for district {d_id}: {type(e).__name__}: {e}")
            report.add_error(
                src_id=0, district_id=d_id, stage="pipeline",
                error_type=type(e).__name__, error_message=str(e),
            )
            grand_failures += len(rows)
            continue

        d_elapsed = time.time() - d_start
        grand_docs += total_docs
        grand_chunks += total_chunks
        grand_failures += feed_failures
        report.districts_succeeded += 1
        report.docs_processed += len(rows)
        report.docs_succeeded += total_docs
        report.docs_failed += (len(rows) - total_docs)
        report.total_chunks += total_chunks

        elapsed_so_far = time.time() - grand_start
        rate = grand_docs / elapsed_so_far if elapsed_so_far > 0 else 0
        remaining = len(district_ids) - di
        if rate > 0 and di > 1:
            avg_per_district = elapsed_so_far / di
            eta_sec = remaining * avg_per_district
            eta_str = f"{eta_sec/3600:.1f}h" if eta_sec > 3600 else f"{eta_sec/60:.0f}m"
        else:
            eta_str = "calculating..."

        logger.info(
            f"  Done: {total_docs} docs, {total_chunks} chunks in {d_elapsed:.0f}s | "
            f"Total: {grand_docs} docs, {grand_chunks} chunks, "
            f"{len(report.errors)} errors | ETA: {eta_str}"
        )

        # Periodic save every 10 districts so we don't lose error data on crash
        if di % 10 == 0:
            report.save()

    elapsed = time.time() - grand_start
    report.finished_at = datetime.now(timezone.utc).isoformat()

    logger.info(f"\n{'='*60}")
    logger.info(f"ALL DONE in {elapsed:.0f}s ({elapsed/60:.1f} min, {elapsed/3600:.1f} hrs)")
    logger.info(f"{'='*60}")
    logger.info(report.summary())
    if grand_docs > 0 and elapsed > 0:
        logger.info(f"Throughput: {grand_docs/elapsed:.1f} docs/sec, {grand_chunks/elapsed:.1f} chunks/sec")

    report.save()
    logger.info(f"{'='*60}")


if __name__ == "__main__":
    main()
