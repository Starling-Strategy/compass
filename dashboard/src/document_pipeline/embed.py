"""Batch embedding via gemini-embedding-001.

Pre-computes embeddings client-side for feeding to Vespa Cloud.
Uses asyncio + semaphore for parallel API calls with rate limiting.

Embedding-001 specs:
- Max input: 2048 tokens
- Output: configurable dimensions (3072/1536/768) via Matryoshka
- Pricing: $0.15/1M tokens
- Batch size: up to 100 texts per API call
"""

import asyncio
import logging
import os
import time
from contextlib import nullcontext as _nullcontext

from google import genai
from google.genai.types import EmbedContentConfig

try:
    import logfire
except ImportError:
    logfire = None

logger = logging.getLogger(__name__)

EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 768
API_BATCH_SIZE = 100  # Gemini batch_embed_contents limit
MAX_CONCURRENT_BATCHES = 8
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0


def _get_client():
    """Lazy-init the google.genai client."""
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("DOCPIPE_GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("Set GOOGLE_API_KEY or DOCPIPE_GOOGLE_API_KEY")
    return genai.Client(api_key=api_key)


def embed_texts_sync(
    texts: list[str],
    task_type: str = "RETRIEVAL_DOCUMENT",
    dim: int = EMBED_DIM,
) -> list[list[float]]:
    """Embed a list of texts synchronously. Handles internal batching.

    For small batches or when called outside an async context.
    """
    if not texts:
        return []

    client = _get_client()
    all_embeddings: list[list[float]] = []

    for i in range(0, len(texts), API_BATCH_SIZE):
        batch = texts[i : i + API_BATCH_SIZE]
        result = client.models.embed_content(
            model=EMBED_MODEL,
            contents=batch,
            config=EmbedContentConfig(
                task_type=task_type,
                output_dimensionality=dim,
            ),
        )
        all_embeddings.extend(e.values for e in result.embeddings)

    return all_embeddings


async def embed_texts_async(
    texts: list[str],
    task_type: str = "RETRIEVAL_DOCUMENT",
    dim: int = EMBED_DIM,
    max_concurrent: int = MAX_CONCURRENT_BATCHES,
) -> list[list[float]]:
    """Embed a list of texts with parallel API batches.

    Splits into API_BATCH_SIZE chunks and fires them concurrently
    (bounded by semaphore to avoid rate limits).
    """
    if not texts:
        return []

    client = _get_client()
    sem = asyncio.Semaphore(max_concurrent)
    batches = [texts[i : i + API_BATCH_SIZE] for i in range(0, len(texts), API_BATCH_SIZE)]
    results: list[list[list[float]]] = [[] for _ in batches]

    async def _embed_batch(idx: int, batch: list[str]):
        async with sem:
            loop = asyncio.get_event_loop()
            last_error = None
            for attempt in range(MAX_RETRIES):
                try:
                    result = await loop.run_in_executor(
                        None,
                        lambda: client.models.embed_content(
                            model=EMBED_MODEL,
                            contents=batch,
                            config=EmbedContentConfig(
                                task_type=task_type,
                                output_dimensionality=dim,
                            ),
                        ),
                    )
                    results[idx] = [e.values for e in result.embeddings]
                    return
                except Exception as e:
                    last_error = e
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        "Embed batch %s attempt %s/%s failed (%s): %s — retrying in %.0fs",
                        idx, attempt + 1, MAX_RETRIES, type(e).__name__, e, delay,
                    )
                    await asyncio.sleep(delay)
            raise RuntimeError(
                f"embed batch {idx} failed after {MAX_RETRIES} retries: "
                f"{type(last_error).__name__}: {last_error}"
            )

    tasks = [_embed_batch(i, batch) for i, batch in enumerate(batches)]
    await asyncio.gather(*tasks)

    all_embeddings: list[list[float]] = []
    for batch_result in results:
        all_embeddings.extend(batch_result)

    return all_embeddings


async def embed_document_chunks(
    chunk_texts: list[str],
    doc_summary: str,
    max_concurrent: int = MAX_CONCURRENT_BATCHES,
) -> tuple[list[list[float]], list[float]]:
    """Embed all chunks + the document summary in parallel.

    Returns (chunk_embeddings, doc_embedding).
    Runs chunk and doc embedding concurrently for maximum throughput.
    """
    chunk_task = embed_texts_async(
        chunk_texts,
        task_type="RETRIEVAL_DOCUMENT",
        max_concurrent=max_concurrent,
    )
    doc_task = embed_texts_async(
        [doc_summary],
        task_type="RETRIEVAL_DOCUMENT",
        max_concurrent=1,
    )

    chunk_embeddings, doc_embeddings = await asyncio.gather(chunk_task, doc_task)
    doc_embedding = doc_embeddings[0] if doc_embeddings else [0.0] * EMBED_DIM

    return chunk_embeddings, doc_embedding


def embed_query_sync(query: str, dim: int = EMBED_DIM) -> list[float]:
    """Embed a single search query (sync, for retrieval time)."""
    with logfire.span(
        "embed_query", query_length=len(query), model=EMBED_MODEL,
    ) if logfire else _nullcontext():
        result = embed_texts_sync([query], task_type="RETRIEVAL_QUERY", dim=dim)
        logger.debug("Embedded query (%d chars) -> %d-d vector", len(query), len(result[0]))
        return result[0]
