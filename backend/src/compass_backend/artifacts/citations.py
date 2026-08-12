"""Citation artifacts for deterministic Compass results."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

_DOC_TYPE_MAP: dict[str, str] = {
    "contract": "Contract",
    "salary schedule": "Salary Schedule",
    "evaluation handbook": "Evaluation Handbook",
    "benefits handbook": "Benefits Handbook",
    "annual calendar": "Annual Calendar",
    "board policy": "Board Policy",
    "board policies": "Board Policy",
    "board regulation": "Board Policy",
    "other": "Other",
    "website": "Website",
    "state law": "State Law",
    "union document": "Union Document",
}
_YEAR_RE = re.compile(r"\((\d{4})(?:\s*-\s*(\d{4}))?\)")
_PAGE_RE = re.compile(
    r"""
    (?:^|[.\s])
    (p{1,2})\.\s*
    (\d+)
    (
        (?:\s*[-&,]\s*\d+)*
        (?:\s*\(pdf\))?
        (?:\s*[^.]*)?
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
_BOARD_POLICY_RE = re.compile(
    r"^(.+?)\.\s*([A-Z]{2})\.\s*Board\s+Polic(?:y|ies)",
    re.IGNORECASE,
)


class CitationRef(BaseModel):
    """A source reference attached to a deterministic result artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    marker: int = Field(ge=1)
    title: str = Field(min_length=1)
    url: str | None = None
    page_number: int | None = None
    page_ref: str | None = None
    academic_year: str | None = None
    document_type: str | None = None
    district_id: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CitationSource(BaseModel):
    """Raw source metadata from the policy answer source join."""

    model_config = ConfigDict(extra="forbid")

    source_name: str | None = None
    source_url: str | None = None
    document_type: str | None = None
    academic_year: str | None = None
    district_id: int | None = None
    citation_order: int | None = None


@dataclass(frozen=True)
class ParsedSourceName:
    """Small parsed shape extracted from a TCD source name."""

    title: str
    document_type: str | None
    page_number: int | None
    page_ref: str | None


def parse_source_name(source_name: str | None) -> ParsedSourceName:
    """Parse a TCD answer source name into citation display fields."""

    if not source_name or not source_name.strip():
        return ParsedSourceName(
            title="",
            document_type=None,
            page_number=None,
            page_ref=None,
        )

    text = source_name.strip()
    page_number, page_ref = _extract_pages(text)

    board_policy_match = _BOARD_POLICY_RE.match(text)
    if board_policy_match:
        district = board_policy_match.group(1).strip()
        return ParsedSourceName(
            title=_clean_title(district, "Board Policy", None),
            document_type="Board Policy",
            page_number=page_number,
            page_ref=page_ref,
        )

    year_match = _YEAR_RE.search(text)
    if year_match:
        year_range = year_match.group(0)[1:-1]
        before_year = text[: year_match.start()].rstrip(". ")
        state_match = re.search(r"\.\s*([A-Z]{2})\s*$", before_year)
        district = (
            before_year[: state_match.start()].rstrip(". ")
            if state_match
            else before_year
        )
        after_year = text[year_match.end() :].lstrip(". ")
        document_type = _extract_doc_type(after_year)
        return ParsedSourceName(
            title=_clean_title(district, document_type, year_range),
            document_type=document_type,
            page_number=page_number,
            page_ref=page_ref,
        )

    state_match = re.match(r"^(.+?)\.\s*([A-Z]{2})\.", text)
    if state_match:
        district = state_match.group(1).strip()
        remainder = text[state_match.end() :].strip()
        document_type = _extract_doc_type(remainder)
        return ParsedSourceName(
            title=_clean_title(district, document_type, None),
            document_type=document_type,
            page_number=page_number,
            page_ref=page_ref,
        )

    return ParsedSourceName(
        title=text,
        document_type=None,
        page_number=page_number,
        page_ref=page_ref,
    )


def citation_ref_from_source(source: CitationSource, *, marker: int) -> CitationRef:
    """Build a public citation reference from raw answer source metadata."""

    parsed = parse_source_name(source.source_name)
    document_type = source.document_type or parsed.document_type
    metadata: dict[str, Any] = {}
    if source.citation_order is not None:
        metadata["citation_order"] = source.citation_order

    return CitationRef(
        marker=marker,
        title=parsed.title or _fallback_title(document_type, source.source_url),
        url=source.source_url,
        page_number=parsed.page_number,
        page_ref=parsed.page_ref,
        academic_year=source.academic_year,
        document_type=document_type,
        district_id=source.district_id,
        metadata=metadata,
    )


def _extract_doc_type(text: str) -> str | None:
    cleaned = re.sub(r"\s+accessed\s+.*$", "", text, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"\.\s*$", "", cleaned)
    for raw, normalized in _DOC_TYPE_MAP.items():
        if cleaned.casefold().startswith(raw):
            return normalized
    return None


def _extract_pages(text: str) -> tuple[int | None, str | None]:
    match = _PAGE_RE.search(text)
    if match is None:
        return None, None
    page_number = int(match.group(2))
    continuation = match.group(3).strip() if match.group(3) else ""
    page_ref = f"{match.group(1)}. {match.group(2)}{continuation}".strip()
    page_ref = re.sub(r"[.\s]+$", "", page_ref)
    return page_number, page_ref


def _clean_title(
    district: str,
    document_type: str | None,
    year_range: str | None,
) -> str:
    parts = [district.strip()]
    if document_type:
        parts.append(document_type)
    title = " ".join(part for part in parts if part)
    if year_range:
        title += f", {year_range}"
    return title


def _fallback_title(document_type: str | None, source_url: str | None) -> str:
    if source_url:
        hostname = urlparse(source_url).hostname
        if hostname:
            return hostname.removeprefix("www.")
    if document_type:
        return document_type
    return "Source"
