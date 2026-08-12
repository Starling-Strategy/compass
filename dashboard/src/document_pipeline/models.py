"""Document model — the single source of truth for the pipeline.

One Document flows through all stages, progressively filled in.
Field descriptions serve as both documentation AND Gemini instructions.
"""

import hashlib
from datetime import date
from enum import Enum

from pydantic import BaseModel, Field, computed_field


class NctqDocumentType(str, Enum):
    """Document classification matching NCTQ's 8 human-coded categories.

    Used for BOTH human classification (from bronze.district_sources.src_type)
    and AI classification (from Gemini enrichment). Same enum enables
    apples-to-apples agreement analysis.
    """
    SALARY_SCHEDULE = "salary_schedule"
    ANNUAL_CALENDAR = "annual_calendar"
    EVALUATION_HANDBOOK = "evaluation_handbook"
    CONTRACT = "contract"
    UNION_DOCUMENT = "union_document"
    BENEFITS_HANDBOOK = "benefits_handbook"
    BOARD_POLICY = "board_policy"
    OTHER = "other"


def normalize_bronze_doc_type(value: str | None) -> NctqDocumentType:
    """Map bronze.district_sources.src_type Title Case to NctqDocumentType.

    Bronze uses Title Case ("Salary Schedule"), we store snake_case.
    Anything unrecognized maps to OTHER.
    """
    if not value:
        return NctqDocumentType.OTHER
    if isinstance(value, NctqDocumentType):
        return value
    mapping = {
        "salary schedule": NctqDocumentType.SALARY_SCHEDULE,
        "annual calendar": NctqDocumentType.ANNUAL_CALENDAR,
        "evaluation handbook": NctqDocumentType.EVALUATION_HANDBOOK,
        "contract": NctqDocumentType.CONTRACT,
        "union document": NctqDocumentType.UNION_DOCUMENT,
        "benefits handbook": NctqDocumentType.BENEFITS_HANDBOOK,
        "board policy": NctqDocumentType.BOARD_POLICY,
        "other": NctqDocumentType.OTHER,
    }
    return mapping.get(value.lower().strip(), NctqDocumentType.OTHER)


class TemporalClass(str, Enum):
    SINGLE_YEAR = "single_year"
    MULTI_YEAR = "multi_year"
    TIMELESS = "timeless"


class Readability(str, Enum):
    GOOD = "good"
    FAIR = "fair"
    POOR = "poor"


class DocumentEnrichment(BaseModel):
    """Output schema for Gemini enrichment. Field descriptions are Gemini's instructions."""

    ai_title: str = Field(
        description="Clear human-readable title. Include the district name, "
        "document type, and year range if apparent. "
        "E.g. 'Broward County School Calendar 2018-2019'"
    )
    ai_summary: str = Field(
        min_length=20,
        max_length=500,
        description="2-3 sentence summary of what this document covers "
        "and what policy areas it addresses.",
    )
    ai_document_type: NctqDocumentType = Field(
        description="Classification of document type. Choose from: "
        "salary_schedule (pay scales, compensation tables, salary grids), "
        "annual_calendar (school year calendars, academic calendars), "
        "evaluation_handbook (teacher/staff evaluation procedures and rubrics), "
        "contract (collective bargaining agreements, union contracts, MOUs), "
        "union_document (union-related docs that aren't contracts — newsletters, grievance forms), "
        "benefits_handbook (health insurance, retirement, leave policies), "
        "board_policy (board policies, administrative regulations, bylaws), "
        "other (anything that doesn't fit the above categories)."
    )
    ai_ay_ids: list[int] = Field(
        default_factory=list,
        description="Academic year IDs this document is relevant to. "
        "ay_id 25 = 2024-2025, ay_id 26 = 2025-2026. "
        "A calendar is usually one year. A contract may span multiple years. "
        "A policy handbook with no dates may be timeless (empty list).",
    )
    ai_temporal_class: TemporalClass = Field(
        description="SINGLE_YEAR (applies to one school year), "
        "MULTI_YEAR (spans specific years), or "
        "TIMELESS (no expiration, like a policy handbook).",
    )
    ai_readability: Readability = Field(
        description="Rate the extracted text quality. "
        "GOOD: text is clean, tables well-formed, structure clear. "
        "FAIR: mostly readable but some tables garbled or formatting lost. "
        "POOR: significant OCR errors, garbled text, or missing content.",
    )
    ai_confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="0.0-1.0 confidence in this enrichment. "
        "Low if text is garbled, very short, or ambiguous.",
    )


class Document(BaseModel):
    """A district policy document moving through the pipeline."""

    # ── Identity (from bronze, read-only) ──
    src_id: int
    district_id: int
    district_name: str
    src_name: str  # ugly filename from NCTQ
    src_link: str  # blob URL
    src_type: NctqDocumentType
    valid_from: date | None = None
    valid_to: date | None = None

    # ── Academic years (human vs AI) ──
    human_ay_ids: list[int] = Field(default_factory=list)

    # ── Extraction (from Docling) ──
    full_text: str | None = None
    text_length: int = 0
    text_hash: str | None = None
    page_count: int | None = None
    extraction_status: str = "pending"
    extraction_error: str | None = None

    # ── AI Enrichment (from Gemini) ──
    ai_title: str | None = None
    ai_summary: str | None = None
    ai_document_type: str | None = None
    ai_ay_ids: list[int] | None = None
    ai_temporal_class: str | None = None
    ai_readability: str | None = None
    ai_confidence: float | None = None

    # ── Computed quality signals ──
    @computed_field
    @property
    def effective_ay_ids(self) -> list[int]:
        """Resolved academic years: human wins, AI fills gaps."""
        if self.human_ay_ids:
            return self.human_ay_ids
        if self.ai_ay_ids:
            return self.ai_ay_ids
        return []

    @computed_field
    @property
    def effective_doc_type(self) -> str:
        """Resolved document type: human wins, AI fills gaps.

        src_type always has a value (defaults to OTHER via normalize_bronze_doc_type),
        so AI only wins when human is OTHER and AI has something more specific.
        """
        if self.src_type != NctqDocumentType.OTHER:
            return self.src_type.value
        if self.ai_document_type and self.ai_document_type != "other":
            return self.ai_document_type
        return self.src_type.value

    @computed_field
    @property
    def has_content(self) -> bool:
        return self.text_length > 200

    @computed_field
    @property
    def ay_alignment(self) -> str | None:
        """How well AI and human year assignments agree."""
        if not self.ai_ay_ids or not self.human_ay_ids:
            return None
        if set(self.ai_ay_ids) == set(self.human_ay_ids):
            return "exact_match"
        if set(self.ai_ay_ids) & set(self.human_ay_ids):
            return "partial_overlap"
        return "disagreement"

    @computed_field
    @property
    def type_alignment(self) -> str | None:
        """Compare human vs AI document type classification."""
        if not self.ai_document_type:
            return None
        if self.src_type.value == self.ai_document_type:
            return "agree"
        return "disagree"

    @computed_field
    @property
    def quality_flags(self) -> list[str]:
        """Problems detected. Empty list = healthy document."""
        flags = []
        if self.extraction_status == "failed":
            flags.append("extraction_failed")
        if 0 < self.text_length < 500:
            flags.append("suspiciously_short")
        if self.ai_confidence is not None and self.ai_confidence < 0.5:
            flags.append("low_ai_confidence")
        if self.full_text and len(set(self.full_text[:1000])) < 20:
            flags.append("garbled_ocr")
        if self.full_text and "\n#" not in self.full_text:
            flags.append("no_structure")
        return flags

    @computed_field
    @property
    def table_count(self) -> int:
        """Count of markdown tables in extracted text."""
        if not self.full_text:
            return 0
        return self.full_text.count("\n|") // 2

    def compute_text_hash(self) -> str:
        """SHA-256 hash of full_text for deduplication."""
        if not self.full_text:
            return ""
        return hashlib.sha256(self.full_text.encode()).hexdigest()
