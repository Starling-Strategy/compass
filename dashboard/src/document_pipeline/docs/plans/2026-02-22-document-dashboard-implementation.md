# Document Dashboard Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add `/documents` and `/documents/{src_id}` pages to the nctq3 dashboard for validating document pipeline output.

**Architecture:** Two new routes in the existing nctq3 dashboard (FastHTML + MonsterUI + HTMX). Service layer for SQL queries against `silver.district_documents`. Badge components for document-specific visualizations. All code follows established nctq3 patterns: routes → services → Pydantic models → SQL.

**Tech Stack:** FastHTML, MonsterUI, HTMX, Pydantic, psycopg2 (via `library.db`)

**Target directory:** `piper/src/nctq3/dashboard/` (the existing nctq3 dashboard app)

**Key reference files (read before implementing):**
- Route pattern: `piper/src/nctq3/dashboard/routes/districts.py`
- Service pattern: `piper/src/nctq3/dashboard/services/districts.py`
- Badge pattern: `piper/src/nctq3/dashboard/components/badges.py`
- Layout: `piper/src/nctq3/dashboard/layout.py`
- Theme constants: `piper/src/nctq3/dashboard/theme_constants.py`
- DB layer: `piper/src/library/db.py` → `run_sql_pandas_params(sql, params)`
- Route registration: `piper/src/nctq3/dashboard/routes/__init__.py`

---

### Task 1: Add document badge components

**Files:**
- Create: `src/nctq3/dashboard/components/document_badges.py`

**Step 1: Write document_badges.py**

```python
"""Badge components for the document pipeline dashboard."""

from fasthtml.common import Span

from ..theme_constants import Colors


def ReadabilityBadge(readability):
    """Color-coded readability badge: good(green), fair(amber), poor(red)."""
    if not readability:
        return Span("—", style=f"color: {Colors.MUTED};")
    config = {
        "good": (Colors.INVERSE, Colors.APPROVED, "Good"),
        "fair": (Colors.PRIMARY, Colors.CONFIDENCE_MEDIUM, "Fair"),
        "poor": (Colors.INVERSE, Colors.INCORRECT, "Poor"),
    }
    color, bg, label = config.get(
        readability.lower(), (Colors.MUTED, Colors.UNREVIEWED_LIGHT, readability)
    )
    return Span(
        label,
        cls="uk-label",
        style=f"color: {color}; background-color: {bg};",
    )


def ConfidenceScoreBadge(score):
    """Color-coded confidence score: green ≥0.8, amber ≥0.5, red <0.5."""
    if score is None:
        return Span("—", style=f"color: {Colors.MUTED};")
    label = f"{score:.2f}"
    if score >= 0.8:
        color, bg = Colors.INVERSE, Colors.APPROVED
    elif score >= 0.5:
        color, bg = Colors.PRIMARY, Colors.CONFIDENCE_MEDIUM
    else:
        color, bg = Colors.INVERSE, Colors.INCORRECT
    return Span(
        label,
        cls="uk-label",
        style=f"color: {color}; background-color: {bg};",
    )


def AYAlignmentBadge(human_ay_ids, ai_ay_ids):
    """Badge showing human vs AI academic year agreement."""
    if not ai_ay_ids or not human_ay_ids:
        return Span("—", style=f"color: {Colors.MUTED};")
    human_set = set(human_ay_ids) if human_ay_ids else set()
    ai_set = set(ai_ay_ids) if ai_ay_ids else set()
    if human_set == ai_set:
        return Span("Exact Match", cls="uk-label", style=f"color: {Colors.INVERSE}; background-color: {Colors.APPROVED};")
    if human_set & ai_set:
        return Span("Partial", cls="uk-label", style=f"color: {Colors.PRIMARY}; background-color: {Colors.CONFIDENCE_MEDIUM};")
    return Span("Disagree", cls="uk-label", style=f"color: {Colors.INVERSE}; background-color: {Colors.INCORRECT};")


def DocumentTypeBadge(doc_type):
    """Neutral badge showing document classification."""
    if not doc_type:
        return Span("—", style=f"color: {Colors.MUTED};")
    label = doc_type.replace("_", " ").title()
    return Span(
        label,
        cls="uk-label",
        style=f"color: {Colors.SECONDARY}; background-color: {Colors.HEADER}; border: 1px solid {Colors.BORDER};",
    )


def ExtractionStatusBadge(status):
    """Status badge: success(green), failed(red), pending(gray)."""
    config = {
        "success": (Colors.INVERSE, Colors.APPROVED, "Success"),
        "failed": (Colors.INVERSE, Colors.INCORRECT, "Failed"),
        "pending": (Colors.PRIMARY, Colors.UNREVIEWED_LIGHT, "Pending"),
    }
    color, bg, label = config.get(
        (status or "").lower(), (Colors.MUTED, Colors.UNREVIEWED_LIGHT, status or "Unknown")
    )
    return Span(
        label,
        cls="uk-label",
        style=f"color: {color}; background-color: {bg};",
    )


def QualityFlagBadge(flag):
    """Small warning badge for a quality flag."""
    return Span(
        flag.replace("_", " "),
        style=f"display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.7rem; "
        f"background: {Colors.INCORRECT_LIGHT}; color: {Colors.INCORRECT}; margin-right: 4px; margin-bottom: 4px;",
    )
```

**Step 2: Verify import works**

```bash
cd piper && PYTHONPATH=src python -c "from nctq3.dashboard.components.document_badges import ReadabilityBadge; print('OK')"
```

**Step 3: Commit**

```bash
git add src/nctq3/dashboard/components/document_badges.py
git commit -m "feat(dashboard): add document pipeline badge components"
```

---

### Task 2: Add Pydantic models for document dashboard data

**Files:**
- Create: `src/nctq3/models/documents.py` (or extend existing if one exists — check first)

**Step 1: Write the models**

```python
"""Pydantic models for document dashboard data."""

from typing import Optional
from pydantic import BaseModel


class DocumentStats(BaseModel):
    """Aggregate stats for the health cards."""
    total: int = 0
    success: int = 0
    failed: int = 0
    pending: int = 0
    avg_confidence: Optional[float] = None
    readability_good: int = 0
    readability_fair: int = 0
    readability_poor: int = 0
    ay_exact_match: int = 0
    ay_total_enriched: int = 0

    @property
    def success_pct(self) -> int:
        if self.total == 0:
            return 0
        return round(100 * self.success / self.total)

    @property
    def ay_alignment_pct(self) -> int:
        if self.ay_total_enriched == 0:
            return 0
        return round(100 * self.ay_exact_match / self.ay_total_enriched)


class DocumentSummary(BaseModel):
    """One row in the document table."""
    src_id: int
    district_id: int
    district_name: str
    ai_title: Optional[str] = None
    src_name: str = ""
    ai_document_type: Optional[str] = None
    human_ay_ids: list[int] = []
    ai_ay_ids: Optional[list[int]] = None
    ai_readability: Optional[str] = None
    ai_confidence: Optional[float] = None
    text_length: int = 0
    extraction_status: Optional[str] = None

    @property
    def display_title(self) -> str:
        return self.ai_title or self.src_name or f"src_{self.src_id}"

    @property
    def ay_display(self) -> str:
        if not self.human_ay_ids:
            return "—"
        return ", ".join(str(a) for a in sorted(self.human_ay_ids))


class DocumentDetail(BaseModel):
    """Full document for the detail page."""
    src_id: int
    district_id: int
    district_name: str
    src_name: str = ""
    src_link: str = ""
    src_type: Optional[str] = None
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    extraction_status: Optional[str] = None
    extraction_error: Optional[str] = None
    full_text: Optional[str] = None
    text_length: int = 0
    text_hash: Optional[str] = None
    human_ay_ids: list[int] = []
    ai_ay_ids: Optional[list[int]] = None
    effective_ay_ids: list[int] = []
    ai_title: Optional[str] = None
    ai_summary: Optional[str] = None
    ai_document_type: Optional[str] = None
    ai_temporal_class: Optional[str] = None
    ai_readability: Optional[str] = None
    ai_confidence: Optional[float] = None

    @property
    def display_title(self) -> str:
        return self.ai_title or self.src_name or f"src_{self.src_id}"

    @property
    def quality_flags(self) -> list[str]:
        """Compute quality flags (same logic as document_pipeline model)."""
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

    @property
    def table_count(self) -> int:
        if not self.full_text:
            return 0
        return self.full_text.count("\n|") // 2
```

**Step 2: Verify**

```bash
PYTHONPATH=src python -c "from nctq3.models.documents import DocumentStats, DocumentSummary, DocumentDetail; print('OK')"
```

**Step 3: Commit**

```bash
git add src/nctq3/models/documents.py
git commit -m "feat(dashboard): add Pydantic models for document dashboard"
```

---

### Task 3: Add document service layer

**Files:**
- Create: `src/nctq3/dashboard/services/documents.py`

**Step 1: Write the service**

All SQL lives here. Uses `run_sql_pandas_params` from `nctq3.pipeline.persistence.db`.

```python
"""Document data services for the dashboard.

All SQL for /documents and /documents/{src_id} pages lives here.
"""

from typing import List, Optional

from nctq3.models.documents import DocumentDetail, DocumentStats, DocumentSummary
from nctq3.pipeline.persistence.db import run_sql_pandas_params


def get_document_stats(district_id: Optional[int] = None) -> DocumentStats:
    """Aggregate stats for the health cards row."""
    where = "WHERE 1=1"
    params = []
    if district_id:
        where += " AND dd.district_id = %s"
        params.append(district_id)

    sql = f"""
        SELECT
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE dd.extraction_status = 'success') as success,
            COUNT(*) FILTER (WHERE dd.extraction_status = 'failed') as failed,
            COUNT(*) FILTER (WHERE dd.extraction_status = 'pending' OR dd.extraction_status IS NULL) as pending,
            AVG(dd.ai_confidence) FILTER (WHERE dd.ai_confidence IS NOT NULL) as avg_confidence,
            COUNT(*) FILTER (WHERE dd.ai_readability = 'good') as readability_good,
            COUNT(*) FILTER (WHERE dd.ai_readability = 'fair') as readability_fair,
            COUNT(*) FILTER (WHERE dd.ai_readability = 'poor') as readability_poor,
            COUNT(*) FILTER (WHERE dd.human_ay_ids IS NOT NULL AND dd.ai_ay_ids IS NOT NULL
                             AND dd.human_ay_ids = dd.ai_ay_ids) as ay_exact_match,
            COUNT(*) FILTER (WHERE dd.ai_ay_ids IS NOT NULL AND dd.ai_ay_ids != '{{}}') as ay_total_enriched
        FROM silver.district_documents dd
        {where}
    """
    df = run_sql_pandas_params(sql, tuple(params))
    if df.empty:
        return DocumentStats()

    row = df.iloc[0]
    return DocumentStats(
        total=int(row["total"]),
        success=int(row["success"]),
        failed=int(row["failed"]),
        pending=int(row["pending"]),
        avg_confidence=float(row["avg_confidence"]) if row["avg_confidence"] is not None else None,
        readability_good=int(row["readability_good"]),
        readability_fair=int(row["readability_fair"]),
        readability_poor=int(row["readability_poor"]),
        ay_exact_match=int(row["ay_exact_match"]),
        ay_total_enriched=int(row["ay_total_enriched"]),
    )


def get_documents(
    district_id: Optional[int] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    sort: str = "src_id_desc",
    limit: int = 100,
    offset: int = 0,
) -> List[DocumentSummary]:
    """Get document list for the table."""
    where = "WHERE 1=1"
    params = []

    if district_id:
        where += " AND dd.district_id = %s"
        params.append(district_id)
    if status and status != "all":
        where += " AND dd.extraction_status = %s"
        params.append(status)
    if search:
        where += " AND (dd.ai_title ILIKE %s OR dd.doc_name ILIKE %s)"
        params.extend([f"%{search}%", f"%{search}%"])

    # Sort mapping
    sort_map = {
        "src_id_desc": "dd.src_id DESC",
        "src_id_asc": "dd.src_id ASC",
        "district": "d.district_name ASC, dd.src_id DESC",
        "confidence_desc": "dd.ai_confidence DESC NULLS LAST",
        "confidence_asc": "dd.ai_confidence ASC NULLS LAST",
        "text_length_desc": "dd.text_length DESC",
        "title": "dd.ai_title ASC NULLS LAST",
    }
    order = sort_map.get(sort, "dd.src_id DESC")

    sql = f"""
        SELECT
            dd.src_id,
            dd.district_id,
            d.district_name,
            dd.ai_title,
            dd.doc_name as src_name,
            dd.ai_document_type,
            dd.human_ay_ids,
            dd.ai_ay_ids,
            dd.ai_readability,
            dd.ai_confidence,
            dd.text_length,
            dd.extraction_status
        FROM silver.district_documents dd
        JOIN bronze.district d ON dd.district_id = d.district_id
        {where}
        ORDER BY {order}
        LIMIT %s OFFSET %s
    """
    params.extend([limit, offset])

    df = run_sql_pandas_params(sql, tuple(params))

    results = []
    for _, row in df.iterrows():
        results.append(
            DocumentSummary(
                src_id=row["src_id"],
                district_id=row["district_id"],
                district_name=row["district_name"],
                ai_title=row["ai_title"],
                src_name=row["src_name"] or "",
                ai_document_type=row["ai_document_type"],
                human_ay_ids=list(row["human_ay_ids"]) if row["human_ay_ids"] else [],
                ai_ay_ids=list(row["ai_ay_ids"]) if row["ai_ay_ids"] else None,
                ai_readability=row["ai_readability"],
                ai_confidence=float(row["ai_confidence"]) if row["ai_confidence"] is not None else None,
                text_length=int(row["text_length"] or 0),
                extraction_status=row["extraction_status"],
            )
        )
    return results


def get_document_detail(src_id: int) -> Optional[DocumentDetail]:
    """Get full document detail for the detail page."""
    sql = """
        SELECT
            dd.src_id,
            dd.district_id,
            d.district_name,
            dd.doc_name as src_name,
            dd.src_link,
            dd.src_type,
            dd.valid_from::text,
            dd.valid_to::text,
            dd.extraction_status,
            dd.extraction_error,
            dd.full_text,
            dd.text_length,
            dd.text_hash,
            dd.human_ay_ids,
            dd.ai_ay_ids,
            dd.effective_ay_ids,
            dd.ai_title,
            dd.ai_summary,
            dd.ai_document_type,
            dd.ai_temporal_class,
            dd.ai_readability,
            dd.ai_confidence
        FROM silver.district_documents dd
        JOIN bronze.district d ON dd.district_id = d.district_id
        WHERE dd.src_id = %s
    """
    df = run_sql_pandas_params(sql, (src_id,))
    if df.empty:
        return None

    row = df.iloc[0]
    return DocumentDetail(
        src_id=row["src_id"],
        district_id=row["district_id"],
        district_name=row["district_name"],
        src_name=row["src_name"] or "",
        src_link=row["src_link"] or "",
        src_type=row["src_type"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        extraction_status=row["extraction_status"],
        extraction_error=row["extraction_error"],
        full_text=row["full_text"],
        text_length=int(row["text_length"] or 0),
        text_hash=row["text_hash"],
        human_ay_ids=list(row["human_ay_ids"]) if row["human_ay_ids"] else [],
        ai_ay_ids=list(row["ai_ay_ids"]) if row["ai_ay_ids"] else None,
        effective_ay_ids=list(row["effective_ay_ids"]) if row["effective_ay_ids"] else [],
        ai_title=row["ai_title"],
        ai_summary=row["ai_summary"],
        ai_document_type=row["ai_document_type"],
        ai_temporal_class=row["ai_temporal_class"],
        ai_readability=row["ai_readability"],
        ai_confidence=float(row["ai_confidence"]) if row["ai_confidence"] is not None else None,
    )


def get_district_options() -> list[tuple[int, str]]:
    """Get (district_id, name) pairs for the filter dropdown."""
    sql = """
        SELECT DISTINCT dd.district_id, d.district_name
        FROM silver.district_documents dd
        JOIN bronze.district d ON dd.district_id = d.district_id
        ORDER BY d.district_name
    """
    df = run_sql_pandas_params(sql, ())
    return [(int(row["district_id"]), row["district_name"]) for _, row in df.iterrows()]
```

**Step 2: Test the service loads**

```bash
PYTHONPATH=src python -c "
from nctq3.dashboard.services.documents import get_document_stats, get_documents
stats = get_document_stats(district_id=37)
print(f'Total: {stats.total}, Success: {stats.success_pct}%')
docs = get_documents(district_id=37, limit=3)
for d in docs:
    print(f'  {d.src_id}: {d.display_title[:50]}')
"
```

**Step 3: Commit**

```bash
git add src/nctq3/dashboard/services/documents.py
git commit -m "feat(dashboard): add document service layer with SQL queries"
```

---

### Task 4: Add /documents route (index page)

**Files:**
- Create: `src/nctq3/dashboard/routes/documents.py`

**Step 1: Write the documents route**

Follow the pattern from `routes/districts.py`: summary cards → filter bar → table.

```python
"""Documents list page — /documents

Pipeline health cards + browsable document table.
"""

from fasthtml.common import (
    A,
    Div,
    Input,
    Option,
    P,
    Select,
    Span,
    Table,
    Tbody,
    Td,
    Th,
    Thead,
    Tr,
)
from starlette.requests import Request

from ..components.document_badges import (
    AYAlignmentBadge,
    ConfidenceScoreBadge,
    DocumentTypeBadge,
    ExtractionStatusBadge,
    ReadabilityBadge,
)
from ..layout import Layout
from ..services.documents import get_district_options, get_document_stats, get_documents
from ..theme_constants import Colors


def register_routes(rt):

    @rt("/documents")
    def get_documents_page(
        request: Request,
        auth,
        district: int = 0,
        status: str = "all",
        search: str = "",
        sort: str = "src_id_desc",
    ):
        """Documents list with health cards and filterable table."""
        user = auth
        district_id = district if district > 0 else None

        stats = get_document_stats(district_id=district_id)
        docs = get_documents(
            district_id=district_id,
            status=status if status != "all" else None,
            search=search or None,
            sort=sort,
        )
        district_options = get_district_options()

        # Health cards
        summary = Div(
            _summary_card("Total Documents", str(stats.total), Colors.PRIMARY, Colors.HEADER),
            _summary_card("Extraction Success", f"{stats.success_pct}%", Colors.APPROVED, Colors.APPROVED_LIGHT),
            _summary_card(
                "Avg Confidence",
                f"{stats.avg_confidence:.2f}" if stats.avg_confidence else "—",
                Colors.INFO,
                Colors.HEADER,
            ),
            _readability_card(stats),
            _summary_card("AY Alignment", f"{stats.ay_alignment_pct}%", Colors.APPROVED, Colors.APPROVED_LIGHT),
            style="display: flex; gap: 16px; margin-bottom: 24px; flex-wrap: wrap;",
        )

        # Filter bar
        base_url = "/documents"
        district_select = Select(
            Option("All Districts", value="0", selected=district_id is None),
            *[Option(name, value=str(did), selected=did == district_id) for did, name in district_options],
            name="district",
            hx_get=base_url,
            hx_target="#documents-content",
            hx_include="[name='status'],[name='search'],[name='sort']",
            hx_push_url="true",
            cls="uk-select uk-form-small",
            style="width: 200px;",
        )

        status_tabs = Div(
            *[
                A(
                    label,
                    href=f"{base_url}?district={district or 0}&status={val}&search={search}&sort={sort}",
                    cls=f"uk-button uk-button-small {'uk-button-primary' if status == val else 'uk-button-default'}",
                )
                for label, val in [("All", "all"), ("Success", "success"), ("Failed", "failed"), ("Pending", "pending")]
            ],
            cls="uk-button-group",
        )

        search_input = Input(
            name="search",
            type="text",
            placeholder="Search by title or filename...",
            value=search,
            hx_get=base_url,
            hx_trigger="keyup changed delay:300ms",
            hx_target="#documents-content",
            hx_include="[name='district'],[name='status'],[name='sort']",
            hx_push_url="true",
            cls="uk-input uk-form-small",
            style="width: 300px;",
        )

        filter_bar = Div(
            district_select,
            status_tabs,
            search_input,
            style="display: flex; gap: 16px; align-items: center; margin-bottom: 16px; flex-wrap: wrap;",
        )

        # Hidden sort input for HTMX includes
        sort_input = Input(name="sort", type="hidden", value=sort)

        # Table
        table = _build_table(docs, district_id or 0, status, search, sort)

        content = Div(
            summary,
            filter_bar,
            sort_input,
            Div(table, id="documents-content"),
        )

        return Layout("Documents", f"{stats.total} documents indexed", content, user, "/documents")


def _summary_card(label, value, color, bg):
    """Small summary card."""
    return Div(
        Div(label, style=f"font-size: 0.75rem; text-transform: uppercase; color: {Colors.MUTED}; margin-bottom: 4px;"),
        Div(value, style=f"font-size: 1.5rem; font-weight: 700; color: {color};"),
        style=f"flex: 1; min-width: 120px; padding: 16px; background: {bg}; border-radius: 12px; border: 1px solid {Colors.BORDER}; text-align: center;",
    )


def _readability_card(stats):
    """Readability card with mini stacked bar."""
    total = stats.readability_good + stats.readability_fair + stats.readability_poor
    if total == 0:
        return _summary_card("Readability", "—", Colors.MUTED, Colors.HEADER)

    good_pct = 100 * stats.readability_good / total
    fair_pct = 100 * stats.readability_fair / total

    bar = Div(
        Div(style=f"width: {good_pct}%; height: 100%; background: {Colors.APPROVED};"),
        Div(style=f"width: {fair_pct}%; height: 100%; background: {Colors.CONFIDENCE_MEDIUM};"),
        Div(style=f"width: {100 - good_pct - fair_pct}%; height: 100%; background: {Colors.INCORRECT};"),
        style="display: flex; height: 8px; border-radius: 4px; overflow: hidden; margin-top: 8px;",
    )
    counts = Span(
        f"{stats.readability_good}G / {stats.readability_fair}F / {stats.readability_poor}P",
        style=f"font-size: 0.7rem; color: {Colors.MUTED};",
    )

    return Div(
        Div("Readability", style=f"font-size: 0.75rem; text-transform: uppercase; color: {Colors.MUTED}; margin-bottom: 4px;"),
        counts,
        bar,
        style=f"flex: 1; min-width: 160px; padding: 16px; background: {Colors.HEADER}; border-radius: 12px; border: 1px solid {Colors.BORDER}; text-align: center;",
    )


def _build_table(docs, district, status, search, sort):
    """Build the document table."""
    if not docs:
        return P("No documents found.", style=f"color: {Colors.MUTED}; padding: 40px; text-align: center;")

    rows = []
    for d in docs:
        rows.append(
            Tr(
                Td(d.district_name, style="white-space: nowrap;"),
                Td(
                    A(
                        d.display_title[:60] + ("..." if len(d.display_title) > 60 else ""),
                        href=f"/documents/{d.src_id}",
                        style=f"font-weight: 600; color: {Colors.PRIMARY}; text-decoration: none;",
                    ),
                ),
                Td(DocumentTypeBadge(d.ai_document_type), cls="uk-text-center"),
                Td(d.ay_display, cls="uk-text-center", style="white-space: nowrap;"),
                Td(AYAlignmentBadge(d.human_ay_ids, d.ai_ay_ids), cls="uk-text-center"),
                Td(ReadabilityBadge(d.ai_readability), cls="uk-text-center"),
                Td(ConfidenceScoreBadge(d.ai_confidence), cls="uk-text-center"),
                Td(f"{d.text_length:,}", cls="uk-text-right", style=f"color: {Colors.SECONDARY};"),
                style="cursor: pointer;",
                onclick=f"window.location='/documents/{d.src_id}'",
            )
        )

    return Table(
        Thead(
            Tr(
                Th("District"),
                Th("Title"),
                Th("Type", cls="uk-text-center"),
                Th("AY", cls="uk-text-center"),
                Th("Align", cls="uk-text-center"),
                Th("Readability", cls="uk-text-center"),
                Th("Confidence", cls="uk-text-center"),
                Th("Length", cls="uk-text-right"),
            )
        ),
        Tbody(*rows),
        cls="uk-table uk-table-hover uk-table-divider uk-table-small uk-table-middle",
    )
```

**Step 2: Verify page loads**

```bash
PYTHONPATH=src uvicorn nctq3.dashboard.main:app --port 5002 --reload --reload-dir src/nctq3
# Open http://localhost:5002/documents
```

**Step 3: Commit**

```bash
git add src/nctq3/dashboard/routes/documents.py
git commit -m "feat(dashboard): add /documents index page with health cards and table"
```

---

### Task 5: Add /documents/{src_id} route (detail page)

**Files:**
- Create: `src/nctq3/dashboard/routes/document_detail.py`

**Step 1: Write the detail route**

Uses the `.review-card` CSS pattern from the existing question review page.

```python
"""Document detail page — /documents/{src_id}

Full inspection of a single document's extraction and AI enrichment.
"""

from fasthtml.common import A, Button, Div, H3, P, Pre, Script, Span
from starlette.requests import Request

from ..components.document_badges import (
    AYAlignmentBadge,
    ConfidenceScoreBadge,
    DocumentTypeBadge,
    ExtractionStatusBadge,
    QualityFlagBadge,
    ReadabilityBadge,
)
from ..layout import Layout
from ..services.documents import get_document_detail
from ..theme_constants import Colors


def register_routes(rt):

    @rt("/documents/{src_id}")
    def get_document_detail_page(request: Request, auth, src_id: int):
        """Document detail with identity, enrichment, quality, and text."""
        user = auth

        doc = get_document_detail(src_id)
        if not doc:
            return Layout("Not Found", "", P("Document not found."), user, "/documents")

        breadcrumb = [
            ("Documents", "/documents"),
            (doc.display_title[:50], None),
        ]

        # Card 1: Identity
        identity_card = _card(
            "Identity",
            _field_row("District", f"{doc.district_name} (ID: {doc.district_id})"),
            _field_row("Filename", doc.src_name),
            _field_row("Source Type", DocumentTypeBadge(doc.src_type)),
            _field_row(
                "Source URL",
                A("Open original", href=doc.src_link, target="_blank", style=f"color: {Colors.INFO};")
                if doc.src_link
                else Span("—", style=f"color: {Colors.MUTED};"),
            ),
            _field_row("Valid From", doc.valid_from or "—"),
            _field_row("Valid To", doc.valid_to or "—"),
            _field_row("Status", ExtractionStatusBadge(doc.extraction_status)),
        )

        # Card 2: AI Enrichment
        ay_comparison = Div(
            Div(
                Span("Human: ", style=f"color: {Colors.MUTED}; font-size: 0.85rem;"),
                Span(
                    _format_ay_ids(doc.human_ay_ids) if doc.human_ay_ids else "—",
                    style=f"font-weight: 600;",
                ),
                style="margin-right: 24px;",
            ),
            Div(
                Span("AI: ", style=f"color: {Colors.MUTED}; font-size: 0.85rem;"),
                Span(
                    _format_ay_ids(doc.ai_ay_ids) if doc.ai_ay_ids else "—",
                    style=f"font-weight: 600;",
                ),
                style="margin-right: 24px;",
            ),
            AYAlignmentBadge(doc.human_ay_ids, doc.ai_ay_ids),
            style="display: flex; align-items: center; flex-wrap: wrap; gap: 8px;",
        )

        enrichment_card = _card(
            "AI Enrichment",
            Div(
                doc.ai_title or "No title",
                style=f"font-size: 1.25rem; font-weight: 700; color: {Colors.PRIMARY}; margin-bottom: 12px;",
            ),
            P(doc.ai_summary or "No summary available.", style=f"color: {Colors.SECONDARY}; margin-bottom: 16px;"),
            _field_row("Document Type", DocumentTypeBadge(doc.ai_document_type)),
            _field_row("Temporal Class", DocumentTypeBadge(doc.ai_temporal_class)),
            _field_row("Academic Years", ay_comparison),
            _field_row("Confidence", ConfidenceScoreBadge(doc.ai_confidence)),
            _field_row("Readability", ReadabilityBadge(doc.ai_readability)),
        )

        # Card 3: Quality Signals
        flags = doc.quality_flags
        flags_display = (
            Div(*[QualityFlagBadge(f) for f in flags], style="display: flex; flex-wrap: wrap;")
            if flags
            else Span("No issues detected", style=f"color: {Colors.APPROVED}; font-weight: 600;")
        )

        quality_card = _card(
            "Quality Signals",
            _field_row("Quality Flags", flags_display),
            _field_row("Text Length", f"{doc.text_length:,} chars"),
            _field_row("Table Count", str(doc.table_count)),
            _field_row("Text Hash", Span(doc.text_hash[:16] + "..." if doc.text_hash else "—", style=f"font-family: monospace; font-size: 0.8rem; color: {Colors.MUTED};")),
            _field_row("Extraction Error", Span(doc.extraction_error, style=f"color: {Colors.INCORRECT};") if doc.extraction_error else "—"),
        )

        # Card 4: Extracted Text (collapsible)
        text_card = _card(
            "Extracted Text",
            Button(
                "Show/Hide Text",
                onclick="document.getElementById('doc-text').classList.toggle('uk-hidden')",
                cls="uk-button uk-button-small uk-button-default",
                style="margin-bottom: 12px;",
            ),
            Div(
                Pre(
                    doc.full_text[:50000] if doc.full_text else "No text extracted.",
                    style=f"white-space: pre-wrap; word-wrap: break-word; font-size: 0.8rem; "
                    f"max-height: 60vh; overflow-y: auto; padding: 16px; "
                    f"background: {Colors.HEADER}; border-radius: 8px; border: 1px solid {Colors.BORDER};",
                ),
                id="doc-text",
                cls="uk-hidden" if doc.text_length > 1000 else "",
            ),
        )

        content = Div(identity_card, enrichment_card, quality_card, text_card)

        return Layout(doc.display_title[:60], f"src_id={doc.src_id}", content, user, "/documents", breadcrumb=breadcrumb)


def _card(header, *children):
    """Review-style card with header."""
    return Div(
        Div(header, cls="review-card-header"),
        *children,
        cls="review-card",
    )


def _field_row(label, value):
    """Label: value row inside a card."""
    return Div(
        Span(label, style=f"display: inline-block; width: 140px; font-size: 0.85rem; color: {Colors.MUTED}; font-weight: 500;"),
        Span(value) if isinstance(value, str) else value,
        style="margin-bottom: 8px; display: flex; align-items: center;",
    )


def _format_ay_ids(ay_ids):
    """Format [25, 26] as '24-25, 25-26'."""
    if not ay_ids:
        return "—"
    return ", ".join(f"20{a-1}-{a}" for a in sorted(ay_ids))
```

**Step 2: Verify page loads**

```bash
# With the server running on port 5002:
# Open http://localhost:5002/documents/12478  (a Broward doc from our test run)
```

**Step 3: Commit**

```bash
git add src/nctq3/dashboard/routes/document_detail.py
git commit -m "feat(dashboard): add /documents/{src_id} detail page"
```

---

### Task 6: Register routes and verify end-to-end

**Files:**
- Modify: `src/nctq3/dashboard/routes/__init__.py`

**Step 1: Add document route registration**

Add two imports and two `register_routes` calls:

```python
def register_nctq3_routes(rt):
    """Register all NCTQ 3.0 routes."""
    from . import api, district_detail, district_policy, districts, question_review
    from . import documents, document_detail

    districts.register_routes(rt)
    district_detail.register_routes(rt)
    district_policy.register_routes(rt)
    question_review.register_routes(rt)
    api.register_routes(rt)
    documents.register_routes(rt)
    document_detail.register_routes(rt)
```

**Step 2: Start the dashboard and verify both pages**

```bash
PYTHONPATH=src uvicorn nctq3.dashboard.main:app --port 5002 --reload --reload-dir src/nctq3
```

Verify:
1. http://localhost:5002/documents — health cards + table with Broward docs
2. Click a document row → detail page with all 4 cards
3. "Documents" nav link is highlighted
4. Breadcrumbs work on detail page
5. Filter by district dropdown works
6. Status filter tabs work

**Step 3: Commit**

```bash
git add src/nctq3/dashboard/routes/__init__.py
git commit -m "feat(dashboard): register document routes in dashboard"
```
