# Document Pipeline (docpipe)

A standalone document processing pipeline that extracts text from school district policy documents using IBM Docling and enriches them with AI metadata using PydanticAI + Gemini.

**Code:** `src/document_pipeline/`
**Target table:** `silver.district_documents` (marked with `source_pipeline = 'docpipe'`)
**Dashboard:** `src/document_pipeline/dashboard/` (FastHTML + MonsterUI, port 5003)

---

## Architecture

```
bronze.district_sources          silver.district_documents
(src_link, src_name, src_type)   (full_text, ai_title, ai_confidence, ...)
         │                                ▲
         │ load_sources()                 │ save_document()
         ▼                                │
   ┌─────────────────────────────────────────┐
   │           Document Pipeline             │
   │                                         │
   │  1. Download  ──→  httpx GET blob URL   │
   │  2. Extract   ──→  IBM Docling (OCR)    │
   │  3. Enrich    ──→  Gemini 2.5 Flash     │
   │  4. Save      ──→  PostgreSQL upsert    │
   └─────────────────────────────────────────┘
```

### Four Stages

| Stage | Module | What It Does |
|-------|--------|-------------|
| **Download** | `download.py` | Downloads blob URL to a temp file using httpx. Guesses file extension from URL. Caller must delete the temp file. |
| **Extract** | `extract.py` | Converts PDF/DOCX/XLSX/HTML/images to Markdown using IBM Docling. Singleton converter with hardware acceleration (MPS on Apple Silicon, CUDA, or CPU). |
| **Enrich** | `enrich.py` | Sends extracted text to Gemini 2.5 Flash via PydanticAI. Produces structured `DocumentEnrichment` (title, summary, doc type, temporal class, academic years, readability, confidence). |
| **Save** | `db.py` | Upserts into `silver.district_documents` using `src_id` as conflict key. Marks `source_pipeline = 'docpipe'` and clears legacy columns on every write. |

### Data Model

One `Document` object (in `models.py`) flows through all stages, progressively accumulating fields:

- **Identity** (from bronze): `src_id`, `district_id`, `src_name`, `src_link`, `src_type`
- **Academic years** (from bronze): `human_ay_ids` (human-coded year assignments)
- **Extraction** (from Docling): `full_text`, `text_length`, `text_hash`, `page_count`, `extraction_status`
- **AI Enrichment** (from Gemini): `ai_title`, `ai_summary`, `ai_document_type`, `ai_temporal_class`, `ai_readability`, `ai_confidence`, `ai_ay_ids`
- **Computed fields**: `effective_ay_ids` (human wins over AI), `quality_flags`, `ay_alignment`, `table_count`

The `DocumentEnrichment` Pydantic model's `Field(description=...)` values serve as both documentation and Gemini's instructions (Pydantic-First pattern).

---

## Running the Pipeline

### Quick Start

```bash
source .venv/bin/activate

# Preview what would be processed (dry run is the default)
PYTHONPATH=src python src/document_pipeline/run.py --district 37

# Actually process documents
DOCPIPE_DRY_RUN=false PYTHONPATH=src python src/document_pipeline/run.py --district 37

# Process with a limit
DOCPIPE_DRY_RUN=false PYTHONPATH=src python src/document_pipeline/run.py --district 37 --limit 10

# Reprocess only failed/unenriched documents
DOCPIPE_DRY_RUN=false PYTHONPATH=src python src/document_pipeline/run.py --district 37 --failed-only

# Process all districts
DOCPIPE_DRY_RUN=false PYTHONPATH=src python src/document_pipeline/run.py --all
```

### Configuration

All settings use the `DOCPIPE_` environment prefix via Pydantic Settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `DOCPIPE_DRY_RUN` | `true` | Preview without writing to DB |
| `DOCPIPE_LIMIT` | `0` | Max documents (0 = no limit) |
| `DOCPIPE_DISTRICT_ID` | — | Filter to one district |
| `DOCPIPE_HTTP_TIMEOUT` | `60` | Download timeout in seconds |
| `DOCPIPE_USE_OCR` | `true` | Enable OCR for scanned PDFs |
| `DOCPIPE_GOOGLE_API_KEY` | — | Gemini API key (falls back to `GOOGLE_API_KEY`) |
| `DOCPIPE_ENRICHMENT_MODEL` | `gemini-2.5-flash` | Model for AI enrichment |
| `DOCPIPE_PG_HOST` | `20.118.250.76` | PostgreSQL host (production Azure VM) |
| `DOCPIPE_PG_PORT` | `5432` | PostgreSQL port |
| `DOCPIPE_PG_DATABASE` | `postgres` | PostgreSQL database |
| `DOCPIPE_PG_USER` | `postgres` | PostgreSQL user |
| `DOCPIPE_PG_PASSWORD` | — | PostgreSQL password |

### CLI Flags

| Flag | Description |
|------|-------------|
| `--district N` | Process a specific district (by `district_id`) |
| `--limit N` | Process at most N documents |
| `--all` | Process all districts (required if no `--district`) |
| `--dry-run` | Preview mode (no DB writes) |
| `--failed-only` | Only reprocess documents that previously failed or lack enrichment |

### The `--failed-only` Flag

Finds documents in `silver.district_documents` that need reprocessing:
- `extraction_status IN ('failed', 'ocr_failed')` — extraction failures
- `extraction_status = 'success' AND (ai_confidence IS NULL OR ai_confidence = 0)` — extracted but not enriched

This is safe to run repeatedly. Documents are upserted, so re-running overwrites the previous failed attempt.

---

## Dashboard

A FastHTML + MonsterUI monitoring dashboard for inspecting pipeline results.

### Running Locally

```bash
PYTHONPATH=src uvicorn document_pipeline.dashboard.main:app --port 5003 --reload --reload-dir src/document_pipeline
```

### Pages

**`/documents` — Document Index**
- Health cards: total docs, extraction success %, avg confidence, readability breakdown, AY alignment
- Filter bar: district dropdown, status tabs (All/Success/Failed/Pending), search, sort
- Sortable table with doc title, district, type, AY, readability, confidence, text length
- Click any row to view detail page

**`/documents/{src_id}` — Document Detail**
- **Identity card**: district, filename, source type, source URL, valid dates, extraction status
- **AI Enrichment card**: AI title, summary, document type, temporal class, academic years (human vs AI with alignment badge), confidence, readability
- **Quality Signals card**: computed flags (extraction_failed, suspiciously_short, low_ai_confidence, garbled_ocr, no_structure), text length, table count, text hash, extraction error
- **Extracted Text card**: collapsible full text viewer (auto-hidden for docs >1000 chars)

### Dashboard Architecture

```
src/document_pipeline/dashboard/
├── main.py                  # FastHTML app, MonsterUI theme (violet), route registration
├── layout.py                # Shared layout wrapper
├── theme.py                 # Theme configuration
├── theme_constants.py       # Color constants (APPROVED, INCORRECT, MUTED, etc.)
├── db.py                    # PostgreSQL connection (psycopg2 + RealDictCursor)
├── models.py                # Pydantic models (DocumentStats, DocumentSummary, DocumentDetail)
├── components/
│   └── document_badges.py   # Badge components (Readability, Confidence, AYAlignment, etc.)
├── services/
│   └── documents.py         # All SQL queries (filtered by source_pipeline = 'docpipe')
└── routes/
    ├── __init__.py           # Route registration
    ├── documents.py          # /documents index page
    └── document_detail.py    # /documents/{src_id} detail page
```

### Pipeline Filter

The dashboard only shows documents processed by this pipeline, not legacy data:

```python
_PIPELINE_FILTER = "dd.source_pipeline = 'docpipe'"
```

This filter is applied to every query in `services/documents.py`.

---

## Database Details

### Target Table: `silver.district_documents`

The pipeline upserts on `src_id`. On conflict, it:
1. Updates all extraction and enrichment columns
2. Sets `source_pipeline = 'docpipe'`
3. Sets `last_updated = NOW()`
4. **Clears legacy columns** to prevent stale data: `ai_effective_date`, `ai_expiration_date`, `ai_academic_years`, `ai_parties`, `enriched_at`, `http_status`

### Source Table: `bronze.district_sources`

Documents are loaded with their human academic year mappings from `bronze.district_source_yrs`. Only documents with a non-empty `src_link` are eligible.

### Academic Year Display

Academic year IDs are integers where `ay_id 25` = school year 2024-2025. Display format:

```python
def _format_ay(ay_id: int) -> str:
    return f"20{ay_id - 1:02d}-20{ay_id:02d}"
# 25 → "2024-2025", 18 → "2017-2018"
```

---

## Lessons Learned

### 1. Environment Variable Prefix Mismatch

The pipeline config uses the `DOCPIPE_` prefix, so `GOOGLE_API_KEY` in `.env` is **not** automatically available. Either set `DOCPIPE_GOOGLE_API_KEY` or export all `.env` vars:

```bash
export $(grep -v '^#' .env | xargs)
```

The `enrich.py` module has a fallback: `config.google_api_key or os.environ.get("GOOGLE_API_KEY")`. But this only works if `GOOGLE_API_KEY` is in the actual environment, not just in `.env`.

### 2. Legacy Data Distinction

`silver.district_documents` contains data from multiple sources. The `source_pipeline` column distinguishes them:
- `'docpipe'` — processed by this pipeline (Docling extraction + Gemini enrichment)
- `'district_sources'` or other values — legacy data from older pipelines

**Do not filter by `ai_confidence IS NOT NULL`** to identify our docs — legacy data also has `ai_confidence` values. The `source_pipeline` column is the reliable marker.

Our pipeline uniquely populates: `ai_readability`, `ai_temporal_class`, `ai_document_type` (with our enum values).

### 3. Reprocessing Is Safe (Idempotent Upserts)

Because `save_document` uses `ON CONFLICT (src_id) DO UPDATE`, you can safely re-run the pipeline on the same documents. Each run overwrites the previous results. The `--failed-only` flag makes reprocessing targeted and efficient.

### 4. Image-Only PDFs

Some district PDFs are scanned images with no selectable text. Docling extracts these as `<!-- image -->` placeholders. The AI enrichment correctly flags these with low confidence (0.00) and "poor" readability. The `garbled_ocr` quality flag catches documents where `len(set(text[:1000])) < 20` (low character diversity).

### 5. Docling Singleton Pattern

The Docling `DocumentConverter` loads heavy ML models (~2GB). The `extract.py` module uses a singleton pattern (`_converter`) so models are only loaded once, even when processing hundreds of documents.

### 6. Event Loop Reuse

PydanticAI's `agent.run()` is async. Since the pipeline runs synchronously, `enrich.py` manages its own event loop (`_loop`) rather than calling `asyncio.run()` each time (which closes the loop after each call).

---

## Current Status (Feb 2025)

| Metric | Value |
|--------|-------|
| Districts processed | Broward County (37) |
| Documents processed | 48 (docpipe) |
| Extraction success | 100% (after reprocessing) |
| Average confidence | ~0.94 |
| Docs with quality flags | 4 (image-only PDFs with 0.00 confidence) |

### Next Steps

- Process additional districts beyond Broward
- Investigate the 4 image-only PDFs (0.00 confidence) for potential OCR improvements
- Add pipeline run history to the dashboard (`/pipeline` page)
- Consider batch processing optimizations for large districts
