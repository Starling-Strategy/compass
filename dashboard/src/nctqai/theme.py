"""Theme and CSS for the NCTQ.ai dashboard.

Light mode with navy (#0F223A) accent. Uses MonsterUI + custom overrides.
Design tokens provide consistent spacing, color, and typography across all pages.
"""

from fasthtml.common import Style

CUSTOM_CSS = Style("""
/* ═══════════════════════════════════════════════════════════
   ACCENT SYSTEM (read before recoloring anything)
   ───────────────────────────────────────────────────────────
   Three accent roles, kept strictly separate so the palette
   doesn't re-drift:

     • brand-blue (--brand-blue #1D6CD0): GLOBAL CHROME ONLY.
       The navy top-nav's focus ring, and the Metric Calculator /
       Documents / Journal sections' own chrome (sub-nav underline,
       buttons, links). It is ALSO the deliberately *sober* accent of
       the Compass Quality scorecard cluster — every `quality-*` rule
       scoped under main[data-cluster="quality"] stays brand-blue ON
       PURPOSE (see the flag-panel note ~line 2137). Do NOT recolor
       Quality to teal to make this rule literally "chrome only."

     • compass-teal (--compass-teal #0E7C7B): the Compass observatory
       BODY accent. Every decorative/interactive element inside the
       Compass section that is NOT the Quality cluster is teal: the
       activity/coverage bars, the Compass sub-nav underline
       (.sub-nav--compass), convo/turn/footer links, pills, etc.

     • green / red (--status-green / --status-red and their -bg/-dark
       pairs): RESERVED FOR SEMANTIC STATE only — pass/fail, up/down,
       accepted/rejected, fresh/stale. Never used as a decorative
       brand accent. Data Universe's green is semantic (freshness),
       not decorative — leave it green.

   Rule of thumb when adding a rule: chrome or Quality cluster → blue;
   anything else inside Compass → teal; a true pass/fail/up/down signal
   → green/red.
   ═══════════════════════════════════════════════════════════ */

/* ── 1. CSS Variables (Design Tokens) ───────────────────── */
:root {
    --primary: 214 76% 46%;
    --primary-foreground: 0 0% 100%;
    --ring: 214 76% 46%;

    --brand-navy: #0F223A;
    --brand-blue: #1D6CD0;
    --brand-deep: #004a7c;
    --brand-orange: #F68E1E;

    /* Status colors pass WCAG AA on white (--bg-card). The "-dark" and "-bg"
       pairs are for badges (dark text on light tinted bg — already AA). The
       base values were updated 2026-04-21 to pass AA when used as text/icon
       colors directly (previous #22c55e and #ef4444 failed at 3.2:1). */
    --status-green: #0B8A00;       /* 5.1:1 on white */
    --status-green-dark: #166534;
    --status-green-bg: #dcfce7;
    --status-red: #C62828;         /* 5.9:1 on white */
    --status-red-dark: #991b1b;
    --status-red-bg: #fee2e2;
    --status-amber: #f59e0b;
    --status-amber-dark: #92400e;
    --status-amber-bg: #fef3c7;
    --status-blue-bg: #dbeafe;
    --status-blue-dark: #1e40af;

    --text-primary: #0f172a;
    --text-secondary: #475569;
    --text-muted: #64748b;
    --text-faint: #94a3b8;

    --border-light: #e2e8f0;
    --border-card: #e8ecf1;
    --bg-page: #f0f2f5;
    --bg-card: #ffffff;
    --bg-subtle: #f8fafc;
    --bg-muted: #f1f5f9;

    --text-xs: 0.75rem;
    --text-sm: 0.8rem;
    --text-base: 0.875rem;
    --text-lg: 1rem;
    --text-xl: 1.25rem;
    --text-2xl: 1.75rem;
    --text-3xl: 2.25rem;

    --leading-tight: 1.2;
    --leading-snug: 1.375;
    --leading-relaxed: 1.5;

    --space-xs: 4px;
    --space-sm: 8px;
    --space-md: 16px;
    --space-lg: 24px;
    --space-xl: 32px;
    --space-2xl: 40px;

    --shadow-sm: 0 1px 2px rgba(15, 34, 58, 0.05);
    --shadow-card: 0 4px 6px -1px rgba(15, 34, 58, 0.07);
    --shadow-dropdown: 0 10px 15px -3px rgba(15, 34, 58, 0.1);

    --transition-fast: 150ms cubic-bezier(0.4, 0, 0.2, 1);

    --max-prose: 640px;
}

/* ── 2. Global Base Styles ──────────────────────────────── */
/* G5: anchor body text on the primary text token so prose is legible by default
   without per-component color overrides (top-nav/auth set their own colors and
   still win via specificity). Line-height is left per-component and bumped only
   where prose is actually dense (e.g. .quality-turn-assistant), not globally. */
body {
    background: var(--bg-page);
    color: var(--text-primary);
}
:focus-visible {
    outline: none;
    box-shadow: 0 0 0 2px var(--bg-card), 0 0 0 4px var(--brand-blue);
}

/* ── 3. Top Navigation Bar (navy) ───────────────────────── */
/* !important is required here: UIkit's NavBar applies inline styles
   that cannot be overridden via CSS specificity alone. This is a
   known exception to the CLAUDE.md guideline against !important. */
/* Full-bleed navy bar, but content padded into the same centered 1200px column
   as the footer + page cards. MonsterUI's NavBar has NO inner container (brand
   and links are direct flex children of .top-nav with justify-between/w-full),
   so we center via horizontal padding rather than a max-width wrapper. */
.top-nav {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    background: var(--brand-navy) !important;
    border-bottom: none !important;
    box-shadow: var(--shadow-card);
    padding-top: 12px !important;
    padding-bottom: 12px !important;
    padding-inline: max(1.5rem, calc((100% - 1200px) / 2)) !important;
    min-height: 56px;
}
.nav-right { display: flex; align-items: center; gap: 4px; }
.top-nav .uk-navbar-container { background: transparent !important; }
.top-nav .uk-navbar-left,
.top-nav .uk-navbar-right,
.top-nav .uk-navbar-center { gap: 0; }
/* Brand: TQ monogram + NCTQ.ai wordmark */
.top-nav .nav-brand {
    display: flex;
    align-items: center;
    gap: 10px;
}
.top-nav .nav-brand-logo {
    height: 28px;
    width: auto;
    display: block;
}
.top-nav .nav-brand-logo .logo-primary { fill: #ffffff; }
.top-nav .nav-brand-logo .logo-secondary { fill: #98abce; }
.top-nav .brand-text {
    color: #fff !important;
    font-weight: 700;
    font-size: 1.2rem;
    letter-spacing: 0.02em;
    margin: 0 !important;
}
.nav-link {
    color: rgba(255,255,255,0.75) !important;
    text-decoration: none !important;
    font-weight: 500;
    padding: 16px 20px;
    transition: color var(--transition-fast), background var(--transition-fast);
    font-size: 0.9rem;
    letter-spacing: 0.01em;
}
.nav-link:hover {
    color: #fff !important;
    background: rgba(255,255,255,0.1);
}
.nav-link.active {
    color: #fff !important;
    font-weight: 700;
}
.top-nav .uk-navbar-toggle { color: #fff !important; }
.top-nav .uk-navbar-toggle-icon { color: #fff !important; }

/* ── Top-nav dropdown menus (Metric Calculator / Documents / Compass) ─── */
.top-nav .nav-dd { display: inline-flex; }
.top-nav .nav-dd-toggle {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    cursor: pointer;
}
.top-nav .nav-caret { transition: transform var(--transition-fast); opacity: 0.85; }
.top-nav .nav-dd:hover .nav-caret,
.top-nav .nav-dd .uk-open .nav-caret { transform: rotate(180deg); }

.nav-dd-menu.uk-dropdown {
    min-width: 212px;
    padding: 8px !important;
    background: #ffffff !important;
    border: 1px solid var(--border-subtle, #e5e7eb);
    border-radius: 10px;
    box-shadow: 0 14px 30px rgba(15, 34, 58, 0.18), 0 2px 6px rgba(15, 34, 58, 0.08) !important;
    margin-top: 6px !important;
}
.nav-dd-list { list-style: none; margin: 0; padding: 0; }
.nav-dd-list li { margin: 0; }
.nav-dd-list li a {
    display: block;
    padding: 9px 12px;
    border-radius: 7px;
    color: var(--brand-navy) !important;
    font-size: 0.875rem;
    font-weight: 500;
    line-height: 1.2;
    text-decoration: none !important;
    white-space: nowrap;
    transition: background var(--transition-fast), color var(--transition-fast);
}
.nav-dd-list li a:hover {
    background: var(--bg-subtle, #f1f5f9);
    color: var(--brand-blue) !important;
}

/* ── Mobile hamburger menu ──────────────────────────────────── */
.top-nav { position: relative; }
.nav-desktop-links { display: flex; align-items: center; }
.m-nav { display: none; }               /* hidden on desktop */
.m-nav-cb { display: none; }            /* the checkbox-hack toggle, never shown */
.m-nav-burger {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 40px; height: 40px;
    color: #fff;
    cursor: pointer;
    border-radius: 8px;
    transition: background var(--transition-fast);
}
.m-nav-burger:hover { background: rgba(255, 255, 255, 0.12); }
/* Swap hamburger <-> close icon based on checkbox state */
.m-nav-burger svg:last-child { display: none; }
.m-nav-cb:checked ~ .m-nav-burger svg:first-child { display: none; }
.m-nav-cb:checked ~ .m-nav-burger svg:last-child { display: inline; }

.m-nav-panel {
    display: none;
    position: absolute;
    top: 100%; left: 0; right: 0;
    background: var(--brand-navy);
    border-top: 1px solid rgba(255, 255, 255, 0.12);
    box-shadow: 0 16px 32px rgba(0, 0, 0, 0.28);
    padding: 10px;
    max-height: calc(100vh - 64px);
    overflow-y: auto;
    z-index: 1030;
}
.m-nav-cb:checked ~ .m-nav-panel { display: block; }

.m-nav-link,
.m-nav-group-title {
    display: block;
    padding: 12px 14px;
    color: rgba(255, 255, 255, 0.92) !important;
    font-size: 0.95rem;
    font-weight: 600;
    text-decoration: none !important;
    border-radius: 8px;
}
.m-nav-link:hover,
.m-nav-group-title:hover { background: rgba(255, 255, 255, 0.1); }
.m-nav-link.active,
.m-nav-group-title.active { color: #fff !important; background: rgba(255, 255, 255, 0.08); }
.m-nav-group { margin-bottom: 2px; }
.m-nav-group-title {
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: rgba(255, 255, 255, 0.55) !important;
    padding-bottom: 6px;
}
.m-nav-sublink {
    display: block;
    padding: 10px 14px 10px 26px;
    color: rgba(255, 255, 255, 0.85) !important;
    font-size: 0.9rem;
    font-weight: 500;
    text-decoration: none !important;
    border-radius: 8px;
}
.m-nav-sublink:hover { background: rgba(255, 255, 255, 0.1); color: #fff !important; }

@media (max-width: 900px) {
    .nav-desktop-links { display: none; }
    .m-nav { display: inline-flex; align-items: center; }
}

/* ── 4. Avatar Trigger + Dropdown ───────────────────────── */
.uk-inline { position: relative; }
.avatar-trigger {
    width: 36px;
    height: 36px;
    border-radius: 50%;
    background: rgba(255,255,255,0.2);
    color: #fff;
    font-size: 0.75rem;
    font-weight: 700;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    border: 2px solid rgba(255,255,255,0.4);
    transition: background var(--transition-fast);
    line-height: 1;
}
.avatar-trigger:hover { background: rgba(255,255,255,0.3); }
.user-dropdown {
    min-width: 200px;
    padding: var(--space-md);
    border-radius: 8px;
    box-shadow: var(--shadow-dropdown);
    border: 1px solid var(--border-card);
    background: var(--bg-card);
}
.user-dropdown .dropdown-name { font-weight: 600; font-size: 0.9rem; color: var(--text-primary); }
.user-dropdown .dropdown-role {
    font-size: 0.7rem; color: var(--text-muted); text-transform: uppercase;
    letter-spacing: 0.05em; margin-top: 2px;
}
.user-dropdown hr { margin: 10px 0; border: none; border-top: 1px solid var(--border-light); }
.user-dropdown .sign-out-btn {
    font-size: var(--text-sm); color: var(--text-muted); background: none;
    border: none; padding: 4px 0; cursor: pointer; transition: color var(--transition-fast);
}
.user-dropdown .sign-out-btn:hover { color: var(--status-red); }

/* ── 5. Badges ──────────────────────────────────────────── */
.badge {
    display: inline-block; padding: 2px 10px; border-radius: 9999px;
    font-size: var(--text-xs); font-weight: 600; line-height: 1.5; white-space: nowrap;
}
.badge-accepted { background: var(--status-green-bg); color: var(--status-green-dark); }
.badge-rejected { background: var(--status-red-bg); color: var(--status-red-dark); }
.badge-unreviewed { background: var(--bg-muted); color: var(--text-secondary); }
.badge-running { background: var(--status-blue-bg); color: var(--status-blue-dark); }
.badge-ina { background: var(--status-amber-bg); color: var(--status-amber-dark); }
.badge-priority { background: var(--status-amber-bg); color: var(--status-amber-dark); }
.badge-confidence-high { background: var(--status-green-bg); color: var(--status-green-dark); }
.badge-confidence-medium { background: var(--status-amber-bg); color: var(--status-amber-dark); }
.badge-confidence-low { background: var(--status-red-bg); color: var(--status-red-dark); }
.badge-doc-success { background: var(--status-green-bg); color: var(--status-green-dark); }
.badge-doc-warning { background: var(--status-amber-bg); color: var(--status-amber-dark); }
.badge-doc-danger { background: var(--status-red-bg); color: var(--status-red-dark); }
.badge-doc-neutral { background: var(--bg-muted); color: var(--text-secondary); }
.badge-warning { background: var(--status-amber-bg); color: var(--status-amber-dark); }
.badge-qid { background: #e3edf7; color: #3a6ea5; }

/* ── 6. KPI Cards ───────────────────────────────────────── */
.kpi-cards { display: flex; gap: var(--space-md); margin-bottom: var(--space-lg); flex-wrap: wrap; }
/* Opt-in fixed 4-column grid (Overview): 7 tiles wrap to 4 + 3 across two rows
   with uniform tile widths, instead of squishing into a single flex row. */
.kpi-cards--grid4 { display: grid; grid-template-columns: repeat(4, 1fr); }
@media (max-width: 760px) { .kpi-cards--grid4 { grid-template-columns: repeat(2, 1fr); } }
.kpi-card {
    background: var(--bg-card); box-shadow: var(--shadow-card);
    border: 1px solid var(--border-card); border-radius: 8px;
    /* G2: tighter vertical padding closes the dead space under a hollow
       (subtitle-less) card while keeping the comfortable side padding. */
    padding: 14px var(--space-lg); min-width: 140px; flex: 1;
}
.kpi-value {
    font-size: var(--text-2xl); font-weight: 700; color: var(--text-primary);
    line-height: var(--leading-tight); font-variant-numeric: tabular-nums;
}
.kpi-label {
    font-size: var(--text-xs); text-transform: uppercase; letter-spacing: 0.05em;
    color: var(--text-muted); margin-top: var(--space-xs);
}
.kpi-subtitle { font-size: var(--text-xs); color: var(--text-faint); margin-top: 2px; }
/* G2 / contract C-1: optional trend line rendered after the subtitle (e.g.
   "+12% vs prior period"). Muted gray — a trend caption is context, not a
   pass/fail state, so it stays neutral (semantic green/red is reserved for true
   state per the accent system header). */
.kpi-trend {
    font-size: var(--text-xs); color: var(--text-muted); margin-top: 2px;
    font-variant-numeric: tabular-nums;
}
.kpi-placeholder { opacity: 0.5; border-style: dashed; }
.kpi-card--clickable {
    display: block; text-decoration: none; color: inherit; cursor: pointer;
    transition: box-shadow 0.12s ease, transform 0.12s ease, border-color 0.12s ease;
}
.kpi-card--clickable:hover {
    box-shadow: var(--shadow-card-hover, 0 4px 12px rgba(0,0,0,0.08));
    border-color: var(--border-card-hover, var(--border-card));
    transform: translateY(-1px);
}
.kpi-card--clickable:focus-visible {
    outline: 2px solid var(--accent, #2563eb); outline-offset: 2px;
}
.coming-soon-badge {
    display: inline-block; font-size: 0.65rem; text-transform: uppercase;
    letter-spacing: 0.05em; color: var(--text-faint); background: var(--bg-muted);
    padding: 1px 6px; border-radius: 4px;
}

/* ── 6b. Overview Dashboard ────────────────────────────── */
.overview-section { margin-bottom: var(--space-xl); }
.overview-section-heading { margin-bottom: var(--space-md); }
.overview-section-title {
    font-size: var(--text-lg); font-weight: 600; color: var(--text-primary);
    margin: 0 0 2px 0;
}
.overview-section-subtitle {
    font-size: var(--text-xs); color: var(--text-muted); margin: 0;
}
.overview-subsection-title {
    font-size: var(--text-sm); font-weight: 600; color: var(--text-secondary);
    margin: 0 0 var(--space-sm) 0;
}
/* Honest replacement for the old fabricated-0% Critic tiles: a teal text
   link to the real offline-eval Scorecard. Reuses --compass-teal (Compass
   body accent); no new color token. */
.overview-quality-link {
    display: inline-block; margin-top: var(--space-sm);
    font-size: var(--text-sm); font-weight: 600;
    color: var(--compass-teal); text-decoration: none;
    transition: color var(--transition-fast);
}
.overview-quality-link:hover {
    color: var(--compass-teal-dark); text-decoration: underline;
}
.overview-grid-2 {
    display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-md);
    margin-top: var(--space-md);
}
.overview-grid-1 { margin-top: var(--space-md); }
.overview-panel {
    background: var(--bg-card); border: 1px solid var(--border-card);
    border-radius: 8px; padding: var(--space-md); box-shadow: var(--shadow-card);
}

/* Bar chart rows */
.bar-chart { display: flex; flex-direction: column; gap: 6px; }
.bar-row { display: flex; align-items: center; gap: var(--space-sm); }
.bar-label {
    flex: 0 0 140px; font-size: var(--text-xs); color: var(--text-secondary);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.bar-track {
    flex: 1; height: 18px; background: var(--bg-muted); border-radius: 4px;
    overflow: hidden;
}
/* All `bar_row`/.bar-fill consumers live inside the Compass section (Overview
   Feedback + Operations bars, the activity trend, and Data Universe coverage
   bars — see components/compass/bars.py), so the decorative bar fill is the
   Compass BODY accent (teal), per the accent system at the top of this file.
   The width transition is neutralized for reduced-motion in the single U1-owned
   @media (prefers-reduced-motion) block. */
.bar-fill {
    height: 100%; background: var(--compass-teal); border-radius: 4px;
    min-width: 2px; transition: width 0.3s ease;
}
.bar-value {
    flex: 0 0 auto; font-size: var(--text-xs); color: var(--text-muted);
    font-variant-numeric: tabular-nums; min-width: 30px; text-align: right;
}
/* Layered activity-trend bars: a lighter Questions fill with the darker
   Sessions fill overlaid on top (questions are always >= sessions, so the
   shorter sessions bar sits within the longer questions bar). Both fills anchor
   at the track's left edge; the sessions fill is later in the DOM so it paints
   on top of the questions fill's left portion. */
.bar-track-layered { position: relative; }
.bar-track-layered .bar-fill {
    position: absolute; left: 0; top: 0; min-width: 0; transition: width 0.3s ease;
}
.bar-fill-questions { background: var(--compass-teal-20); }
.bar-fill-sessions { background: var(--compass-teal); }
.bar-value-layered {
    display: flex; gap: 8px; align-items: baseline;
    flex: 0 0 auto; min-width: 150px; justify-content: flex-end;
}
.trend-val-sessions { color: var(--compass-teal-dark); font-weight: 600; }
.trend-val-questions { color: var(--text-muted); }
.trend-val-avg { color: var(--text-faint); font-size: 11px; }
.trend-legend {
    display: flex; align-items: center; gap: 14px; margin-bottom: 10px;
    font-size: var(--text-xs); color: var(--text-secondary);
}
.trend-legend-item { display: inline-flex; align-items: center; gap: 5px; }
.trend-legend-swatch {
    display: inline-block; width: 11px; height: 11px; border-radius: 3px;
}
.trend-legend-sessions { background: var(--compass-teal); }
.trend-legend-questions { background: var(--compass-teal-20); }
.trend-legend-note { color: var(--text-faint); }
/* .compass-trend wrapper retained as a semantic hook for the activity trend
   (referenced by overview._trend_section + its test); the teal fill now comes
   from the base .bar-fill rule above, so no per-trend color override is needed. */

/* Topic cloud */
.topic-cloud { display: flex; flex-wrap: wrap; gap: 6px; }
.topic-tag {
    display: inline-block; font-size: var(--text-xs); color: var(--text-secondary);
    background: var(--bg-muted); padding: 3px 10px; border-radius: 12px;
    border: 1px solid var(--border-light);
}

/* ── 7. Sub-navigation Strip ────────────────────────────── */
.sub-nav { background: var(--bg-subtle); border-bottom: 1px solid var(--border-light); margin-bottom: 0; }
.sub-nav + .uk-container,
.sub-nav-group + .uk-container { padding-top: 28px; }
/* G4: group wrapper holds the bar + the full-width description below it. */
.sub-nav-group { margin-bottom: 0; }
.sub-nav-group .sub-nav-description { padding-bottom: 12px; }
.sub-nav-inner { display: flex; align-items: center; justify-content: space-between; gap: 0; }
.sub-nav-tabs { display: flex; gap: 0; }
.sub-nav-tabs a {
    padding: 12px 22px; font-size: var(--text-base); font-weight: 500;
    color: var(--text-muted); text-decoration: none;
    border-bottom: 2px solid transparent;
    transition: color var(--transition-fast), border-color var(--transition-fast);
}
/* Sub-nav tabs are shared chrome (Metric Calculator / Documents / Journal /
   Compass), so the default active/hover accent stays brand-blue. Inside the
   Compass section the wrapper carries .sub-nav--compass (set in nav.py), which
   recolors only the Compass sub-nav underline to the Compass body teal — the
   other sections keep blue. */
.sub-nav-tabs a:hover { color: var(--brand-blue); }
.sub-nav-tabs a.active { color: var(--brand-blue); border-bottom-color: var(--brand-blue); font-weight: 600; }
.sub-nav--compass .sub-nav-tabs a:hover { color: var(--compass-teal-dark); }
.sub-nav--compass .sub-nav-tabs a.active {
    color: var(--compass-teal-dark); border-bottom-color: var(--compass-teal);
}
/* Tab row must never wrap (G4): the per-tab description now renders below the
   bar (.sub-nav-description), not inline. The old inline .sub-nav-annotation
   rule was removed with its sole emitter (SubNav, nav.py). */
.sub-nav-tabs { flex-wrap: nowrap; }
/* G4: full-width muted description below the sub-nav bar. */
/* Section page title (the line under the sub-nav tabs, e.g. "District review
   progress — 2024-2025"). Reads as a page title, left-aligned, and its left edge
   matches the page content because it shares .uk-container.uk-padding with the
   content wrapper. We only override the vertical padding (keep uk-padding's
   horizontal so the left edge stays aligned with the cards). */
.sub-nav-description {
    font-size: 1.3rem;
    font-weight: 700;
    color: var(--brand-navy);
    line-height: 1.25;
    text-align: left;
    /* Do NOT set margin:0 — that kills uk-container's `margin: 0 auto`, which
       centers the block at its max-width. Killing it pinned the title to the
       page's left edge while the (centered) content stayed in its column.
       Keep the auto left/right margins so the title shares the content column;
       only zero the vertical margin. */
    margin-top: 0;
    margin-bottom: 0;
    padding-top: 1.75rem !important;
    padding-bottom: 0 !important;
}

/* ── 8. Review Content ──────────────────────────────────── */
.review-content { max-width: 960px; }
.question-text { font-size: var(--text-xl); line-height: 1.6; color: var(--text-primary); }
.reasoning-text { font-size: var(--text-lg); line-height: 1.65; color: var(--text-primary); }
.citation-block { border-left: 3px solid var(--brand-blue); padding-left: var(--space-md); font-style: italic; color: var(--text-muted); margin: 12px 0; }

.reasoning-card { background: #f0f7ff; border: 1px solid #d0e3f7; border-left: 4px solid var(--brand-blue); }
.reasoning-card .review-card-label { color: var(--brand-blue); }
.reasoning-card .reasoning-text { font-size: 0.95rem; line-height: 1.75; color: var(--text-primary); }

/* ── 9. Review Cards ────────────────────────────────────── */
.review-card {
    background: var(--bg-card); box-shadow: var(--shadow-card);
    border: 1px solid var(--border-card); border-radius: 8px;
    padding: 20px var(--space-lg); margin-bottom: 20px;
}
.review-card-label {
    font-size: var(--text-xs); text-transform: uppercase; letter-spacing: 0.05em;
    color: var(--text-muted); font-weight: 600; margin-bottom: var(--space-sm);
}
.review-card-answer-detail { border-left: 3px solid var(--brand-blue); }
.review-card-decision { background: var(--bg-subtle); border: 1px solid var(--border-light); }

/* Journal — rendered lesson body */
.lesson-body { max-width: 70ch; }
.lesson-body h2 { font-size: var(--text-lg); font-weight: 600; margin: var(--space-lg) 0 var(--space-sm); }
.lesson-body p { margin: 0 0 var(--space-md); line-height: 1.6; }
.lesson-body ul, .lesson-body ol { margin: 0 0 var(--space-md) var(--space-lg); line-height: 1.6; }
.lesson-body li { margin-bottom: 4px; }
.lesson-body a { color: var(--brand-blue); text-decoration: underline; }

/* ── 10. Review Page Layout ─────────────────────────────── */
.review-context { font-size: var(--text-sm); color: var(--text-muted); }
.review-meta { font-size: var(--text-sm); color: var(--text-muted); }
.review-meta-row {
    font-size: var(--text-sm); color: var(--text-muted);
    display: flex; align-items: center; flex-wrap: wrap; gap: 2px;
}
.review-nav {
    display: flex; justify-content: space-between; align-items: center;
    margin-top: var(--space-lg); padding-top: var(--space-md);
    border-top: 1px solid var(--border-light);
}
.review-actions { display: flex; align-items: center; gap: var(--space-md); flex-wrap: wrap; }

/* ── 11. Buttons ────────────────────────────────────────── */
button.btn-approve {
    background: #2e7d32; color: #fff; border: none; padding: 8px 20px;
    border-radius: 6px; font-size: var(--text-base); font-weight: 600;
    cursor: pointer; transition: all var(--transition-fast);
}
button.btn-approve:hover { background: #256b29; color: #fff; transform: translateY(-1px); }
button.btn-reject {
    background: #c62828; color: #fff; border: none; padding: 8px 20px;
    border-radius: 6px; font-size: var(--text-base); font-weight: 600;
    cursor: pointer; transition: all var(--transition-fast);
}
button.btn-reject:hover { background: #a12222; color: #fff; transform: translateY(-1px); }
.reject-select { width: 180px; margin-right: var(--space-sm); }
.reject-checkbox-group {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-xs) var(--space-md);
    margin-top: var(--space-sm);
}
.reject-checkbox-item {
    display: flex;
    align-items: center;
    gap: var(--space-xs);
    font-size: var(--text-sm);
    cursor: pointer;
    white-space: nowrap;
}
.reject-checkbox-label {
    user-select: none;
}
.reject-form-actions { display: flex; align-items: center; }
.reject-validation-msg { color: var(--status-red-dark); font-size: var(--text-xs); display: none; }

.btn-primary {
    display: inline-flex; align-items: center; gap: 6px;
    background: var(--brand-blue); color: #fff; border: none;
    padding: 8px 20px; border-radius: 6px; font-size: var(--text-base);
    font-weight: 600; cursor: pointer; transition: all var(--transition-fast);
}
.btn-primary:hover { background: var(--brand-deep); color: #fff; transform: translateY(-1px); }
.btn-outline {
    display: inline-flex; align-items: center; gap: 6px;
    background: transparent; color: var(--brand-blue); border: 1px solid var(--brand-blue);
    padding: 8px 16px; border-radius: 6px; font-size: 14px; font-weight: 500;
    text-decoration: none; cursor: pointer; transition: all var(--transition-fast);
}
.btn-outline:hover { background: var(--brand-blue); color: #fff; text-decoration: none; }
.btn-ghost {
    background: none; border: none; color: var(--text-muted);
    font-size: var(--text-sm); font-weight: 500; cursor: pointer;
    padding: 4px 0; transition: color var(--transition-fast); text-decoration: none;
}
.btn-ghost:hover { color: var(--text-primary); text-decoration: none; }

/* ── 12. Filter System ──────────────────────────────────── */
.filter-tabs {
    display: inline-flex; border: 1px solid var(--border-light);
    border-radius: 6px; overflow: hidden;
}
.filter-tabs a {
    padding: 6px 14px; font-size: var(--text-sm); font-weight: 500;
    color: var(--text-secondary); text-decoration: none; background: var(--bg-card);
    border-right: 1px solid var(--border-light);
    transition: background var(--transition-fast), color var(--transition-fast);
    cursor: pointer; white-space: nowrap;
}
.filter-tabs a:last-child { border-right: none; }
.filter-tabs a:hover { background: var(--bg-muted); color: var(--text-primary); }
.filter-tabs a.active { background: var(--brand-blue); color: #fff; font-weight: 600; }
.filter-bar-actions { margin-left: auto; }
.filter-bar {
    display: flex; gap: var(--space-md); align-items: center;
    margin-bottom: var(--space-md); flex-wrap: wrap;
    background: var(--bg-card); padding: var(--space-md) var(--space-lg);
    box-shadow: var(--shadow-sm); border-radius: 8px; border: 1px solid var(--border-card);
}
.filter-group { display: flex; flex-direction: column; gap: var(--space-xs); }
.filter-group-label {
    font-size: 11px; font-weight: 600; color: var(--text-muted);
    text-transform: uppercase; letter-spacing: .04em;
}

/* ── 13. Progress Bars ──────────────────────────────────── */
.progress-stacked { display: flex; height: 8px; border-radius: 4px; overflow: hidden; background: var(--bg-muted); }
.progress-accepted { background: var(--status-green); }
.progress-rejected { background: var(--status-red); }
.progress-unreviewed { background: #cbd5e1; }
.progress-fill { height: 100%; background: var(--brand-blue); border-radius: 2px; }

/* ── 14. Modal Dots (k-depth indicator) ─────────────────── */
.modal-dots { display: flex; gap: 4px; align-items: center; }
.modal-dot { width: 20px; height: 20px; border-radius: 4px; border: none; background: #e2e6ea; }
.modal-dot.filled { background: var(--brand-blue); }
.modal-dots-label { font-size: 15px; font-weight: 700; color: var(--brand-blue); margin-left: 6px; }

/* ── 15. Tables ─────────────────────────────────────────── */
.clickable-row { cursor: pointer; transition: background var(--transition-fast); }
.clickable-row:hover { background: var(--bg-muted); }
.table-card { background: var(--bg-card); border-radius: 10px; box-shadow: var(--shadow-card); overflow: hidden; border: 1px solid var(--border-card); }

/* Professional data table (district list, admin, scenarios — anything in a
   .table-card). Replaces FrankenUI's bare .uk-table chrome with a clean header,
   comfortable padding, subtle row dividers, hover, and tabular figures. */
/* Data tables get a self-contained "card" look (border + rounded, clipped
   corners) so pages that don't wrap them in .table-card still look finished.
   border-collapse: separate + overflow: hidden lets the radius clip the corners
   while per-cell border-bottoms still render as continuous row dividers. */
.uk-table {
    margin: 0 0 var(--space-lg) 0 !important;
    width: 100%;
    border-collapse: separate;
    border-spacing: 0;
    font-size: 0.9rem;
    background: var(--bg-card);
    border: 1px solid var(--border-card);
    border-radius: 10px;
    overflow: hidden;
    box-shadow: var(--shadow-card);
}
/* When a table is already inside a .table-card wrapper (e.g. the districts
   list), the card supplies the frame — drop the table's own to avoid doubling. */
.table-card .uk-table {
    border: none;
    border-radius: 0;
    box-shadow: none;
    margin-bottom: 0 !important;
}
.uk-table thead th {
    background: var(--bg-subtle, #f8fafc) !important;
    color: var(--text-muted) !important;
    font-size: 0.7rem !important;
    font-weight: 700 !important;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding: 13px 18px !important;
    border-bottom: 1px solid var(--border, #e2e8f0) !important;
    white-space: nowrap;
}
.uk-table tbody td {
    padding: 15px 18px !important;
    border-top: none !important;
    border-bottom: 1px solid var(--border-light, #eef2f6) !important;
    vertical-align: middle;
    color: var(--text-primary);
    font-variant-numeric: tabular-nums;
}
.uk-table tbody tr:last-child td { border-bottom: none !important; }
.uk-table tbody tr { transition: background var(--transition-fast); }
.uk-table tbody tr:hover { background: var(--bg-subtle, #f8fafc) !important; }
/* District name (first column) reads as the row's primary label */
.uk-table tbody td:first-child a {
    color: var(--brand-navy) !important;
    font-weight: 600;
    text-decoration: none !important;
}
.uk-table tbody td:first-child a:hover { color: var(--brand-blue) !important; }
/* Per-cell row polish (state pill, emphasized counts, review % + bar) */
.state-badge {
    display: inline-block;
    min-width: 34px;
    padding: 3px 9px;
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.03em;
    color: var(--text-secondary);
    background: var(--bg-muted);
    border: 1px solid var(--border-light, #eef2f6);
    border-radius: 999px;
}
.uk-table .cell-status-green { color: var(--status-green-dark) !important; font-weight: 700; font-size: 0.95rem; }
.uk-table .cell-status-red { color: var(--status-red-dark) !important; font-weight: 700; font-size: 0.95rem; }
.uk-table .cell-status-muted { color: var(--text-muted) !important; font-weight: 600; font-size: 0.95rem; }
.review-cell { display: flex; flex-direction: column; gap: 5px; }
.review-pct { font-size: 0.8rem; font-weight: 700; color: var(--brand-navy); line-height: 1; }
.table-responsive { overflow-x: auto; -webkit-overflow-scrolling: touch; }
.table-zebra tr:nth-child(even) { background: var(--bg-subtle); }

/* ── 16. Document Page Styles ───────────────────────────── */
.readability-bar { display: flex; height: 8px; border-radius: 4px; overflow: hidden; margin-top: var(--space-sm); }
.readability-bar > div:first-child { background: var(--status-green); }
.readability-bar > div:nth-child(2) { background: #eab308; }
.readability-bar > div:last-child { background: var(--status-red); }
.detail-card-header {
    font-size: var(--text-xs); text-transform: uppercase; letter-spacing: 0.05em;
    color: var(--text-muted); font-weight: 600;
}
.detail-field-row { margin-bottom: var(--space-sm); display: flex; align-items: center; }
.detail-field-label {
    display: inline-block; width: 160px; font-size: var(--text-sm);
    color: var(--text-muted); font-weight: 500; flex-shrink: 0;
}
.doc-text-pre {
    white-space: pre-wrap; word-wrap: break-word; font-size: var(--text-sm);
    max-height: 70vh; overflow-y: auto; padding: var(--space-md);
    border-radius: 8px; background: var(--bg-subtle); border: 1px solid var(--border-light);
}
.stat-cards { display: flex; gap: var(--space-md); margin-bottom: var(--space-lg); flex-wrap: wrap; }
.stat-card-value { font-size: 1.5rem; font-weight: 700; color: var(--text-primary); font-variant-numeric: tabular-nums; }
.stat-card-label {
    font-size: var(--text-xs); text-transform: uppercase; letter-spacing: 0.05em;
    color: var(--text-muted); margin-bottom: var(--space-xs);
}

/* ── 17. Subpolicy + INA Flag + Detail Grid + Q-Nav + HTMX + Doc Hero Actions ── */

/* Subpolicy group header */
.subpolicy-header {
    font-size: 14px;
    font-weight: 600;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: .04em;
    padding: 10px 20px;
    background: var(--bg-muted);
    border-radius: 6px 6px 0 0;
    margin-top: var(--space-lg);
    margin-bottom: 0;
    display: flex;
    justify-content: space-between;
    align-items: center;
}
.subpolicy-progress {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 3px;
}
.subpolicy-progress-text {
    font-size: var(--text-xs);
    font-weight: 400;
    color: var(--text-muted);
    text-transform: none;
    letter-spacing: normal;
}
.subpolicy-progress-done {
    font-size: var(--text-xs);
    font-weight: 600;
    color: var(--status-green-dark);
    text-transform: none;
    letter-spacing: normal;
}
.subpolicy-progress-bar {
    width: 80px;
    height: 4px;
    background: var(--border-light);
    border-radius: 2px;
    overflow: hidden;
}
.subpolicy-complete {
    opacity: 0.7;
}
.subpolicy-header-standalone {
    border-radius: 6px;
}
.subpolicy-header + .table-card {
    border-radius: 0 0 8px 8px;
}

/* INA flag banner */
.ina-flag {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: var(--status-amber-bg);
    color: var(--status-amber-dark);
    font-size: 13px;
    font-weight: 600;
    padding: 6px 14px;
    border-radius: 6px;
    border: 1px solid var(--status-amber);
    margin-bottom: var(--space-md);
}

/* Detail grid (Q-review AI Answer card) */
.detail-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: var(--space-md) var(--space-xl);
}
.detail-item {
    display: flex;
    flex-direction: column;
    gap: 3px;
}
.detail-item .label {
    font-size: 12px;
    font-weight: 600;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: .04em;
}
.detail-item .value {
    font-size: 15px;
    color: var(--text-primary);
}
.detail-item .value.bold {
    font-weight: 700;
}
.detail-item.full {
    grid-column: 1 / -1;
}

/* Question nav buttons (brand-blue bordered) */
.q-nav-btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 14px;
    font-weight: 500;
    color: var(--brand-blue);
    padding: 8px 16px;
    border: 1px solid var(--brand-blue);
    border-radius: 6px;
    text-decoration: none;
    transition: all var(--transition-fast);
}
.q-nav-btn:hover {
    background: var(--brand-blue);
    color: #fff;
    text-decoration: none;
}

/* HTMX loading indicator */
.htmx-request {
    opacity: 0.6;
    transition: opacity 0.2s;
}

/* Document hero action buttons */
.doc-hero-actions {
    display: flex;
    gap: 12px;
    margin-top: var(--space-md);
}
.doc-hero-actions a {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 14px;
    font-weight: 500;
    padding: 8px 16px;
    border-radius: 6px;
    text-decoration: none;
    transition: all var(--transition-fast);
}
.doc-action-primary {
    background: var(--brand-blue);
    color: #fff;
}
.doc-action-primary:hover {
    background: var(--brand-deep);
    color: #fff;
}
.doc-action-secondary {
    background: var(--bg-card);
    color: var(--brand-blue);
    border: 1px solid var(--brand-blue);
}
.doc-action-secondary:hover {
    background: var(--brand-blue);
    color: #fff;
}

/* ── 18. Home Page Cards ────────────────────────────────── */
.home-cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--space-lg); margin-bottom: var(--space-lg); }
.home-card {
    background: var(--bg-card); border: 1px solid var(--border-card); border-radius: 8px;
    padding: var(--space-lg); box-shadow: var(--shadow-card);
    transition: transform var(--transition-fast), box-shadow var(--transition-fast);
    border-top: 3px solid transparent;
}
.home-card:hover { transform: translateY(-3px); box-shadow: var(--shadow-dropdown); }
.home-card-blue { border-top-color: var(--brand-blue); }
.home-card-green { border-top-color: var(--status-green); }
.home-card-orange { border-top-color: var(--brand-orange); }
.home-card-title {
    font-size: var(--text-xs); font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.05em; color: var(--text-muted); margin-bottom: var(--space-sm);
}
.home-card-value {
    font-size: var(--text-3xl); font-weight: 700; color: var(--text-primary);
    line-height: var(--leading-tight); font-variant-numeric: tabular-nums;
}
.home-card-label {
    font-size: var(--text-xs); font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.05em; color: var(--text-muted); margin-top: var(--space-xs);
}
.home-card-detail {
    margin-top: var(--space-md); padding-top: var(--space-md);
    border-top: 1px solid var(--border-light);
    font-size: var(--text-sm); color: var(--text-muted);
}
.home-card-link { text-decoration: none; color: inherit; }
.home-card-link:hover { text-decoration: none; color: inherit; }
.home-header {
    display: flex; justify-content: space-between; align-items: center;
    gap: var(--space-lg);
    margin-bottom: var(--space-xl);
}
.home-greeting {
    font-size: 1.6rem; font-weight: 700; color: var(--brand-navy);
    line-height: 1.2; letter-spacing: -0.01em;
}
.home-greeting-sub {
    font-size: 0.9rem; color: var(--text-muted);
    margin-top: 4px; margin-bottom: 0;
}
/* Academic-year selector cluster (right side of the home header) */
.home-ay-group { display: flex; flex-direction: column; gap: 5px; align-items: flex-end; }
.home-ay-label {
    font-size: 0.68rem; font-weight: 700; letter-spacing: 0.06em;
    text-transform: uppercase; color: var(--text-muted);
}

/* Page content breathing room — the main content wrapper was hugging the nav
   bar with almost no top gap. Give it generous vertical padding so every page
   sits comfortably below the nav / sub-nav. */
main > .uk-container.uk-padding {
    padding-top: 2.5rem !important;
    padding-bottom: 2.5rem !important;
}

/* ── 19. Auth Page ──────────────────────────────────────── */
.auth-page {
    min-height: 100vh; display: flex; align-items: center; justify-content: center;
    background: var(--brand-navy); padding: var(--space-lg);
}
.auth-card {
    background: var(--bg-card); border-radius: 12px; box-shadow: var(--shadow-dropdown);
    padding: var(--space-2xl); max-width: 420px; width: 100%;
}
.auth-brand { font-size: 1.5rem; font-weight: 700; color: var(--brand-navy); text-align: center; margin-bottom: var(--space-md); }
.auth-divider { height: 3px; background: var(--brand-orange); border: none; border-radius: 2px; margin-bottom: var(--space-lg); }
.auth-title { font-size: var(--text-xl); font-weight: 600; color: var(--text-primary); text-align: center; margin-bottom: var(--space-sm); }
.auth-subtitle { font-size: var(--text-base); color: var(--text-muted); text-align: center; margin-bottom: var(--space-lg); }
.auth-error { background: var(--status-red-bg); color: var(--status-red-dark); padding: var(--space-md); border-radius: 8px; margin-bottom: var(--space-md); font-size: var(--text-sm); }
.auth-btn {
    display: block; width: 100%; padding: 12px var(--space-lg);
    background: var(--brand-orange); color: #fff; border: none; border-radius: 6px;
    font-size: var(--text-base); font-weight: 600; cursor: pointer;
    transition: background var(--transition-fast), transform var(--transition-fast);
    margin-top: var(--space-md);
}
.auth-btn:hover { background: #e07d15; transform: translateY(-1px); }
.auth-label { font-size: var(--text-sm); font-weight: 500; color: var(--text-secondary); margin-bottom: 6px; display: block; }
.auth-footer { text-align: center; margin-top: var(--space-md); font-size: var(--text-sm); color: var(--text-muted); }
.otp-container { display: flex; gap: 10px; justify-content: center; margin-bottom: var(--space-lg); }
.otp-digit {
    width: 48px; height: 56px; text-align: center; font-size: 1.5rem; font-weight: 600;
    border: 2px solid var(--border-light); border-radius: 8px;
    transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
    -moz-appearance: textfield;
}
.otp-digit::-webkit-inner-spin-button, .otp-digit::-webkit-outer-spin-button { -webkit-appearance: none; margin: 0; }
.otp-digit:focus { border-color: var(--brand-blue); box-shadow: 0 0 0 3px rgba(29, 108, 208, 0.15); outline: none; }
.otp-digit.filled { border-color: var(--brand-blue); }

/* ── 20. Alert Boxes ────────────────────────────────────── */
.alert-success { background: var(--status-green-bg); color: var(--status-green-dark); padding: var(--space-md); border-radius: 8px; margin-bottom: var(--space-md); font-size: var(--text-sm); }
.alert-error { background: var(--status-red-bg); color: var(--status-red-dark); padding: var(--space-md); border-radius: 8px; margin-bottom: var(--space-md); font-size: var(--text-sm); }

/* ── 21. Message Bubbles ────────────────────────────────── */
.msg-assistant { background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 12px; padding: 12px var(--space-md); margin-bottom: var(--space-md); max-width: 85%; }
.msg-user { background: var(--bg-subtle); border: 1px solid var(--border-light); border-radius: 12px; padding: 12px var(--space-md); margin-bottom: var(--space-md); max-width: 85%; margin-left: auto; }
.msg-role { font-weight: 600; font-size: var(--text-xs); text-transform: uppercase; color: var(--text-muted); }
.msg-time { font-size: var(--text-xs); color: var(--text-faint); }
.msg-content { font-size: var(--text-base); line-height: var(--leading-relaxed); white-space: pre-wrap; max-width: var(--max-prose); }
.msg-container { max-width: 720px; }

/* ── 22. Utility Classes ────────────────────────────────── */
.cell-nowrap { white-space: nowrap; }
.cell-mono { font-family: ui-monospace, monospace; font-size: var(--text-sm); }
.cell-status-green { color: var(--status-green-dark); font-weight: 600; }
.cell-status-red { color: var(--status-red-dark); font-weight: 600; }
.cell-status-muted { color: var(--text-muted); }
.tabular-nums { font-variant-numeric: tabular-nums; }
.content-prose { max-width: var(--max-prose); }
.content-wide { max-width: 960px; }
.text-xs { font-size: var(--text-xs); }
.text-sm { font-size: var(--text-sm); }
.text-base { font-size: var(--text-base); }
.text-lg { font-size: var(--text-lg); }
.text-primary { color: var(--text-primary); }
.text-secondary { color: var(--text-secondary); }
.text-muted { color: var(--text-muted); }
.text-faint { color: var(--text-faint); }
.text-success { color: var(--status-green-dark); }
.text-error { color: var(--status-red-dark); }
.text-warning { color: var(--status-amber-dark); }
.font-medium { font-weight: 500; }
.font-semibold { font-weight: 600; }
.font-bold { font-weight: 700; }
.mt-xs { margin-top: var(--space-xs); }
.mt-sm { margin-top: var(--space-sm); }
.mt-md { margin-top: var(--space-md); }
.mt-lg { margin-top: var(--space-lg); }
.mb-xs { margin-bottom: var(--space-xs); }
.mb-sm { margin-bottom: var(--space-sm); }
.mb-md { margin-bottom: var(--space-md); }
.mb-lg { margin-bottom: var(--space-lg); }
.ml-sm { margin-left: var(--space-sm); }
.flex-1 { flex: 1; }
.d-block { display: block; }
.select-sm { width: 168px; }

/* Polished native <select> — replaces the default browser/UIkit chrome with a
   clean bordered control + custom chevron. Applies app-wide for consistency. */
select.uk-select {
    appearance: none !important;
    -webkit-appearance: none !important;
    -moz-appearance: none !important;
    background-color: #fff !important;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 12 12'%3E%3Cpath d='M3 4.5 6 7.5 9 4.5' fill='none' stroke='%231b3862' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") !important;
    background-repeat: no-repeat !important;
    background-position: right 11px center !important;
    background-size: 13px !important;
    border: 1px solid var(--border, #d1d5db) !important;
    border-radius: 8px !important;
    padding: 8px 34px 8px 12px !important;
    height: auto !important;
    min-height: 38px;
    font-size: 0.875rem !important;
    font-weight: 500;
    color: var(--text-primary) !important;
    line-height: 1.3 !important;
    box-shadow: 0 1px 2px rgba(15, 34, 58, 0.05);
    transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
    cursor: pointer;
}
select.uk-select:hover { border-color: var(--brand-blue) !important; }
select.uk-select:focus {
    outline: none !important;
    border-color: var(--brand-blue) !important;
    box-shadow: 0 0 0 3px rgba(29, 108, 208, 0.15) !important;
}
.select-md { width: 200px; }
.select-lg { width: 300px; }

/* Unified search field — consistent sizing/typography with the polished selects,
   plus a built-in magnifier icon (background SVG, left-inset). Used on every page
   search box so they all look identical. */
.search-field {
    width: 300px;
    max-width: 100%;
    height: auto !important;
    min-height: 38px;
    padding: 8px 12px 8px 36px !important;
    font-size: 0.875rem !important;
    font-weight: 500;
    color: var(--text-primary) !important;
    line-height: 1.3 !important;
    background-color: #fff !important;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='16' viewBox='0 0 24 24' fill='none' stroke='%2364748b' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='11' cy='11' r='8'/%3E%3Cline x1='21' y1='21' x2='16.65' y2='16.65'/%3E%3C/svg%3E") !important;
    background-repeat: no-repeat !important;
    background-position: left 12px center !important;
    background-size: 15px !important;
    border: 1px solid var(--border, #d1d5db) !important;
    border-radius: 8px !important;
    box-shadow: 0 1px 2px rgba(15, 34, 58, 0.05);
    transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}
.search-field::placeholder { color: var(--text-muted); font-weight: 400; }
.search-field:hover { border-color: var(--brand-blue) !important; }
.search-field:focus {
    outline: none !important;
    border-color: var(--brand-blue) !important;
    box-shadow: 0 0 0 3px rgba(29, 108, 208, 0.15) !important;
}
.select-xl { width: 320px; }
.form-label { font-size: var(--text-sm); font-weight: 500; color: var(--text-secondary); display: block; margin-bottom: 6px; }
.empty-state { padding: var(--space-2xl); text-align: center; color: var(--text-muted); }
.empty-state-lg { padding: 80px 0; text-align: center; }
.quality-flag-card { border-left: 3px solid var(--status-amber); }

/* ── 23. Citation Classes ───────────────────────────────── */
.citation-meta-tag { display: inline-block; padding: 1px 8px; border-radius: 4px; font-size: var(--text-xs); font-weight: 500; background: var(--bg-muted); color: var(--text-secondary); margin-right: var(--space-xs); }
.citation-section-heading { font-size: 0.82rem; color: var(--text-secondary); margin-top: 2px; }
.citation-note-textarea { width: 100%; margin-top: 6px; font-size: 0.82rem; min-height: 48px; resize: vertical; }
.citation-doc { border: 1px solid var(--border-light); border-radius: 6px; padding: var(--space-md); margin-bottom: var(--space-sm); }
.citation-title { font-weight: 600; color: var(--brand-blue); text-decoration: none; }
.citation-title:hover { text-decoration: underline; }
.citation-bib { font-size: var(--text-xs); color: var(--text-faint); }
.citation-header { display: flex; align-items: center; margin-bottom: var(--space-sm); }
.citation-footnote { margin-top: var(--space-md); padding: 14px var(--space-md); background: var(--bg-muted); border-radius: 6px; font-size: var(--text-sm); color: var(--text-muted); }
.citation-notes-section { margin-top: var(--space-lg); padding-top: var(--space-md); border-top: 1px solid var(--border-light); }

/* ── 24. Quality Hold System ───────────────────────────── */
.badge-on-hold { background: var(--status-blue-bg); color: var(--status-blue-dark); }
button.btn-hold {
    background: var(--status-blue-dark); color: #fff; border: none; padding: 8px 20px;
    border-radius: 6px; font-size: var(--text-base); font-weight: 600;
    cursor: pointer; transition: all var(--transition-fast);
}
button.btn-hold:hover { background: #1a3a8a; color: #fff; transform: translateY(-1px); }
button.btn-release-hold {
    background: transparent; color: var(--status-blue-dark); border: 1px solid var(--status-blue-dark);
    padding: 8px 20px; border-radius: 6px; font-size: var(--text-base); font-weight: 600;
    cursor: pointer; transition: all var(--transition-fast);
}
button.btn-release-hold:hover { background: var(--status-blue-dark); color: #fff; transform: translateY(-1px); }
.hold-form-inline { display: inline-flex; align-items: center; gap: var(--space-sm); }
.review-card-hold {
    background: var(--status-blue-bg); border: 1px solid #93c5fd;
    border-left: 4px solid var(--status-blue-dark);
}
.review-card-hold .review-card-label { color: var(--status-blue-dark); }
.hold-banner-inline {
    display: flex; align-items: center; gap: var(--space-sm);
    padding-bottom: var(--space-md); margin-bottom: var(--space-md);
    border-bottom: 1px solid #93c5fd;
    font-size: var(--text-sm); color: var(--status-blue-dark); font-weight: 500;
}
.review-card-held {
    background: var(--status-blue-bg); border: 1px solid #93c5fd;
    border-left: 4px solid var(--status-blue-dark);
}

/* ── 25. Responsive Breakpoint ──────────────────────────── */
@media (max-width: 768px) {
    .home-cards { grid-template-columns: 1fr; }
    .home-header { flex-direction: column; gap: var(--space-md); }
    .review-content { max-width: min(960px, 90vw); }
    .kpi-cards { gap: var(--space-sm); }
    .kpi-card { min-width: 120px; padding: 12px var(--space-md); }
    .filter-bar { gap: var(--space-sm); }
    .detail-grid { grid-template-columns: 1fr; }
    .auth-card { margin: var(--space-md); padding: var(--space-lg); }
    .otp-digit { width: 40px; height: 48px; font-size: 1.25rem; }
    .overview-grid-2 { grid-template-columns: 1fr; }
    .bar-label { flex: 0 0 100px; }
}

/* ═══════════════════════════════════════════════════════════
   26. COMPASS SECTION — "The Observatory"
   Deep teal accent, progressive disclosure, trace-aware.
   Generous spacing, clear surface hierarchy, warm accents.
   ═══════════════════════════════════════════════════════════ */

/* ── 26a. Compass Design Tokens ───────────────────────── */
:root {
    --compass-teal: #0E7C7B;
    --compass-teal-light: #17BEBB;
    --compass-teal-dark: #084848;
    --compass-teal-bg: #E0F5F5;
    --compass-teal-surface: #EEF9F9;
    --compass-teal-10: rgba(14, 124, 123, 0.10);
    --compass-teal-20: rgba(14, 124, 123, 0.20);
    --compass-sidebar-w: 320px;
    --compass-sidebar-bg: #f8f9fa;
    --compass-detail-bg: #f3f4f5;
    --compass-card-shadow: 0 20px 40px rgba(25, 28, 29, 0.06);
    --compass-card-hover-shadow: 0 20px 40px rgba(25, 28, 29, 0.10);
}

/* ── 26b. Compass Layout — Sidebar + Detail ───────────── */
.compass-layout {
    display: flex;
    height: calc(100vh - 56px - 44px - 56px);
    margin: calc(-1 * var(--space-lg)) calc(-1 * var(--space-xl));
    /* Clear gap below the search bar (was a negative pull-up that cramped them). */
    margin-top: var(--space-md);
    overflow: hidden;
}

/* ── 26c. Compass Sidebar ─────────────────────────────── */
.compass-sidebar {
    width: var(--compass-sidebar-w);
    min-width: var(--compass-sidebar-w);
    background: var(--compass-sidebar-bg);
    display: flex;
    flex-direction: column;
    overflow: hidden;
}
.compass-filter-bar {
    padding: 16px 16px 12px;
    display: flex;
    flex-direction: column;
    gap: 14px;
    background: #fff;
}
/* The date-range + triage controls live in #compass-convo-results but should
   read as ONE white filter panel continuing from the feedback select above. */
.compass-convo-results > .range-selector {
    background: #fff;
    padding: 0 16px 12px;
}
.compass-convo-results > .triage-tabs {
    background: #fff;
    margin: 0;
    padding: 2px 16px 14px;
    border-bottom: 1px solid var(--border-light, #e6e9f0);
}
.triage-tab-icon {
    flex-shrink: 0;
    opacity: 0.85;
}
.compass-filter-select {
    width: 100%;
    padding: 8px 12px;
    font-size: var(--text-sm);
    border: 1px solid var(--border, #d6dbe5);
    border-radius: 8px;
    background: var(--bg-card);
    color: var(--text-primary);
    transition: border-color var(--transition-fast);
}
.compass-filter-select:focus {
    outline: none;
    border-color: var(--compass-teal);
}
/* ── 26d. Conversation List Items ─────────────────────── */
.compass-convo-list {
    flex: 1;
    overflow-y: auto;
    overflow-x: hidden;
    padding: 6px 0;
}
.compass-convo-item {
    display: block;
    padding: 12px 14px;
    margin: 0 6px 1px;
    border-radius: 8px;
    text-decoration: none;
    color: inherit;
    transition: background var(--transition-fast), box-shadow var(--transition-fast);
}
.compass-convo-item:hover {
    background: #fff;
    box-shadow: var(--compass-card-shadow);
    text-decoration: none;
    color: inherit;
}
.compass-convo-item.has-fail {
    box-shadow: inset 3px 0 0 rgba(198, 40, 40, 0.55);
}
.compass-convo-item.has-fail:hover {
    background: #fff;
    box-shadow: inset 3px 0 0 rgba(198, 40, 40, 0.55), var(--compass-card-shadow);
}
/* Active ("currently viewing") wins over the flagged border — declared last and
   with .has-fail.active raising specificity so a flagged row still shows teal
   when selected. */
.compass-convo-item.active,
.compass-convo-item.has-fail.active,
.compass-convo-item.has-fail.active:hover {
    background: #fff;
    box-shadow: inset 3px 0 0 var(--compass-teal), var(--compass-card-shadow);
}
.compass-convo-inner { display: flex; flex-direction: column; gap: 5px; }
.compass-convo-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 10px;
}
.compass-convo-preview {
    font-size: 13px;
    color: var(--text-primary);
    line-height: 1.45;
    flex: 1;
    overflow: hidden;
    display: -webkit-box;
    -webkit-line-clamp: 3;
    -webkit-box-orient: vertical;
}
.compass-convo-item.active .compass-convo-preview {
    font-weight: 600;
    color: var(--compass-teal-dark);
}
.compass-convo-meta {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
    flex-wrap: wrap;
}
.compass-convo-time {
    font-size: 11px;
    color: var(--text-faint);
    font-variant-numeric: tabular-nums;
}

/* Eval pill */
.compass-eval-pill {
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 2px 7px;
    border-radius: 4px;
    background: var(--compass-teal-bg);
    color: var(--compass-teal);
}

/* ── 26e. Detail Panel ────────────────────────────────── */
.compass-detail {
    flex: 1;
    overflow-y: auto;
    overflow-x: hidden;
    padding: 18px 40px var(--space-xl);
    background: var(--compass-detail-bg);
}

/* ── 26g. Turn Card ───────────────────────────────────── */
.compass-turn-card {
    background: #fff;
    border-radius: 12px;
    padding: 0;
    margin-bottom: 18px;
    overflow: hidden;
    box-shadow: var(--compass-card-shadow);
    transition: box-shadow var(--transition-fast);
}
.compass-turn-card:hover {
    box-shadow: var(--compass-card-hover-shadow);
}
.compass-turn-header {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px var(--space-xl);
    background: #fff;
    border-bottom: 1px solid rgba(194, 198, 213, 0.2);
}
.compass-turn-number {
    font-size: var(--text-base);
    font-weight: 600;
    color: var(--compass-teal);
    font-variant-numeric: tabular-nums;
}
.compass-turn-time {
    font-size: 12px;
    color: var(--text-muted);
    font-variant-numeric: tabular-nums;
}

/* Intent + complexity badges */
.compass-intent-badge {
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 3px 10px;
    border-radius: 5px;
    background: var(--compass-teal-bg);
    color: var(--compass-teal-dark);
}
.compass-complexity-quick {
    font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em;
    padding: 3px 10px; border-radius: 5px;
    background: var(--status-green-bg); color: var(--status-green-dark);
}
.compass-complexity-standard {
    font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em;
    padding: 3px 10px; border-radius: 5px;
    background: var(--status-blue-bg); color: var(--status-blue-dark);
}
.compass-complexity-deep {
    font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em;
    padding: 3px 10px; border-radius: 5px;
    background: var(--status-amber-bg); color: var(--status-amber-dark);
}

/* User feedback (thumbs up/down) badges */
.compass-feedback-badge {
    font-size: 11px;
    font-weight: 600;
    padding: 3px 8px;
    border-radius: 5px;
    display: inline-flex;
    align-items: center;
    gap: 4px;
}
.compass-feedback-up {
    background: var(--status-green-bg);
    color: var(--status-green-dark);
}
.compass-feedback-down {
    background: var(--status-red-bg, #FEE2E2);
    color: var(--status-red-dark, #991B1B);
}
.compass-msg-text-wrap {
    flex: 1;
    min-width: 0;
}
.compass-feedback-note {
    margin-top: 8px;
    padding: 8px 12px;
    background: var(--status-red-bg, #FEE2E2);
    border-left: 3px solid var(--status-red-dark, #991B1B);
    border-radius: 4px;
    font-size: 13px;
}
.compass-feedback-note-label {
    font-weight: 600;
    color: var(--status-red-dark, #991B1B);
    margin-right: 6px;
}
.compass-feedback-note-text {
    color: var(--text-primary);
    font-style: italic;
}

/* ── 26h. Messages ────────────────────────────────────── */
.compass-msg {
    display: flex;
    gap: 14px;
    padding: 20px var(--space-xl);
    align-items: flex-start;
}
.compass-msg-avatar {
    width: 32px;
    height: 32px;
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 12px;
    font-weight: 500;
    flex-shrink: 0;
    letter-spacing: 0.02em;
}
.compass-msg-avatar-user {
    background: #EEF0F3;
    color: var(--text-secondary);
}
.compass-msg-avatar-assistant {
    background: var(--compass-teal);
    color: #fff;
}
.compass-msg-text {
    font-size: 14px;
    line-height: 1.65;
    color: var(--text-primary);
    flex: 1;
    min-width: 0;
}
.compass-msg-response {
    word-wrap: break-word;
}
.compass-msg-response p {
    margin: 0 0 12px;
}
.compass-msg-response p:last-child {
    margin-bottom: 0;
}
.compass-msg-response ul,
.compass-msg-response ol {
    margin: 8px 0 12px 18px;
    padding: 0;
}
.compass-msg-response li {
    margin-bottom: 4px;
}
.compass-msg-response table {
    width: 100%;
    border-collapse: collapse;
    margin: 12px 0;
    font-size: 13px;
}
.compass-msg-response th,
.compass-msg-response td {
    border: 1px solid rgba(194, 198, 213, 0.45);
    padding: 8px 10px;
    text-align: left;
    vertical-align: top;
}
.compass-msg-response th {
    background: #F5F7F9;
    font-weight: 600;
}
.compass-msg-user {
    background: #F0F4F8;
}
/* ── 26j. Verdict Panel ───────────────────────────────── */
.compass-verdict-panel {
    padding: 0 var(--space-xl) var(--space-lg);
}
.compass-verdict-header {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px 0;
    cursor: pointer;
}
.compass-verdict-badge {
    font-size: 11px;
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 4px 12px;
    border-radius: 6px;
}
.compass-verdict-approved {
    background: var(--status-green-bg);
    color: var(--status-green-dark);
}
.compass-verdict-revision {
    background: var(--status-amber-bg);
    color: var(--status-amber-dark);
}
.compass-verdict-override {
    background: var(--status-red-bg);
    color: var(--status-red-dark);
}
.compass-verdict-score {
    font-size: 15px;
    font-weight: 500;
    color: var(--text-primary);
    font-variant-numeric: tabular-nums;
}
.compass-verdict-toggle {
    font-size: 12px;
    color: var(--text-faint);
    margin-left: auto;
    transition: color var(--transition-fast);
}
.compass-verdict-toggle:hover { color: var(--compass-teal); }

/* Verdict detail (expandable) */
.compass-verdict-detail {
    max-height: 0;
    overflow: hidden;
    transition: max-height 0.35s cubic-bezier(0.4, 0, 0.2, 1), padding 0.35s ease;
}
.compass-verdict-detail.expanded {
    max-height: 600px;
    padding-bottom: var(--space-md);
}

/* Score bars */
.compass-score-bars {
    display: flex;
    flex-direction: column;
    gap: 12px;
    margin-bottom: var(--space-lg);
    padding: 16px;
    background: #F8FAFB;
    border-radius: 10px;
}
.compass-score-bar { display: flex; flex-direction: column; gap: 4px; }
.compass-score-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
}
.compass-score-label {
    font-size: 12px;
    font-weight: 500;
    color: var(--text-secondary);
}
.compass-score-value {
    font-size: 12px;
    font-weight: 600;
    color: var(--text-primary);
    font-variant-numeric: tabular-nums;
}
.compass-score-track {
    height: 6px;
    background: #E5E9ED;
    border-radius: 3px;
    overflow: hidden;
}
.compass-score-fill {
    height: 100%;
    background: linear-gradient(90deg, var(--compass-teal), var(--compass-teal-light));
    border-radius: 3px;
    transition: width 0.5s cubic-bezier(0.4, 0, 0.2, 1);
}

/* Verdict feedback */
.compass-verdict-feedback {
    background: #F8FAFB;
    border-radius: 10px;
    padding: 14px 16px;
}
.compass-verdict-feedback-label {
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-faint);
    margin-bottom: 6px;
}
.compass-verdict-feedback-text {
    font-size: 13px;
    color: var(--text-secondary);
    line-height: 1.6;
    max-width: var(--max-prose); /* G5: keep long judge-rationale lines readable */
}

/* ── 26k. Turn Footer ─────────────────────────────────── */
.compass-turn-footer {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 12px var(--space-xl);
    background: #F8FAFB;
}
.compass-turn-footer-stat {
    font-size: 12px;
    color: var(--text-faint);
    font-variant-numeric: tabular-nums;
    font-family: ui-monospace, 'Cascadia Code', 'Source Code Pro', monospace;
}
.compass-turn-footer-link {
    font-size: 12px;
    color: var(--compass-teal);
    text-decoration: none;
    font-weight: 600;
    margin-left: auto;
    transition: color var(--transition-fast);
}
.compass-turn-footer-link:hover {
    color: var(--compass-teal-dark);
    text-decoration: underline;
}

/* ── 26l. Empty State ─────────────────────────────────── */
.compass-empty {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 100%;
    text-align: center;
}
.compass-empty-content {
    max-width: 480px;
    padding: var(--space-xl);
}
.compass-empty-brand {
    font-size: 2.25rem;
    font-weight: 600;
    color: var(--compass-teal);
    letter-spacing: -0.03em;
    margin-bottom: var(--space-md);
}
.compass-empty-desc {
    font-size: 15px;
    color: var(--text-muted);
    line-height: 1.6;
    margin-bottom: 36px;
}
.compass-empty-title {
    font-size: 14px;
    font-weight: 600;
    color: var(--text-muted);
    margin-bottom: 6px;
}
.compass-empty-sub {
    font-size: 12px;
    color: var(--text-faint);
    line-height: 1.5;
    max-width: 240px;
    margin: 0 auto;
}

/* ── 26m. Compass Responsive ──────────────────────────── */
@media (max-width: 768px) {
    .compass-layout {
        flex-direction: column;
        height: auto;
    }
    .compass-sidebar {
        width: 100%;
        min-width: 100%;
        max-height: 40vh;
    }
    .compass-detail {
        padding: var(--space-md);
    }
}

/* ── 26q. HTMX Loading States ─────────────────────────── */
.compass-detail.htmx-request {
    opacity: 0.5;
    transition: opacity 0.2s;
}
.compass-convo-list.htmx-request {
    opacity: 0.5;
    transition: opacity 0.2s;
}
/* Shared aria-busy loading hook (single authority, U1-owned). U2-U5 reference
   this for any aria-live region they add — they must not re-add their own. It
   ties the screen-reader busy signal (aria-busy, toggled via hx-on handlers)
   to the same visual fade .htmx-request gives sighted users. */
[aria-busy="true"] {
    opacity: 0.5;
    transition: opacity 0.2s;
}

/* ── 26r. Overview Strip + Range Pills ────────────────── */
.compass-overview-wrap {
    display: flex;
    flex-direction: column;
    gap: var(--space-md);
}
.strip-range {
    display: flex;
    justify-content: flex-end;
}
.ops-stat-line {
    display: flex;
    flex-wrap: wrap;
    gap: 18px;
    margin: 4px 0 14px;
    font-size: 13px;
    color: var(--text-muted);
}
.ops-stat-value {
    color: var(--text-primary);
    font-weight: 600;
}
.ops-stat-line--stacked {
    align-items: flex-start;
    margin: var(--space-sm) 0;
}
.ops-cost-note {
    margin-top: var(--space-md);
}
.range-pills {
    display: inline-flex;
    gap: 2px;
    background: #eef1f6;
    padding: 3px;
    border-radius: 999px;
}
.range-pill {
    font-size: 12px;
    font-weight: 500;
    padding: 4px 12px;
    border-radius: 999px;
    color: var(--text-muted);
    text-decoration: none;
    transition: background var(--transition-fast), color var(--transition-fast);
}
.range-pill:hover {
    color: var(--compass-teal-dark);
    text-decoration: none;
}
.range-pill.active {
    background: #fff;
    color: var(--compass-teal-dark);
    box-shadow: 0 1px 2px rgba(25,28,29,0.08);
}

/* ── 26s. Custom "since" span (presets + date input) ───── */
/* One reusable date-range control: the preset pills above plus a custom
   lower-bound ("since this date"). Compass body accent only — no brand blue,
   no new color token. Used on Overview (full-page form) and the Conversations
   list (HTMX swap). */
.range-selector {
    display: flex;
    flex-direction: column;
    align-items: stretch;
    gap: 9px;
    width: 100%;
}
.range-custom-form {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
}
.range-custom-form .range-custom-input { flex: 1; min-width: 0; }
.range-custom-input {
    font-size: 12px;
    padding: 5px 8px;
    border: 1px solid var(--border, #d6dbe5);
    border-radius: 8px;
    color: var(--text-primary);
    background: #fff;
}
.range-custom-input:focus {
    outline: none;
    border-color: var(--compass-teal);
    box-shadow: 0 0 0 2px var(--compass-teal-10);
}
.range-custom-form.active .range-custom-input,
.range-custom-apply.active {
    border-color: var(--compass-teal);
    box-shadow: 0 0 0 2px var(--compass-teal-10);
}
.range-custom-apply {
    font-size: 12px;
    font-weight: 500;
    padding: 5px 12px;
    border: 1px solid var(--border, #d6dbe5);
    border-radius: 999px;
    background: #fff;
    color: var(--text-secondary);
    cursor: pointer;
    transition: background var(--transition-fast), color var(--transition-fast),
        border-color var(--transition-fast);
}
.range-custom-apply:hover {
    border-color: var(--compass-teal);
    color: var(--compass-teal-dark);
}

/* ── 26u. Triage quick-filter tabs (live counts) ───────── */
/* Conversations review-inbox tabs (All / 👎 / Has data table / Unreviewed). Teal
   accent only (Compass body) — chrome stays brand blue. The count chip uses
   tabular-nums so the numbers don't jitter on swap. */
.compass-convo-results {
    display: flex;
    flex-direction: column;
    min-height: 0;
}
.triage-tabs {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    padding: 4px 0 8px;
}
.triage-tab {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    font-weight: 500;
    padding: 5px 11px;
    border-radius: 999px;
    border: 1px solid var(--border, #d6dbe5);
    background: #fff;
    color: var(--text-secondary);
    cursor: pointer;
    transition: background var(--transition-fast), color var(--transition-fast),
        border-color var(--transition-fast);
}
.triage-tab:hover {
    color: var(--compass-teal-dark);
    border-color: var(--compass-teal);
}
.triage-tab.active {
    background: var(--compass-teal-surface);
    border-color: var(--compass-teal);
    color: var(--compass-teal-dark);
    font-weight: 600;
}
.triage-tab-count {
    font-variant-numeric: tabular-nums;
    font-size: 11px;
    font-weight: 600;
    padding: 1px 6px;
    border-radius: 999px;
    background: #eef1f6;
    color: var(--text-secondary);
}
.triage-tab.active .triage-tab-count {
    background: #fff;
    color: var(--compass-teal-dark);
}

/* ── 26v. Inline L1 verdicts in the turn card ──────────── */
/* The unified per-turn verdict block (DASH-R5) reuses the quality-verdict-list
   <details> styling; this only spaces it inside the turn card so it reads as a
   footer to the answer, not a sibling card. */
.compass-turn-verdicts {
    padding: 8px var(--space-xl) 14px;
    border-top: 1px solid rgba(194, 198, 213, 0.2);
}

/* ── 26t. Reduced-motion guard (single authority) ──────── */
/* U1 owns the ONE prefers-reduced-motion block; U2-U5 reference it, never
   re-add their own. Neutralizes the range-pill hover transition, the list/
   detail HTMX opacity fade, and the score/bar fill width animations so users
   who ask for less motion get a still UI. */
@media (prefers-reduced-motion: reduce) {
    .range-pill,
    .range-custom-apply,
    .triage-tab,
    .compass-detail.htmx-request,
    .compass-convo-list.htmx-request,
    .bar-fill,
    .compass-score-fill {
        transition: none !important;
    }
}

/* ── 26r. Smart Search Bar (Conversations) ─────────────── */
.conversations-top-bar {
    display: flex;
    align-items: center;
    gap: 12px;
    /* Break out to the SAME full-bleed width as the content panel below so the
       search bar spans the whole area instead of sitting inset. */
    margin: 0 calc(-1 * var(--space-xl));
    padding: 0 0 var(--space-md);
    flex-wrap: wrap;
}
.smart-search-bar {
    flex: 1 1 480px;
    min-width: 320px;
}
.smart-search-form {
    display: flex;
    gap: 8px;
}
.smart-search-input {
    flex: 1;
    padding: 9px 14px 9px 38px;
    font-size: 14px;
    line-height: 1.4;
    border: 1px solid var(--border, #d6dbe5);
    border-radius: 8px;
    background-color: var(--bg-card);
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='16' viewBox='0 0 24 24' fill='none' stroke='%2364748b' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='11' cy='11' r='8'/%3E%3Cline x1='21' y1='21' x2='16.65' y2='16.65'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: left 13px center;
    background-size: 15px;
    color: var(--text-primary);
    transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}
.smart-search-input::placeholder { color: var(--text-muted); }
.smart-search-input:focus {
    outline: none;
    border-color: var(--compass-teal);
    box-shadow: 0 0 0 3px var(--compass-teal-10);
}
.smart-search-submit {
    padding: 9px 22px;
    font-size: 14px;
    font-weight: 600;
    background: var(--compass-teal);
    color: #fff;
    border: none;
    border-radius: 8px;
    cursor: pointer;
    transition: background var(--transition-fast);
}
.smart-search-submit:hover { background: var(--compass-teal-dark); }
.smart-search-error {
    margin-top: 6px;
    font-size: 13px;
    font-weight: 500;
    color: #b45309;
}

/* ── 26s. Detail header (white card) ──────────────────── */
.compass-permalink-banner {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 14px;
    margin-bottom: 10px;
    background: #fff;
    border: 1px solid rgba(194, 198, 213, 0.35);
    border-radius: 8px;
    box-shadow: var(--compass-card-shadow);
    font-size: 12px;
}
.compass-permalink-icon {
    color: var(--text-faint);
    flex-shrink: 0;
}
.compass-permalink-url {
    flex: 1;
    min-width: 0;
    font-family: ui-monospace, 'Cascadia Code', 'Source Code Pro', monospace;
    font-size: 11.5px;
    color: var(--text-secondary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.compass-permalink-copy {
    padding: 4px 12px;
    font-size: 11px;
    font-weight: 600;
    background: var(--compass-teal);
    color: #fff;
    border: none;
    border-radius: 4px;
    cursor: pointer;
    flex-shrink: 0;
    transition: background var(--transition-fast);
}
.compass-permalink-copy:hover { background: var(--compass-teal-dark); }

.compass-detail-header {
    background: #fff;
    border-radius: 12px;
    padding: 18px 22px;
    box-shadow: var(--compass-card-shadow);
    margin-bottom: 14px;
    display: flex;
    flex-direction: column;
    gap: 10px;
}
.compass-detail-question {
    font-size: 17px;
    font-weight: 600;
    color: var(--text-primary);
    line-height: 1.4;
    margin: 0;
}
.compass-detail-meta-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 12px;
    font-size: 12px;
    color: var(--text-muted);
}
.compass-detail-meta-item {
    font-variant-numeric: tabular-nums;
}
.compass-detail-meta-id {
    font-family: ui-monospace, 'Cascadia Code', 'Source Code Pro', monospace;
    font-size: 11px;
    color: var(--text-faint);
    cursor: pointer;
}
.compass-detail-badges {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: center;
}

/* ── 26s2. Turn navigator pips ────────────────────────── */
.compass-turn-nav {
    display: flex;
    gap: 4px;
    margin-bottom: 12px;
}
.compass-turn-pip {
    min-width: 36px;
    padding: 5px 10px;
    font-size: 11px;
    font-weight: 600;
    border-radius: 4px;
    text-align: center;
    text-decoration: none;
    font-variant-numeric: tabular-nums;
    font-family: ui-monospace, 'Cascadia Code', 'Source Code Pro', monospace;
    background: #fff;
    color: var(--text-secondary);
    border: 1px solid rgba(194, 198, 213, 0.35);
    transition: background var(--transition-fast), color var(--transition-fast);
}
.compass-turn-pip:hover {
    background: var(--compass-teal-surface);
    color: var(--compass-teal-dark);
    text-decoration: none;
}
.compass-turn-pip-fail {
    background: var(--status-red-bg, #FEE2E2);
    color: var(--status-red-dark, #991B1B);
    border-color: #fca5a5;
}
.compass-turn-pip-fail:hover {
    background: #fca5a5;
    color: var(--status-red-dark, #991B1B);
}
.compass-turn-pip-pass {
    color: var(--status-green-dark);
}
.compass-turn-pip-neutral {
    color: var(--text-faint);
}

/* Turn-header verdict pill (right-aligned, replaces old feedback badge) */
.compass-turn-verdict-pill {
    margin-left: auto;
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 3px 8px;
    border-radius: 4px;
    white-space: nowrap;
}
.compass-turn-verdict-pass {
    background: var(--status-green-bg);
    color: var(--status-green-dark);
}
.compass-turn-verdict-fail {
    background: var(--status-red-bg, #FEE2E2);
    color: var(--status-red-dark, #991B1B);
}

/* ── 26t. Generic Badges ──────────────────────────────── */
.compass-badge {
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 3px 8px;
    border-radius: 5px;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    white-space: nowrap;
}
.compass-badge-approved {
    background: rgba(46, 160, 67, 0.12);
    color: var(--status-green);
}
.compass-badge-rejected {
    background: rgba(218, 54, 51, 0.12);
    color: var(--status-red);
}
.compass-badge-none {
    background: #eef0f2;
    color: var(--text-muted);
}
.compass-badge-thumbs-up {
    background: rgba(46, 160, 67, 0.10);
    color: var(--status-green);
}
.compass-badge-thumbs-down {
    background: rgba(218, 54, 51, 0.10);
    color: var(--status-red);
}

/* ── 26u. Convo list extras ───────────────────────────── */
.compass-convo-district {
    font-size: 11px;
    color: var(--text-muted);
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
/* C2 scannability: per-row turn-count chip. Muted, faint — a quiet
   differentiator for otherwise-identical prompts, not a loud badge. */
.compass-convo-count {
    font-size: 11px;
    color: var(--text-faint);
    white-space: nowrap;
}
/* "to" separator between the custom from/to date inputs (W2-1 upper bound). */
.range-custom-sep {
    font-size: 12px;
    color: var(--text-muted);
    padding: 0 2px;
}
/* Saved-snapshot data table + citation/CSV affordances rendered in the
   conversation detail (W2-0/W2-2/W2-3). Teal accent only — Compass body. */
.compass-snapshot-block {
    margin-top: 10px;
}
.compass-snapshot-table-label {
    font-size: 11px;
    font-weight: 600;
    color: var(--text-muted);
    margin-bottom: 4px;
}
.compass-snapshot-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
}
.compass-snapshot-table th,
.compass-snapshot-table td {
    border: 1px solid var(--border, #e2e8f0);
    padding: 4px 8px;
    text-align: left;
    vertical-align: top;
}
.compass-snapshot-table th {
    background: var(--compass-teal-surface);
    color: var(--compass-teal-dark);
    font-weight: 600;
}
.compass-snapshot-actions {
    margin-top: 6px;
}
/* Saved-snapshot chart (#1809) — a self-contained SVG bar/line chart rendered
   verbatim from result.chart, sitting above the grounded data table. */
.compass-snapshot-chart {
    margin: 4px 0 6px;
}
.compass-snapshot-chart-svg {
    width: 100%;
    height: auto;
    max-height: 320px;
}
.compass-chart-legend {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 12px;
    margin-top: 4px;
    font-size: 11px;
    color: var(--text-secondary);
}
.compass-chart-legend-item {
    display: inline-flex;
    align-items: center;
    gap: 5px;
}
.compass-chart-legend-swatch {
    display: inline-block;
    width: 10px;
    height: 10px;
    border-radius: 2px;
}
.compass-citation-link {
    color: var(--compass-teal-dark);
    text-decoration: underline;
}
.compass-citation-link:hover {
    color: var(--compass-teal);
}
.compass-csv-download {
    display: inline-block;
    font-size: 12px;
    font-weight: 600;
    color: var(--compass-teal-dark);
    text-decoration: none;
    padding: 4px 10px;
    border: 1px solid var(--compass-teal);
    border-radius: 6px;
}
.compass-csv-download:hover {
    background: var(--compass-teal-surface);
}
.compass-convo-badges {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    margin-top: 2px;
}
.compass-empty-sidebar {
    padding: 20px 18px;
    text-align: center;
}

/* ─── Compass Quality Scorecard ─────────────────────────────── */
/* Cluster-scoped via main[data-cluster="quality"].              */
/* Print-friendly: Scorecard hero renders as clean 1-2 pages.   */

/* Breadcrumb strip */
.quality-breadcrumb-strip {
    font-size: var(--text-sm);
    color: var(--text-secondary);
    margin-bottom: 16px;
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0;
}
.quality-breadcrumb-link {
    color: var(--brand-blue);
    text-decoration: none;
}
.quality-breadcrumb-link:hover { text-decoration: underline; }
.quality-breadcrumb-sep { color: var(--text-faint); margin: 0 4px; }
.quality-breadcrumb-current { font-weight: 600; color: var(--text-primary); }

/* Build context strip */
.quality-context-strip {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    padding: 8px 0 16px;
    border-bottom: 1px solid var(--border-light);
    margin-bottom: 20px;
}
.quality-context-chip {
    background: var(--bg-subtle);
    border: 1px solid var(--border-card);
    border-radius: 4px;
    padding: 3px 8px;
    font-size: var(--text-xs);
    display: inline-flex;
    gap: 4px;
    align-items: center;
}
.quality-context-label {
    color: var(--text-muted);
    font-weight: 500;
}
.quality-context-value {
    color: var(--text-primary);
    font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: 0.72rem;
}
.quality-last-sweep-strip {
    display: flex;
    gap: 6px;
    align-items: baseline;
    padding: 0 0 16px;
    font-size: var(--text-xs);
}
.quality-last-sweep-label {
    color: var(--text-muted);
    font-weight: 500;
}
.quality-last-sweep-value {
    color: var(--text-primary);
    font-weight: 600;
}

/* Scorecard hero table */
.quality-scorecard-table {
    width: 100%;
    border-collapse: collapse;
    font-family: Georgia, "Times New Roman", serif; /* visual sobriety */
    font-size: var(--text-sm);
    margin-bottom: 32px;
}
.quality-scorecard-table th {
    text-align: left;
    padding: 10px 12px;
    font-size: var(--text-xs);
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--text-muted);
    border-bottom: 2px solid var(--border-light);
    font-family: ui-sans-serif, system-ui, sans-serif;
}
.quality-dimension-row td { border-bottom: 1px solid var(--border-light); }
.quality-name-cell { padding: 14px 12px; width: 38%; }
.quality-score-cell { padding: 14px 12px; text-align: right; width: 10%; }
.quality-threshold-cell,
.quality-trend-cell,
.quality-exemplar-cell,
.quality-n-cell,
.quality-detail-cell {
    padding: 14px 12px;
    text-align: right;
    color: var(--text-muted);
    font-size: var(--text-xs);
    font-family: ui-sans-serif, system-ui, sans-serif;
}
.quality-n-cell { width: 8%; }
.quality-n-empty { color: var(--text-faint); }
.quality-detail-cell { text-align: left; width: 8%; }
.quality-detail-link { color: var(--brand-blue); text-decoration: none; font-weight: 600; }
.quality-detail-link:hover { text-decoration: underline; }
.quality-threshold-cell { width: 11%; }
.quality-threshold-value,
.quality-trend-value,
.quality-exemplar-count {
    display: block;
    color: var(--text-primary);
    font-weight: 700;
}
.quality-threshold-label,
.quality-exemplar-status {
    display: block;
    margin-top: 2px;
    color: var(--text-muted);
    font-size: 0.68rem;
}
.quality-threshold-pass .quality-threshold-label,
.quality-exemplar-complete { color: var(--status-green); }
.quality-threshold-fail .quality-threshold-label,
.quality-trend-negative,
.quality-exemplar-incomplete { color: var(--status-red); }
.quality-trend-positive { color: var(--status-green); }
.quality-trend-flat,
.quality-trend-empty { color: var(--text-muted); }

/* Dimension name + definition */
.quality-dim-link { color: var(--text-primary); text-decoration: none; font-weight: 600; }
.quality-dim-link:hover { color: var(--brand-blue); text-decoration: underline; }
.quality-dim-name { font-weight: 600; color: var(--text-primary); }
.quality-dim-definition { font-size: var(--text-xs); color: var(--text-muted); margin-top: 2px; font-family: ui-sans-serif, system-ui, sans-serif; }

/* Score display — no color on hero (sobriety); color is only on K3 squares */
.quality-score-pct { font-size: 1.1rem; font-weight: 700; color: var(--text-primary); }
.quality-no-data-dash { color: var(--text-faint); margin-right: 6px; }
.quality-no-data-pill {
    display: inline-block;
    padding: 1px 7px;
    border-radius: 10px;
    font-size: var(--text-xs);
    background: var(--bg-muted);
    color: var(--text-muted);
    font-family: ui-sans-serif, system-ui, sans-serif;
}

/* K=3 squares (drill-down only) */
.quality-k3-strip { display: flex; gap: 4px; align-items: center; }
.quality-k3-square {
    display: inline-block;
    width: 16px;
    height: 16px;
    border-radius: 3px;
    border: 1px solid rgba(0,0,0,0.08);
    cursor: default;
}
.quality-k3-pass { background: var(--status-green); }
.quality-k3-fail { background: var(--status-red); }
.quality-k3-error { background: var(--text-faint); }

/* Drill-down: case rows */
.quality-case-row { border-bottom: 1px solid var(--border-light); }
.quality-case-name { padding: 12px; font-size: var(--text-sm); }
.quality-case-scenario { font-size: var(--text-xs); color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 2px; }
.quality-case-name-text { font-weight: 600; }
.quality-case-rate { padding: 12px; text-align: right; font-weight: 700; font-size: var(--text-sm); }
.quality-case-k3 { padding: 12px; }
.quality-case-sessions { padding: 12px; font-size: var(--text-xs); color: var(--text-muted); }

/* Session links in drill-down */
.quality-session-link {
    font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: 0.72rem;
    color: var(--brand-blue);
    text-decoration: none;
}
.quality-session-link:hover { text-decoration: underline; }

/* Verdict list (<details>) */
.quality-verdict-list { margin: 8px 0; }
.quality-verdict-list summary {
    cursor: pointer;
    font-size: var(--text-sm);
    color: var(--text-secondary);
    padding: 4px 0;
    user-select: none;
}
.quality-verdict-list summary:hover { color: var(--text-primary); }
.quality-verdict-empty { color: var(--text-faint); font-size: var(--text-xs); padding: 8px 0; }
.quality-verdict-table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--text-xs);
    margin-top: 6px;
}
.quality-verdict-th {
    text-align: left;
    padding: 4px 8px;
    color: var(--text-muted);
    font-weight: 600;
    border-bottom: 1px solid var(--border-light);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-size: 0.68rem;
}
.quality-verdict-row td { padding: 5px 8px; border-bottom: 1px solid var(--border-light); }
.quality-verdict-outcome { font-weight: 600; white-space: nowrap; }
.quality-verdict-icon { margin-right: 4px; }
.quality-verdict-criterion { font-family: ui-monospace, SFMono-Regular, monospace; font-size: 0.72rem; }
.quality-verdict-source-badge {
    display: inline-block;
    padding: 1px 6px;
    border-radius: 3px;
    background: var(--bg-muted);
    color: var(--text-muted);
    font-size: 0.68rem;
}
.quality-verdict-reason { color: var(--text-secondary); }
.quality-verdict-pass { color: var(--status-green); }
.quality-verdict-fail { color: var(--status-red); }
.quality-verdict-error { color: var(--text-muted); }
/* Contract C-3: not-applicable verdict rows. Muted gray, explicitly NOT red —
   "N/A" is the absence of a signal, not a failure. The VERDICT agent applies
   this class to not-applicable rows; the icon inherits the same muted color. */
.quality-verdict-na { color: var(--text-muted); }
.quality-verdict-na .quality-verdict-icon { color: var(--text-faint); }

/* Conversation quality detail */
.quality-turn-block {
    border: 1px solid var(--border-card);
    border-radius: 6px;
    padding: 16px;
    margin-bottom: 16px;
    background: var(--bg-card);
}
.quality-turn-index {
    font-size: var(--text-xs);
    color: var(--text-muted);
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-bottom: 8px;
}
.quality-turn-user {
    background: var(--bg-subtle);
    border-left: 3px solid var(--brand-blue);
    padding: 8px 12px;
    border-radius: 3px;
    margin-bottom: 8px;
    font-size: var(--text-sm);
}
.quality-turn-assistant {
    padding: 8px 0;
    font-size: var(--text-sm);
    color: var(--text-secondary);
    line-height: var(--leading-relaxed); /* G5: dense raw-answer dump */
    white-space: pre-wrap;
    max-height: 200px;
    overflow-y: auto;
}

/* Reports queue (#1349 P2) — reviewer triage list over compass.case_reports.
   Reuses the scorecard table + verdict palette; only the queue-specific
   chrome (summary pills, filter bar, per-row status select) is new here. */
.quality-reports-wrap { margin-top: 12px; }
.quality-reports-summary {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 16px;
}
.quality-summary-pill {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 12px;
    background: var(--bg-muted);
    border: 1px solid var(--border-card);
    font-size: var(--text-xs);
    font-weight: 600;
    text-transform: capitalize;
}
.quality-summary-total {
    background: var(--bg-subtle);
    color: var(--text-secondary);
}
.quality-reports-filter-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    margin-bottom: 16px;
}
.quality-reports-filter {
    padding: 4px 8px;
    border: 1px solid var(--border-card);
    border-radius: 4px;
    font-size: var(--text-sm);
    background: var(--bg-card);
}
.quality-reports-filter-submit {
    padding: 4px 14px;
    border-radius: 4px;
    border: 1px solid var(--brand-blue);
    background: var(--brand-blue);
    color: #fff;
    font-size: var(--text-sm);
    cursor: pointer;
}
.quality-reports-filter-reset {
    font-size: var(--text-sm);
    color: var(--text-muted);
}
.quality-reports-table td { vertical-align: top; font-size: var(--text-sm); }
.quality-reports-created { white-space: nowrap; color: var(--text-muted); }
.quality-reports-outcome { font-weight: 600; text-transform: capitalize; }
.quality-reports-comments {
    max-width: 320px;
    color: var(--text-secondary);
}
.quality-reports-comment-details summary {
    cursor: pointer;
    display: flex;
    gap: 6px;
    align-items: baseline;
}
.quality-reports-comment-toggle {
    color: var(--brand-blue);
    font-weight: 600;
    white-space: nowrap;
}
.quality-reports-comment-full {
    margin-top: 6px;
    white-space: normal;
    overflow-wrap: anywhere;
}
.quality-reports-comment-link { color: var(--brand-blue); }
.quality-reports-status-select {
    padding: 2px 6px;
    border: 1px solid var(--border-card);
    border-radius: 4px;
    font-size: var(--text-xs);
    background: var(--bg-card);
}
.quality-reports-debug-link {
    white-space: nowrap;
    color: var(--brand-blue);
    font-weight: 600;
}
.quality-reports-banner {
    padding: 8px 12px;
    border-radius: 4px;
    margin-bottom: 12px;
    font-size: var(--text-sm);
}
.quality-reports-banner-ok {
    background: var(--bg-subtle);
    border-left: 3px solid var(--status-green);
}
.quality-reports-banner-error {
    background: var(--bg-subtle);
    border-left: 3px solid var(--status-red);
}
.quality-reports-convo-link {
    white-space: nowrap;
    color: var(--brand-blue);
    font-weight: 600;
}

/* Flag-for-review control (DASH-R6) — mounted at the bottom of the conversation
   detail pane. Part of the Quality cluster, so it stays on the deliberately
   sober brand blue (NOT the Compass-body teal), reusing the quality-reports-*
   banner tokens for its ok/error result. No new color tokens. */
.compass-flag-panel {
    margin-top: 20px;
    padding-top: 16px;
    border-top: 1px solid var(--border-card);
}
.compass-flag-heading {
    font-size: var(--text-sm);
    font-weight: 600;
    color: var(--text-secondary);
    margin-bottom: 8px;
}
.compass-flag-form {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-start;
    gap: 8px;
}
.compass-flag-outcome {
    padding: 6px 8px;
    border: 1px solid var(--border-card);
    border-radius: 4px;
    font-size: var(--text-sm);
    background: var(--bg-card);
}
.compass-flag-comments {
    flex: 1 1 260px;
    min-width: 200px;
    padding: 6px 8px;
    border: 1px solid var(--border-card);
    border-radius: 4px;
    font-size: var(--text-sm);
    background: var(--bg-card);
    resize: vertical;
}
.compass-flag-submit {
    padding: 6px 16px;
    border-radius: 4px;
    border: 1px solid var(--brand-blue);
    background: var(--brand-blue);
    color: #fff;
    font-size: var(--text-sm);
    font-weight: 600;
    cursor: pointer;
}
.compass-flag-status:empty { display: none; }
.compass-flag-status { margin-top: 8px; }

/* Print overrides — Scorecard hero prints as clean 1-2 page artifact */
@media print {
    .top-nav, .sub-nav, .quality-breadcrumb-strip { display: none; }
    .quality-scorecard-table { font-size: 10pt; }
    .quality-context-strip { font-size: 9pt; }
    .quality-dimension-row td { padding: 8px 4px; }
    .dashboard-footer { display: none; }
}

/* ── Site footer (NCTQ wordmark) ──────────────────────────────── */
.dashboard-footer {
    margin-top: 3rem;
    border-top: 1px solid var(--border-subtle, #e5e7eb);
    background: var(--bg-card, #ffffff);
    padding: 1.75rem 1.5rem;
}
.dashboard-footer__inner {
    max-width: 1200px;
    margin: 0 auto;
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 2.5rem;
    flex-wrap: wrap;
}
.dashboard-footer__brand {
    display: flex;
    flex-direction: column;
    gap: 6px;
}
.dashboard-footer__logo svg {
    height: 42px;
    width: auto;
    display: block;
}
/* Brand fills for the inline wordmark SVG — two-tone NCTQ mark:
   the "T"/wordmark (logo-primary) is dark navy; the "Q" swoosh
   (logo-secondary) is light blue. Exact brand hexes. */
.dashboard-footer .logo-primary { fill: #1b3862; }
.dashboard-footer .logo-secondary { fill: #98abce; }
.dashboard-footer__tagline {
    font-size: 0.8rem;
    font-weight: 600;
    color: var(--brand-navy);
}
.dashboard-footer__copyright {
    font-size: 0.75rem;
    color: var(--text-muted, #6b7280);
}
/* Two link columns */
.dashboard-footer__links {
    display: flex;
    gap: 3rem;
    flex-wrap: wrap;
}
.dashboard-footer__col {
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-width: 140px;
}
.dashboard-footer__col-title {
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--text-muted, #6b7280);
    margin-bottom: 2px;
}
.dashboard-footer__link {
    font-size: 0.85rem;
    color: var(--brand-navy);
    text-decoration: none;
}
.dashboard-footer__link:hover {
    color: var(--brand-blue);
    text-decoration: underline;
}
@media (max-width: 760px) {
    .dashboard-footer__inner { flex-direction: column; align-items: flex-start; gap: 1.75rem; }
    .dashboard-footer__links { gap: 2rem; }
}
""")
