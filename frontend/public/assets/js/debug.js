/**
 * Debug Mode for Compass
 *
 * Activated by ?debug=true URL parameter. Shows a sticky top bar with
 * scenario context, phase timing, critic verdict, cost, and Logfire trace link.
 * Degrades gracefully when data is unavailable.
 */

function escapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/** Check if debug mode is active (cached after first call). */
let _debugMode = null;
export function isDebugMode() {
  if (_debugMode === null) {
    _debugMode = new URL(window.location).searchParams.get("debug") === "true";
  }
  return _debugMode;
}

/** Read case_id from URL (may be null). */
export function getCaseId() {
  return new URL(window.location).searchParams.get("case_id");
}

export function buildCaseProxyParams(id, sourceSearch = null) {
  const params = new URLSearchParams();
  params.set("case_id", id);

  const source = new URLSearchParams(sourceSearch ?? window.location.search);
  const exp = source.get("case_exp");
  const sig = source.get("case_sig");
  if (exp) params.set("case_exp", exp);
  if (sig) params.set("case_sig", sig);
  return params.toString();
}

export function buildDebugProxyParams(sessionId, sourceSearch = null) {
  const params = new URLSearchParams();
  params.set("session_id", sessionId);

  const source = new URLSearchParams(sourceSearch ?? window.location.search);
  const caseId = source.get("case_id");
  const exp = source.get("case_exp");
  const sig = source.get("case_sig");
  if (caseId) params.set("case_id", caseId);
  if (exp) params.set("case_exp", exp);
  if (sig) params.set("case_sig", sig);
  return params.toString();
}

/** Fetch B-spine case details via PHP proxy (adds server-side auth token). */
export async function fetchScenarioCase(id) {
  try {
    const resp = await fetch(`/api/scenario.php?${buildCaseProxyParams(id)}`);
    if (!resp.ok) return null;
    return await resp.json();
  } catch {
    return null;
  }
}

export function normalizeScenarioCaseSteps(scenarioCase) {
  if (!scenarioCase || typeof scenarioCase !== "object") return [];
  if (Array.isArray(scenarioCase.input_steps) && scenarioCase.input_steps.length > 0) {
    return scenarioCase.input_steps
      .filter((step) => step && typeof step.prompt === "string" && step.prompt.trim())
      .map((step) => ({
        prompt: step.prompt,
        step_complete_when: step.step_complete_when ?? null,
      }));
  }
  if (typeof scenarioCase.initial_user_message === "string" && scenarioCase.initial_user_message.trim()) {
    return [{
      prompt: scenarioCase.initial_user_message,
      step_complete_when: null,
    }];
  }
  return [];
}

export function legacyScenarioLaunchError(sourceSearch = null) {
  const source = new URLSearchParams(sourceSearch ?? window.location.search);
  if (source.get("case_id") || !source.get("scenario_id")) return null;
  return {
    type: "error",
    feature: "B-spine cutover",
    scenario_title: "B-spine case_id required",
    what_we_are_testing: "Legacy scenario_id debug links are no longer supported.",
    expected_behaviour: "Use a /compass/scenarios launch link with case_id.",
    input_steps: [],
  };
}

/** Mutable state for the current debug session. */
export const debugState = {
  traceId: null,
  timing: null,
  criticVerdict: null,
  routing: null,
  sessionId: null,
};

/** Reset debug state for a new conversation. */
export function resetDebugState() {
  debugState.traceId = null;
  debugState.timing = null;
  debugState.criticVerdict = null;
  debugState.routing = null;
  debugState.sessionId = null;
}

// ── Debug Bar DOM ───────────────────────────────────────

/**
 * Create the debug bar element. Case section is populated if case data is
 * provided; results section starts empty and fills after response.
 */
export function createDebugBar(scenario) {
  const bar = document.createElement("div");
  bar.id = "debugBar";
  bar.innerHTML = `
    <div class="debug-bar-inner">
      <div class="debug-header">
        <div class="debug-title">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="12" cy="12" r="10"/><path d="M12 8v4l3 3"/>
          </svg>
          Debug Mode
        </div>
        <div class="debug-links">
          <a id="debugLogfireLink" href="#" target="_blank" style="display:none">View in Logfire &nearr;</a>
          <span id="debugSessionId" class="debug-session-id" style="display:none"></span>
          <button class="debug-toggle" id="debugToggleBtn">Collapse</button>
        </div>
      </div>
      <div class="debug-body" id="debugBody">
        ${scenario ? renderScenarioContext(scenario) : ""}
        ${renderReviewWidget()}
        <div id="debugMetrics" class="debug-metrics">
          <div class="debug-waiting">Waiting for response&hellip;</div>
        </div>
      </div>
    </div>
  `;

  // Toggle collapse
  bar.querySelector("#debugToggleBtn").addEventListener("click", () => {
    const body = bar.querySelector("#debugBody");
    const btn = bar.querySelector("#debugToggleBtn");
    body.classList.toggle("collapsed");
    btn.textContent = body.classList.contains("collapsed") ? "Expand" : "Collapse";
  });

  // Copy session ID on click
  bar.querySelector("#debugSessionId")?.addEventListener("click", function () {
    navigator.clipboard?.writeText(this.textContent.trim());
    const orig = this.textContent;
    this.textContent = "Copied!";
    setTimeout(() => { this.textContent = orig; }, 1500);
  });

  return bar;
}

function renderScenarioContext(s) {
  const typeBadge = {
    golden: "badge-golden",
    regression: "badge-regression",
    boundary: "badge-boundary",
    dangerous: "badge-dangerous",
  }[s.type] || "badge-feature";

  return `
    <div class="debug-scenario">
      <span class="debug-label">Case</span>
      <span class="debug-value">
        <span class="debug-badges">
          <span class="debug-badge ${typeBadge}">${escapeHtml(s.type || "test")}</span>
          ${s.feature ? `<span class="debug-badge badge-feature">${escapeHtml(s.feature)}</span>` : ""}
        </span>
        ${escapeHtml(s.scenario_title || s.case_code || "Untitled")}
      </span>
      ${Array.isArray(s.input_steps) && s.input_steps.length > 1 ? `
        <span class="debug-label">Steps</span>
        <span class="debug-value">${escapeHtml(s.input_steps.length)}</span>
      ` : ""}
      ${s.what_we_are_testing ? `
        <span class="debug-label">Testing</span>
        <span class="debug-value">${escapeHtml(s.what_we_are_testing)}</span>
      ` : ""}
      ${s.expected_behaviour ? `
        <span class="debug-label">Expected</span>
        <span class="debug-value">${escapeHtml(s.expected_behaviour)}</span>
      ` : ""}
    </div>
  `;
}

// ── Review Widget ("Did that happen?") ──────────────────

/**
 * Static DOM for the reviewer report widget. The outcome buttons start
 * unselected and the notes/save row is hidden until an outcome is chosen.
 * Behaviour (existing-report lookup, button wiring, POST) is attached later
 * by initReviewWidget once the session/case context is known.
 */
function renderReviewWidget() {
  return `
    <div id="reviewWidget" class="debug-review">
      <span class="debug-label">Did that happen?</span>
      <div class="debug-review-body">
        <div class="debug-review-buttons" id="reviewButtons">
          <button type="button" class="debug-review-btn" data-outcome="pass">Pass</button>
          <button type="button" class="debug-review-btn" data-outcome="partial">Partial</button>
          <button type="button" class="debug-review-btn" data-outcome="fail">Fail</button>
        </div>
        <div class="debug-review-form" id="reviewForm" style="display:none">
          <textarea id="reviewComments" class="debug-review-comments" rows="2"
            placeholder="Optional notes&hellip;"></textarea>
          <button type="button" class="debug-review-save" id="reviewSaveBtn">Save</button>
        </div>
        <span id="reviewStatus" class="debug-review-status" style="display:none"></span>
      </div>
    </div>
  `;
}

/**
 * Wire the review widget once the turn's session/case context is known.
 *
 * `ctx` carries { sessionId, caseId, traceId, dimension }. On init we GET any
 * existing report for this session/case; if one exists we render its saved
 * state (outcome + status + comments) instead of the blank form. Otherwise the
 * outcome buttons reveal a notes textarea + Save, and Save POSTs the report.
 */
export async function initReviewWidget(bar, ctx = {}) {
  const widget = bar.querySelector("#reviewWidget");
  if (!widget) return;

  const { sessionId, caseId, traceId, dimension } = ctx;
  if (!sessionId) return; // No session means nothing to report against yet.

  const buttonsEl = widget.querySelector("#reviewButtons");
  const formEl = widget.querySelector("#reviewForm");
  const commentsEl = widget.querySelector("#reviewComments");
  const saveBtn = widget.querySelector("#reviewSaveBtn");
  const statusEl = widget.querySelector("#reviewStatus");

  let selectedOutcome = null;

  const showStatus = (text, kind) => {
    statusEl.textContent = text;
    statusEl.className = "debug-review-status" + (kind ? " " + kind : "");
    statusEl.style.display = "";
  };

  // Best-effort "Next →" link when the debug URL carries a review-set cursor.
  const source = new URLSearchParams(window.location.search);
  const reviewSet = source.get("review_set");
  const pos = source.get("pos");
  if (reviewSet) {
    const next = document.createElement("a");
    next.className = "debug-review-next";
    const nextParams = new URLSearchParams(source);
    const nextPos = Number.parseInt(pos ?? "0", 10);
    if (Number.isFinite(nextPos)) nextParams.set("pos", String(nextPos + 1));
    next.href = "?" + nextParams.toString();
    next.textContent = "Next →";
    widget.querySelector(".debug-review-body").appendChild(next);
  }

  const selectOutcome = (outcome) => {
    selectedOutcome = outcome;
    buttonsEl.querySelectorAll(".debug-review-btn").forEach((btn) => {
      btn.classList.toggle("selected", btn.dataset.outcome === outcome);
    });
    formEl.style.display = "";
  };

  // Existing-report lookup: render saved ticket state if one is already filed.
  try {
    const params = new URLSearchParams({ session_id: sessionId });
    if (caseId) params.set("case_id", caseId);
    const resp = await fetch(`/api/report.php?${params.toString()}`);
    if (resp.ok) {
      const report = await resp.json();
      if (report && report.outcome) {
        selectOutcome(report.outcome);
        if (report.comments) commentsEl.value = report.comments;
        showStatus(
          `Saved ✓ (${report.outcome}${report.status ? " · " + report.status : ""})`,
          "saved",
        );
      }
    }
  } catch {
    // Graceful degradation — fall through to the blank form.
  }

  buttonsEl.querySelectorAll(".debug-review-btn").forEach((btn) => {
    btn.addEventListener("click", () => selectOutcome(btn.dataset.outcome));
  });

  saveBtn.addEventListener("click", async () => {
    if (!selectedOutcome) return;
    saveBtn.disabled = true;
    const payload = {
      session_id: sessionId,
      case_id: caseId ? Number.parseInt(caseId, 10) : null,
      trace_id: traceId ?? null,
      dimension: dimension ?? null,
      outcome: selectedOutcome,
      comments: commentsEl.value.trim() || null,
    };
    try {
      const resp = await fetch("/api/report.php", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (resp.ok) {
        showStatus("Saved ✓", "saved");
      } else {
        showStatus("Save failed", "error");
      }
    } catch {
      showStatus("Save failed", "error");
    } finally {
      saveBtn.disabled = false;
    }
  });
}

/**
 * Fill in the debug bar with results after the response completes.
 * Any missing data is handled gracefully (section just doesn't render).
 */
export function fillDebugResults(bar) {
  const { timing, traceId, criticVerdict, routing, sessionId } = debugState;

  // Update Logfire link
  if (traceId) {
    const link = bar.querySelector("#debugLogfireLink");
    const base = window.APP_CONFIG?.logfireBaseUrl || "";
    if (base) {
      link.href = `${base}/live?traceId=${traceId}`;
    } else {
      link.href = "#";
      link.title = traceId;
    }
    link.style.display = "";
  }

  // Update session ID
  if (sessionId) {
    const el = bar.querySelector("#debugSessionId");
    el.textContent = sessionId;
    el.style.display = "";
  }

  // Build the metric cards
  const metricsEl = bar.querySelector("#debugMetrics");
  metricsEl.innerHTML = `
    ${renderRoutingCard(routing)}
    ${timing ? renderTimingCard(timing) : ""}
    ${criticVerdict ? renderVerdictCard(criticVerdict) : ""}
    ${timing ? renderCostPlaceholder() : ""}
  `;
}

function normalizeRouting(routing) {
  if (!routing || typeof routing !== "object") return null;
  const engine = routing.engine || null;
  const v3Mode = routing.v3_mode || routing.v3Mode || null;
  const fallbackReason = routing.fallback_reason ?? routing.fallbackReason ?? null;
  const dataSource = routing.v3_data_source || routing.v3DataSource || null;

  if (!engine && !v3Mode && !fallbackReason && !dataSource) return null;
  return { engine, v3Mode, fallbackReason, dataSource };
}

function humanizeToken(value) {
  if (!value) return "";
  return String(value).replaceAll("_", " ");
}

export function renderRoutingCard(routing) {
  const normalized = normalizeRouting(routing);
  if (!normalized) return "";

  const { engine, v3Mode, fallbackReason, dataSource } = normalized;
  const isV3 = engine === "compass_v3";
  const isFallback = engine === "classic" && fallbackReason;
  const badgeClass = isV3 ? "routing-v3" : (isFallback ? "routing-fallback" : "routing-classic");
  const badgeLabel = isV3 ? "Compass v3" : (isFallback ? "Classic fallback" : humanizeToken(engine || "Unknown"));

  return `
    <div class="debug-card">
      <h3>Routing</h3>
      <div class="debug-routing-badge ${badgeClass}">${escapeHtml(badgeLabel)}</div>
      <div class="debug-routing-detail">
        ${engine ? renderRoutingRow("Engine", humanizeToken(engine)) : ""}
        ${v3Mode ? renderRoutingRow("Mode", humanizeToken(v3Mode)) : ""}
        ${dataSource ? renderRoutingRow("Data", humanizeToken(dataSource)) : ""}
        ${fallbackReason ? renderRoutingRow("Reason", humanizeToken(fallbackReason)) : ""}
      </div>
    </div>
  `;
}

function renderRoutingRow(label, value) {
  return `
    <div class="debug-cost-row">
      <span>${escapeHtml(label)}</span>
      <span class="debug-mono">${escapeHtml(value)}</span>
    </div>
  `;
}

function renderTimingCard(t) {
  const total = t.total_ms || (
    (t.concierge_ms || 0) + (t.generator_ms || 0) +
    (t.enrichment_ms || 0) + (t.writer_critic_ms || 0)
  );
  const pct = (ms) => total > 0 ? ((ms / total) * 100).toFixed(1) : 0;
  const fmt = (ms) => ms >= 1000 ? (ms / 1000).toFixed(1) + "s" : ms + "ms";

  return `
    <div class="debug-card">
      <h3>Phase Timing</h3>
      ${renderTimingRow("Concierge", t.concierge_ms, pct(t.concierge_ms || 0), "concierge")}
      ${renderTimingRow("Generator", t.generator_ms, pct(t.generator_ms || 0), "generator")}
      ${renderTimingRow("Enrichment", t.enrichment_ms, pct(t.enrichment_ms || 0), "enrichment")}
      ${renderTimingRow("Writer+Critic", t.writer_critic_ms, pct(t.writer_critic_ms || 0), "writer")}
      <div class="debug-timing-total">
        <span>Total wall time</span>
        <span class="debug-mono">${fmt(total)}</span>
      </div>
    </div>
  `;
}

function renderTimingRow(name, ms, pct, colorClass) {
  if (ms == null) return "";
  const fmt = ms >= 1000 ? (ms / 1000).toFixed(1) + "s" : ms + "ms";
  return `
    <div class="debug-timing-row">
      <span class="debug-timing-name">${name}</span>
      <div class="debug-timing-track">
        <div class="debug-timing-fill ${colorClass}" style="width:${pct}%"></div>
      </div>
      <span class="debug-mono debug-timing-ms">${fmt}</span>
    </div>
  `;
}

function renderVerdictCard(v) {
  const badgeClass = v.approved ? "verdict-approved" : "verdict-rejected";
  const badgeIcon = v.approved
    ? '<svg width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M3 8l3.5 3.5L13 5" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/></svg>'
    : '<svg width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/></svg>';
  const scores = v.quality_scores || {};

  return `
    <div class="debug-card">
      <h3>Critic Verdict</h3>
      <div class="debug-verdict-badge ${badgeClass}">
        ${badgeIcon} ${v.approved ? "Approved" : "Rejected"}
      </div>
      ${renderScoreRow("Accuracy", scores.accuracy)}
      ${renderScoreRow("Complete", scores.completeness)}
      ${renderScoreRow("Citations", scores.citation_quality)}
      ${renderScoreRow("Tone", scores.tone)}
      ${renderScoreRow("Clarity", scores.clarity)}
      <div class="debug-quality-footer">
        <span>Avg: <strong>${v.average ?? "—"}</strong></span>
        <span>Attempt: <strong>${v.attempt ?? "—"}</strong></span>
        ${v.flags?.length ? `<span>Flags: <strong>${v.flags.length}</strong></span>` : ""}
      </div>
    </div>
  `;
}

function renderScoreRow(label, score) {
  if (score == null) return "";
  const dots = Array.from({ length: 5 }, (_, i) =>
    `<div class="debug-score-dot${i < score ? " filled" : ""}"></div>`
  ).join("");
  return `
    <div class="debug-score-row">
      <span class="debug-score-label">${label}</span>
      <div class="debug-score-dots">${dots}</div>
      <span class="debug-mono">${score}</span>
    </div>
  `;
}

function renderCostPlaceholder() {
  // Cost data comes from the debug API endpoint (historical).
  // For live sessions, we don't have cost data until after persistence.
  // Show a placeholder that can be populated via fetchDebugData().
  return `
    <div class="debug-card" id="debugCostCard">
      <h3>Cost &amp; Brief</h3>
      <div class="debug-cost-loading">Available after reload</div>
    </div>
  `;
}

/**
 * For historical debug: fetch assembled debug data and update the cost card.
 */
export async function fetchAndFillCostData(bar, sessionId) {
  try {
    const resp = await fetch(`/api/debug.php?${buildDebugProxyParams(sessionId)}`);
    if (!resp.ok) return;
    const data = await resp.json();

    const costCard = bar.querySelector("#debugCostCard");
    if (!costCard || !data.usage_totals) return;

    const t = data.usage_totals;
    costCard.innerHTML = `
      <h3>Cost &amp; Brief</h3>
      <div class="debug-cost-headline">$${t.cost_usd.toFixed(3)}</div>
      <div class="debug-cost-detail">
        <div class="debug-cost-row"><span>Input tokens</span><span class="debug-mono">${t.input_tokens.toLocaleString()}</span></div>
        <div class="debug-cost-row"><span>Output tokens</span><span class="debug-mono">${t.output_tokens.toLocaleString()}</span></div>
        <div class="debug-cost-row"><span>Cache read</span><span class="debug-mono">${t.cache_read_tokens.toLocaleString()}</span></div>
        <div class="debug-cost-row"><span>API requests</span><span class="debug-mono">${t.requests}</span></div>
      </div>
    `;

    // Also populate routing info from verdicts if available
    if (data.verdicts?.length) {
      const v = data.verdicts[data.verdicts.length - 1];
      const brief = v.policy_brief;
      if (brief) {
        const routingDiv = document.createElement("div");
        routingDiv.className = "debug-routing-info";
        const tags = [];
        if (brief.intent) tags.push(brief.intent);
        if (brief.complexity) tags.push(brief.complexity);
        routingDiv.innerHTML = `
          ${tags.length ? `<div>${tags.map(t => `<span class="debug-tag">${escapeHtml(t)}</span>`).join("")}</div>` : ""}
          ${brief.district_ids?.length ? `<div>Districts: ${brief.district_ids.map(d => `<span class="debug-tag">${escapeHtml(d)}</span>`).join("")}</div>` : ""}
          ${brief.academic_year ? `<div>Year: <span class="debug-tag">${escapeHtml(brief.academic_year)}</span></div>` : ""}
        `;
        costCard.appendChild(routingDiv);
      }
    }
  } catch {
    // Graceful degradation
  }
}
