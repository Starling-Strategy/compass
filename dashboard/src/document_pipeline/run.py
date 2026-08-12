"""Document processing pipeline entry point.

Usage:
    PYTHONPATH=src python src/document_pipeline/run.py --district 37 --limit 5
    PYTHONPATH=src python src/document_pipeline/run.py --district 37 --limit 5 --dry-run
    DOCPIPE_DRY_RUN=false PYTHONPATH=src python src/document_pipeline/run.py --all

Parallel mode (default): pre-downloads files and batches Gemini enrichment.
    DOCPIPE_DRY_RUN=false PYTHONPATH=src python src/document_pipeline/run.py --district 37

Sequential mode: original one-at-a-time processing.
    DOCPIPE_DRY_RUN=false PYTHONPATH=src python src/document_pipeline/run.py --district 37 --sequential
"""

import argparse
import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from document_pipeline.config import Config
from document_pipeline.db import get_reprocess_src_ids, load_sources, save_document
from document_pipeline.download import download
from document_pipeline.enrich import enrich, enrich_async
from document_pipeline.extract import extract
from document_pipeline.observability import setup as setup_observability

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Try to import logfire for spans (no-op if unavailable)
try:
    import logfire
except ImportError:
    logfire = None

# Parallel pipeline tuning
DOWNLOAD_WORKERS = 4   # Thread pool size for pre-downloading
PREFETCH_AHEAD = 8     # How many docs to download ahead of extraction
ENRICH_BATCH_SIZE = 8  # How many docs to enrich concurrently


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Process NCTQ source documents")
    parser.add_argument("--district", type=int, help="District ID to process")
    parser.add_argument("--limit", type=int, default=0, help="Max documents (0 = all)")
    parser.add_argument("--all", action="store_true", help="Process all districts")
    parser.add_argument("--dry-run", action="store_true", help="Preview without saving")
    parser.add_argument("--failed-only", action="store_true", help="Only reprocess docs that failed previously")
    parser.add_argument("--sequential", action="store_true", help="Disable parallel processing")
    parser.add_argument("--new-only", action="store_true",
                        help="Only process docs not yet in silver (new bronze sources)")
    return parser.parse_args()


async def _enrich_batch(docs, config):
    """Enrich a batch of documents concurrently via asyncio.gather."""
    tasks = [enrich_async(doc, config) for doc in docs]
    return await asyncio.gather(*tasks, return_exceptions=True)


def _process_parallel(documents, config):
    """Parallel pipeline: pre-download files, extract sequentially, batch-enrich concurrently."""
    succeeded, failed = 0, 0
    total = len(documents)
    logger.info(f"Parallel mode: prefetch={PREFETCH_AHEAD}, enrich_batch={ENRICH_BATCH_SIZE}, download_workers={DOWNLOAD_WORKERS}")

    # Persistent event loop for async enrichment — asyncio.run() would close the loop
    # after each call, breaking PydanticAI's httpx client on subsequent batches.
    enrich_loop = asyncio.new_event_loop()

    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as download_pool:
        # Submit initial downloads ahead of extraction
        download_futures = {}
        for doc in documents[:PREFETCH_AHEAD]:
            download_futures[doc.src_id] = download_pool.submit(
                download, doc.src_link, config.http_timeout
            )

        pending_enrich = []  # Extracted docs waiting for enrichment

        for i, doc in enumerate(documents):
            # Submit next download to keep the prefetch window full
            next_idx = i + PREFETCH_AHEAD
            if next_idx < total:
                next_doc = documents[next_idx]
                download_futures[next_doc.src_id] = download_pool.submit(
                    download, next_doc.src_link, config.http_timeout
                )

            logger.info(f"[{i + 1}/{total}] Processing src_id={doc.src_id}: {doc.src_name[:50]}")

            try:
                # Get pre-downloaded file (should already be ready)
                file_path = download_futures.pop(doc.src_id).result()

                try:
                    # Extract (sequential — Docling singleton, GPU-bound)
                    doc = extract(doc, file_path, use_ocr=config.use_ocr)
                finally:
                    file_path.unlink(missing_ok=True)

                if doc.extraction_status == "success":
                    # Save immediately so progress is visible in DB
                    doc.text_hash = doc.compute_text_hash()
                    save_document(doc, config)
                    pending_enrich.append(doc)
                else:
                    # Save failed extraction immediately
                    doc.text_hash = doc.compute_text_hash()
                    save_document(doc, config)
                    failed += 1
                    logger.warning(f"  FAIL: {doc.extraction_error}")

            except Exception as e:
                failed += 1
                doc.extraction_status = "failed"
                doc.extraction_error = str(e)[:500]
                save_document(doc, config)
                logger.error(f"  ERROR: {e}")

            # Batch-enrich when we have enough extracted docs
            if len(pending_enrich) >= ENRICH_BATCH_SIZE:
                s, f = _flush_enrich_batch(pending_enrich, config, enrich_loop)
                succeeded += s
                failed += f
                pending_enrich = []

        # Flush remaining extracted docs
        if pending_enrich:
            s, f = _flush_enrich_batch(pending_enrich, config, enrich_loop)
            succeeded += s
            failed += f

    enrich_loop.close()
    return succeeded, failed


def _flush_enrich_batch(docs, config, loop):
    """Enrich a batch concurrently, save results, return (succeeded, failed) counts."""
    succeeded, failed = 0, 0
    batch_start = time.time()
    logger.info(f"  Enriching batch of {len(docs)} docs concurrently...")

    results = loop.run_until_complete(_enrich_batch(docs, config))

    for doc, result in zip(docs, results):
        if isinstance(result, Exception):
            doc.ai_confidence = 0.0
            logger.error(f"  Enrichment exception for {doc.src_id}: {result}")

        doc.text_hash = doc.compute_text_hash()
        save_document(doc, config)

        if doc.extraction_status == "success":
            succeeded += 1
            logger.info(f"  OK: {doc.ai_title or doc.src_name[:50]} ({doc.text_length:,} chars)")
        else:
            failed += 1

    elapsed = time.time() - batch_start
    logger.info(f"  Batch enriched in {elapsed:.1f}s ({len(docs)} docs)")
    return succeeded, failed


def _process_sequential(documents, config):
    """Original sequential pipeline: download → extract → enrich → save, one at a time."""
    succeeded, failed = 0, 0
    total = len(documents)
    logger.info("Sequential mode")

    for i, doc in enumerate(documents, 1):
        logger.info(f"[{i}/{total}] Processing src_id={doc.src_id}: {doc.src_name[:50]}")

        try:
            file_path = download(doc.src_link, timeout=config.http_timeout)

            try:
                doc = extract(doc, file_path, use_ocr=config.use_ocr)

                if doc.extraction_status == "success":
                    doc = enrich(doc, config)

                doc.text_hash = doc.compute_text_hash()
            finally:
                file_path.unlink(missing_ok=True)

            save_document(doc, config)

            if doc.extraction_status == "success":
                succeeded += 1
                logger.info(f"  OK: {doc.ai_title or doc.src_name[:50]} ({doc.text_length:,} chars)")
            else:
                failed += 1
                logger.warning(f"  FAIL: {doc.extraction_error}")

        except Exception as e:
            failed += 1
            doc.extraction_status = "failed"
            doc.extraction_error = str(e)[:500]
            save_document(doc, config)
            logger.error(f"  ERROR: {e}")

    return succeeded, failed


def main():
    setup_observability("docpipe-pipeline")

    args = parse_args()
    config = Config()

    # CLI args override env vars
    if args.district:
        config.district_id = args.district
    if args.limit:
        config.limit = args.limit
    if args.all:
        config.process_all = True
    if args.dry_run:
        config.dry_run = True

    # Load
    logger.info("Loading sources from bronze...")
    documents = load_sources(
        config,
        district_id=config.district_id,
        limit=config.limit,
        process_all=config.process_all,
        new_only=args.new_only,
    )
    logger.info(f"Loaded {len(documents)} documents")

    # Filter to only docs that need reprocessing (failed or missing enrichment)
    if args.failed_only:
        reprocess_ids = get_reprocess_src_ids(config, district_id=config.district_id)
        documents = [d for d in documents if d.src_id in reprocess_ids]
        logger.info(f"Filtered to {len(documents)} docs needing reprocessing")

    if not documents:
        logger.info("Nothing to process.")
        return

    if config.dry_run:
        logger.info("DRY RUN — showing what would be processed:")
        for d in documents[:20]:
            print(f"  src_id={d.src_id:5d} | D{d.district_id:3d} | {d.src_name[:60]}")
        if len(documents) > 20:
            print(f"  ... and {len(documents) - 20} more")
        print(f"\nSet DOCPIPE_DRY_RUN=false to process.")
        return

    # Process
    start = time.time()

    # Wrap entire pipeline run in a Logfire span
    span_ctx = logfire.span(
        "run_docpipe_pipeline",
        total_documents=len(documents),
        district_id=config.district_id,
    ) if logfire else None

    if span_ctx:
        span_ctx.__enter__()

    try:
        if args.sequential:
            succeeded, failed = _process_sequential(documents, config)
        else:
            succeeded, failed = _process_parallel(documents, config)
    finally:
        if span_ctx:
            span_ctx.__exit__(None, None, None)

    # Summary
    elapsed = time.time() - start
    if logfire:
        logfire.info(
            "pipeline_complete",
            total=succeeded + failed,
            succeeded=succeeded,
            failed=failed,
            elapsed_seconds=round(elapsed, 1),
        )
    logger.info("=" * 60)
    logger.info(f"DONE in {elapsed:.0f}s")
    logger.info(f"  Processed: {succeeded + failed}")
    logger.info(f"  Succeeded: {succeeded}")
    logger.info(f"  Failed:    {failed}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
