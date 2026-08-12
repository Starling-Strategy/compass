# NCTQ Document Type Unification — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the AI DocumentType enum with the 8 NCTQ human-coded categories, preserve bronze human classification through to silver, and add an effective_doc_type fallback (human wins, AI fills gaps).

**Architecture:** The docpipe `Document` model gains a `human_doc_type` field populated from bronze. The `DocumentEnrichment` Pydantic output schema constrains Gemini to the same 8 categories. A computed `effective_doc_type` provides the fallback. The `save_document()` function writes both classifications to silver.

**Tech Stack:** Python 3.12, Pydantic, PydanticAI, psycopg2, PostgreSQL

**Design doc:** `docs/plans/2026-02-23-nctq-doc-type-unification-design.md`

---

### Task 1: Replace DocumentType enum with NctqDocumentType

**Files:**
- Modify: `src/document_pipeline/models.py:34-43`

**Step 1: Replace the DocumentType enum**

Replace lines 34-43 in `src/document_pipeline/models.py`. The old `DocumentType` has 9 values (contract, salary_schedule, evaluation_handbook, policy, budget, report, handbook, calendar, other). Replace with exactly the 8 NCTQ bronze categories:

```python
class NctqDocumentType(str, Enum):
    """The 8 human-coded document categories from NCTQ's bronze.district_sources.src_type.

    Used for BOTH human classification (from bronze) and AI classification (from Gemini).
    Having both use the same enum enables apples-to-apples agreement analysis.
    """
    SALARY_SCHEDULE = "salary_schedule"
    OTHER = "other"
    ANNUAL_CALENDAR = "annual_calendar"
    EVALUATION_HANDBOOK = "evaluation_handbook"
    CONTRACT = "contract"
    UNION_DOCUMENT = "union_document"
    BENEFITS_HANDBOOK = "benefits_handbook"
    BOARD_POLICY = "board_policy"
```

**Step 2: Update DocumentEnrichment to use NctqDocumentType**

Change `DocumentEnrichment.ai_document_type` (line 60-62) from `DocumentType` to `NctqDocumentType` with a descriptive Field that tells Gemini what each category means:

```python
    ai_document_type: NctqDocumentType = Field(
        description="Classification of document type. Choose from: "
        "salary_schedule (pay scales, compensation tables, salary grids), "
        "annual_calendar (school year calendars, academic calendars), "
        "evaluation_handbook (teacher/staff evaluation procedures and rubrics), "
        "contract (collective bargaining agreements, union contracts, MOUs), "
        "union_document (union-related docs that aren't contracts — newsletters, grievance forms), "
        "benefits_handbook (health insurance, retirement, leave policies), "
        "board_policy (board policies, administrative regulations, bylaws), "
        "other (anything that doesn't fit the above categories)"
    )
```

**Step 3: Verify the model loads**

Run:
```bash
PYTHONPATH=src python -c "from document_pipeline.models import NctqDocumentType, DocumentEnrichment; print('OK:', list(NctqDocumentType))"
```
Expected: `OK: [NctqDocumentType.SALARY_SCHEDULE, ...]` — all 8 values listed.

**Step 4: Commit**

```bash
git add src/document_pipeline/models.py
git commit -m "refactor(docpipe): replace DocumentType with NctqDocumentType (8 NCTQ categories)"
```

---

### Task 2: Add human_doc_type and effective_doc_type to Document model

**Files:**
- Modify: `src/document_pipeline/models.py:89-120` (Document class)

**Step 1: Add human_doc_type field**

After `src_type: SourceType` (line 98), add:

```python
    human_doc_type: NctqDocumentType | None = None  # from bronze.district_sources.src_type
```

**Step 2: Add effective_doc_type computed field**

After the existing `effective_ay_ids` computed field (lines 122-131), add:

```python
    @computed_field
    @property
    def effective_doc_type(self) -> str:
        """Resolved document type: human classification wins, AI fills gaps."""
        if self.human_doc_type:
            return self.human_doc_type.value
        if self.ai_document_type:
            return self.ai_document_type
        return "other"
```

**Step 3: Add doc_type_alignment computed field**

After `effective_doc_type`, add:

```python
    @computed_field
    @property
    def doc_type_alignment(self) -> str | None:
        """How well AI and human document type classifications agree."""
        if not self.ai_document_type or not self.human_doc_type:
            return None
        if self.human_doc_type.value == self.ai_document_type:
            return "agree"
        return "disagree"
```

**Step 4: Verify**

```bash
PYTHONPATH=src python -c "
from document_pipeline.models import Document, SourceType, NctqDocumentType
d = Document(src_id=1, district_id=37, district_name='Test', src_name='test.pdf', src_link='http://x', src_type=SourceType.PDF, human_doc_type=NctqDocumentType.CONTRACT, ai_document_type='salary_schedule')
print('human:', d.human_doc_type)
print('effective:', d.effective_doc_type)
print('alignment:', d.doc_type_alignment)
"
```
Expected:
```
human: NctqDocumentType.CONTRACT
effective: contract
alignment: disagree
```

**Step 5: Commit**

```bash
git add src/document_pipeline/models.py
git commit -m "feat(docpipe): add human_doc_type and effective_doc_type to Document model"
```

---

### Task 3: Fix db.py — preserve human classification from bronze

**Files:**
- Modify: `src/document_pipeline/db.py:11` (imports)
- Modify: `src/document_pipeline/db.py:25-41` (normalize_src_type function)
- Modify: `src/document_pipeline/db.py:106-119` (load_sources document construction)
- Modify: `src/document_pipeline/db.py:141-217` (save_document)

**Step 1: Add normalize_bronze_doc_type function**

Replace the `normalize_src_type` function (lines 25-41) with a new function that preserves the human classification. Keep the old function too since `load_sources` still needs to determine file format for `src_type`:

```python
def normalize_src_type(raw: str | None) -> SourceType:
    """Detect file format from bronze src_type or filename extension."""
    if not raw:
        return SourceType.OTHER
    lower = raw.lower().strip()
    # These are actual file formats
    format_mapping = {
        "pdf": SourceType.PDF,
        "docx": SourceType.DOCX,
        "xlsx": SourceType.XLSX,
        "web_page": SourceType.WEB_PAGE,
        "web page": SourceType.WEB_PAGE,
    }
    if lower in format_mapping:
        return format_mapping[lower]
    # Everything else (Contract, Salary Schedule, etc.) is a classification, not a format.
    # Assume PDF since most NCTQ docs are PDFs.
    return SourceType.PDF


def normalize_bronze_doc_type(raw: str | None) -> NctqDocumentType | None:
    """Map bronze.district_sources.src_type Title Case to NctqDocumentType.

    Bronze uses Title Case ("Salary Schedule"), we store snake_case ("salary_schedule").
    Returns None if the raw value is a file format (pdf, docx) rather than a classification.
    """
    if not raw:
        return None
    lower = raw.lower().strip()
    mapping = {
        "salary schedule": NctqDocumentType.SALARY_SCHEDULE,
        "annual calendar": NctqDocumentType.ANNUAL_CALENDAR,
        "evaluation handbook": NctqDocumentType.EVALUATION_HANDBOOK,
        "contract": NctqDocumentType.CONTRACT,
        "union document": NctqDocumentType.UNION_DOCUMENT,
        "benefits handbook": NctqDocumentType.BENEFITS_HANDBOOK,
        "board policy": NctqDocumentType.BOARD_POLICY,
        "other": NctqDocumentType.OTHER,
        "generic docs": NctqDocumentType.OTHER,
    }
    return mapping.get(lower)  # Returns None for file formats like "pdf", "docx"
```

**Step 2: Update the import line**

Change line 11:
```python
from document_pipeline.models import Document, NctqDocumentType, SourceType
```

**Step 3: Update load_sources document construction**

In `load_sources()`, update the Document construction (lines 107-119) to populate `human_doc_type`:

```python
    documents = []
    for row in rows:
        documents.append(
            Document(
                src_id=row["src_id"],
                district_id=row["district_id"],
                district_name=row["district_name"],
                src_name=row["src_name"] or "",
                src_link=row["src_link"],
                src_type=normalize_src_type(row["src_type"]),
                human_doc_type=normalize_bronze_doc_type(row["src_type"]),
                valid_from=row["valid_from"],
                valid_to=row["valid_to"],
                human_ay_ids=list(row["human_ay_ids"]) if row["human_ay_ids"] else [],
            )
        )
    return documents
```

**Step 4: Update save_document to write human_doc_type to src_type column**

In `save_document()`, change the `src_type` param (line 195) to write the human classification instead of the file format:

```python
        "src_type": doc.human_doc_type.value if doc.human_doc_type else doc.src_type.value,
```

Also add `effective_doc_type` to the params if it's being saved (it's already in the SQL as `effective_ay_ids` pattern — but `effective_doc_type` is not a column yet, so skip this for now).

**Step 5: Verify with a dry run**

```bash
PYTHONPATH=src python src/document_pipeline/run.py --district 37 --limit 3 --dry-run
```
Expected: No errors, shows 3 documents ready to process.

**Step 6: Commit**

```bash
git add src/document_pipeline/db.py
git commit -m "fix(docpipe): preserve human doc type classification from bronze"
```

---

### Task 4: Update enrich.py system prompt for new categories

**Files:**
- Modify: `src/document_pipeline/enrich.py:17-29` (SYSTEM_PROMPT)

**Step 1: Add document type guidance to system prompt**

The `DocumentEnrichment` field description already tells Gemini the categories (from Task 1). But the system prompt should reinforce it. Add a section after the existing guidelines:

```python
SYSTEM_PROMPT = """You are a document metadata extraction specialist for school district policy documents.

Extract structured metadata from the provided document text. Key guidelines:

1. TITLE: Extract or compose a clear, human-readable title. Include district name, document type, and year range.

2. ACADEMIC YEARS: Use the ay_id system where ay_id 25 = school year 2024-2025. A contract spanning 2019-2024 = ay_ids [20, 21, 22, 23, 24]. If the document has no time-bound dates, return an empty list.

3. DOCUMENT TYPE: Classify into one of the 8 NCTQ categories. Most documents will be contract, salary_schedule, or board_policy. Use union_document only for union materials that are NOT collective bargaining agreements (those are contracts). Use other sparingly — try to fit into a specific category first.

4. READABILITY: Rate the extracted text quality honestly. If tables are garbled, OCR has errors, or content is missing, say so.

5. CONFIDENCE: Rate your confidence based on text quality. Garbled OCR = low confidence.

If information is not clearly stated, use null/empty rather than guessing."""
```

**Step 2: Verify agent still initializes**

```bash
PYTHONPATH=src python -c "from document_pipeline.enrich import SYSTEM_PROMPT; print(SYSTEM_PROMPT[:100])"
```
Expected: First 100 chars of the prompt printed without error.

**Step 3: Commit**

```bash
git add src/document_pipeline/enrich.py
git commit -m "feat(docpipe): add document type classification guidance to system prompt"
```

---

### Task 5: SQL migration to normalize existing silver data

**Files:**
- Create: `src/document_pipeline/migrations/001_normalize_doc_types.sql`

**Step 1: Write the migration**

```sql
-- Migration: Normalize src_type and ai_document_type to unified NctqDocumentType values
-- Date: 2026-02-23
-- Purpose: Clean up existing silver data to use the 8 NCTQ snake_case categories
--
-- Run with: psql -h <private-db-host> -p 5432 -U postgres -d postgres -f src/document_pipeline/migrations/001_normalize_doc_types.sql

BEGIN;

-- 1. Re-derive src_type from bronze (the human classification was destroyed by normalize_src_type)
UPDATE silver.district_documents dd
SET src_type = CASE LOWER(TRIM(bs.src_type))
    WHEN 'salary schedule' THEN 'salary_schedule'
    WHEN 'annual calendar' THEN 'annual_calendar'
    WHEN 'evaluation handbook' THEN 'evaluation_handbook'
    WHEN 'contract' THEN 'contract'
    WHEN 'union document' THEN 'union_document'
    WHEN 'benefits handbook' THEN 'benefits_handbook'
    WHEN 'board policy' THEN 'board_policy'
    WHEN 'other' THEN 'other'
    WHEN 'generic docs' THEN 'other'
    ELSE 'other'
END
FROM bronze.district_sources bs
WHERE dd.src_id = bs.src_id
  AND dd.src_id IS NOT NULL;

-- 2. Normalize ai_document_type (old AI values -> new NCTQ categories)
UPDATE silver.district_documents
SET ai_document_type = CASE LOWER(TRIM(ai_document_type))
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
    ELSE ai_document_type  -- leave NULL as NULL
END
WHERE ai_document_type IS NOT NULL;

COMMIT;
```

**Step 2: Verify migration (check before/after)**

Before running:
```bash
psql -h <private-db-host> -p 5432 -U postgres -d postgres -c "SELECT src_type, COUNT(*) FROM silver.district_documents GROUP BY 1 ORDER BY 2 DESC;"
psql -h <private-db-host> -p 5432 -U postgres -d postgres -c "SELECT ai_document_type, COUNT(*) FROM silver.district_documents WHERE ai_document_type IS NOT NULL GROUP BY 1 ORDER BY 2 DESC;"
```
Expected before: mixed values like "pdf", "other", "web_page" for src_type; "policy", "calendar", "handbook" for ai_document_type.

Run:
```bash
psql -h <private-db-host> -p 5432 -U postgres -d postgres -f src/document_pipeline/migrations/001_normalize_doc_types.sql
```

After running:
```bash
psql -h <private-db-host> -p 5432 -U postgres -d postgres -c "SELECT src_type, COUNT(*) FROM silver.district_documents GROUP BY 1 ORDER BY 2 DESC;"
psql -h <private-db-host> -p 5432 -U postgres -d postgres -c "SELECT ai_document_type, COUNT(*) FROM silver.district_documents WHERE ai_document_type IS NOT NULL GROUP BY 1 ORDER BY 2 DESC;"
```
Expected after: Only the 8 clean snake_case values for both columns.

**Step 3: Commit**

```bash
git add src/document_pipeline/migrations/001_normalize_doc_types.sql
git commit -m "feat(docpipe): add SQL migration to normalize doc types in silver"
```

---

### Task 6: End-to-end validation — process a small batch

**Step 1: Process 3 docs from Broward**

```bash
DOCPIPE_DRY_RUN=false PYTHONPATH=src python src/document_pipeline/run.py --district 37 --limit 3
```
Expected: 3 docs processed. Log output shows `ai_document_type` values from the 8 NCTQ categories.

**Step 2: Verify silver data**

```bash
psql -h <private-db-host> -p 5432 -U postgres -d postgres -c "
SELECT src_id, src_type, ai_document_type,
       CASE WHEN src_type = ai_document_type THEN 'AGREE' ELSE 'DISAGREE' END as alignment
FROM silver.district_documents
WHERE source_pipeline = 'docpipe' AND ai_document_type IS NOT NULL
ORDER BY last_updated DESC
LIMIT 5;
"
```
Expected: `src_type` shows NCTQ classification (e.g., "contract"), `ai_document_type` shows AI classification from same enum, alignment shows AGREE or DISAGREE.

**Step 3: Commit if all looks good**

No code changes needed — this is validation only.
