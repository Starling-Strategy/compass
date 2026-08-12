"""Pydantic models for the Documents section of the NCTQ.ai dashboard.

Ported from document_pipeline.dashboard.models with computed_field upgrades.
"""

from pydantic import BaseModel, computed_field


class DashboardModel(BaseModel):
    """Base model with extra='ignore' for safe SQL row unpacking."""

    model_config = {"extra": "ignore"}


class DocumentStats(DashboardModel):
    """Aggregate stats for the KPI health cards on /docs."""

    total: int = 0
    success: int = 0
    failed: int = 0
    pending: int = 0
    avg_confidence: float | None = None
    readability_good: int = 0
    readability_fair: int = 0
    readability_poor: int = 0
    ay_exact_match: int = 0
    ay_total_enriched: int = 0
    type_agree: int = 0
    type_total: int = 0

    @computed_field
    @property
    def success_pct(self) -> int:
        return round(self.success / self.total * 100) if self.total else 0

    @computed_field
    @property
    def ay_alignment_pct(self) -> int:
        return round(self.ay_exact_match / self.ay_total_enriched * 100) if self.ay_total_enriched else 0

    @computed_field
    @property
    def type_alignment_pct(self) -> int:
        return round(self.type_agree / self.type_total * 100) if self.type_total else 0


class DocumentSummary(DashboardModel):
    """One row in the document list table on /docs."""

    src_id: int
    district_id: int
    district_name: str | None = None
    ai_title: str | None = None
    src_name: str | None = None
    src_type: str | None = None
    ai_document_type: str | None = None
    human_ay_ids: list | None = None
    ai_ay_ids: list | None = None
    ai_readability: str | None = None
    ai_confidence: float | None = None
    text_length: int | None = None
    extraction_status: str | None = None

    @computed_field
    @property
    def display_title(self) -> str:
        return self.ai_title or self.src_name or f"Document {self.src_id}"

    @computed_field
    @property
    def ay_display(self) -> str:
        ids = self.ai_ay_ids or self.human_ay_ids or []
        if not ids:
            return "\u2014"
        formatted = [f"20{int(ay)-1:02d}-20{int(ay):02d}" for ay in sorted(ids)]
        return ", ".join(formatted)


class DocumentDetail(DashboardModel):
    """Full document for the /docs/{src_id} detail page."""

    src_id: int
    district_id: int
    district_name: str | None = None
    src_name: str | None = None
    src_link: str | None = None
    src_type: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    extraction_status: str | None = None
    extraction_error: str | None = None
    full_text: str | None = None
    text_length: int | None = None
    text_hash: str | None = None
    ai_title: str | None = None
    ai_summary: str | None = None
    ai_document_type: str | None = None
    ai_temporal_class: str | None = None
    ai_readability: str | None = None
    ai_confidence: float | None = None
    human_ay_ids: list | None = None
    ai_ay_ids: list | None = None
    effective_ay_ids: list | None = None

    @computed_field
    @property
    def display_title(self) -> str:
        return self.ai_title or self.src_name or f"Document {self.src_id}"

    @computed_field
    @property
    def quality_flags(self) -> list[str]:
        flags = []
        if self.extraction_status == "failed":
            flags.append("extraction_failed")
        if self.text_length and 0 < self.text_length < 500:
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
        if not self.full_text:
            return 0
        return self.full_text.count("\n|") // 2


class PublicationSummary(DashboardModel):
    """One row in the NCTQ publications list on /docs/publications."""

    publication_id: str
    title: str
    author: str | None = None
    publication_type: str | None = None
    tags: list | None = None
    published_date: str | None = None
    url: str | None = None
