"""All data models for the prediction pipeline.

Field descriptions on PredictionOutput serve as Gemini instructions
(Pydantic-First AI pattern — the model IS the prompt).

PiedPiper equivalent: prediction_models.py (dataclasses).
Key difference: We use Pydantic models with field descriptions that
double as Gemini prompts via PydanticAI structured output.
"""
from datetime import datetime
from enum import Enum
from uuid import UUID  # used by PredictionRun.generation_id, SuggestedAnswer.generation_id

from typing import Literal

from pydantic import BaseModel, Field

# Canonical set of all INA variant strings (lowercase).
# Single source of truth — evaluate.py, predict.py, and db.py derive from this.
INA_VARIANTS: set[str] = {
    "ina", "n/a", "na", "not available", "information not available",
    "issue not addressed", "no information", "not found",
    "no evidence", "insufficient evidence",
}


# ── Enums ──

class ReviewStatus(str, Enum):
    UNREVIEWED = "unreviewed"
    APPROVED = "approved"
    INCORRECT = "incorrect"


class RejectionReason(str, Enum):
    SOURCE_UNAVAILABLE = "source_document_unavailable"
    WRONG_DOCUMENT = "wrong_document_cited"
    INCORRECT_REASONING = "incorrect_reasoning"
    AI_INA_BUT_EXISTS = "ai_predicted_ina_but_answer_exists"
    AI_VALUE_BUT_INA = "ai_predicted_value_but_should_be_ina"
    NOT_APPLICABLE = "question_doesnt_apply"
    OTHER = "other"


class MatchStatus(str, Enum):
    """6-way evaluation for INA-aware accuracy analysis."""
    EXACT = "EXACT"
    INA_CORRECT = "INA_CORRECT"
    INA_FALSE_POS = "INA_FALSE_POS"
    INA_FALSE_NEG = "INA_FALSE_NEG"
    VALUE_DIFFERENT = "VALUE_DIFFERENT"
    NOGOLDEN = "NOGOLDEN"


class QuestionType(str, Enum):
    """Adaptive question-type classification for per-type prediction strategies."""
    SALARY = "salary"
    NUMERIC = "numeric"
    DATE = "date"
    MULTISELECT = "multiselect"
    EVAL = "eval"
    SELECT = "select"
    DEFAULT = "default"


class PredictionStrategy(BaseModel):
    """Per-question-type prediction parameters resolved from classification + config."""
    question_type: QuestionType
    k_min: int = Field(ge=1)
    k_max: int = Field(ge=1)
    temperature: float = Field(ge=0.0, le=2.0)
    ina_threshold: int = Field(ge=1)
    always_verify_ina: bool = Field(default=False)


# ── Retrieval ──

class Chunk(BaseModel):
    """A retrieved text passage from Vespa or PostgreSQL."""
    doc_id: str
    doc_name: str = ""
    text: str
    summary: str = ""
    page_number: int | None = None
    section_heading: str | None = None
    score: float = Field(default=0.0, description="Relevance score from retrieval")


# ── Citations ──

class Citation(BaseModel):
    """Gemini's citation output — only what the model knows."""
    document_index: int = Field(
        description="Document number from Evidence Passages (1-indexed)")
    quote: str = Field(
        description="Verbatim quote from the document supporting your answer")


class ResolvedCitation(BaseModel):
    """Citation with deterministic metadata resolved from retrieval chunks."""
    document_index: int
    doc_id: str
    doc_name: str
    quote: str
    page_number: int | None = None
    section_heading: str | None = None
    relevance_score: float = 0.0


class CuratedCitation(BaseModel):
    """Citation after validation against source chunk text."""
    document_index: int
    doc_id: str
    doc_name: str
    original_quote: str = Field(description="What Gemini claimed as a verbatim quote")
    corrected_quote: str = Field(
        description=(
            "Exact substring from the source chunk text that matches the claimed quote. "
            "Must appear character-for-character in the chunk. Empty if rejected."
        )
    )
    verified: bool = Field(
        description=(
            "True if the original quote is semantically faithful to a passage in the source. "
            "False if fabricated, from a different document, or meaning materially changed."
        )
    )
    match_type: Literal["exact", "normalized", "corrected", "rejected"] = Field(
        description=(
            "'exact' if original quote appears verbatim in source. "
            "'normalized' if quote matches after whitespace/case normalization. "
            "'corrected' if meaning faithful but wording needed fixing. "
            "'rejected' if quote cannot be matched to any passage."
        )
    )
    page_number: int | None = None
    section_heading: str | None = None
    relevance_score: float = 0.0


# ── Question Input ──

class QuestionContext(BaseModel):
    """Question with guidance, loaded from silver.questions."""
    q_id: int
    q_text: str
    q_ans_type: str
    valid_options: list[str] = Field(default_factory=list)
    allows_ina: bool = True
    allows_na: bool = False
    coding_guidance: str | None = None
    ai_coding_notes: str | None = None
    silence_policy: str | None = None
    focus_terms: list[str] | None = None
    target_sections: list[str] | None = None
    terminology_context: str | None = None
    synthesis_logic: str | None = None
    q_priority: str | None = None
    parent_q_text: str | None = Field(default=None, description="Parent question text for conditional 'If...' questions")
    dsubpol_id: int | None = Field(default=None, description="Sub-policy ID linking to parent question")


# ── AI Output ──

class PredictionOutput(BaseModel):
    """PydanticAI output schema. Field descriptions are Gemini's instructions.
    The silence rule is encoded in predicted_answer description.
    """
    predicted_answer: str = Field(
        description=(
            "Your answer based ONLY on the provided evidence passages. "
            "If the evidence does not contain information to answer this question, "
            "you MUST answer 'INA' (Issue Not Addressed — issue not addressed in scope of NCTQ reviewed documents). "
            "SILENCE ALWAYS MEANS INA — do NOT infer 'No' from absence of evidence. "
            "Absence of evidence is NOT evidence of absence. "
            "Match valid_options exactly when possible."
        )
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description=(
            "Your confidence in this answer. "
            "1.0 = evidence explicitly states the answer. "
            "0.7 = evidence strongly implies the answer. "
            "0.3 = evidence is tangential or ambiguous."
        ),
    )
    reasoning: str = Field(
        description=(
            "Explain which documents and passages support your answer. "
            "When quoting evidence, use EXACTLY this format: "
            '[Citation N: "verbatim quote"] where N is the document_index '
            "from your key_citations. Every quote in your reasoning MUST "
            "correspond to a citation in key_citations. "
            "If INA, explain what you looked for and why it wasn't found."
        )
    )
    key_citations: list[Citation] = Field(
        default_factory=list,
        description=(
            "Up to 3 citations with document_index (from Evidence Passages) "
            "and verbatim quote supporting your answer. "
            "If your answer is INA, leave this list empty — there is nothing to cite when no evidence is found."
        ),
    )
    evidence_agreement: str = Field(
        default="no_evidence",
        description=(
            "How documents agree on this answer: "
            "'unanimous' (all say the same), 'majority' (most agree), "
            "'mixed' (some disagree), 'conflict' (contradictory), "
            "'no_evidence' (nothing found)."
        ),
    )


# ── Pipeline Data ──

class PredictionRun(BaseModel):
    """One prediction at a specific retrieval depth k."""
    model_config = {"extra": "ignore"}

    district_id: int
    ay_id: int
    q_id: int
    generation_id: UUID
    top_k: int = Field(ge=1)
    predicted_answer: str
    confidence: float | None = None
    reasoning: str | None = None
    key_citations_json: list[dict] | None = None
    evidence_agreement: str | None = None
    retrieval_scores: list[float] | None = None
    source: str | None = None
    model_version: str | None = None
    run_id: str | None = None
    predicted_at: datetime | None = None


class SuggestedAnswer(BaseModel):
    """One synthesized answer per (district, question, year)."""
    model_config = {"extra": "ignore"}

    district_id: int
    ay_id: int
    q_id: int
    suggested_answer: str
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str | None = None
    citations_json: list[dict] | None = None
    is_ina: bool = False
    generation_id: UUID
    entropy: float = Field(ge=0.0, default=0.0)
    agreement_pct: float = Field(ge=0.0, le=100.0, default=0.0)
    n_unique_answers: int = Field(ge=1, default=1)
    n_predictions: int = Field(ge=1, default=1)
    vote_distribution: dict[str, int] | None = None
    status: ReviewStatus = ReviewStatus.UNREVIEWED
    rejection_reason: str | None = None
    decision_note: str | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    batch_id: str | None = None
    run_id: str | None = None
    model_version: str | None = None
    source: str = "piedpiper"
    created_at: datetime | None = None
    match_status: str | None = None


# ── Evaluation Snapshot ──

class QuestionResult(BaseModel):
    """Evaluation result for one question."""
    q_id: int
    q_text: str
    q_ans_type: str
    predicted: str
    golden: str | None
    match_status: str
    entropy: float
    agreement_pct: float
    n_unique_answers: int
    vote_distribution: dict[str, int]
    confidence: float
    reasoning: str | None = None


class EvaluationSnapshot(BaseModel):
    """Full evaluation run for regression testing."""
    district_id: int
    district_name: str
    ay_id: int
    model_version: str
    prediction_model: str
    timestamp: datetime
    k_range: tuple[int, int]
    ina_threshold: int
    results: list[QuestionResult]
    summary: dict
