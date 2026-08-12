# Prediction Pipeline Design Doc

**Date:** 2026-02-23
**Status:** Ready for implementation
**Test district:** Broward County (37), AY 25 (2024-2025), 5-10 questions initially

---

## Context: Why We're Building This

Piper is an AI pipeline that answers policy questions about school districts by reading their documents. The existing codebase (`piper/`) has grown complex — two prediction pipelines, elaborate silence policy logic, dynamic model generation, and deep coupling between components.

Meanwhile, Nathan Roll built **PiedPiper** (`github.com/Nathan-Roll1/PiedPiper`), a parallel implementation that introduced key innovations: k-diversity voting (running 11 predictions per question at different retrieval depths) and Vespa Cloud for hybrid document search.

We already ported Nathan's ideas into `piper/src/nctq3/`, but that code inherited piper's complexity. Now we want a **clean-room implementation** in `piper-drano/` that:

1. Follows the clean patterns already established by the document pipeline
2. Cherry-picks proven ideas from PiedPiper and nctq3
3. Drops accumulated complexity (silence policy logic, two-stage models, AY23 fallbacks)
4. Enforces Katherine's rule: **silence = INA, always**
5. **Builds serious evaluation and observability from day one** — not bolted on later

### Katherine's Silence Rule (Jan 22, 2026)

From the TCD.ai check-in transcript:

> "If the answer is not found in the documents, it's INA. We kind of stay away from saying the district doesn't do X, Y, and Z because there are policies that could be embedded in the website somewhere, someplace that we haven't found. So just to make sure that we're not being inaccurate by saying no, we say INA."

> "No. If it's silent, if there is no evidence, then it's INA always."

This eliminates an entire subsystem from piper: the `SilencePolicy` enum, the AI classifier, the two-stage validation model, the regeneration scripts, and ~15 files of code. In the new pipeline, this rule is encoded directly in Pydantic field descriptions — the AI sees it as part of its output schema.

### What Exists in piper-drano Today

The `document_pipeline/` package handles documents: download → extract (Docling) → enrich (Gemini via PydanticAI) → store (PostgreSQL). It established patterns we'll follow:

- **Pydantic-First**: Field descriptions ARE AI instructions
- **PydanticAI agents**: Structured output, lazy singleton pattern
- **Pydantic Settings**: Config with env prefix (`DOCPIPE_`)
- **psycopg2**: Direct PostgreSQL, RealDictCursor, all SQL in `db.py`
- **Logfire**: Optional observability with graceful degradation
- **FastHTML + MonsterUI**: Dashboard on port 5003

---

## Architecture Overview

```
                    silver.questions
                         │
                    ┌────▼────┐
                    │  Load   │  Load questions + guidance from DB
                    │Questions│
                    └────┬────┘
                         │
              ┌──────────▼──────────┐
              │  For each question  │
              │  generation_id = ←──── UUID links all 11 runs
              │  uuid4()           │
              └──────────┬──────────┘
                         │
           ┌─────────────▼─────────────┐
           │  For k = 1..11           │
           │  ┌─────────────────────┐ │
           │  │ 1. Retrieve k chunks│ │  Vespa hybrid search
           │  │ 2. Build prompt     │ │  4-section prompt
           │  │ 3. Call PydanticAI  │ │  → PredictionOutput
           │  │ 4. Validate answer  │ │  normalize INA, match options
           │  │ 5. Verify citations │ │  check quotes vs source text
           │  │ 6. → PredictionRun  │ │
           │  └─────────────────────┘ │
           └─────────────┬─────────────┘
                         │ 11 PredictionRuns
                         ▼
              ┌──────────────────────┐
              │  Save to             │  silver.prediction_history
              │  prediction_history  │  (11 rows, same generation_id)
              └──────────┬───────────┘
                         │
              ┌──────────▼───────────┐
              │  Synthesize          │  Modal vote → entropy →
              │  (pure logic)        │  citation pick → SuggestedAnswer
              └──────────┬───────────┘
                         │
              ┌──────────▼───────────┐
              │  Evaluate            │  6-way match status
              │  (INA-aware)         │  citation quality
              └──────────┬───────────┘  pydantic-evals integration
                         │
              ┌──────────▼───────────┐
              │  Save to             │  silver.suggested_answers
              │  suggested_answers   │  + evaluation snapshot
              └──────────┬───────────┘
                         │
              ┌──────────▼───────────┐
              │  Dashboard           │  Analyst reviews, approves
              │  (FastHTML)          │  or rejects with reason
              └──────────────────────┘
```

---

## Directory Structure

```
piper-drano/src/prediction_pipeline/
├── __init__.py
├── config.py            # Pydantic Settings (PREDICTOR_ prefix)
├── models.py            # All Pydantic models
├── db.py                # All SQL operations
├── retriever.py         # Vespa Cloud + Postgres fallback
├── predict.py           # PydanticAI agent for single prediction
├── synthesize.py        # Modal vote + entropy (pure logic, no AI)
├── evaluate.py          # 6-way match status, citation verification, INA analysis
├── evals.py             # pydantic-evals Dataset builder, custom evaluators, snapshots
├── run.py               # CLI entry point
├── observability.py     # Logfire setup
└── dashboard/
    ├── __init__.py
    ├── routes/
    │   ├── __init__.py
    │   ├── predictions.py    # /predictions list + scoreboard
    │   └── review.py         # /review/{d}/{q} approve/reject
    ├── services/
    │   ├── __init__.py
    │   └── predictions.py    # SQL queries for views
    └── components/
        ├── __init__.py
        └── badges.py         # Status badges
```

---

## File-by-File Specification

### 1. `config.py` — Pipeline Configuration

**Pattern:** Match `document_pipeline/config.py` exactly.

```python
from pydantic import Field
from pydantic_settings import BaseSettings

class Config(BaseSettings):
    # Execution
    dry_run: bool = Field(default=True, description="Preview without writing to DB")
    district_id: int | None = Field(default=None, description="Filter to one district")
    ay_id: int = Field(default=26, description="Academic year ID (26 = 2025-2026)")
    q_ids: list[int] | None = Field(default=None, description="Specific question IDs")
    limit: int = Field(default=0, ge=0, description="Max questions (0 = no limit)")

    # K-Diversity
    k_min: int = Field(default=1, ge=1, description="Min retrieval depth")
    k_max: int = Field(default=11, ge=1, le=20, description="Max retrieval depth")
    ina_threshold: int = Field(default=6, ge=1, description="INA consensus threshold (of k_max)")

    # Model
    prediction_model: str = Field(default="gemini-2.5-flash-lite", description="Gemini model")
    prediction_temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    google_api_key: str | None = Field(default=None, description="Gemini API key")

    # Vespa
    vespa_url: str | None = Field(default=None, description="Vespa Cloud endpoint")
    vespa_cert_path: str | None = Field(default=None, description="mTLS certificate path")
    vespa_key_path: str | None = Field(default=None, description="mTLS key path")
    vespa_api_key: str | None = Field(default=None, description="Vespa API key")

    # Concurrency
    max_concurrent_predictions: int = Field(default=15, ge=1, le=50)

    # PostgreSQL
    pg_host: str = Field(default="<private-db-host>")
    pg_port: int = Field(default=5432)
    pg_database: str = Field(default="postgres")
    pg_user: str = Field(default="postgres")
    pg_password: str = Field(default="")

    # Identity (for audit trail)
    model_version: str = Field(default="drano-v1.0")
    source: str = Field(default="drano")

    # Evaluation
    save_snapshots: bool = Field(default=True, description="Save evaluation snapshots to disk")
    snapshot_dir: str = Field(default="eval_snapshots", description="Directory for eval snapshots")

    class Config:
        env_prefix = "PREDICTOR_"
        env_file = ".env"
        extra = "ignore"
```

---

### 2. `models.py` — All Pydantic Models

**What this replaces:**
- piper's `SilencePolicy` enum (GONE — Katherine's rule is a field description)
- piper's `TwoStageValidatorMixin` (GONE — single-stage prediction)
- piper's `create_two_stage_model()` dynamic generation (GONE — fixed model)
- nctq3's `SuggestedAnswer`, `PredictionRun`, `ReviewStatus` (PORTED, simplified)
- nctq3's 3-value MatchStatus (EXPANDED to 6 values for INA-aware evaluation)

```python
"""All data models for the prediction pipeline.

Field descriptions on PredictionOutput serve as Gemini instructions
(Pydantic-First AI pattern — the model IS the prompt).
"""
from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field


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
    EXACT = "EXACT"                   # predicted matches golden
    INA_CORRECT = "INA_CORRECT"       # predicted INA, golden is INA
    INA_FALSE_POS = "INA_FALSE_POS"   # predicted INA, golden has a value (missed it)
    INA_FALSE_NEG = "INA_FALSE_NEG"   # predicted a value, golden is INA (hallucinated)
    VALUE_DIFFERENT = "VALUE_DIFFERENT" # both have values, but they differ
    NOGOLDEN = "NOGOLDEN"             # no golden answer to compare against


class CitationStatus(str, Enum):
    """Whether a citation's quoted text matches the source document."""
    VERIFIED = "verified"          # quote found in source text (fuzzy match)
    PARTIAL_MATCH = "partial"      # partial overlap
    NOT_FOUND = "not_found"        # quote not in any retrieved chunk
    WRONG_DOC = "wrong_doc"        # doc_id doesn't match any retrieved chunk


# ── Retrieval ──

class Chunk(BaseModel):
    """A retrieved text passage from Vespa or PostgreSQL."""
    doc_id: str
    doc_name: str = ""
    text: str
    page_number: int | None = None
    section_heading: str | None = None
    score: float = Field(default=0.0, description="Relevance score from retrieval")


# ── Question Input ──

class QuestionContext(BaseModel):
    """Question with guidance, loaded from silver.questions. Read-only pipeline input."""
    q_id: int
    q_text: str
    q_ans_type: str  # Numeric, Dropdown, Checkbox, Date, etc.
    valid_options: list[str] = Field(default_factory=list)
    allows_ina: bool = True
    allows_na: bool = False
    coding_guidance: str | None = None
    focus_terms: list[str] | None = None
    target_sections: list[str] | None = None
    terminology_context: str | None = None
    synthesis_logic: str | None = None
    q_priority: str | None = None


# ── AI Output ──

class PredictionOutput(BaseModel):
    """PydanticAI output schema for a single prediction.

    CRITICAL: Field descriptions are Gemini's instructions. Katherine's silence
    rule is encoded here — not in config, not in an enum, not in a validator.
    """
    predicted_answer: str = Field(
        description=(
            "Your answer based ONLY on the provided evidence passages. "
            "If the evidence does not contain information to answer this question, "
            "you MUST answer 'INA' (Issue Not Addressed). "
            "SILENCE ALWAYS MEANS INA — do NOT infer 'No' from absence of evidence. "
            "Absence of evidence is NOT evidence of absence. "
            "Match valid_options exactly when possible."
        )
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Your confidence in this answer. "
            "1.0 = evidence is explicit and directly states the answer. "
            "0.7 = evidence strongly implies the answer. "
            "0.3 = evidence is tangential or ambiguous. "
            "0.0 = no relevant evidence found (should be INA)."
        ),
    )
    reasoning: str = Field(
        description=(
            "Explain which documents and passages support your answer. "
            "Reference specific document names and quote key phrases. "
            "If INA, explain what you looked for and why it wasn't found."
        )
    )
    key_citations: list[dict] = Field(
        default_factory=list,
        description=(
            "Up to 3 supporting citations. Each dict has: "
            "'doc_name' (source document), 'quote' (verbatim text), 'doc_id' (document ID)."
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


# ── Citation Verification ──

class CitationVerification(BaseModel):
    """Result of verifying one citation against retrieved source text."""
    doc_id: str
    doc_name: str = ""
    quoted_text: str
    status: CitationStatus
    match_score: float = Field(ge=0.0, le=1.0, default=0.0, description="Fuzzy match ratio")
    matched_text: str | None = None  # actual text found in source (if any)


# ── Pipeline Data ──

class PredictionRun(BaseModel):
    """One prediction at a specific retrieval depth k. Maps to silver.prediction_history."""
    model_config = {"extra": "ignore"}

    history_id: int = 0
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
    citation_verifications: list[CitationVerification] | None = None
    citation_quality: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="Fraction of citations verified against source text"
    )
    retrieval_scores: list[float] | None = None  # relevance scores from retriever
    source: str | None = None
    model_version: str | None = None
    run_id: str | None = None
    predicted_at: datetime | None = None


class SuggestedAnswer(BaseModel):
    """One synthesized answer per (district, question, year).
    Maps to silver.suggested_answers.
    """
    model_config = {"extra": "ignore"}

    district_id: int
    ay_id: int
    q_id: int
    suggested_answer: str
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str | None = None
    citations_json: list[dict] | None = None
    citation_doc_ids: list[str] | None = None
    citation_quality: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="Average citation verification score across winning runs"
    )
    is_ina: bool = False
    generation_id: UUID
    entropy: float = Field(ge=0.0, default=0.0)
    agreement_pct: float = Field(ge=0.0, le=100.0, default=0.0)
    n_unique_answers: int = Field(ge=1, default=1)
    n_predictions: int = Field(ge=1, default=1)
    vote_distribution: dict[str, int] | None = None  # {"No": 8, "Yes": 2, "INA": 1}
    status: ReviewStatus = ReviewStatus.UNREVIEWED
    rejection_reason: str | None = None
    decision_note: str | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    batch_id: str | None = None
    run_id: str | None = None
    model_version: str | None = None
    source: str = "drano"
    created_at: datetime | None = None
    match_status: str | None = None  # filled by evaluate step


# ── Evaluation Snapshot ──

class QuestionResult(BaseModel):
    """Evaluation result for one question, saved in snapshot."""
    q_id: int
    q_text: str
    q_ans_type: str
    predicted: str
    golden: str | None
    match_status: str
    entropy: float
    agreement_pct: float
    n_unique_answers: int
    citation_quality: float | None
    vote_distribution: dict[str, int]
    confidence: float
    reasoning: str | None = None


class EvaluationSnapshot(BaseModel):
    """Full evaluation run, serialized for regression testing."""
    district_id: int
    district_name: str
    ay_id: int
    model_version: str
    prediction_model: str
    timestamp: datetime
    k_range: tuple[int, int]  # (k_min, k_max)
    ina_threshold: int
    results: list[QuestionResult]
    summary: dict  # accuracy, ina_precision, ina_recall, etc.
```

---

### 3. `db.py` — All Database Operations

**Pattern:** Match `document_pipeline/db.py`. All SQL in this one file.

**Key functions:**

```python
def get_pg_connection(config):
    """psycopg2 connection from config. Same as docpipe."""

def load_questions(config, q_ids=None) -> list[QuestionContext]:
    """Load from silver.questions. Parses JSON fields (valid_options, focus_terms)."""

def load_golden_answers(config, district_id, ay_id) -> dict[int, str]:
    """Load from bronze.district_answers. Returns {q_id: answer_text}."""

def get_district_info(config, district_id) -> tuple[str, str]:
    """(district_name, state) from bronze.district."""

def save_prediction_runs(runs, generation_id, config):
    """Batch INSERT to silver.prediction_history using executemany."""

def upsert_suggested_answer(answer, config):
    """INSERT ... ON CONFLICT (district_id, ay_id, q_id) DO UPDATE to silver.suggested_answers."""

def load_suggested_answers(config, district_id, ay_id, status=None) -> list[SuggestedAnswer]:
    """Load from silver.suggested_answers with optional status filter."""

def load_suggested_answer(config, district_id, ay_id, q_id) -> SuggestedAnswer | None:
    """Load single suggested answer for review page."""

def update_review_status(config, district_id, ay_id, q_id, status, reviewed_by,
                         rejection_reason=None, decision_note=None):
    """UPDATE silver.suggested_answers SET status, reviewed_by, reviewed_at, etc."""

def approve_answer(config, district_id, ay_id, q_id, reviewed_by, note=None):
    """Two-step: update suggested_answers status + upsert silver.answers with golden_source='ai_approved'."""
```

---

### 4. `retriever.py` — Evidence Retrieval

**What this ports from:** PiedPiper's `db/vespa_client.py` (Vespa hybrid search) and nctq3's `StubRetriever` (PostgreSQL fallback).

```python
"""Evidence retrieval via Vespa Cloud with PostgreSQL fallback."""

from typing import Protocol

class BaseRetriever(Protocol):
    def retrieve(self, district_id: int, ay_id: int, query: str, top_k: int) -> list[Chunk]: ...


class VespaRetriever:
    """Hybrid BM25 + semantic search via Vespa Cloud.

    Uses API key auth (VESPA_API_KEY). Returns Chunk objects with relevance scores.
    """
    def __init__(self, url, api_key): ...
    def retrieve(self, district_id, ay_id, query, top_k) -> list[Chunk]: ...
    # Internally:
    # 1. YQL: "select * from passage where userQuery() OR nearestNeighbor(...)"
    #    filtered by district_id
    # 2. Ranking: hybrid (reciprocal rank fusion of bm25 + semantic)
    # 3. Convert hits to Chunk objects with scores


class PostgresRetriever:
    """Fallback: reads full document text from silver.district_documents.

    For districts without Vespa data. Loads top documents by ai_confidence,
    returns full text as single chunk per document.
    """
    def __init__(self, config): ...
    def retrieve(self, district_id, ay_id, query, top_k) -> list[Chunk]: ...


def get_retriever(config) -> BaseRetriever:
    """Factory: Vespa if configured, PostgreSQL fallback otherwise."""
    api_key = config.vespa_api_key or os.environ.get("VESPA_API_KEY")
    if config.vespa_url and api_key:
        return VespaRetriever(config.vespa_url, api_key)
    return PostgresRetriever(config)
```

---

### 5. `predict.py` — PydanticAI Prediction Agent

**Pattern:** Match `document_pipeline/enrich.py` (lazy singleton, structured output, system prompt).

```python
"""Single prediction via PydanticAI agent."""

SYSTEM_PROMPT = """You are an expert policy analyst for the National Council on Teacher Quality (NCTQ).

Your task: answer questions about school district policies using ONLY the provided evidence passages.

CRITICAL RULES:
1. Base your answer ONLY on the provided evidence. Never use prior knowledge.
2. If the evidence does not contain information to answer the question, answer "INA".
3. SILENCE ALWAYS MEANS INA. If documents don't mention a topic, answer "INA".
   Do NOT infer "No" from absence of evidence. Absence of evidence is NOT evidence of absence.
4. Match valid_options exactly when provided (case-sensitive).
5. For numeric questions, provide raw numbers without formatting (94444 not $94,444).
6. For multi-select checkboxes, provide all that apply as a comma-separated list.
7. For dates, use YYYY-MM-DD format.

OUTPUT FORMAT: Return a JSON object with predicted_answer, confidence, reasoning,
key_citations, and evidence_agreement."""


_agent = None  # Lazy singleton


def _get_agent(config):
    """Create PydanticAI Agent with PredictionOutput schema. Created once, reused."""

def build_prompt(question: QuestionContext, district_name: str, state: str,
                 chunks: list[Chunk]) -> str:
    """Build the 4-section prediction prompt.

    Section 1: District context (name, state)
    Section 2: Evidence passages (from Vespa/Postgres)
    Section 3: Question text + valid options + coding guidance
    Section 4: Focus terms, target sections, terminology context
    """

async def predict(question, district_id, ay_id, district_name, state,
                  chunks, config, generation_id, top_k, run_id=None) -> PredictionRun:
    """Generate one prediction. Returns PredictionRun.

    1. Build prompt from question + chunks
    2. Call PydanticAI agent.run()
    3. Validate/normalize answer
    4. Return PredictionRun (with retrieval_scores from chunks)
    """

def _validate_answer(answer: str, question: QuestionContext) -> str:
    """Post-hoc normalization:
    - INA variants: 'N/A', 'n/a', 'Not Available', etc. → 'INA'
    - Case-insensitive option matching for dropdowns
    - Strip whitespace
    """
```

---

### 6. `synthesize.py` — Modal Voting and Synthesis

**Pure logic — no AI calls, no DB calls.**

```python
"""Combine 11 prediction runs into 1 SuggestedAnswer.

All functions are pure: take data in, return data out. No AI calls, no DB calls.
"""
import math
from collections import Counter


def modal_vote(runs: list[PredictionRun]) -> str:
    """Most common predicted_answer. Ties broken by Counter insertion order."""

def vote_distribution(runs: list[PredictionRun]) -> dict[str, int]:
    """Full vote counts: {'No': 8, 'Yes': 2, 'INA': 1}."""

def is_ina_consensus(runs: list[PredictionRun], threshold: int = 6) -> bool:
    """True if >= threshold runs predicted INA."""

def calculate_entropy(runs: list[PredictionRun]) -> float:
    """Shannon entropy of answer distribution.
    0.0 = all runs agree (unanimous). Higher = more disagreement.
    """

def calculate_agreement(runs: list[PredictionRun]) -> tuple[float, int]:
    """(agreement_pct, n_unique_answers)."""

def pick_citations(runs: list[PredictionRun], winning_answer: str) -> tuple[list[dict], list[str]]:
    """Best citations from winning runs, deduplicated by doc_id.
    Returns (citations_list, doc_id_list).
    """

def aggregate_citation_quality(runs: list[PredictionRun], winning_answer: str) -> float | None:
    """Average citation_quality across winning runs that have it."""

def synthesize(generation_id, runs: list[PredictionRun], config) -> SuggestedAnswer:
    """Orchestrate: vote → INA check → entropy → citations → build SuggestedAnswer."""
```

---

### 7. `evaluate.py` — INA-Aware Evaluation + Citation Verification

**This is significantly expanded from the original design.** Two responsibilities:

1. **6-way match status** — not just EXACT/DIFFERENT, but INA-specific breakdowns
2. **Citation verification** — check that quoted text actually appears in retrieved chunks

```python
"""Evaluate predictions: INA-aware accuracy + citation verification."""

from difflib import SequenceMatcher


# ── Answer Evaluation ──

def normalize_answer(answer: str) -> str:
    """Normalize for comparison. Handles:
    - Case: lowercase
    - Whitespace: strip
    - INA variants: 'N/A', 'n/a', 'Issue Not Addressed' → 'ina'
    - Currency: '$94,444' → '94444'
    - Percentages: '5%' → '5'
    - Multi-select: sort comma-separated values
    """

def is_ina(answer: str) -> bool:
    """True if answer is any INA variant."""
    return normalize_answer(answer) == "ina"

def evaluate(suggested: SuggestedAnswer, golden_answers: dict[int, str]) -> MatchStatus:
    """6-way comparison of one suggested answer to its golden answer.

    EXACT         — normalized predicted == normalized golden
    INA_CORRECT   — both are INA
    INA_FALSE_POS — predicted INA, golden has a value (we missed real answer)
    INA_FALSE_NEG — predicted a value, golden is INA (Katherine's rule violated)
    VALUE_DIFFERENT — both have values but don't match
    NOGOLDEN      — no golden answer exists for this question
    """

def evaluate_batch(answers: list[SuggestedAnswer], golden: dict[int, str]) -> dict:
    """Evaluate all answers. Returns counts + computed metrics:
    {
        "total": 10,
        "EXACT": 7, "INA_CORRECT": 1, "INA_FALSE_POS": 1,
        "INA_FALSE_NEG": 0, "VALUE_DIFFERENT": 1, "NOGOLDEN": 0,
        "accuracy": 80.0,           # (EXACT + INA_CORRECT) / (total - NOGOLDEN)
        "ina_precision": 1.0,       # INA_CORRECT / (INA_CORRECT + INA_FALSE_POS)
        "ina_recall": 1.0,          # INA_CORRECT / (INA_CORRECT + INA_FALSE_NEG)
        "katherine_violations": 0,  # INA_FALSE_NEG count (most serious error)
    }
    """


# ── Citation Verification ──

def verify_citation(citation: dict, chunks: list[Chunk],
                    threshold: float = 0.6) -> CitationVerification:
    """Verify one citation against retrieved chunks.

    1. Check if citation's doc_id matches any chunk's doc_id
    2. If match, fuzzy-match quoted text against chunk text (SequenceMatcher)
    3. Return CitationVerification with status and match score
    """

def verify_run_citations(run: PredictionRun, chunks: list[Chunk]) -> list[CitationVerification]:
    """Verify all citations in a prediction run. Sets run.citation_quality."""

def citation_quality_score(verifications: list[CitationVerification]) -> float:
    """Fraction of citations that are VERIFIED or PARTIAL_MATCH."""
```

---

### 8. `evals.py` — pydantic-evals Integration

**New file.** Bridges the prediction pipeline with `pydantic-evals` for structured evaluation with Logfire integration.

```python
"""pydantic-evals integration for structured evaluation and regression testing.

Uses the pydantic-evals framework (already installed with pydantic-ai) to:
1. Build eval datasets from prediction runs
2. Run custom evaluators (answer match, INA calibration, citation quality)
3. Generate evaluation reports (terminal + Logfire)
4. Save/load evaluation snapshots for regression testing
"""
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EqualsExpected


# ── Custom Evaluators ──

class AnswerMatchEvaluator(Evaluator):
    """6-way match status evaluation.
    Returns: float score (1.0 for EXACT/INA_CORRECT, 0.0 for others)
    Plus labels for the confusion matrix.
    """

class INACalibrationEvaluator(Evaluator):
    """Evaluate INA prediction quality specifically.
    Returns: 'ina_correct', 'ina_false_pos', 'ina_false_neg', 'non_ina'
    For confusion matrix and precision/recall computation.
    """

class CitationQualityEvaluator(Evaluator):
    """Score based on citation verification results.
    Returns: float (average citation_quality across winning runs)
    """

class ConfidenceCalibrationEvaluator(Evaluator):
    """Are high-confidence predictions more likely correct?
    Returns: 'calibrated' if (high confidence + correct) or (low confidence + wrong)
             'overconfident' if high confidence + wrong
             'underconfident' if low confidence + correct
    """

class EntropyReliabilityEvaluator(Evaluator):
    """Does low entropy predict correctness?
    Returns: float correlation score
    """


# ── Dataset Building ──

def build_eval_dataset(
    suggested_answers: list[SuggestedAnswer],
    golden_answers: dict[int, str],
    questions: dict[int, QuestionContext],
) -> Dataset:
    """Build a pydantic-evals Dataset from prediction results.

    Each Case has:
      inputs: {district_id, q_id, ay_id}
      expected_output: golden answer
      metadata: {q_text, q_ans_type, entropy, agreement_pct, citation_quality, vote_distribution}
    """

def run_evaluation(dataset: Dataset) -> EvaluationReport:
    """Run the dataset with all custom evaluators. Returns printable + Logfire-integrated report."""


# ── Snapshots ──

def save_snapshot(snapshot: EvaluationSnapshot, config: Config) -> str:
    """Save evaluation snapshot to disk as JSON. Returns file path.
    File: {snapshot_dir}/{district_id}_ay{ay_id}_{timestamp}.json
    """

def load_snapshot(path: str) -> EvaluationSnapshot:
    """Load a previous snapshot for regression comparison."""

def compare_snapshots(current: EvaluationSnapshot, baseline: EvaluationSnapshot) -> dict:
    """Compare two snapshots. Returns per-question regressions and improvements.
    Flags questions that went from EXACT→wrong or wrong→EXACT.
    """
```

---

### 9. `run.py` — CLI Entry Point

**Pattern:** Match `document_pipeline/run.py` (argparse, logging, logfire spans).

```python
"""Prediction pipeline entry point.

Usage:
    PYTHONPATH=src python src/prediction_pipeline/run.py --district 37 --ay 25 --dry-run
    PREDICTOR_DRY_RUN=false PYTHONPATH=src python src/prediction_pipeline/run.py --district 37 --ay 25
    PREDICTOR_DRY_RUN=false PYTHONPATH=src python src/prediction_pipeline/run.py --district 37 --ay 25 --q-ids 4,5,6
"""

# CLI args: --district, --ay, --q-ids, --limit, --dry-run, --no-save

# Flow:
# 1. Setup observability
# 2. Load questions from silver.questions
# 3. Get district info from bronze.district
# 4. Get retriever (Vespa or Postgres fallback)
# 5. Load golden answers from bronze.district_answers
# 6. For each question:
#    a. generation_id = uuid4()
#    b. For k in k_min..k_max:
#       - Retrieve k chunks via retriever
#       - Call predict() async
#       - Verify citations against retrieved chunks
#       - Collect PredictionRun
#    c. Save runs to silver.prediction_history (if not dry_run)
#    d. Synthesize into SuggestedAnswer (with citation_quality)
#    e. Evaluate against golden answer (6-way match status)
#    f. Save SuggestedAnswer (if not dry_run)
# 7. Build pydantic-evals Dataset, run evaluators, print report
# 8. Save evaluation snapshot
# 9. Print summary:
#    Total: 10 | Accuracy: 80.0%
#    EXACT: 7 | INA_CORRECT: 1 | INA_FALSE_POS: 1 | VALUE_DIFFERENT: 1
#    Katherine violations: 0 | Citation quality: 0.85
```

---

### 10. `observability.py` — Logfire Setup

Copy of `document_pipeline/observability.py` adapted for `prediction_pipeline`. Same graceful degradation pattern.

```python
def setup(service_name="predictor", console=False) -> bool:
    """Same pattern as docpipe: configure Logfire, instrument httpx + pydantic-ai."""
```

---

### 11. Dashboard Integration

**Approach:** Add routes to the existing FastHTML app at port 5003.

**Modify existing files:**
- `document_pipeline/dashboard/layout.py` — add "Predictions" to `NAV_LINKS`
- `document_pipeline/dashboard/routes/__init__.py` — import and register prediction routes

**New routes:**

| Route | Purpose |
|-------|---------|
| `GET /predictions` | District list with stats (total, reviewed %, accuracy) |
| `GET /predictions/{district_id}` | Question scoreboard (answer, confidence, entropy, status, golden match) |
| `GET /review/{district_id}/{q_id}` | Full review page with citation quality indicators |
| `POST /review/{district_id}/{q_id}/approve` | HTMX: approve answer |
| `POST /review/{district_id}/{q_id}/reject` | HTMX: reject with reason |

**Review page shows:**
- Question text, coding guidance, valid options
- Suggested answer with confidence, entropy, agreement bar
- **Citation quality indicator** (verified/partial/not_found badges per citation)
- Vote distribution (e.g., "No: 8/11, Yes: 2/11, INA: 1/11")
- Table of all 11 runs (k, answer, confidence, citation quality)
- Golden answer comparison with **6-way match status badge**
- Approve / Mark Incorrect buttons

---

## Implementation Order

Steps 1-5 are the pipeline core. Steps 6-7 are integration and dashboard.

```
Step 1: config.py + observability.py + models.py     (foundation, no deps)
Step 2: db.py                                         (depends on Step 1)
Step 3: retriever.py                                  (depends on Step 1-2)
Step 4: predict.py                                    (depends on Step 1-3)
Step 5: synthesize.py + evaluate.py + evals.py        (synthesize/evaluate depend on models only;
                                                       evals.py depends on evaluate.py)
Step 6: run.py                                        (ties everything together)
Step 7: Dashboard services + components + routes      (depends on Step 1-2)
Step 8: Dashboard integration                         (modify existing layout + routes)
```

Steps 3, 4, and 5 can be implemented in parallel.

---

## Database Tables

Both tables already exist from nctq3 migrations. No new migrations needed.

**`silver.prediction_history`**
```sql
CREATE TABLE silver.prediction_history (
    history_id SERIAL PRIMARY KEY,
    district_id INTEGER NOT NULL,
    ay_id INTEGER NOT NULL,
    q_id INTEGER NOT NULL,
    generation_id UUID,
    top_k INTEGER,
    predicted_answer TEXT,
    confidence FLOAT,
    reasoning TEXT,
    key_citations_json JSONB,
    evidence_agreement TEXT,
    source TEXT,
    model_version TEXT,
    run_id TEXT,
    predicted_at TIMESTAMPTZ DEFAULT NOW()
);
```

**`silver.suggested_answers`**
```sql
CREATE TABLE silver.suggested_answers (
    district_id INTEGER NOT NULL,
    ay_id INTEGER NOT NULL,
    q_id INTEGER NOT NULL,
    suggested_answer TEXT,
    confidence FLOAT,
    reasoning TEXT,
    citations_json JSONB,
    citation_doc_ids UUID[],
    is_ina BOOLEAN DEFAULT FALSE,
    generation_id UUID,
    entropy FLOAT,
    agreement_pct FLOAT,
    n_unique_answers INTEGER,
    n_predictions INTEGER,
    status TEXT DEFAULT 'unreviewed' CHECK (status IN ('unreviewed', 'approved', 'incorrect')),
    rejection_reason TEXT,
    decision_note TEXT,
    reviewed_by TEXT,
    reviewed_at TIMESTAMPTZ,
    batch_id TEXT,
    run_id TEXT,
    model_version TEXT,
    source TEXT DEFAULT 'drano',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (district_id, ay_id, q_id)
);
```

---

## Verification Plan

1. **Import check:** `PYTHONPATH=src python -c "from prediction_pipeline.config import Config; print(Config())"`
2. **Dry run:** `PYTHONPATH=src python src/prediction_pipeline/run.py --district 37 --ay 25 --dry-run`
3. **Single question:** `PREDICTOR_DRY_RUN=false PYTHONPATH=src python src/prediction_pipeline/run.py --district 37 --ay 25 --q-ids 4`
4. **5-10 questions:** `PREDICTOR_DRY_RUN=false PYTHONPATH=src python src/prediction_pipeline/run.py --district 37 --ay 25 --limit 10`
5. **Evaluation report:** Check terminal output for 6-way accuracy, INA precision/recall, citation quality
6. **Logfire:** Check https://logfire-us.pydantic.dev/murmuration/nctqai for traces
7. **Snapshot:** Check `eval_snapshots/` for saved JSON
8. **Katherine's rule check:** `grep -r "silence_implies_no\|SILENCE_MEANS_NO\|AY23\|ay23_fallback" src/prediction_pipeline/` should return nothing
9. **Dashboard:** `PYTHONPATH=src uvicorn document_pipeline.dashboard.main:app --port 5003 --reload` → `/predictions`

---

## What We Deliberately Left Out

| Feature | Why |
|---------|-----|
| `SilencePolicy` enum | Katherine's rule is absolute. No per-question override needed. |
| AY23 fallback | Overriding INA with historical data contradicts Katherine. |
| Conservative minority override | Flipping INA→value contradicts Katherine. |
| Two-stage model (can_answer) | Unnecessary complexity. Single-stage PredictionOutput is cleaner. |
| Dynamic model generation | Fixed PredictionOutput works for all question types. |
| 10 prompt variations | One good prompt. Experimentation belongs in notebooks. |
| 3-tier LRU+TTL cache | Premature optimization. Add when needed. |
| Batch inference (3 Qs per call) | Simpler to predict one question at a time. Add if cost is a problem. |
| CI/CD | Manual deploys, same as docpipe. |
| mTLS Vespa auth | Using API key auth instead. Simpler, no cert management. |
