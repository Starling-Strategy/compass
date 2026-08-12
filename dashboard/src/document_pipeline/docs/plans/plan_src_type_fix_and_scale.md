# Plan: Fix src_type, Unify Document Classification, Scale to 100+ Docs

**Date:** 2026-02-23
**Status:** Ready to implement

---

## Context

48 Broward docs processed by docpipe. Need to: assess quality for Nathan's pipeline, fix src_type handling (human vs AI classification), then scale to 100+ docs across districts.

## Quality Assessment Summary (48 docs)

**Extraction:** 100% success rate. OCR quality strong — clean markdown, tables captured well, headers preserved.

**Readability:** 37 good (77%), 5 fair (10%), 6 poor (13%). The 6 "poor" docs are old scanned images (pre-2010) that yield only `<!-- image -->` tags — expected and acceptable.

**4 problem docs** (src_ids 12486, 12488, 12489, 12562): Near-empty extractions from degraded scans. These are 2007-2010 vintage — low relevance for current predictions.

**AI titles:** Excellent. Fills the gap left by 45/48 NULL `doc_name` values. Clean, descriptive, includes district + type + year.

**AY alignment:** Strong. Human and AI agree on most. AI tends to be more expansive on multi-year contracts (e.g., human=[18], AI=[14-18]). `effective_ay_ids` correctly defers to human.

**Ready for Nathan?** Yes — for docs rated "good" or "fair" (87%), the extracted text is clean enough for chunking and prediction. The pipeline correctly flags poor-quality docs so they can be skipped downstream.

---

## The src_type Problem

Current `SourceType` enum in `src/models/core.py` = file format (PDF, DOCX, etc.). Bronze `src_type` = document classification (Contract, Salary Schedule, etc.). The `normalize_src_type()` function in `src/models/core.py:345-372` maps everything → `SourceType.PDF` or `SourceType.OTHER`, **destroying the human classification**.

Meanwhile, AI classifies into `DocumentType` in `src/pipelines/district_document_indexing/extractors/ai_enrichment.py:114-124` (contract, salary_schedule, policy, report, etc.) — a different enum than bronze uses. No apples-to-apples comparison possible.

Silver has 18 distinct `src_type` values with case chaos ("Contract" vs "contract", "Other" vs "other").

### Bronze src_type values (the 8 human categories)

| Value | Count |
|-------|-------|
| Salary Schedule | 2,622 |
| Other | 2,584 |
| Annual Calendar | 1,954 |
| Evaluation Handbook | 1,762 |
| Contract | 1,386 |
| Union Document | 656 |
| Benefits Handbook | 485 |
| Board Policy | 260 |

### Current AI DocumentType enum (8 values, different from bronze)

| AI Value | Bronze Equivalent |
|----------|-------------------|
| contract | Contract |
| salary_schedule | Salary Schedule |
| evaluation_handbook | Evaluation Handbook |
| policy | Board Policy |
| budget | Other (no bronze equivalent) |
| report | Other (no bronze equivalent) |
| handbook | Other (no bronze equivalent) |
| other | Other |

---

## Actual File Locations (corrected from plan mode)

The plan referenced `src/document_pipeline/` which **does not exist**. Here are the real file paths:

| What | Actual Path |
|------|-------------|
| SourceType enum | `src/models/core.py:21-29` |
| normalize_src_type() | `src/models/core.py:345-372` |
| AI DocumentType enum | `src/pipelines/district_document_indexing/extractors/ai_enrichment.py:114-124` |
| DocumentEnrichment model | `src/pipelines/district_document_indexing/extractors/ai_enrichment.py:127-178` |
| AI enrichment prompt | `src/pipelines/district_document_indexing/extractors/ai_enrichment.py:205-242` |
| DistrictDocument model | `src/models/documents.py:32-306` |
| DistrictDocument.src_type field | `src/models/documents.py:109-111` |
| src_type validator | `src/models/documents.py:223-227` |
| DocumentView model | `src/models/documents.py:314-358` |
| create_district_document() | `src/models/documents.py:366-417` |
| sync_document_stubs | `src/pipelines/bronze_to_silver/sync_document_stubs.py` |
| Dashboard document detail | `src/dashboard_fast/routes/documents.py` |
| Dashboard document service | `src/dashboard_fast/services/documents.py` |
| Dashboard document models | `src/dashboard_fast/models/documents.py` |
| Dashboard badges | `src/dashboard_fast/components/badges.py` |
| District doc indexer | `src/pipelines/district_document_indexing/district_document_indexer.py` |
| Existing migrations | `src/pipelines/bronze_to_silver/migrations/` (no `20260223_*` yet) |

---

## Implementation Steps

### Step 1: Create unified `NctqDocumentType` enum

**File:** `src/models/core.py`

Add new enum alongside (not replacing yet) SourceType. Once migration is done, SourceType can be deprecated.

```python
class NctqDocumentType(str, Enum):
    """Document classification matching NCTQ's 8 human-coded categories.

    Used for BOTH human classification (from bronze.district_sources.src_type)
    and AI classification (from Gemini enrichment). Having both use the same
    enum enables apples-to-apples agreement analysis.
    """
    SALARY_SCHEDULE = "salary_schedule"
    ANNUAL_CALENDAR = "annual_calendar"
    EVALUATION_HANDBOOK = "evaluation_handbook"
    CONTRACT = "contract"
    UNION_DOCUMENT = "union_document"
    BENEFITS_HANDBOOK = "benefits_handbook"
    BOARD_POLICY = "board_policy"
    OTHER = "other"
```

Add mapping function (replaces `normalize_src_type` for document classification):

```python
def normalize_bronze_doc_type(value: str) -> NctqDocumentType:
    """Map bronze.district_sources.src_type Title Case → NctqDocumentType enum.

    Bronze uses Title Case ("Salary Schedule"), we store snake_case ("salary_schedule").
    Anything unrecognized maps to OTHER.
    """
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
```

### Step 2: Update AI enrichment to use same enum

**File:** `src/pipelines/district_document_indexing/extractors/ai_enrichment.py`

Replace `DocumentType` enum (lines 114-124) with import from `models.core.NctqDocumentType`.

Update `DocumentEnrichment.document_type` field to use `NctqDocumentType` with a descriptive Field that tells Gemini what each category means:

```python
from models.core import NctqDocumentType

class DocumentEnrichment(BaseModel):
    document_type: NctqDocumentType = Field(
        ...,
        description="""Classification of document type. Choose from:
        - salary_schedule: Pay scales, compensation tables, salary grids
        - annual_calendar: School year calendars, academic calendars
        - evaluation_handbook: Teacher/staff evaluation procedures and rubrics
        - contract: Collective bargaining agreements, union contracts, MOUs
        - union_document: Union-related docs that aren't contracts (newsletters, grievance forms)
        - benefits_handbook: Health insurance, retirement, leave policies
        - board_policy: Board policies, administrative regulations, bylaws
        - other: Anything that doesn't fit the above categories"""
    )
```

Update the system prompt (line 217-225) to match the 8 categories instead of the current 8 AI categories.

**Do NOT** pass the human classification to Gemini — let AI classify independently so we can see genuine disagreements.

### Step 3: Update DistrictDocument model

**File:** `src/models/documents.py`

Change `src_type` field semantics. The column in silver stays `src_type` but now stores clean snake_case NctqDocumentType values instead of file format:

```python
from .core import NctqDocumentType, normalize_bronze_doc_type

class DistrictDocument(BaseModel):
    # Change from SourceType to NctqDocumentType
    src_type: NctqDocumentType = Field(
        default=NctqDocumentType.OTHER,
        description="Human document classification from NCTQ (contract, salary_schedule, etc.)."
    )

    # Update validator
    @field_validator("src_type", mode="before")
    @classmethod
    def normalize_src_type_validator(cls, v) -> NctqDocumentType:
        """Normalize source type from string input."""
        return normalize_bronze_doc_type(v) if isinstance(v, str) else v

    # Add computed field for type alignment
    @computed_field
    @property
    def type_alignment(self) -> str | None:
        """Compare human vs AI document type classification.
        Returns 'agree', 'disagree', or None if AI hasn't classified yet.
        """
        if not self.ai_document_type:
            return None
        return "agree" if self.src_type.value == self.ai_document_type else "disagree"
```

Also update `DocumentView` to use `NctqDocumentType` instead of `SourceType`.

Update `to_row_dict()` — `src_type` serialization stays as `.value` (already snake_case).

Update `create_district_document()` — `src_type` param type becomes `str` (still accepts strings, validator normalizes).

### Step 4: Fix sync_document_stubs.py

**File:** `src/pipelines/bronze_to_silver/sync_document_stubs.py`

The INSERT SQL at line 138 already does `COALESCE(LOWER(bs.src_type), 'other')` — but LOWER("Salary Schedule") = "salary schedule", not "salary_schedule". Need to add the underscore mapping:

```sql
CASE LOWER(bs.src_type)
    WHEN 'salary schedule' THEN 'salary_schedule'
    WHEN 'annual calendar' THEN 'annual_calendar'
    WHEN 'evaluation handbook' THEN 'evaluation_handbook'
    WHEN 'contract' THEN 'contract'
    WHEN 'union document' THEN 'union_document'
    WHEN 'benefits handbook' THEN 'benefits_handbook'
    WHEN 'board policy' THEN 'board_policy'
    ELSE 'other'
END as src_type
```

### Step 5: SQL migration — normalize existing silver data

**File:** `src/pipelines/bronze_to_silver/migrations/20260223_normalize_src_type.sql`

```sql
-- Migration: Normalize src_type and ai_document_type to unified NctqDocumentType enum
-- Date: 2026-02-23
-- Purpose: Clean up 18 distinct src_type values → 8 clean snake_case values

BEGIN;

-- 1. Backup current values
ALTER TABLE silver.district_documents ADD COLUMN IF NOT EXISTS src_type_backup TEXT;
ALTER TABLE silver.district_documents ADD COLUMN IF NOT EXISTS ai_document_type_backup TEXT;

UPDATE silver.district_documents SET src_type_backup = src_type WHERE src_type_backup IS NULL;
UPDATE silver.district_documents SET ai_document_type_backup = ai_document_type WHERE ai_document_type_backup IS NULL;

-- 2. Normalize src_type (human classification from bronze)
UPDATE silver.district_documents SET src_type = CASE LOWER(TRIM(src_type))
    WHEN 'salary schedule' THEN 'salary_schedule'
    WHEN 'annual calendar' THEN 'annual_calendar'
    WHEN 'evaluation handbook' THEN 'evaluation_handbook'
    WHEN 'contract' THEN 'contract'
    WHEN 'union document' THEN 'union_document'
    WHEN 'benefits handbook' THEN 'benefits_handbook'
    WHEN 'board policy' THEN 'board_policy'
    WHEN 'other' THEN 'other'
    -- File format values that got stored incorrectly (from old normalize_src_type)
    WHEN 'pdf' THEN 'other'
    WHEN 'docx' THEN 'other'
    WHEN 'doc' THEN 'other'
    WHEN 'xlsx' THEN 'other'
    WHEN 'web_page' THEN 'other'
    WHEN 'scraped_page' THEN 'other'
    ELSE 'other'
END;

-- 3. Normalize ai_document_type (AI classification → same enum)
UPDATE silver.district_documents SET ai_document_type = CASE LOWER(TRIM(ai_document_type))
    WHEN 'contract' THEN 'contract'
    WHEN 'salary_schedule' THEN 'salary_schedule'
    WHEN 'evaluation_handbook' THEN 'evaluation_handbook'
    WHEN 'policy' THEN 'board_policy'
    WHEN 'board_policy' THEN 'board_policy'
    WHEN 'calendar' THEN 'annual_calendar'
    WHEN 'annual_calendar' THEN 'annual_calendar'
    WHEN 'union_document' THEN 'union_document'
    WHEN 'benefits_handbook' THEN 'benefits_handbook'
    WHEN 'budget' THEN 'other'
    WHEN 'report' THEN 'other'
    WHEN 'handbook' THEN 'other'
    WHEN 'other' THEN 'other'
    ELSE ai_document_type  -- leave NULL as NULL, keep unknowns
END
WHERE ai_document_type IS NOT NULL;

COMMIT;
```

### Step 6: Dashboard — add type alignment display

**Files to modify:**

1. **`src/dashboard_fast/components/badges.py`** — Add `TypeAlignmentBadge`:

```python
def TypeAlignmentBadge(human_type: str, ai_type: str):
    """Badge showing human vs AI document type agreement."""
    if not ai_type or ai_type == "unknown":
        return Span("")  # No AI classification yet
    agree = human_type == ai_type
    if agree:
        return Badge("Types Agree", color=Colors.INVERSE, bg=Colors.EXACT)
    else:
        human_label = human_type.replace("_", " ").title()
        ai_label = ai_type.replace("_", " ").title()
        return Span(
            Badge(f"Human: {human_label}", color=Colors.INVERSE, bg=Colors.WARNING),
            " ",
            Badge(f"AI: {ai_label}", color=Colors.INVERSE, bg=Colors.BRAND),
        )
```

2. **`src/dashboard_fast/routes/documents.py`** — Add alignment badge to detail page header (`_document_metadata` function, around line 462):
   - Add type alignment row showing both human and AI classifications side by side
   - Use `TypeAlignmentBadge(doc.src_type, doc.ai_document_type)`

3. **`src/dashboard_fast/services/documents.py`** — No SQL changes needed; `src_type` and `ai_document_type` already fetched.

4. **List page health stats** — Add type agreement percentage to stats header on `/documents`:
```sql
-- Add to stats query
SELECT
    COUNT(*) FILTER (WHERE src_type = ai_document_type) as type_agree,
    COUNT(*) FILTER (WHERE ai_document_type IS NOT NULL) as type_total
FROM silver.district_documents
WHERE %s = ANY(ay_ids) AND text_length > 0
```

### Step 7: Run 100+ docs across 5 districts

```bash
# ~20 per district, diverse types
# NOTE: Actual run script is in district_document_indexing, not document_pipeline
PYTHONPATH=src python src/pipelines/district_document_indexing/district_document_indexer.py --district 37 --limit 20
PYTHONPATH=src python src/pipelines/district_document_indexing/district_document_indexer.py --district 60 --limit 20
PYTHONPATH=src python src/pipelines/district_document_indexing/district_document_indexer.py --district 2 --limit 20
PYTHONPATH=src python src/pipelines/district_document_indexing/district_document_indexer.py --district 100 --limit 20
PYTHONPATH=src python src/pipelines/district_document_indexing/district_document_indexer.py --district 150 --limit 20
```

Then query type alignment:

```sql
SELECT src_type as human, ai_document_type as ai,
       CASE WHEN src_type = ai_document_type THEN 'AGREE' ELSE 'DISAGREE' END as alignment,
       COUNT(*)
FROM silver.district_documents
WHERE ai_document_type IS NOT NULL AND text_length > 0
GROUP BY 1, 2, 3 ORDER BY 4 DESC;
```

---

## Verification Checklist

1. **Migration:** `SELECT src_type, COUNT(*) FROM silver.district_documents GROUP BY 1 ORDER BY 2 DESC` — should show only 8 clean snake_case values
2. **AI enum:** `SELECT DISTINCT ai_document_type FROM silver.district_documents WHERE ai_document_type IS NOT NULL` — same 8 values
3. **Pipeline run:** Process 5 docs, check that `src_type` column gets clean values and `ai_document_type` uses same enum
4. **Dashboard:** Load `/documents` — verify type badges, alignment indicator
5. **Dashboard detail:** Load `/documents/{doc_id}` — verify TypeAlignmentBadge appears
6. **Scale run:** 100 docs across 5 districts, check type alignment query results
7. **No regressions:** Existing document list page still works, filters still work, predictor pipeline still loads documents correctly

---

## Risk / Notes

- **SourceType enum is used by DocumentView** (predictor pipeline reads it) — need to update `DocumentView.src_type` type from `SourceType` to `NctqDocumentType` or `str`
- **district_document_indexer.py imports from Databricks** (`databricks.connect`, `delta.tables`) — this is the old Spark-based pipeline. The "docpipe" referenced in the plan may be a newer pipeline not yet in the codebase. Verify which pipeline is actually running.
- **SourcePipeline enum** in `src/models/core.py` — the `source_pipeline` column values like `'district_sources'` are separate from document type. No changes needed there.
- **Backward compat:** The `normalize_src_type()` function is imported by `src/models/documents.py` and `src/models/__init__.py`. Don't delete it until all references are migrated. Add `normalize_bronze_doc_type()` alongside it.
- **Dashboard models** (`src/dashboard_fast/models/documents.py`) use plain `str` for `src_type` and `ai_document_type` — these don't need enum changes, just display changes.
