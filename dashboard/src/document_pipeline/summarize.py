"""Chunk summarization using Gemini 2.5 Flash-Lite.

Generates 1-2 sentence summaries per chunk with surrounding context.
Processes chunks concurrently via asyncio semaphore for throughput.

Flash-Lite pricing: $0.10/1M input, $0.40/1M output.
At ~2K input + ~50 output tokens per chunk, cost is negligible.
"""

import asyncio
import logging
import os

try:
    import logfire
except ImportError:
    logfire = None

logger = logging.getLogger(__name__)

SUMMARY_MODEL = "gemini-2.5-flash-lite"
MAX_CONCURRENT = 16  # Flash-Lite handles high concurrency
CONTEXT_WINDOW = 400  # chars from adjacent chunks for context

SUMMARY_PROMPT = """Summarize this text chunk from a {doc_type} document titled "{doc_title}" \
in 1-2 sentences. Focus on specific policy details, numbers, dates, or rules stated.

{context_section}
--- CHUNK TO SUMMARIZE ---
{chunk}
--- END CHUNK ---

Write a concise 1-2 sentence summary:"""


def _build_context_section(prev_chunk: str | None, next_chunk: str | None) -> str:
    """Build the context section of the prompt from adjacent chunks."""
    parts = []
    if prev_chunk:
        parts.append(f"Previous context: ...{prev_chunk[-CONTEXT_WINDOW:]}")
    if next_chunk:
        parts.append(f"Next context: {next_chunk[:CONTEXT_WINDOW]}...")
    return "\n".join(parts) if parts else "This chunk has no surrounding context."


MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0


async def _summarize_one(
    client,
    chunk_text: str,
    doc_title: str,
    doc_type: str,
    prev_chunk: str | None,
    next_chunk: str | None,
    sem: asyncio.Semaphore,
    chunk_idx: int,
) -> tuple[str, str | None]:
    """Summarize a single chunk with rate limiting and retry.

    Returns (summary_text, error_message_or_None).
    """
    async with sem:
        prompt = SUMMARY_PROMPT.format(
            doc_type=doc_type,
            doc_title=doc_title,
            context_section=_build_context_section(prev_chunk, next_chunk),
            chunk=chunk_text,
        )
        loop = asyncio.get_event_loop()
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                response = await loop.run_in_executor(
                    None,
                    lambda: client.models.generate_content(
                        model=SUMMARY_MODEL,
                        contents=prompt,
                        config={
                            "max_output_tokens": 150,
                            "temperature": 0.1,
                        },
                    ),
                )
                summary = response.text.strip()
                logger.debug(f"  Chunk {chunk_idx}: {summary[:80]}...")
                return summary, None
            except Exception as e:
                last_error = e
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                err_name = type(e).__name__
                logger.warning(
                    f"  Chunk {chunk_idx} summary attempt {attempt+1}/{MAX_RETRIES} "
                    f"failed ({err_name}): {e} — retrying in {delay:.0f}s"
                )
                await asyncio.sleep(delay)

        err_msg = f"summarize failed after {MAX_RETRIES} retries: {type(last_error).__name__}: {last_error}"
        logger.error(f"  Chunk {chunk_idx}: {err_msg}")
        return chunk_text[:200], err_msg


async def summarize_chunks(
    chunk_texts: list[str],
    doc_title: str,
    doc_type: str = "policy",
    max_concurrent: int = MAX_CONCURRENT,
) -> tuple[list[str], list[str]]:
    """Summarize all chunks concurrently with adjacent-chunk context.

    Returns (summaries, errors) — both parallel to the input chunk_texts.
    errors[i] is None if chunk i succeeded, else an error description string.
    """
    if not chunk_texts:
        return [], []

    from google import genai
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("DOCPIPE_GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("Set GOOGLE_API_KEY or DOCPIPE_GOOGLE_API_KEY")
    client = genai.Client(api_key=api_key)

    sem = asyncio.Semaphore(max_concurrent)
    tasks = []

    for i, chunk in enumerate(chunk_texts):
        prev_chunk = chunk_texts[i - 1] if i > 0 else None
        next_chunk = chunk_texts[i + 1] if i < len(chunk_texts) - 1 else None
        tasks.append(
            _summarize_one(client, chunk, doc_title, doc_type, prev_chunk, next_chunk, sem, i)
        )

    results = await asyncio.gather(*tasks)
    summaries = [r[0] for r in results]
    errors = [r[1] for r in results]
    return summaries, errors


def summarize_chunks_sync(
    chunk_texts: list[str],
    doc_title: str,
    doc_type: str = "policy",
    max_concurrent: int = MAX_CONCURRENT,
) -> tuple[list[str], list[str]]:
    """Sync wrapper for summarize_chunks."""
    return asyncio.run(
        summarize_chunks(chunk_texts, doc_title, doc_type, max_concurrent)
    )
