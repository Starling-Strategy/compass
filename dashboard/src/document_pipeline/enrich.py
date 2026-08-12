"""AI enrichment using PydanticAI + Gemini.

The DocumentEnrichment model's Field descriptions are Gemini's instructions.
"""

import asyncio
import logging
import os
from contextlib import nullcontext as _nullcontext

try:
    import logfire
except ImportError:
    logfire = None

from pydantic_ai import Agent

from document_pipeline.config import Config
from document_pipeline.models import Document, DocumentEnrichment

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a document metadata extraction specialist for school district policy documents.

Extract structured metadata from the provided document text. Key guidelines:

1. TITLE: Extract or compose a clear, human-readable title. Include district name, document type, and year range.

2. DOCUMENT TYPE: Classify into one of NCTQ's 8 standard categories:
   - salary_schedule: Pay scales, compensation tables, salary grids
   - annual_calendar: School year calendars, academic calendars
   - evaluation_handbook: Teacher/staff evaluation procedures and rubrics
   - contract: Collective bargaining agreements, union contracts, MOUs
   - union_document: Union-related docs that aren't contracts (newsletters, grievance forms)
   - benefits_handbook: Health insurance, retirement, leave policies
   - board_policy: Board policies, administrative regulations, bylaws
   - other: Anything that doesn't fit the above categories

3. ACADEMIC YEARS: Use the ay_id system where ay_id 25 = school year 2024-2025. A contract spanning 2019-2024 = ay_ids [20, 21, 22, 23, 24]. If the document has no time-bound dates, return an empty list.

4. READABILITY: Rate the extracted text quality honestly. If tables are garbled, OCR has errors, or content is missing, say so.

5. CONFIDENCE: Rate your confidence based on text quality. Garbled OCR = low confidence.

If information is not clearly stated, use null/empty rather than guessing."""

# Lazy singletons
_agent: Agent | None = None
_loop: asyncio.AbstractEventLoop | None = None


def _get_agent(config: Config) -> Agent:
    global _agent
    if _agent is not None:
        return _agent

    api_key = config.google_api_key or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("Set GOOGLE_API_KEY in .env")
    os.environ["GOOGLE_API_KEY"] = api_key

    _agent = Agent(
        model=f"google-gla:{config.enrichment_model}",
        output_type=DocumentEnrichment,
        system_prompt=SYSTEM_PROMPT,
    )
    return _agent


def _build_prompt(doc: Document, config: Config) -> str:
    """Build the enrichment prompt for a document."""
    text = doc.full_text
    max_chars = config.max_text_for_enrichment
    if len(text) > max_chars:
        half = max_chars // 2
        text = text[:half] + "\n\n[...TRUNCATED...]\n\n" + text[-half:]

    prompt = f"Document text:\n\n{text}"
    prompt += f"\n\nOriginal filename: {doc.src_name}"
    prompt += f"\nDistrict: {doc.district_name}"
    if doc.human_ay_ids:
        prompt += f"\nKnown academic years from human coders: {doc.human_ay_ids}"
    return prompt


def _apply_enrichment(doc: Document, enrichment: DocumentEnrichment) -> Document:
    """Apply enrichment results to a document."""
    doc.ai_title = enrichment.ai_title
    doc.ai_summary = enrichment.ai_summary
    doc.ai_document_type = enrichment.ai_document_type.value
    doc.ai_ay_ids = enrichment.ai_ay_ids
    doc.ai_temporal_class = enrichment.ai_temporal_class.value
    doc.ai_readability = enrichment.ai_readability.value
    doc.ai_confidence = enrichment.ai_confidence
    return doc


def enrich(doc: Document, config: Config) -> Document:
    """Enrich a document with AI-extracted metadata (sync wrapper).

    Runs Gemini on the extracted text. Populates ai_* fields on the Document.
    If enrichment fails, sets ai_confidence to 0.0 and logs the error.
    """
    if not doc.full_text or doc.extraction_status != "success":
        return doc

    try:
        with logfire.span(
            "enrich_document",
            src_id=doc.src_id,
            district_id=doc.district_id,
            text_length=doc.text_length,
        ) if logfire else _nullcontext():
            agent = _get_agent(config)
            prompt = _build_prompt(doc, config)

            # Reuse a single event loop across calls (asyncio.run closes it after each use)
            global _loop
            if _loop is None or _loop.is_closed():
                _loop = asyncio.new_event_loop()
            result = _loop.run_until_complete(agent.run(prompt))
            _apply_enrichment(doc, result.output)

            logger.info(
                "Enriched %s: %s (confidence=%s, readability=%s)",
                doc.src_id, doc.ai_title, doc.ai_confidence, doc.ai_readability,
            )

    except Exception as e:
        doc.ai_confidence = 0.0
        logger.error("Enrichment failed for %s: %s", doc.src_id, e)

    return doc


async def enrich_async(doc: Document, config: Config) -> Document:
    """Enrich a document with AI-extracted metadata (async native).

    Same as enrich() but uses PydanticAI's native async interface.
    For use in the parallel pipeline.
    """
    if not doc.full_text or doc.extraction_status != "success":
        return doc

    try:
        agent = _get_agent(config)
        prompt = _build_prompt(doc, config)
        result = await agent.run(prompt)
        _apply_enrichment(doc, result.output)

        logger.info(
            "Enriched %s: %s (confidence=%s, readability=%s)",
            doc.src_id, doc.ai_title, doc.ai_confidence, doc.ai_readability,
        )

    except Exception as e:
        doc.ai_confidence = 0.0
        logger.error("Enrichment failed for %s: %s", doc.src_id, e)

    return doc
