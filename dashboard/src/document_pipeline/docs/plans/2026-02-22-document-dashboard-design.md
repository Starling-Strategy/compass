# Document Pipeline Dashboard — Design

**Goal:** Add `/documents` and `/documents/{src_id}` routes to the nctq3 dashboard for validating the document pipeline output.

**Audience:** Internal engineering — validate extraction quality, AI enrichment accuracy, and completeness before scaling to all districts.

**Base:** Built on `piper/src/nctq3/dashboard` architecture (FastHTML + MonsterUI + HTMX, layered routes → services → models → DB).

---

## Page 1: `/documents` — Document Index

**Purpose:** Pipeline health overview + browsable document table for spot-checking.

### Health Cards (top row, 5 cards)

| Card | Query | Display |
|------|-------|---------|
| Total Documents | `COUNT(*)` from silver.district_documents | Scalar |
| Extraction Success | `COUNT(status='success') / COUNT(*)` | Percentage |
| Avg AI Confidence | `AVG(ai_confidence)` where not null | 0.00–1.00 |
| Readability | Count by ai_readability | Mini stacked bar (good/fair/poor) |
| AY Alignment | Count where `human_ay_ids = ai_ay_ids` vs total enriched | Percentage |

### Filter Bar

- **District dropdown** — All districts + individual selection (HTMX, reloads table)
- **Status filter** — All / Success / Failed (tab-style, like existing policy page)
- **Search** — Filter by ai_title or src_name (debounced HTMX)

### Document Table

| Column | Source | Notes |
|--------|--------|-------|
| District | district_id → district name | |
| AI Title | ai_title, fallback to src_name | Clickable → detail page |
| Type | ai_document_type | Badge |
| AY (Human) | human_ay_ids | Comma-joined |
| AY Align | computed: human vs ai match | Badge: exact/partial/disagree |
| Readability | ai_readability | Badge: good(green)/fair(amber)/poor(red) |
| Confidence | ai_confidence | Badge with color threshold |
| Text Length | text_length | Formatted number |

Sortable by any column. Default sort: src_id DESC (newest first).
HTMX partial updates for filter/sort/search.

---

## Page 2: `/documents/{src_id}` — Document Detail

**Purpose:** Inspect one document's extraction and AI enrichment.

**Breadcrumb:** Documents > {ai_title or src_name}

### Card 1: Identity

- District name + ID
- Original filename (src_name)
- Source type badge
- Blob URL (clickable link to original)
- Valid from / valid to dates

### Card 2: AI Enrichment

- AI Title (large)
- AI Summary (paragraph)
- Document Type badge
- Temporal Class badge
- Academic Years: side-by-side comparison
  - Human: [19] → "2018-2019"
  - AI: [19] → "2018-2019"
  - Alignment: "exact_match" badge
- Confidence score (with color)
- Readability rating (with color)

### Card 3: Quality Signals

- Quality flags list (or "No issues" in green)
- Text length + page count
- Table count (markdown tables detected)
- Text hash (for dedup reference)
- Extraction status + error (if failed)

### Card 4: Extracted Text

- Collapsible (collapsed by default for long docs)
- Rendered markdown in a scrollable container (max-height ~60vh)
- "Copy to clipboard" button

---

## Data Layer

### New service: `services/documents.py`

```
get_document_stats(district_id=None) → DocumentStats
get_documents(district_id=None, status=None, search=None, sort=None, limit=50, offset=0) → list[DocumentSummary]
get_document_detail(src_id) → DocumentDetail
```

### Connection

Uses the same PostgreSQL connection as the document pipeline (<private-db-host>:5432).
Since nctq3 dashboard uses `run_sql_pandas_params()`, we'll add a connection path or adapt to use psycopg2 directly matching the document_pipeline pattern.

### New models (Pydantic)

- `DocumentStats` — health card aggregates
- `DocumentSummary` — table row data
- `DocumentDetail` — full document with all fields

---

## Components

### New badges needed:

- `ReadabilityBadge(readability)` — good(green), fair(amber), poor(red)
- `ConfidenceScoreBadge(score)` — green ≥0.8, amber ≥0.5, red <0.5
- `AYAlignmentBadge(alignment)` — exact_match(green), partial_overlap(amber), disagreement(red)
- `DocumentTypeBadge(doc_type)` — neutral color, shows type label
- `ExtractionStatusBadge(status)` — success(green), failed(red), pending(gray)

### Reuse existing:

- `Layout()` — consistent nav + breadcrumbs
- `Badge()` — base badge component
- Card patterns from review page (`.review-card` CSS class)

---

## File Plan

```
nctq3/dashboard/                    # or document_pipeline/dashboard/ — TBD based on where it fits
├── routes/
│   ├── documents.py               # /documents route
│   └── document_detail.py         # /documents/{src_id} route
├── services/
│   └── documents.py               # SQL queries + aggregation
├── components/
│   └── document_badges.py         # Readability, Confidence, AYAlignment badges
```

Register routes in `routes/__init__.py`.
