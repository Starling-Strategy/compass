"""Citation validation — deterministic helpers + google-genai curation.

Deduplicates citations, attempts exact substring matching against source
chunks, patches reasoning text with validated quotes, and generates
deterministic evidence IDs.  Non-exact matches fall back to an AI curation
call that corrects quotes to be exact substrings of the source text.
"""

import asyncio
import hashlib
import logging
import re
from contextlib import nullcontext as _nullcontext

try:
    import logfire
except ImportError:
    logfire = None

from pydantic import BaseModel, Field

from prediction_pipeline.config import Config
from prediction_pipeline.models import Chunk, CuratedCitation, ResolvedCitation

logger = logging.getLogger(__name__)


def collect_unique_citations(
    citations: list[ResolvedCitation],
    chunks: list[Chunk],
) -> list[tuple[ResolvedCitation, Chunk]]:
    """Deduplicate by (doc_id, quote), pair each with its source chunk."""
    seen: set[tuple[str, str]] = set()
    result: list[tuple[ResolvedCitation, Chunk]] = []
    for cit in citations:
        key = (cit.doc_id, cit.quote)
        if key in seen:
            continue
        seen.add(key)
        idx = cit.document_index - 1
        if 0 <= idx < len(chunks):
            result.append((cit, chunks[idx]))
        else:
            logger.warning(
                "Citation document_index %s out of range (have %s chunks)",
                cit.document_index, len(chunks),
            )
    return result


def build_curated_from_exact_match(
    citation: ResolvedCitation,
    chunk: Chunk,
) -> CuratedCitation | None:
    """Try exact substring match (case-sensitive). Returns CuratedCitation or None."""
    if citation.quote in chunk.text:
        return CuratedCitation(
            document_index=citation.document_index,
            doc_id=citation.doc_id,
            doc_name=citation.doc_name,
            original_quote=citation.quote,
            corrected_quote=citation.quote,
            verified=True,
            match_type="exact",
            page_number=citation.page_number,
            section_heading=citation.section_heading,
            relevance_score=citation.relevance_score,
        )
    return None


# ── Normalized Matching (Tier 1.5) ──

_MARKDOWN_STRIP_RE = re.compile(r"[*_#`~]")


def _normalize_text(text: str) -> str:
    """Collapse whitespace, strip markdown formatting, lowercase."""
    text = _MARKDOWN_STRIP_RE.sub("", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def _extract_original_substring(original: str, norm_start: int, norm_len: int) -> str:
    """Map a position in normalized text back to the original text.

    Walks the original tracking how many normalized characters have been consumed,
    to extract the corresponding exact substring.
    """
    orig_start = None
    orig_end = None
    norm_pos = 0

    i = 0
    while i < len(original):
        ch = original[i]
        # Skip markdown chars (same as _normalize_text strips)
        if ch in "*_#`~":
            i += 1
            continue
        # Whitespace run collapses to single space in normalized form
        if ch in " \t\n\r\f\v":
            # Consume the entire whitespace run
            ws_start = i
            while i < len(original) and original[i] in " \t\n\r\f\v":
                i += 1
            # Skip leading whitespace (normalize strips leading)
            if norm_pos == 0 and orig_start is None:
                continue
            # This run maps to one space in normalized text
            if norm_pos == norm_start and orig_start is None:
                orig_start = ws_start
            norm_pos += 1  # the space
            if norm_pos >= norm_start + norm_len:
                orig_end = i
                break
            continue
        # Regular character
        if norm_pos == norm_start and orig_start is None:
            orig_start = i
        norm_pos += 1
        i += 1
        if norm_pos >= norm_start + norm_len:
            orig_end = i
            break

    if orig_start is None:
        return ""
    if orig_end is None:
        orig_end = len(original)
    return original[orig_start:orig_end]


def build_curated_from_normalized_match(
    citation: ResolvedCitation,
    chunk: Chunk,
) -> CuratedCitation | None:
    """Tier 1.5: match after whitespace/case/markdown normalization.

    If a match is found, maps position back to original text to extract
    the exact substring as corrected_quote.
    """
    norm_quote = _normalize_text(citation.quote)
    norm_source = _normalize_text(chunk.text)
    if not norm_quote:
        return None

    idx = norm_source.find(norm_quote)
    if idx == -1:
        return None

    corrected = _extract_original_substring(chunk.text, idx, len(norm_quote))
    # Safety: verify the corrected quote actually appears in the source
    if corrected not in chunk.text:
        return None

    return CuratedCitation(
        document_index=citation.document_index,
        doc_id=citation.doc_id,
        doc_name=citation.doc_name,
        original_quote=citation.quote,
        corrected_quote=corrected,
        verified=True,
        match_type="normalized",
        page_number=citation.page_number,
        section_heading=citation.section_heading,
        relevance_score=citation.relevance_score,
    )


def _find_best_window(quote: str, source: str, max_chars: int = 8000) -> str:
    """Extract a window of source text centered around the likely quote location.

    Uses the first 40 chars of the quote (case-insensitive) to locate the region.
    Falls back to the first max_chars if no match found.
    """
    if len(source) <= max_chars:
        return source

    search_key = quote[:40].lower()
    pos = source.lower().find(search_key)
    if pos == -1:
        # Try with normalized whitespace
        norm_key = re.sub(r"\s+", " ", search_key).strip()
        norm_source = re.sub(r"\s+", " ", source.lower())
        pos = norm_source.find(norm_key)

    if pos == -1:
        return source[:max_chars]

    half = max_chars // 2
    start = max(0, pos - half)
    end = min(len(source), start + max_chars)
    if end - start < max_chars:
        start = max(0, end - max_chars)
    return source[start:end]


_CITATION_RE = re.compile(r'\[Citation\s+(\d+):\s*"([^"]*?)"\]')


def patch_reasoning(reasoning: str, curated: dict[int, CuratedCitation]) -> str:
    """Replace [Citation N: "quote"] with validated quotes. Strip rejected ones."""

    def _replace(match: re.Match) -> str:
        idx = int(match.group(1))
        cc = curated.get(idx)
        if cc is None:
            # No curation info — leave as-is
            return match.group(0)
        if cc.match_type == "rejected":
            return ""
        return f'[Citation {idx}: "{cc.corrected_quote}"]'

    result = _CITATION_RE.sub(_replace, reasoning)
    # Clean up double spaces left by stripped citations
    result = re.sub(r"  +", " ", result)
    return result.strip()


def make_evidence_id(
    district_id: int,
    ay_id: int,
    q_id: int,
    doc_id: str,
    quote: str = "",
) -> str:
    """Deterministic evidence ID: pp-{d}-{ay}-{q}-{doc_id}-{sha256[:8]}."""
    hash_input = quote.encode()
    sha = hashlib.sha256(hash_input).hexdigest()[:8]
    return f"pp-{district_id}-{ay_id}-{q_id}-{doc_id}-{sha}"


# ── AI Curation Models ──


class _CurationItem(BaseModel):
    """AI output for one citation validation."""
    citation_index: int = Field(description="1-based index matching the citation number in the prompt")
    corrected_quote: str = Field(
        description=(
            "The corrected quote that is an EXACT substring of the source text. "
            "Must appear character-for-character in the source. "
            "Empty string if the quote cannot be matched at all."
        )
    )
    verified: bool = Field(
        description=(
            "True if the original quote is semantically faithful to a passage in the source. "
            "False if fabricated, from a different document, or meaning materially changed."
        )
    )
    match_type: str = Field(
        description=(
            "'corrected' if meaning faithful but wording needed fixing. "
            "'rejected' if quote cannot be matched to any passage in the source."
        )
    )


class _CurationResult(BaseModel):
    """AI output: list of validated citations."""
    items: list[_CurationItem] = Field(description="One item per citation submitted for validation")


# ── System Prompt ──

_CURATION_SYSTEM = """You are a citation verification specialist. Your job is to validate
whether claimed quotes actually appear in their source documents.

For each citation you receive:
1. Compare the "Claimed quote" against the "Source text".
2. If the claimed quote is an exact substring of the source — mark verified=true, match_type="corrected",
   and set corrected_quote to the exact substring as it appears in the source.
3. If the claimed quote has minor errors (typos, missing words, truncation) but the meaning is faithful,
   find the closest passage in the source text and set corrected_quote to an EXACT substring of the source
   that captures the same meaning. Mark verified=true, match_type="corrected".
4. If the claimed quote is fabricated, from a different document, or the meaning is materially different
   from anything in the source text, mark verified=false, match_type="rejected", corrected_quote="".

CRITICAL RULES:
- corrected_quote MUST be an exact, character-for-character substring of the source text.
- Do NOT paraphrase or rewrite. Copy the exact characters from the source.
- When in doubt about faithfulness, prefer "corrected" over "rejected" if ANY passage
  in the source supports the same factual claim."""


# ── Prompt Builder ──


def build_curation_prompt(
    pairs: list[tuple[ResolvedCitation, Chunk]],
    max_source_chars: int = 8000,
) -> str:
    """Build a numbered prompt with claimed quotes and source text for AI validation."""
    sections: list[str] = []
    for i, (citation, chunk) in enumerate(pairs, 1):
        source_text = _find_best_window(citation.quote, chunk.text, max_source_chars)
        sections.append(
            f"## Citation {i}\n"
            f"Document: {citation.doc_name} (doc_id: {citation.doc_id})\n\n"
            f"Claimed quote:\n> {citation.quote}\n\n"
            f"Source text:\n{source_text}"
        )
    return "\n\n---\n\n".join(sections)


# ── Two-Tier Validation ──


async def validate_citations_batch(
    pairs: list[tuple[ResolvedCitation, Chunk]],
    config: Config,
) -> list[CuratedCitation]:
    """Validate citations: exact match first, AI fallback for the rest.

    Returns CuratedCitation list in the same order as input pairs.
    """
    with logfire.span("validate_citations_batch", n_pairs=len(pairs)) if logfire else _nullcontext():
        results: list[CuratedCitation | None] = [None] * len(pairs)

        # Tier 1: exact substring match
        ai_needed: list[tuple[int, ResolvedCitation, Chunk]] = []
        for i, (cit, chunk) in enumerate(pairs):
            curated = build_curated_from_exact_match(cit, chunk)
            if curated is not None:
                results[i] = curated
                continue
            # Tier 1.5: normalized match (whitespace/case/markdown)
            curated = build_curated_from_normalized_match(cit, chunk)
            if curated is not None:
                results[i] = curated
                continue
            ai_needed.append((i, cit, chunk))

        if not ai_needed:
            return results  # type: ignore[return-value]

        # Tier 2: AI curation in sub-batches via google-genai
        batch_size = config.curation_batch_size
        timeout = config.curation_timeout
        max_source_chars = config.curation_max_source_chars

        from prediction_pipeline.predict import _get_genai_client
        from google.genai import types

        for batch_start in range(0, len(ai_needed), batch_size):
            batch = ai_needed[batch_start:batch_start + batch_size]
            ai_pairs = [(cit, chunk) for _, cit, chunk in batch]
            try:
                prompt = build_curation_prompt(ai_pairs, max_source_chars=max_source_chars)
                gen_config = types.GenerateContentConfig(
                    system_instruction=_CURATION_SYSTEM,
                    temperature=config.prediction_temperature,
                    max_output_tokens=2048,
                    response_mime_type="application/json",
                    response_schema=_CurationResult,
                )
                client = _get_genai_client()
                response = await client.aio.models.generate_content(
                    model=config.prediction_model,
                    contents=prompt,
                    config=gen_config,
                )

                # Track cost
                try:
                    from prediction_pipeline.cost_tracker import get_tracker
                    get_tracker(config.prediction_model).record(response)
                except Exception:
                    pass

                ai_result = _CurationResult.model_validate_json(response.text)
                ai_items_by_index = {item.citation_index: item for item in ai_result.items}

                for j, (orig_idx, cit, chunk) in enumerate(batch):
                    item = ai_items_by_index.get(j + 1)
                    if item is None:
                        results[orig_idx] = CuratedCitation(
                            document_index=cit.document_index,
                            doc_id=cit.doc_id,
                            doc_name=cit.doc_name,
                            original_quote=cit.quote,
                            corrected_quote="",
                            verified=False,
                            match_type="rejected",
                            page_number=cit.page_number,
                            section_heading=cit.section_heading,
                            relevance_score=cit.relevance_score,
                        )
                    else:
                        results[orig_idx] = CuratedCitation(
                            document_index=cit.document_index,
                            doc_id=cit.doc_id,
                            doc_name=cit.doc_name,
                            original_quote=cit.quote,
                            corrected_quote=item.corrected_quote,
                            verified=item.verified,
                            match_type=item.match_type if item.match_type in ("corrected", "rejected") else "corrected",
                            page_number=cit.page_number,
                            section_heading=cit.section_heading,
                            relevance_score=cit.relevance_score,
                        )
            except Exception:
                logger.exception(
                    "AI curation failed for sub-batch of %d citations (batch %d-%d), rejecting",
                    len(batch), batch_start, batch_start + len(batch),
                )
                for orig_idx, cit, chunk in batch:
                    results[orig_idx] = CuratedCitation(
                        document_index=cit.document_index,
                        doc_id=cit.doc_id,
                        doc_name=cit.doc_name,
                        original_quote=cit.quote,
                        corrected_quote="",
                        verified=False,
                        match_type="rejected",
                        page_number=cit.page_number,
                        section_heading=cit.section_heading,
                        relevance_score=cit.relevance_score,
                    )

        return results  # type: ignore[return-value]
