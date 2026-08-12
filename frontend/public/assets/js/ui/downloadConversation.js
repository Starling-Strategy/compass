/**
 * Conversation download payload builder + downloader.
 *
 * Builds a {session_id, exported_at, turns} payload from the in-memory
 * conversation and POSTs it to /api/conversations-export.php (the PHP
 * proxy that injects FASTAPI_API_TOKEN and forwards to FastAPI's
 * /api/v1/conversations/export). The response is a ZIP blob; we trigger
 * a browser download via an anchor element.
 *
 * Errors propagate to the caller — let the button UI decide how to surface them.
 */

import { buildExportFilename } from "../utils/csvExport.js";

/**
 * Returns true iff the conversation has at least one assistant message
 * with exportable artifacts (metrics or charts).
 *
 * @param {object|null|undefined} conversation
 * @returns {boolean}
 */
export function conversationHasExportableData(conversation) {
  const messages = conversation?.messages;
  if (!Array.isArray(messages) || messages.length === 0) return false;

  for (const msg of messages) {
    if (!msg || msg.role !== "assistant") continue;
    const exportData = msg.exportData;
    if (!exportData) continue;
    const metricsLen = Array.isArray(exportData.metrics) ? exportData.metrics.length : 0;
    const chartsLen = Array.isArray(exportData.charts) ? exportData.charts.length : 0;
    if (metricsLen > 0 || chartsLen > 0) return true;
  }
  return false;
}

/**
 * Derive a human-friendly label for a metric event.
 * @param {object} metricEvent  raw MetricResultData payload {type, results: [...]}
 * @returns {string}
 */
function deriveTableLabel(metricEvent) {
  const results = metricEvent?.results;
  if (Array.isArray(results) && results.length > 0) {
    const first = results[0];
    if (first && typeof first.metric_name === "string" && first.metric_name.trim()) {
      return first.metric_name.trim();
    }
  }
  const csvRows = metricEvent?.csv_export?.rows;
  if (Array.isArray(csvRows) && csvRows.length > 0) {
    const first = csvRows[0];
    if (first && typeof first.metric_name === "string" && first.metric_name.trim()) {
      return first.metric_name.trim();
    }
  }
  const publicRows = metricEvent?.csv_export?.public_rows;
  if (Array.isArray(publicRows) && publicRows.length > 0) {
    const first = publicRows[0];
    if (first && typeof first.Measure === "string" && first.Measure.trim()) {
      return first.Measure.trim();
    }
  }
  return "Metric data";
}

/**
 * Derive a label for a chart payload.
 * @param {object} chart  raw backend ChartData dict
 * @returns {string}
 */
function deriveChartLabel(chart) {
  if (!chart || typeof chart !== "object") return "Chart";
  if (typeof chart.title === "string" && chart.title.trim()) return chart.title.trim();
  if (typeof chart.id === "string" && chart.id.trim()) return chart.id.trim();
  return "Chart";
}

/**
 * Build the export payload from in-memory conversation state.
 *
 * Pairs each user message with the immediately-following assistant message
 * to form a turn. Orphan messages (user without assistant, or assistant
 * without preceding user) are skipped.
 *
 * @param {object} conversation
 * @returns {{session_id: string, exported_at: string, turns: Array}}
 * @throws {Error} if no usable session_id can be determined.
 */
export function buildExportPayload(conversation) {
  const messages = Array.isArray(conversation?.messages) ? conversation.messages : [];

  // Resolve session_id: prefer conversation.session_id, then last assistant
  // message's session_id, then conversation.id.
  let sessionId = conversation?.session_id;
  if (!sessionId) {
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      if (m && m.role === "assistant" && typeof m.session_id === "string" && m.session_id) {
        sessionId = m.session_id;
        break;
      }
    }
  }
  if (!sessionId && conversation && typeof conversation.id === "string") {
    sessionId = conversation.id;
  }
  if (typeof sessionId !== "string") sessionId = "";
  sessionId = sessionId.trim();
  if (!sessionId || sessionId.length > 128) {
    throw new Error("Cannot export: missing session_id");
  }

  const turns = [];
  for (let i = 0; i < messages.length; i++) {
    const userMsg = messages[i];
    if (!userMsg || userMsg.role !== "user") continue;
    const assistantMsg = messages[i + 1];
    if (!assistantMsg || assistantMsg.role !== "assistant") continue;

    const tables = [];
    const charts = [];

    const exportData = assistantMsg.exportData;
    if (exportData) {
      // Tables: drill into MetricResultData.results
      if (Array.isArray(exportData.metrics)) {
        for (const metricEvent of exportData.metrics) {
          const csvExport = metricEvent?.csv_export || metricEvent?.result?.csv_export;
          if (
            csvExport &&
            csvExportRows(csvExport).length > 0
          ) {
            const table = {
              label: deriveTableLabel(metricEvent),
              columns: csvExportColumns(csvExport),
              rows: csvExportRows(csvExport),
            };
            // When the SSE artifact carries the pruned public projection, pass
            // its exact field names through so the backend ZIP writer can
            // prefer them (frontend_contracts.py reads public_columns/public_rows).
            // Additive only — columns/rows above stay as the fallback.
            const projection = publicProjection(csvExport);
            if (projection) {
              table.public_columns = projection.public_columns;
              table.public_rows = projection.public_rows;
            }
            tables.push(table);
            continue;
          }
          const results = metricEvent?.results;
          if (Array.isArray(results) && results.length > 0) {
            tables.push({
              label: deriveTableLabel(metricEvent),
              rows: results,
            });
          }
        }
      }
      // Charts: pass through entire ChartData dict
      if (Array.isArray(exportData.charts)) {
        for (const chart of exportData.charts) {
          if (!chart || typeof chart !== "object") continue;
          charts.push({
            label: deriveChartLabel(chart),
            data: chart,
          });
        }
      }
    }

    turns.push({
      user_text: typeof userMsg.content === "string" ? userMsg.content : "",
      assistant_text: typeof assistantMsg.content === "string" ? assistantMsg.content : "",
      tables,
      charts,
      citations: Array.isArray(assistantMsg.citations) ? assistantMsg.citations : [],
    });

    // Advance past the assistant message we just paired.
    i += 1;
  }

  return {
    session_id: sessionId,
    exported_at: new Date().toISOString(),
    turns,
  };
}

/**
 * Return the pruned public projection ({public_columns, public_rows}) verbatim
 * when both are present and non-empty; otherwise null. Used to attach the
 * backend-preferred field names to the ZIP table payload without collapsing
 * them into columns/rows.
 */
function publicProjection(csvExport) {
  const cols = csvExport?.public_columns;
  const rows = csvExport?.public_rows;
  if (
    Array.isArray(cols) && cols.length > 0 &&
    Array.isArray(rows) && rows.length > 0
  ) {
    return { public_columns: cols, public_rows: rows };
  }
  return null;
}

function csvExportColumns(csvExport) {
  if (
    Array.isArray(csvExport?.public_columns) &&
    csvExport.public_columns.length > 0 &&
    Array.isArray(csvExport?.public_rows) &&
    csvExport.public_rows.length > 0
  ) {
    return csvExport.public_columns;
  }
  return Array.isArray(csvExport?.columns) ? csvExport.columns : [];
}

function csvExportRows(csvExport) {
  if (
    Array.isArray(csvExport?.public_columns) &&
    csvExport.public_columns.length > 0 &&
    Array.isArray(csvExport?.public_rows) &&
    csvExport.public_rows.length > 0
  ) {
    return csvExport.public_rows;
  }
  return Array.isArray(csvExport?.rows) ? csvExport.rows : [];
}

/**
 * Parse a Content-Disposition header value and extract the filename.
 * Falls back to a default if the header is missing/unparseable.
 *
 * @param {string|null} headerValue
 * @returns {string}
 */
function extractFilename(headerValue) {
  // The backend normally sets Content-Disposition via _export_filename, so this
  // fallback rarely fires. Use the shared shape with a generic title (no
  // conversation-level human title is in scope here — do not invent one).
  const fallback = buildExportFilename({ title: "Conversation export", ext: "zip" });
  if (!headerValue || typeof headerValue !== "string") return fallback;
  // Try filename*= (RFC 5987) first, then quoted filename=, then unquoted.
  const starMatch = headerValue.match(/filename\*=(?:UTF-8'')?([^;]+)/i);
  if (starMatch && starMatch[1]) {
    try {
      return decodeURIComponent(starMatch[1].trim());
    } catch {
      // fall through
    }
  }
  const quotedMatch = headerValue.match(/filename="([^"]+)"/i);
  if (quotedMatch && quotedMatch[1]) return quotedMatch[1];
  const bareMatch = headerValue.match(/filename=([^;]+)/i);
  if (bareMatch && bareMatch[1]) return bareMatch[1].trim();
  return fallback;
}

/**
 * Download the conversation as a ZIP bundle.
 *
 * @param {object} conversation
 * @returns {Promise<void>}
 * @throws {Error} on payload-build failure or non-2xx response.
 */
export async function downloadConversation(conversation) {
  const payload = buildExportPayload(conversation);

  const response = await fetch("/api/conversations-export.php", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    let message = response.statusText || `HTTP ${response.status}`;
    try {
      const errBody = await response.json();
      if (errBody && (errBody.detail || errBody.error)) {
        message = errBody.detail || errBody.error;
      }
    } catch {
      // body wasn't JSON; keep statusText
    }
    throw new Error(message);
  }

  const filename = extractFilename(response.headers.get("Content-Disposition"));
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);

  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename;
  anchor.style.display = "none";
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);

  // Revoke the URL after a tick so the download has a chance to start.
  setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
}
