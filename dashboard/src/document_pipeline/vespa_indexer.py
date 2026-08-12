"""Feed documents to Vespa Cloud with parallel batch operations.

Uses PyVespa's VespaCloud for deployment (control-plane) and Vespa
for feeding (data-plane). Auth is token-based for simplicity.

Parallelism: documents are fed concurrently via asyncio (default: 32 in-flight).
Each document is a single Vespa document containing all its chunks
as parallel arrays (chunks, chunk_summaries, chunk_embeddings).
"""

import asyncio
import logging
import time
from dataclasses import dataclass

try:
    import logfire
except ImportError:
    logfire = None

from vespa.application import Vespa
from vespa.deployment import VespaCloud

logger = logging.getLogger(__name__)

MAX_CONCURRENT_FEEDS = 32
FEED_TIMEOUT = 120


@dataclass
class VespaDocPayload:
    """One document ready for Vespa feeding."""

    doc_id: str
    district_id: int
    district_name: str
    ay_ids: list[int]
    doc_type: str
    doc_name: str
    doc_summary: str
    full_text: str
    chunks: list[str]
    chunk_summaries: list[str]
    chunk_headings: list[str]
    chunk_embeddings: list[list[float]]
    doc_embedding: list[float]


import re

_ILLEGAL_XML_CHARS = re.compile(
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
    """Strip control characters that Vespa's XML/JSON parser rejects."""
    return _ILLEGAL_XML_CHARS.sub("", text)


def _to_vespa_fields(payload: VespaDocPayload) -> dict:
    """Convert payload to Vespa document fields dict.

    chunk_embeddings becomes a mapped tensor: {0: [v0, v1, ...], 1: [...], ...}
    """
    chunk_tensor = {str(i): v for i, v in enumerate(payload.chunk_embeddings)}

    return {
        "doc_id": payload.doc_id,
        "district_id": payload.district_id,
        "district_name": _sanitize(payload.district_name),
        "ay_ids": payload.ay_ids,
        "doc_type": _sanitize(payload.doc_type),
        "doc_name": _sanitize(payload.doc_name),
        "doc_summary": _sanitize(payload.doc_summary),
        "full_text": _sanitize(payload.full_text),
        "chunks": [_sanitize(c) for c in payload.chunks],
        "chunk_summaries": [_sanitize(s) for s in payload.chunk_summaries],
        "chunk_headings": [_sanitize(h) for h in payload.chunk_headings],
        "chunk_embeddings": {"blocks": chunk_tensor},
        "doc_embedding": {"values": payload.doc_embedding},
    }


def deploy_app(tenant: str, app_name: str, token_id: str | None = None) -> Vespa:
    """Deploy the Vespa application package to Vespa Cloud.

    Uses interactive browser login for control-plane auth.
    Returns a data-plane Vespa connection.
    """
    from document_pipeline.vespa_schema import build_application_package

    app_package = build_application_package(app_name=app_name)

    vespa_cloud = VespaCloud(
        tenant=tenant,
        application=app_name,
        application_package=app_package,
        auth_client_token_id=token_id,
    )

    logger.info(f"Deploying to Vespa Cloud: {tenant}.{app_name} ...")
    app: Vespa = vespa_cloud.deploy()
    logger.info("Deployment complete!")
    return app, vespa_cloud


def get_app_connection(
    tenant: str,
    app_name: str,
    secret_token: str,
    token_id: str,
) -> Vespa:
    """Connect to an already-deployed Vespa Cloud app using token auth."""
    vespa_cloud = VespaCloud(
        tenant=tenant,
        application=app_name,
        auth_client_token_id=token_id,
    )
    app: Vespa = vespa_cloud.get_application(
        endpoint_type="token",
        vespa_cloud_secret_token=secret_token,
    )
    return app


async def feed_documents(
    vespa_app: Vespa,
    payloads: list[VespaDocPayload],
    schema: str = "district_doc",
    max_concurrent: int = MAX_CONCURRENT_FEEDS,
) -> tuple[int, int]:
    """Feed documents to Vespa concurrently.

    Returns (succeeded, failed) counts.
    Uses asyncio.Semaphore to bound concurrent HTTP requests.
    """
    sem = asyncio.Semaphore(max_concurrent)
    total = len(payloads)
    start = time.time()
    results: list[bool] = []

    async def _feed_one(payload: VespaDocPayload) -> bool:
        async with sem:
            fields = _to_vespa_fields(payload)
            loop = asyncio.get_event_loop()
            try:
                response = await loop.run_in_executor(
                    None,
                    lambda p=payload, f=fields: vespa_app.feed_data_point(
                        schema=schema,
                        data_id=p.doc_id,
                        fields=f,
                        timeout=FEED_TIMEOUT,
                    ),
                )
                if response.status_code == 200:
                    return True
                logger.warning(
                    f"Feed {payload.doc_id}: status={response.status_code} "
                    f"msg={getattr(response, 'json', {})}"
                )
                return False
            except Exception as e:
                logger.error(f"Feed {payload.doc_id} failed: {e}")
                return False

    tasks = [_feed_one(p) for p in payloads]
    results = await asyncio.gather(*tasks)

    succeeded = sum(1 for r in results if r)
    failed = total - succeeded
    elapsed = time.time() - start

    rate = total / elapsed if elapsed > 0 else 0
    logger.info(
        f"Fed {total} docs in {elapsed:.1f}s "
        f"({succeeded} ok, {failed} failed, {rate:.1f} docs/sec)"
    )
    return succeeded, failed


def feed_documents_sync(
    vespa_app: Vespa,
    payloads: list[VespaDocPayload],
    schema: str = "district_doc",
    max_concurrent: int = MAX_CONCURRENT_FEEDS,
) -> tuple[int, int]:
    """Sync wrapper for feed_documents."""
    return asyncio.run(feed_documents(vespa_app, payloads, schema, max_concurrent))
