# Document Pipeline Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build `src/document_pipeline/` — 7 files that process NCTQ source documents from bronze through extraction and enrichment into silver.district_documents.

**Architecture:** Sequential pipeline: load from bronze → download blob → Docling extract → Gemini enrich → save to silver. One `Document` model carries state through all stages. Uses `library.db` for database connections (proven infrastructure), fresh code for everything else.

**Tech Stack:** Python 3.12, Pydantic + PydanticAI, IBM Docling (OCR/PDF), Gemini 2.5 Flash, psycopg2, httpx

**Key reference files (read, don't import):**
- Existing extractor: `src/pipelines/district_document_indexing/extractors/file_extractor.py`
- Existing enrichment: `src/pipelines/district_document_indexing/extractors/ai_enrichment.py`
- Existing sync: `src/pipelines/bronze_to_silver/sync_document_stubs.py`
- DB utilities: `src/library/db.py` (OK to import — shared infrastructure)
- DB config: `src/library/config.py`

---

### Task 1: Create folder and config.py

**Files:**
- Create: `src/document_pipeline/__init__.py`
- Create: `src/document_pipeline/config.py`

**Step 1: Create the directory**

```bash
mkdir -p src/document_pipeline
```

**Step 2: Create empty `__init__.py`**

Create `src/document_pipeline/__init__.py` with just a module docstring:

```python
"""Document processing pipeline: bronze.district_sources → silver.district_documents."""
```

**Step 3: Write config.py**

```python
"""Pipeline configuration via environment variables (DOCPIPE_ prefix)."""

from pydantic import Field
from pydantic_settings import BaseSettings


class Config(BaseSettings):
    """All settings for the document pipeline."""

    # Execution
    dry_run: bool = Field(default=True, description="Preview without writing to DB")
    limit: int = Field(default=0, ge=0, description="Max documents to process (0 = no limit)")
    district_id: int | None = Field(default=None, description="Filter to one district")
    process_all: bool = Field(default=False, description="Process all districts")

    # Extraction
    http_timeout: int = Field(default=60, ge=10, description="Download timeout in seconds")
    use_ocr: bool = Field(default=True, description="Enable OCR for scanned PDFs")

    # Enrichment
    google_api_key: str | None = Field(default=None, description="Gemini API key")
    enrichment_model: str = Field(default="gemini-2.5-flash", description="Model for AI enrichment")
    max_text_for_enrichment: int = Field(default=50000, description="Max chars sent to Gemini")

    class Config:
        env_prefix = "DOCPIPE_"
        env_file = ".env"
        extra = "ignore"
```

**Step 4: Verify it loads**

```bash
PYTHONPATH=src python -c "from document_pipeline.config import Config; c = Config(); print(f'dry_run={c.dry_run}, limit={c.limit}')"
```

Expected: `dry_run=True, limit=0`

**Step 5: Commit**

```bash
git add src/document_pipeline/
git commit -m "feat(docpipe): add config.py with DOCPIPE_ env prefix"
```

---

### Task 2: Write models.py — the spec

**Files:**
- Create: `src/document_pipeline/models.py`

This is the most important file. The model IS the spec.

**Step 1: Write models.py**

```python
"""Document model — the single source of truth for the pipeline.

One Document flows through all stages, progressively filled in.
Field descriptions serve as both documentation AND Gemini instructions.
"""

import hashlib
from datetime import date
from enum import Enum

from pydantic import BaseModel, Field, computed_field


class SourceType(str, Enum):
    PDF = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"
    WEB_PAGE = "web_page"
    OTHER = "other"


class TemporalClass(str, Enum):
    SINGLE_YEAR = "single_year"
    MULTI_YEAR = "multi_year"
    TIMELESS = "timeless"


class Readability(str, Enum):
    GOOD = "good"
    FAIR = "fair"
    POOR = "poor"


class DocumentType(str, Enum):
    CONTRACT = "contract"
    SALARY_SCHEDULE = "salary_schedule"
    EVALUATION_HANDBOOK = "evaluation_handbook"
    POLICY = "policy"
    BUDGET = "budget"
    REPORT = "report"
    HANDBOOK = "handbook"
    CALENDAR = "calendar"
    OTHER = "other"


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
    ai_document_type: DocumentType = Field(
        description="Classification of document type."
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
    src_type: SourceType
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
```

**Step 2: Verify it loads and computed fields work**

```bash
PYTHONPATH=src python -c "
from document_pipeline.models import Document, SourceType
d = Document(
    src_id=1, district_id=37, district_name='Broward',
    src_name='test.pdf', src_link='https://example.com/test.pdf',
    src_type=SourceType.PDF, human_ay_ids=[25],
    full_text='# Hello\nSome text here that is long enough to pass checks.' * 10,
    text_length=500, extraction_status='success',
    ai_ay_ids=[25, 26], ai_confidence=0.9,
)
print(f'effective_ay_ids={d.effective_ay_ids}')
print(f'ay_alignment={d.ay_alignment}')
print(f'quality_flags={d.quality_flags}')
print(f'has_content={d.has_content}')
"
```

Expected:
```
effective_ay_ids=[25]
ay_alignment=partial_overlap
quality_flags=[]
has_content=True
```

**Step 3: Commit**

```bash
git add src/document_pipeline/models.py
git commit -m "feat(docpipe): add Document model with computed quality signals"
```

---

### Task 3: Write db.py — all SQL in one place

**Files:**
- Create: `src/document_pipeline/db.py`

Uses `library.db` for connections. All SQL lives here — nowhere else.

**Step 1: Write db.py**

```python
"""All database operations for the document pipeline.

Uses library.db for PostgreSQL connections.
All SQL lives in this file — nowhere else in the pipeline.
"""

from library.db import get_pg_connection, run_sql_pandas

from document_pipeline.models import Document, SourceType


def normalize_src_type(raw: str | None) -> SourceType:
    """Convert bronze src_type strings to our enum."""
    if not raw:
        return SourceType.OTHER
    lower = raw.lower().strip()
    mapping = {
        "pdf": SourceType.PDF,
        "annual calendar": SourceType.PDF,
        "contract": SourceType.PDF,
        "generic docs": SourceType.PDF,
        "salary schedule": SourceType.PDF,
        "docx": SourceType.DOCX,
        "xlsx": SourceType.XLSX,
        "web_page": SourceType.WEB_PAGE,
        "web page": SourceType.WEB_PAGE,
    }
    return mapping.get(lower, SourceType.OTHER)


def load_sources(
    district_id: int | None = None,
    limit: int = 0,
    process_all: bool = False,
) -> list[Document]:
    """Load documents from bronze.district_sources with human academic year mappings.

    Args:
        district_id: Filter to one district (required unless process_all=True)
        limit: Max documents (0 = no limit)
        process_all: Process all districts

    Returns:
        List of Document objects ready for processing.
    """
    if not district_id and not process_all:
        raise ValueError("Provide --district or --all")

    where = ""
    if district_id:
        where = f"AND bs.district_id = {district_id}"

    limit_clause = f"LIMIT {limit}" if limit > 0 else ""

    sql = f"""
        SELECT
            bs.src_id,
            bs.district_id::INTEGER as district_id,
            d.district_name,
            bs.src_name,
            bs.src_link,
            bs.src_type,
            bs.src_valid_from::DATE as valid_from,
            bs.src_valid_to::DATE as valid_to,
            COALESCE(
                ARRAY_AGG(DISTINCT sy.ay_id::INTEGER ORDER BY sy.ay_id::INTEGER)
                FILTER (WHERE sy.ay_id IS NOT NULL),
                ARRAY[]::INTEGER[]
            ) as human_ay_ids
        FROM bronze.district_sources bs
        JOIN bronze.district d ON bs.district_id = d.district_id
        LEFT JOIN bronze.district_source_yrs sy ON bs.src_id = sy.src_id
        WHERE bs.src_link IS NOT NULL
          AND bs.src_link != ''
          {where}
        GROUP BY bs.src_id, bs.district_id, d.district_name,
                 bs.src_name, bs.src_link, bs.src_type,
                 bs.src_valid_from, bs.src_valid_to
        ORDER BY bs.src_id
        {limit_clause}
    """
    df = run_sql_pandas(sql)
    if df.empty:
        return []

    documents = []
    for _, row in df.iterrows():
        documents.append(
            Document(
                src_id=row["src_id"],
                district_id=row["district_id"],
                district_name=row["district_name"],
                src_name=row["src_name"] or "",
                src_link=row["src_link"],
                src_type=normalize_src_type(row["src_type"]),
                valid_from=row["valid_from"],
                valid_to=row["valid_to"],
                human_ay_ids=list(row["human_ay_ids"]) if row["human_ay_ids"] else [],
            )
        )
    return documents


def save_document(doc: Document) -> None:
    """Upsert a processed document into silver.district_documents.

    Uses src_id as the conflict key. Updates all fields on conflict.
    """
    sql = """
        INSERT INTO silver.district_documents (
            src_id, district_id, src_link, doc_name, src_type,
            valid_from, valid_to, source_pipeline, extraction_status,
            full_text, text_length, text_hash, extraction_error,
            human_ay_ids, ai_ay_ids, effective_ay_ids,
            ai_title, ai_summary, ai_document_type,
            ai_temporal_class, ai_readability, ai_confidence,
            ingested_at, last_updated
        ) VALUES (
            %(src_id)s, %(district_id)s, %(src_link)s, %(src_name)s, %(src_type)s,
            %(valid_from)s, %(valid_to)s, 'district_sources', %(extraction_status)s,
            %(full_text)s, %(text_length)s, %(text_hash)s, %(extraction_error)s,
            %(human_ay_ids)s, %(ai_ay_ids)s, %(effective_ay_ids)s,
            %(ai_title)s, %(ai_summary)s, %(ai_document_type)s,
            %(ai_temporal_class)s, %(ai_readability)s, %(ai_confidence)s,
            NOW(), NOW()
        )
        ON CONFLICT (src_id) WHERE src_id IS NOT NULL
        DO UPDATE SET
            extraction_status = EXCLUDED.extraction_status,
            full_text = EXCLUDED.full_text,
            text_length = EXCLUDED.text_length,
            text_hash = EXCLUDED.text_hash,
            extraction_error = EXCLUDED.extraction_error,
            human_ay_ids = EXCLUDED.human_ay_ids,
            ai_ay_ids = EXCLUDED.ai_ay_ids,
            effective_ay_ids = EXCLUDED.effective_ay_ids,
            ai_title = EXCLUDED.ai_title,
            ai_summary = EXCLUDED.ai_summary,
            ai_document_type = EXCLUDED.ai_document_type,
            ai_temporal_class = EXCLUDED.ai_temporal_class,
            ai_readability = EXCLUDED.ai_readability,
            ai_confidence = EXCLUDED.ai_confidence,
            last_updated = NOW()
    """
    params = {
        "src_id": doc.src_id,
        "district_id": doc.district_id,
        "src_link": doc.src_link,
        "src_name": doc.src_name,
        "src_type": doc.src_type.value,
        "valid_from": doc.valid_from,
        "valid_to": doc.valid_to,
        "extraction_status": doc.extraction_status,
        "full_text": doc.full_text or "",
        "text_length": doc.text_length,
        "text_hash": doc.text_hash or "",
        "extraction_error": doc.extraction_error,
        "human_ay_ids": doc.human_ay_ids or [],
        "ai_ay_ids": doc.ai_ay_ids,
        "effective_ay_ids": doc.effective_ay_ids,
        "ai_title": doc.ai_title,
        "ai_summary": doc.ai_summary,
        "ai_document_type": doc.ai_document_type,
        "ai_temporal_class": doc.ai_temporal_class,
        "ai_readability": doc.ai_readability,
        "ai_confidence": doc.ai_confidence,
    }

    with get_pg_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
```

**Step 2: Test load_sources on Broward**

```bash
PYTHONPATH=src python -c "
from document_pipeline.db import load_sources
docs = load_sources(district_id=37, limit=3)
for d in docs:
    print(f'src_id={d.src_id} | {d.src_name[:50]} | ay={d.human_ay_ids}')
print(f'\nLoaded {len(docs)} documents')
"
```

Expected: 3 Broward documents with human_ay_ids populated.

**Step 3: Commit**

```bash
git add src/document_pipeline/db.py
git commit -m "feat(docpipe): add db.py with load_sources and save_document"
```

---

### Task 4: Write download.py

**Files:**
- Create: `src/document_pipeline/download.py`

Cherry-picks the download logic from `file_extractor.py` but simpler — just download, return path.

**Step 1: Write download.py**

```python
"""Download a document from a blob URL to a local temp file."""

import tempfile
from pathlib import Path

import httpx


def download(url: str, timeout: int = 60) -> Path:
    """Download a URL to a temp file with the correct extension.

    Args:
        url: The blob URL to download.
        timeout: HTTP timeout in seconds.

    Returns:
        Path to the downloaded temp file. Caller must delete when done.

    Raises:
        httpx.HTTPStatusError: On 4xx/5xx responses.
        httpx.TimeoutException: On timeout.
    """
    with httpx.Client(timeout=timeout, follow_redirects=True, verify=False) as client:
        response = client.get(url)
        response.raise_for_status()

    # Guess extension from URL
    url_lower = url.lower()
    suffix = ".pdf"  # default
    for ext in [".docx", ".xlsx", ".pptx", ".html", ".jpg", ".png"]:
        if url_lower.endswith(ext):
            suffix = ext
            break

    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.write(response.content)
    tmp.close()
    return Path(tmp.name)
```

**Step 2: Test download on a real blob URL**

```bash
PYTHONPATH=src python -c "
from document_pipeline.db import load_sources
from document_pipeline.download import download

docs = load_sources(district_id=37, limit=1)
d = docs[0]
print(f'Downloading: {d.src_link[:80]}...')
path = download(d.src_link)
print(f'Downloaded to: {path} ({path.stat().st_size:,} bytes)')
path.unlink()
print('Cleaned up.')
"
```

Expected: Downloads a file, prints size, cleans up.

**Step 3: Commit**

```bash
git add src/document_pipeline/download.py
git commit -m "feat(docpipe): add download.py for blob URL fetching"
```

---

### Task 5: Write extract.py — Docling wrapper

**Files:**
- Create: `src/document_pipeline/extract.py`

Cherry-picks from `file_extractor.py`. Simpler interface: file path in, text out.

**Step 1: Write extract.py**

```python
"""Extract text from documents using IBM Docling.

Supports PDF, DOCX, PPTX, XLSX, HTML, Images with OCR.
"""

import hashlib
import logging
import time
from pathlib import Path

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import (
    AcceleratorDevice,
    PdfPipelineOptions,
    TableFormerMode,
)
from docling.document_converter import DocumentConverter, PdfFormatOption

from document_pipeline.models import Document

logger = logging.getLogger(__name__)

# Singleton converter — heavy ML models, create once
_converter: DocumentConverter | None = None


def _get_converter(use_ocr: bool = True) -> DocumentConverter:
    """Get or create the Docling converter singleton."""
    global _converter
    if _converter is not None:
        return _converter

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = use_ocr
    pipeline_options.do_table_structure = True
    pipeline_options.table_structure_options.mode = TableFormerMode.ACCURATE

    # Hardware acceleration
    import torch

    if torch.backends.mps.is_available():
        pipeline_options.accelerator_options.device = AcceleratorDevice.MPS
        logger.info("Using Apple Silicon (MPS) acceleration")
    elif torch.cuda.is_available():
        pipeline_options.accelerator_options.device = AcceleratorDevice.CUDA
        logger.info("Using CUDA acceleration")
    else:
        pipeline_options.accelerator_options.device = AcceleratorDevice.CPU
        logger.info("Using CPU processing")

    _converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
            InputFormat.IMAGE: PdfFormatOption(pipeline_options=pipeline_options),
        }
    )
    return _converter


def extract(doc: Document, file_path: Path, use_ocr: bool = True) -> Document:
    """Extract text from a downloaded file and update the Document.

    Args:
        doc: The Document to update.
        file_path: Path to the downloaded file.
        use_ocr: Enable OCR for scanned documents.

    Returns:
        The same Document with extraction fields populated.
    """
    start = time.time()
    try:
        converter = _get_converter(use_ocr)
        result = converter.convert(file_path)
        markdown = result.document.export_to_markdown()

        page_count = 0
        if hasattr(result.document, "pages"):
            page_count = len(result.document.pages)

        doc.full_text = markdown
        doc.text_length = len(markdown)
        doc.text_hash = hashlib.sha256(markdown.encode()).hexdigest()
        doc.page_count = page_count
        doc.extraction_status = "success"

        elapsed = int((time.time() - start) * 1000)
        logger.info(f"Extracted {doc.src_id}: {doc.text_length:,} chars in {elapsed}ms")

    except Exception as e:
        doc.extraction_status = "failed"
        doc.extraction_error = str(e)[:500]
        elapsed = int((time.time() - start) * 1000)
        logger.error(f"Extraction failed for {doc.src_id}: {e} ({elapsed}ms)")

    return doc
```

**Step 2: Test extraction on one real document**

```bash
PYTHONPATH=src python -c "
from document_pipeline.db import load_sources
from document_pipeline.download import download
from document_pipeline.extract import extract

docs = load_sources(district_id=37, limit=1)
d = docs[0]
print(f'Processing: {d.src_name[:60]}')

path = download(d.src_link)
d = extract(d, path)
path.unlink()

print(f'Status: {d.extraction_status}')
print(f'Text length: {d.text_length:,} chars')
print(f'Page count: {d.page_count}')
print(f'Quality flags: {d.quality_flags}')
print(f'First 200 chars: {d.full_text[:200]}')
"
```

Expected: Successful extraction with text content and quality signals.

**Step 3: Commit**

```bash
git add src/document_pipeline/extract.py
git commit -m "feat(docpipe): add extract.py with Docling OCR extraction"
```

---

### Task 6: Write enrich.py — Gemini enrichment

**Files:**
- Create: `src/document_pipeline/enrich.py`

Cherry-picks from `ai_enrichment.py`. Uses PydanticAI with `DocumentEnrichment` as output schema — field descriptions ARE the prompt.

**Step 1: Write enrich.py**

```python
"""AI enrichment using PydanticAI + Gemini.

The DocumentEnrichment model's Field descriptions are Gemini's instructions.
"""

import asyncio
import logging
import os

from pydantic_ai import Agent

from document_pipeline.config import Config
from document_pipeline.models import Document, DocumentEnrichment

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a document metadata extraction specialist for school district policy documents.

Extract structured metadata from the provided document text. Key guidelines:

1. TITLE: Extract or compose a clear, human-readable title. Include district name, document type, and year range.

2. ACADEMIC YEARS: Use the ay_id system where ay_id 25 = school year 2024-2025. A contract spanning 2019-2024 = ay_ids [20, 21, 22, 23, 24]. If the document has no time-bound dates, return an empty list.

3. READABILITY: Rate the extracted text quality honestly. If tables are garbled, OCR has errors, or content is missing, say so.

4. CONFIDENCE: Rate your confidence based on text quality. Garbled OCR = low confidence.

If information is not clearly stated, use null/empty rather than guessing."""

# Lazy agent singleton
_agent: Agent | None = None


def _get_agent(config: Config) -> Agent:
    global _agent
    if _agent is not None:
        return _agent

    api_key = config.google_api_key or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("Set DOCPIPE_GOOGLE_API_KEY or GOOGLE_API_KEY")
    os.environ["GOOGLE_API_KEY"] = api_key

    _agent = Agent(
        model=f"google-gla:{config.enrichment_model}",
        output_type=DocumentEnrichment,
        system_prompt=SYSTEM_PROMPT,
    )
    return _agent


def enrich(doc: Document, config: Config) -> Document:
    """Enrich a document with AI-extracted metadata.

    Runs Gemini on the extracted text. Populates ai_* fields on the Document.
    If enrichment fails, sets ai_confidence to 0.0 and logs the error.
    """
    if not doc.full_text or doc.extraction_status != "success":
        return doc

    try:
        agent = _get_agent(config)

        # Truncate long documents (keep beginning + end for context)
        text = doc.full_text
        max_chars = config.max_text_for_enrichment
        if len(text) > max_chars:
            half = max_chars // 2
            text = text[:half] + "\n\n[...TRUNCATED...]\n\n" + text[-half:]

        # Build prompt with context
        prompt = f"Document text:\n\n{text}"
        prompt += f"\n\nOriginal filename: {doc.src_name}"
        prompt += f"\nDistrict: {doc.district_name}"
        if doc.human_ay_ids:
            prompt += f"\nKnown academic years from human coders: {doc.human_ay_ids}"

        # Run synchronously (v1 is sequential)
        result = asyncio.run(agent.run(prompt))
        enrichment = result.output

        # Apply enrichment to document
        doc.ai_title = enrichment.ai_title
        doc.ai_summary = enrichment.ai_summary
        doc.ai_document_type = enrichment.ai_document_type.value
        doc.ai_ay_ids = enrichment.ai_ay_ids
        doc.ai_temporal_class = enrichment.ai_temporal_class.value
        doc.ai_readability = enrichment.ai_readability.value
        doc.ai_confidence = enrichment.ai_confidence

        logger.info(
            f"Enriched {doc.src_id}: {doc.ai_title} "
            f"(confidence={doc.ai_confidence}, readability={doc.ai_readability})"
        )

    except Exception as e:
        doc.ai_confidence = 0.0
        logger.error(f"Enrichment failed for {doc.src_id}: {e}")

    return doc
```

**Step 2: Test enrichment on one document**

```bash
DOCPIPE_GOOGLE_API_KEY=$GOOGLE_API_KEY PYTHONPATH=src python -c "
from document_pipeline.config import Config
from document_pipeline.db import load_sources
from document_pipeline.download import download
from document_pipeline.extract import extract
from document_pipeline.enrich import enrich

config = Config()
docs = load_sources(district_id=37, limit=1)
d = docs[0]

path = download(d.src_link)
d = extract(d, path)
path.unlink()

d = enrich(d, config)
print(f'Title: {d.ai_title}')
print(f'Type: {d.ai_document_type}')
print(f'AI years: {d.ai_ay_ids}')
print(f'Human years: {d.human_ay_ids}')
print(f'Alignment: {d.ay_alignment}')
print(f'Readability: {d.ai_readability}')
print(f'Confidence: {d.ai_confidence}')
print(f'Temporal: {d.ai_temporal_class}')
"
```

Expected: AI metadata populated, alignment computed against human years.

**Step 3: Commit**

```bash
git add src/document_pipeline/enrich.py
git commit -m "feat(docpipe): add enrich.py with PydanticAI + Gemini enrichment"
```

---

### Task 7: Write run.py — the orchestrator

**Files:**
- Create: `src/document_pipeline/run.py`

The whole pipeline in one readable file.

**Step 1: Write run.py**

```python
"""Document processing pipeline entry point.

Usage:
    PYTHONPATH=src python src/document_pipeline/run.py --district 37 --limit 5
    PYTHONPATH=src python src/document_pipeline/run.py --district 37 --limit 5 --dry-run
    DOCPIPE_DRY_RUN=false PYTHONPATH=src python src/document_pipeline/run.py --all
"""

import argparse
import logging
import sys
import time

from document_pipeline.config import Config
from document_pipeline.db import load_sources, save_document
from document_pipeline.download import download
from document_pipeline.enrich import enrich
from document_pipeline.extract import extract

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Process NCTQ source documents")
    parser.add_argument("--district", type=int, help="District ID to process")
    parser.add_argument("--limit", type=int, default=0, help="Max documents (0 = all)")
    parser.add_argument("--all", action="store_true", help="Process all districts")
    parser.add_argument("--dry-run", action="store_true", help="Preview without saving")
    return parser.parse_args()


def main():
    args = parse_args()
    config = Config()

    # CLI args override env vars
    if args.district:
        config.district_id = args.district
    if args.limit:
        config.limit = args.limit
    if args.all:
        config.process_all = True
    if args.dry_run:
        config.dry_run = True

    # Load
    logger.info("Loading sources from bronze...")
    documents = load_sources(
        district_id=config.district_id,
        limit=config.limit,
        process_all=config.process_all,
    )
    logger.info(f"Loaded {len(documents)} documents")

    if not documents:
        logger.info("Nothing to process.")
        return

    if config.dry_run:
        logger.info("DRY RUN — showing what would be processed:")
        for d in documents[:20]:
            print(f"  src_id={d.src_id:5d} | D{d.district_id:3d} | {d.src_name[:60]}")
        if len(documents) > 20:
            print(f"  ... and {len(documents) - 20} more")
        print(f"\nSet DOCPIPE_DRY_RUN=false to process.")
        return

    # Process
    succeeded, failed = 0, 0
    start = time.time()

    for i, doc in enumerate(documents, 1):
        logger.info(f"[{i}/{len(documents)}] Processing src_id={doc.src_id}: {doc.src_name[:50]}")

        try:
            # Download
            file_path = download(doc.src_link, timeout=config.http_timeout)

            try:
                # Extract
                doc = extract(doc, file_path, use_ocr=config.use_ocr)

                # Enrich (only if extraction succeeded)
                if doc.extraction_status == "success":
                    doc = enrich(doc, config)

                # Compute hash
                doc.text_hash = doc.compute_text_hash()

            finally:
                # Always clean up temp file
                file_path.unlink(missing_ok=True)

            # Save
            save_document(doc)

            if doc.extraction_status == "success":
                succeeded += 1
                logger.info(f"  OK: {doc.ai_title or doc.src_name[:50]} ({doc.text_length:,} chars)")
            else:
                failed += 1
                logger.warning(f"  FAIL: {doc.extraction_error}")

        except Exception as e:
            failed += 1
            doc.extraction_status = "failed"
            doc.extraction_error = str(e)[:500]
            save_document(doc)
            logger.error(f"  ERROR: {e}")

    # Summary
    elapsed = time.time() - start
    logger.info("=" * 60)
    logger.info(f"DONE in {elapsed:.0f}s")
    logger.info(f"  Processed: {succeeded + failed}")
    logger.info(f"  Succeeded: {succeeded}")
    logger.info(f"  Failed:    {failed}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
```

**Step 2: Test dry run**

```bash
PYTHONPATH=src python src/document_pipeline/run.py --district 37 --limit 5 --dry-run
```

Expected: Lists 5 Broward documents without processing.

**Step 3: Commit**

```bash
git add src/document_pipeline/run.py
git commit -m "feat(docpipe): add run.py orchestrator with CLI interface"
```

---

### Task 8: Schema migration — add new columns to silver.district_documents

**Files:**
- Create: `src/pipelines/bronze_to_silver/migrations/20260222_add_docpipe_columns.sql`

Before we can run for real, silver.district_documents needs the new columns.

**Step 1: Check which columns already exist**

```bash
PYTHONPATH=src python -c "
from library.db import run_sql_pandas
df = run_sql_pandas(\"\"\"
    SELECT column_name, data_type
    FROM information_schema.columns
    WHERE table_schema = 'silver' AND table_name = 'district_documents'
    ORDER BY ordinal_position
\"\"\")
for _, r in df.iterrows():
    print(f'  {r[\"column_name\"]:30s} {r[\"data_type\"]}')
"
```

**Step 2: Write migration for missing columns**

Based on what exists, write the migration. Likely additions:

```sql
-- Migration: Add document pipeline columns to silver.district_documents
-- Date: 2026-02-22

-- Academic year tracking (human vs AI)
ALTER TABLE silver.district_documents ADD COLUMN IF NOT EXISTS human_ay_ids INTEGER[] DEFAULT '{}';
ALTER TABLE silver.district_documents ADD COLUMN IF NOT EXISTS ai_ay_ids INTEGER[];
ALTER TABLE silver.district_documents ADD COLUMN IF NOT EXISTS effective_ay_ids INTEGER[] DEFAULT '{}';

-- AI enrichment additions
ALTER TABLE silver.district_documents ADD COLUMN IF NOT EXISTS ai_temporal_class TEXT;
ALTER TABLE silver.district_documents ADD COLUMN IF NOT EXISTS ai_readability TEXT;

-- Ensure existing columns exist (idempotent)
ALTER TABLE silver.district_documents ADD COLUMN IF NOT EXISTS ai_title TEXT;
ALTER TABLE silver.district_documents ADD COLUMN IF NOT EXISTS ai_summary TEXT;
ALTER TABLE silver.district_documents ADD COLUMN IF NOT EXISTS ai_document_type TEXT;
ALTER TABLE silver.district_documents ADD COLUMN IF NOT EXISTS ai_confidence FLOAT;
ALTER TABLE silver.district_documents ADD COLUMN IF NOT EXISTS extraction_error TEXT;
ALTER TABLE silver.district_documents ADD COLUMN IF NOT EXISTS last_updated TIMESTAMP;
```

**Step 3: Run migration (after reviewing)**

```bash
PGGSSENCMODE=disable psql -h <private-db-host> -p 5432 -U postgres -d postgres -f src/pipelines/bronze_to_silver/migrations/20260222_add_docpipe_columns.sql
```

**Step 4: Commit**

```bash
git add src/pipelines/bronze_to_silver/migrations/20260222_add_docpipe_columns.sql
git commit -m "feat(docpipe): add schema migration for new pipeline columns"
```

---

### Task 9: Integration test — run on 5 Broward documents

**Step 1: Run the full pipeline on 5 documents**

```bash
DOCPIPE_DRY_RUN=false DOCPIPE_GOOGLE_API_KEY=$GOOGLE_API_KEY PYTHONPATH=src python src/document_pipeline/run.py --district 37 --limit 5
```

**Step 2: Verify results in database**

```bash
PYTHONPATH=src python -c "
from library.db import run_sql_pandas
df = run_sql_pandas(\"\"\"
    SELECT src_id, ai_title, extraction_status, ai_readability, ai_confidence,
           human_ay_ids, ai_ay_ids, effective_ay_ids, text_length
    FROM silver.district_documents
    WHERE district_id = 37
      AND last_updated > NOW() - INTERVAL '1 hour'
    ORDER BY src_id
    LIMIT 10
\"\"\")
for _, r in df.iterrows():
    print(f'src_id={r[\"src_id\"]} | {r[\"ai_title\"][:40] if r[\"ai_title\"] else \"NO TITLE\"} | '
          f'{r[\"extraction_status\"]} | readability={r[\"ai_readability\"]} | conf={r[\"ai_confidence\"]}')
    print(f'  human_ay={r[\"human_ay_ids\"]} | ai_ay={r[\"ai_ay_ids\"]} | effective={r[\"effective_ay_ids\"]}')
"
```

**Step 3: Check quality signals**

Verify that `ay_alignment`, `quality_flags`, and `ai_readability` are producing useful data. Review any documents with quality issues.

**Step 4: Commit integration test results**

If everything looks good, commit any final adjustments.

---

### Task 10: Metabase dashboard (persistent war room)

**Prerequisite:** Tasks 1-9 complete and 5+ documents processed.

Use the `/managing-metabase-dashboards` skill to build a dashboard on `data.starlingstrategy.com` that reads from `silver.district_documents`. Key cards:

1. **Total documents** — scalar, count of all district_sources docs
2. **Extraction success rate** — scalar, % with status='success'
3. **Readability breakdown** — bar chart, GOOD/FAIR/POOR counts
4. **AI confidence distribution** — histogram or bar
5. **Academic year alignment** — bar chart, exact_match/partial_overlap/disagreement
6. **Documents by district** — table, top districts by doc count
7. **Quality flag distribution** — bar chart, which flags appear most
8. **Recent processing** — table, last 20 processed docs with titles and status

Dashboard name: `NCTQai - Document Pipeline Health`
Collection: NCTQai (ID 5)
Database: NCTQai Postgres (ID 2)
