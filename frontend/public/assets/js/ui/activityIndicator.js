/**
 * Live activity timeline for the streaming chat response.
 *
 * Driven directly by the backend's real pipeline events (relayed by the
 * proxy and dispatched in agentSSE.js):
 *   - stage_start  -> begin a row     (key "stage:<name>")
 *   - stage_end    -> complete a row  (real duration_ms)
 *   - tool_call_start/end -> begin/complete a tool row, result_summary as a pill
 *
 * The generic `detail` field (rendered as a pill) is fed by tool_call_end's
 * result_summary. The reducer keeps the `detail` event type for that.
 *
 * Rows are a flat timeline capped at the last MAX_ROWS; each new row marks
 * the previous active row done. State lives in a pure reducer (`nextSteps`)
 * so the logic is unit-testable without a DOM; the renderer is a thin map
 * from state to elements. The indicator is removed by app.js when the
 * answer text begins streaming.
 */

export const MAX_ROWS = 4;

export const STAGE_LABELS = {
  planner: "Planning your turn",
  catalog_resolve: "Resolving districts & metrics",
  execute: "Working out your answer",
  persist: "Saving",
};

const TOOL_LABELS = {
  search_official_catalog_candidates: "Searching the catalog",
};

export function friendlyToolLabel(toolName) {
  return TOOL_LABELS[toolName] || "Searching Compass";
}

export function formatDuration(ms) {
  if (typeof ms !== "number" || Number.isNaN(ms) || ms < 0) return "";
  return (ms / 1000).toFixed(1) + "s";
}

/**
 * Pure reducer. Returns a new steps array; never mutates the input.
 * event:
 *   { type: "begin",    key, label }
 *   { type: "complete", key, durationMs? }
 *   { type: "detail",   key, detail }
 */
export function nextSteps(steps, event) {
  const next = steps.map((s) => ({ ...s }));
  if (event.type === "begin") {
    for (const s of next) {
      if (s.status === "active") s.status = "done";
    }
    next.push({
      key: event.key,
      label: event.label,
      status: "active",
      detail: null,
      durationMs: null,
    });
    return next.slice(-MAX_ROWS);
  }
  if (event.type === "complete") {
    const row = next.find((s) => s.key === event.key);
    if (row) {
      row.status = "done";
      if (typeof event.durationMs === "number") row.durationMs = event.durationMs;
    }
    return next;
  }
  if (event.type === "detail") {
    const row = next.find((s) => s.key === event.key);
    if (row && event.detail) row.detail = event.detail;
    return next;
  }
  return next;
}

export function createActivityIndicator() {
  const container = document.createElement("div");
  container.className = "activity-indicator";

  const bar = document.createElement("div");
  bar.className = "activity-bar";

  const rowsEl = document.createElement("div");
  rowsEl.className = "activity-rows";

  container.appendChild(bar);
  container.appendChild(rowsEl);

  container._steps = [];
  container._rowsEl = rowsEl;
  return container;
}

export function applyActivityEvent(indicatorEl, event) {
  if (!indicatorEl) return;
  indicatorEl._steps = nextSteps(indicatorEl._steps || [], event);
  renderActivity(indicatorEl);
}

function renderActivity(indicatorEl) {
  const rowsEl = indicatorEl._rowsEl;
  rowsEl.innerHTML = "";
  for (const s of indicatorEl._steps) {
    const row = document.createElement("div");
    row.className = "activity-row " + s.status;

    const icon = document.createElement("span");
    if (s.status === "done") {
      icon.className = "activity-ic";
      icon.textContent = "✓";
    } else {
      icon.className = "activity-dot";
    }
    row.appendChild(icon);

    const label = document.createElement("span");
    label.className = "activity-label";
    label.textContent = s.label;
    row.appendChild(label);

    if (s.detail) {
      const pill = document.createElement("span");
      pill.className = "activity-pill";
      pill.textContent = s.detail;
      row.appendChild(pill);
    }

    const durText = formatDuration(s.durationMs);
    if (durText) {
      const dur = document.createElement("span");
      dur.className = "activity-dur";
      dur.textContent = durText;
      row.appendChild(dur);
    }

    rowsEl.appendChild(row);
  }
}
