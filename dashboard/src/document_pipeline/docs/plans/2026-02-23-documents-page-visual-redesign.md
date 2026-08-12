# Documents Page Visual Redesign — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the /documents page easy on the eyes with permanent dark mode and soft muted colors.

**Architecture:** Force MonsterUI dark mode, replace inline-styled components with MonsterUI Card/Table components, swap neon badges for soft-tinted pills using CSS utility classes.

**Tech Stack:** FastHTML, MonsterUI (Theme.violet, Card components), CSS custom properties

---

### Task 1: Force permanent dark mode

**Files:**
- Modify: `src/document_pipeline/dashboard/main.py:56-62`

**Step 1: Pass mode="dark" to theme.headers()**

In `main.py`, change:
```python
app, rt = fast_app(
    hdrs=theme.headers(),
)
```
to:
```python
app, rt = fast_app(
    hdrs=theme.headers(mode="dark"),
)
```

**Step 2: Verify dark mode is forced**

Run: `PYTHONPATH=src uvicorn document_pipeline.dashboard.main:app --port 5003 --reload --reload-dir src/document_pipeline`

Open http://localhost:5003/documents — should see dark background regardless of OS preference.

**Step 3: Commit**

```bash
git add src/document_pipeline/dashboard/main.py
git commit -m "style(docpipe): force permanent dark mode"
```

---

### Task 2: Replace theme CSS with dark-mode badge utilities

**Files:**
- Modify: `src/document_pipeline/dashboard/theme.py`
- Modify: `src/document_pipeline/dashboard/theme_constants.py`

**Step 1: Rewrite theme.py**

Replace the entire `THEME_CSS` content. Remove all `:root` light-mode variables. Add only badge utility classes and minor layout helpers that work on dark backgrounds:

```python
"""CSS custom properties for the Document Pipeline dashboard."""

from fasthtml.common import Style

THEME_CSS = Style("""
/* Badge utility classes — soft tinted pills for dark mode */
.badge {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 9999px;
    font-size: 0.75rem;
    font-weight: 600;
    line-height: 1.5;
    white-space: nowrap;
}
.badge-success {
    background: rgba(34, 197, 94, 0.15);
    color: #4ade80;
}
.badge-warning {
    background: rgba(250, 204, 21, 0.15);
    color: #fbbf24;
}
.badge-danger {
    background: rgba(248, 113, 113, 0.15);
    color: #f87171;
}
.badge-neutral {
    background: rgba(148, 163, 184, 0.15);
    color: #94a3b8;
}

/* Stat card layout */
.stat-cards {
    display: flex;
    gap: 16px;
    margin-bottom: 24px;
    flex-wrap: wrap;
}
.stat-card-value {
    font-size: 1.5rem;
    font-weight: 700;
}
.stat-card-label {
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    opacity: 0.6;
    margin-bottom: 4px;
}

/* Readability bar */
.readability-bar {
    display: flex;
    height: 8px;
    border-radius: 4px;
    overflow: hidden;
    margin-top: 8px;
}
.readability-bar > div:first-child { background: #4ade80; }
.readability-bar > div:nth-child(2) { background: #fbbf24; }
.readability-bar > div:last-child { background: #f87171; }

/* Filter bar */
.filter-bar {
    display: flex;
    gap: 16px;
    align-items: center;
    margin-bottom: 16px;
    flex-wrap: wrap;
}
""")
```

**Step 2: Simplify theme_constants.py**

Replace with badge class names only (no more inline color references):

```python
"""Badge CSS class names — replaces inline color constants."""


class BadgeClass:
    SUCCESS = "badge badge-success"
    WARNING = "badge badge-warning"
    DANGER = "badge badge-danger"
    NEUTRAL = "badge badge-neutral"
```

**Step 3: Commit**

```bash
git add src/document_pipeline/dashboard/theme.py src/document_pipeline/dashboard/theme_constants.py
git commit -m "style(docpipe): replace light-mode CSS vars with dark-mode badge utilities"
```

---

### Task 3: Rewrite badge components to use CSS classes

**Files:**
- Modify: `src/document_pipeline/dashboard/components/document_badges.py`

**Step 1: Rewrite document_badges.py**

Replace all inline `style=f"color: {color}; background-color: {bg};"` with CSS class names:

```python
"""Badge components for the document pipeline dashboard."""

from fasthtml.common import Span

from ..theme_constants import BadgeClass


def ReadabilityBadge(readability):
    """Color-coded readability badge: good(green), fair(amber), poor(red)."""
    if not readability:
        return Span("—", cls="uk-text-muted")
    config = {
        "good": (BadgeClass.SUCCESS, "Good"),
        "fair": (BadgeClass.WARNING, "Fair"),
        "poor": (BadgeClass.DANGER, "Poor"),
    }
    cls, label = config.get(readability.lower(), (BadgeClass.NEUTRAL, readability))
    return Span(label, cls=cls)


def ConfidenceScoreBadge(score):
    """Color-coded confidence score: green >=0.8, amber >=0.5, red <0.5."""
    if score is None:
        return Span("—", cls="uk-text-muted")
    label = f"{score:.2f}"
    if score >= 0.8:
        cls = BadgeClass.SUCCESS
    elif score >= 0.5:
        cls = BadgeClass.WARNING
    else:
        cls = BadgeClass.DANGER
    return Span(label, cls=cls)


def AYAlignmentBadge(human_ay_ids, ai_ay_ids):
    """Badge showing human vs AI academic year agreement."""
    if not ai_ay_ids or not human_ay_ids:
        return Span("—", cls="uk-text-muted")
    human_set = set(human_ay_ids) if human_ay_ids else set()
    ai_set = set(ai_ay_ids) if ai_ay_ids else set()
    if human_set == ai_set:
        return Span("Exact Match", cls=BadgeClass.SUCCESS)
    if human_set & ai_set:
        return Span("Partial", cls=BadgeClass.WARNING)
    return Span("Disagree", cls=BadgeClass.DANGER)


def DocumentTypeBadge(doc_type):
    """Neutral badge showing document classification."""
    if not doc_type:
        return Span("—", cls="uk-text-muted")
    label = doc_type.replace("_", " ").title()
    return Span(label, cls=BadgeClass.NEUTRAL)


def ExtractionStatusBadge(status):
    """Status badge: success(green), failed(red), pending(gray)."""
    config = {
        "success": (BadgeClass.SUCCESS, "Success"),
        "failed": (BadgeClass.DANGER, "Failed"),
        "pending": (BadgeClass.NEUTRAL, "Pending"),
    }
    cls, label = config.get(
        (status or "").lower(), (BadgeClass.NEUTRAL, status or "Unknown")
    )
    return Span(label, cls=cls)


def QualityFlagBadge(flag):
    """Small warning badge for a quality flag."""
    return Span(flag.replace("_", " "), cls=BadgeClass.DANGER)
```

**Step 2: Verify badges render**

Run: `PYTHONPATH=src uvicorn document_pipeline.dashboard.main:app --port 5003 --reload --reload-dir src/document_pipeline`

Open http://localhost:5003/documents — badges should be soft tinted pills.

**Step 3: Commit**

```bash
git add src/document_pipeline/dashboard/components/document_badges.py
git commit -m "style(docpipe): rewrite badges to use soft-tint CSS classes"
```

---

### Task 4: Rewrite stat cards and table in documents.py

**Files:**
- Modify: `src/document_pipeline/dashboard/routes/documents.py`

**Step 1: Update imports**

Replace:
```python
from ..theme_constants import Colors
```
with:
```python
from monsterui.all import Card, CardBody
```

Also add to the fasthtml imports: `H3` (or use `Span` — we'll use `Div` which is already imported).

**Step 2: Rewrite _summary_card to use MonsterUI Card**

```python
def _summary_card(label, value):
    """Stat card using MonsterUI Card — uniform dark surface."""
    return Card(
        Div(label, cls="stat-card-label"),
        Div(value, cls="stat-card-value"),
        cls="flex-1",
        style="min-width: 120px; text-align: center;",
    )
```

Note: removed `color` and `bg` parameters entirely. All cards look the same.

**Step 3: Rewrite _readability_card**

```python
def _readability_card(stats):
    """Readability card with mini stacked bar."""
    total = stats.readability_good + stats.readability_fair + stats.readability_poor
    if total == 0:
        return _summary_card("Readability", "—")

    good_pct = 100 * stats.readability_good / total
    fair_pct = 100 * stats.readability_fair / total

    bar = Div(
        Div(style=f"width: {good_pct}%; height: 100%;"),
        Div(style=f"width: {fair_pct}%; height: 100%;"),
        Div(style=f"width: {100 - good_pct - fair_pct}%; height: 100%;"),
        cls="readability-bar",
    )
    counts = Span(
        f"{stats.readability_good}G / {stats.readability_fair}F / {stats.readability_poor}P",
        cls="uk-text-small uk-text-muted",
    )

    return Card(
        Div("Readability", cls="stat-card-label"),
        counts,
        bar,
        cls="flex-1",
        style="min-width: 160px; text-align: center;",
    )
```

**Step 4: Update summary section call sites**

Replace the old summary section:
```python
summary = Div(
    _summary_card("Total Documents", str(stats.total), Colors.PRIMARY, Colors.HEADER),
    _summary_card("Extraction Success", f"{stats.success_pct}%", Colors.APPROVED, Colors.APPROVED_LIGHT),
    ...
)
```
with:
```python
summary = Div(
    _summary_card("Total Documents", str(stats.total)),
    _summary_card("Extraction Success", f"{stats.success_pct}%"),
    _summary_card(
        "Avg Confidence",
        f"{stats.avg_confidence:.2f}" if stats.avg_confidence else "—",
    ),
    _readability_card(stats),
    _summary_card("AY Alignment", f"{stats.ay_alignment_pct}%"),
    cls="stat-cards",
)
```

Note: `cls="stat-cards"` replaces the inline `style="display: flex; gap: 16px; ..."`.

**Step 5: Fix table link text and row styling**

In `_build_table`, change the title link from:
```python
style=f"font-weight: 600; color: {Colors.PRIMARY}; text-decoration: none;",
```
to:
```python
cls="uk-link-text font-semibold",
```

Change the length column from:
```python
style=f"color: {Colors.SECONDARY};",
```
to:
```python
cls="uk-text-muted",
```

Change the filter bar from inline style to:
```python
cls="filter-bar",
```

**Step 6: Remove the Colors import entirely**

The `from ..theme_constants import Colors` line should be gone — replaced by `from monsterui.all import Card`.

**Step 7: Verify the full page**

Run: `PYTHONPATH=src uvicorn document_pipeline.dashboard.main:app --port 5003 --reload --reload-dir src/document_pipeline`

Check:
- Stat cards are uniform dark with white numbers
- Badges are soft tinted pills
- Link text is visible
- Table is readable

**Step 8: Commit**

```bash
git add src/document_pipeline/dashboard/routes/documents.py
git commit -m "style(docpipe): use MonsterUI Card for stat cards, fix table contrast"
```

---

### Task 5: Clean up unused code and verify

**Files:**
- Modify: `src/document_pipeline/dashboard/theme_constants.py` (if Colors class still referenced elsewhere)
- Check: `src/document_pipeline/dashboard/routes/document_detail.py`

**Step 1: Check if Colors is used in document_detail.py**

Search for `Colors` in the detail page. If it uses the old constants, note it but don't change it (out of scope per design doc — separate pass).

**Step 2: Remove old Colors class if not imported anywhere**

If `Colors` is only used by the files we already changed, delete it from `theme_constants.py`. If `document_detail.py` still imports it, keep both `Colors` and `BadgeClass` in the file for now.

**Step 3: Final visual check**

Open http://localhost:5003/documents and verify:
- [ ] Dark background, no white flashes
- [ ] Stat cards: uniform dark surface, white text values, muted labels
- [ ] Badges: soft green/amber/red tints, not neon
- [ ] Table text: readable link titles, visible columns
- [ ] Readability bar: subtle colored segments
- [ ] Filter buttons and search input look clean on dark bg

**Step 4: Commit**

```bash
git add -A src/document_pipeline/dashboard/
git commit -m "style(docpipe): clean up unused theme constants"
```
