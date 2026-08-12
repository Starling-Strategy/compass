# Document Pipeline Design

**Date:** 2026-02-22
**Status:** Approved
**Goal:** Clean, simple pipeline to process NCTQ source documents into silver.district_documents ready for downstream chunking and prediction.

## Problem

The existing document extraction code is scattered across Databricks notebooks, standalone scripts, migration files, and multiple pipeline directories. It works (91% success rate on 10,514 docs) but is hard to follow, maintain, or explain. We need one clean pipeline in one folder that we can read, run on 5 docs to prove it, then scale to all 11,709 overnight.

## Scope

**In scope:** `bronze.district_sources` (NCTQ blob storage documents) — all 11,709, regardless of `src_active_ind` flag.

**Out of scope:** Website crawl pipeline (`district_website_crawl`), downstream chunking (nctq3), prediction pipeline.

## Architecture

```
bronze.district_sources ──→ Download ──→ Extract ──→ Enrich ──→ silver.district_documents
       (11,709 docs)        (blob URL)   (Docling)   (Gemini)    (with quality metrics)
                                                                         │
                                                                         ▼
                                                              Metabase Dashboard
                                                              (pipeline health)
```

### Pipeline Stages

| Stage | File | Input | Output |
|-------|------|-------|--------|
| 1. Load sources | `db.py` | bronze tables | list[Document] with human_ay_ids |
| 2. Download | `download.py` | blob URL | temp file on disk |
| 3. Extract text | `extract.py` | temp file | markdown text via Docling + OCR |
| 4. Enrich | `enrich.py` | text + model | ai_title, ai_ay_ids, ai_readability, ai_confidence |
| 5. Save | `db.py` | Document | UPSERT to silver.district_documents |

Each stage is one function in one file. If a stage fails for a document, the error is recorded and the pipeline moves on.

## Folder Structure

```
src/document_pipeline/
├── run.py              # Single entry point — the whole pipeline
├── download.py         # Download blob → local file
├── extract.py          # Docling: file → markdown text
├── enrich.py           # Gemini: text → AI metadata
├── db.py               # All SQL reads/writes
├── models.py           # Pydantic models — the spec
└── config.py           # Pydantic Settings, env vars
```

7 files. Each does one thing. No nesting, no utils, no magic.

## CLI Interface

```bash
# Prove it on 5 Broward docs
PYTHONPATH=src python src/document_pipeline/run.py --district 37 --limit 5

# Run all of Broward
PYTHONPATH=src python src/document_pipeline/run.py --district 37

# Run everything overnight
PYTHONPATH=src python src/document_pipeline/run.py --all

# Dry run (default) — shows what it would do
PYTHONPATH=src python src/document_pipeline/run.py --district 37 --limit 5 --dry-run
```

Config via Pydantic Settings with `DOCPIPE_` env prefix. Dry run by default.

## Data Model

One `Document` model flows through all stages, progressively filled in. Field descriptions serve as both documentation and Gemini instructions.

### Identity (from bronze, read-only)

| Field | Type | Source |
|-------|------|--------|
| `src_id` | int | bronze.district_sources |
| `district_id` | int | bronze.district_sources |
| `district_name` | str | bronze.district |
| `src_name` | str | bronze.district_sources (ugly filename) |
| `src_link` | str | blob URL |
| `src_type` | SourceType | pdf, docx, xlsx, web_page, other |
| `valid_from` | date? | bronze.district_sources |
| `valid_to` | date? | bronze.district_sources |

### Academic Years (human vs AI, side by side)

| Field | Type | Source | Purpose |
|-------|------|--------|---------|
| `human_ay_ids` | list[int] | bronze.district_source_yrs | Ground truth from NCTQ humans |
| `ai_ay_ids` | list[int]? | Gemini enrichment | AI's independent assessment |
| `effective_ay_ids` | list[int] | Computed | Human wins, AI fills gaps |

**97.2%** of documents have human year mappings (avg 1.5 years per doc). AI fills the 2.8% gap and provides validation.

`ay_alignment` is a computed field: `exact_match`, `partial_overlap`, or `disagreement` — instant QA for the dashboard.

### Extraction (from Docling)

| Field | Type | Notes |
|-------|------|-------|
| `full_text` | str? | Markdown from Docling |
| `text_length` | int | Character count |
| `text_hash` | str? | SHA-256 for deduplication |
| `page_count` | int? | From Docling |
| `extraction_status` | str | pending, success, failed |
| `extraction_error` | str? | Error message if failed |

### AI Enrichment (Gemini)

Field descriptions are Gemini's instructions — the model IS the prompt.

| Field | Description (what Gemini reads) |
|-------|-------------------------------|
| `ai_title` | Clear human-readable title. Include district name, document type, and year range. |
| `ai_ay_ids` | Academic year IDs this document is relevant to. ay_id 25 = 2024-2025. |
| `ai_temporal_class` | SINGLE_YEAR, MULTI_YEAR, or TIMELESS based on document content. |
| `ai_readability` | GOOD (clean text, well-formed tables), FAIR (mostly readable), POOR (OCR errors, garbled). |
| `ai_confidence` | 0.0-1.0 confidence in enrichment quality. Low if text is garbled or ambiguous. |
| `ai_summary` | 2-3 sentence summary of what this document covers. |
| `ai_document_type` | CONTRACT, SALARY_SCHEDULE, EVALUATION_HANDBOOK, POLICY, CALENDAR, etc. |

### Quality (computed, no AI needed)

| Signal | Logic |
|--------|-------|
| `has_content` | text_length > 200 |
| `quality_flags` | List of problems: `suspiciously_short`, `garbled_ocr`, `no_structure`, `low_ai_confidence`, `extraction_failed` |
| `table_count` | Count of markdown tables in extracted text |
| `ay_alignment` | How well AI and human year assignments agree |

## Silver Table Schema

The pipeline writes to `silver.district_documents`. Key additions to existing schema:

- `human_ay_ids int[]` — from bronze.district_source_yrs
- `ai_ay_ids int[]` — from Gemini
- `effective_ay_ids int[]` — resolved (human wins, AI fills gaps)
- `ai_temporal_class text` — SINGLE_YEAR, MULTI_YEAR, TIMELESS
- `ai_readability text` — GOOD, FAIR, POOR

## Downstream Consumer

The nctq3 chunking pipeline reads from `silver.district_documents` and expects:

- `full_text` is not null and length > 100
- `effective_ay_ids` contains the target academic year
- `ai_confidence` populated (for ranking)
- `ai_title` or `doc_name` populated (for display)
- `extraction_status` = 'success'

The `ai_readability` field lets the chunking pipeline filter or handle POOR documents gracefully.

## Metabase Dashboard

A persistent "war room" dashboard reading directly from silver:

- **KPIs:** Total docs, success rate, GOOD/FAIR/POOR breakdown
- **Quality:** Documents by readability, quality flag distribution
- **Academic years:** Human vs AI agreement rate, docs without year data
- **Pipeline progress:** Processed vs remaining, by district

## Key Decisions

1. **Clean room:** New `src/document_pipeline/` folder. Cherry-pick working code (Docling, Gemini), don't import legacy modules.
2. **All 11,709 docs:** Process everything regardless of `src_active_ind` flag.
3. **Array columns for years:** `human_ay_ids`, `ai_ay_ids`, `effective_ay_ids` on the document row. No junction table — averages 1.5 items, PostgreSQL arrays handle this well.
4. **Human wins:** `effective_ay_ids` uses human assignments when present, AI fills the 2.8% gap.
5. **No parallelism in v1:** Sequential for-loop. Add concurrency later if overnight runs are too slow.
6. **No SUPERSEDED classification:** Would require cross-document comparison. Out of scope for single-doc processing.
7. **Quality signals are computed:** Heuristics are `computed_field` (free), AI readability piggybacks on the enrichment call.
