# Design: Unify Document Type Classification (src_type)

**Date:** 2026-02-23
**Status:** Approved
**Scope:** Docpipe pipeline only (`src/document_pipeline/`)

---

## Problem

Bronze `src_type` contains the human classification ("Contract", "Salary Schedule", etc.), but `normalize_src_type()` in `db.py` maps everything to file format — "Contract" becomes `SourceType.PDF`. The classification is destroyed before it reaches silver.

Meanwhile, AI classifies documents into a different enum (`DocumentType`) with values like `budget`, `report`, `handbook` that don't exist in bronze. No apples-to-apples comparison is possible.

## Decision

Replace the AI `DocumentType` enum with `NctqDocumentType` — the exact 8 human-coded categories from bronze. Preserve the human classification through to silver. Add an `effective_doc_type` computed field that uses human classification when available, AI as fallback.

Keep AI blind to the human classification so we get genuine agreement/disagreement data.

## The 8 NCTQ Categories

| Bronze Value (Title Case) | Enum Value (snake_case) |
|---------------------------|------------------------|
| Salary Schedule | salary_schedule |
| Other | other |
| Annual Calendar | annual_calendar |
| Evaluation Handbook | evaluation_handbook |
| Contract | contract |
| Union Document | union_document |
| Benefits Handbook | benefits_handbook |
| Board Policy | board_policy |

## Files Changed

### 1. `src/document_pipeline/models.py`

- Replace `DocumentType` with `NctqDocumentType` (8 values matching bronze)
- Update `DocumentEnrichment.ai_document_type` to use `NctqDocumentType` with descriptive Field guidance for Gemini
- Add `human_doc_type: NctqDocumentType | None` field to `Document` (populated from bronze)
- Add `effective_doc_type` computed field: human wins, AI fills gaps (same pattern as `effective_ay_ids`)

### 2. `src/document_pipeline/db.py`

- Replace `normalize_src_type()` with `normalize_bronze_doc_type()` that maps Title Case to `NctqDocumentType`
- Update `load_sources()` to populate `human_doc_type` from bronze `src_type`
- Keep `src_type` as file format (detected from actual file, not bronze string)
- Update `save_document()` to write `human_doc_type.value` to `src_type` column in silver
- Write `effective_doc_type` to silver for downstream consumers

### 3. `src/document_pipeline/enrich.py`

No changes needed. Uses `DocumentEnrichment` from models.py — the enum swap propagates automatically through PydanticAI.

### 4. SQL Migration

- Normalize existing `ai_document_type` values: `policy` → `board_policy`, `calendar` → `annual_calendar`, `budget`/`report`/`handbook` → `other`
- Re-derive `src_type` from bronze by JOINing back to `bronze.district_sources.src_type` to recover the original human classification

## Data Flow

```
Bronze src_type ("Contract")
  → normalize_bronze_doc_type() → NctqDocumentType.CONTRACT
  → Document.human_doc_type = NctqDocumentType.CONTRACT

Gemini enrichment (blind, independent)
  → DocumentEnrichment.ai_document_type = NctqDocumentType.CONTRACT
  → Document.ai_document_type = "contract"

Document.effective_doc_type:
  → human_doc_type if present, else ai_document_type, else "other"

Silver:
  → src_type column = human_doc_type.value (e.g., "contract")
  → ai_document_type column = AI's classification (e.g., "contract")
```

## Not In Scope

- `src/models/core.py` SourceType enum (stays as file format)
- `src/models/documents.py` DistrictDocument model (separate concern)
- Legacy indexer (`src/pipelines/district_document_indexing/`)
- Dashboard changes
- Scaling to 100+ docs (separate task)
