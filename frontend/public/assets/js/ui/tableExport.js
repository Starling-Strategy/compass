/**
 * Per-table CSV export buttons.
 * Mirrors the per-chart export pattern in chartRenderer.js.
 */

import {
  downloadCSV,
  generateMetricCSV,
  buildExportFilename,
  neutralizeFormula,
} from "../utils/csvExport.js";

/**
 * Scan a container for <table> elements and add a CSV download button
 * below each. When the turn has structured metric data, the button
 * exports the full underlying csv_payload (all columns, all rows);
 * otherwise it falls back to scraping the rendered table.
 *
 * @param {HTMLElement} container - The message bubble to scan
 * @param {string} sessionId - Session ID for filename
 * @param {object} [exportData] - { metrics: [{type, results: [...]}], charts: [...] }
 */
export function addTableExportButtons(container, sessionId, exportData) {
  const tables = container.querySelectorAll("table");
  const metrics = (exportData && exportData.metrics) || [];
  const exportRefs = (exportData && exportData.exports) || [];

  if (metrics.length > 0 && tables.length !== metrics.length) {
    console.warn(
      `tableExport: ${tables.length} rendered tables vs ${metrics.length} metric payloads — index pairing may misalign`
    );
  }

  // Pair tables to metric payloads by render order: the Nth <table> in the
  // DOM corresponds to the Nth entry pushed into metricDataArray (see
  // app.js onData callback). This holds because the writer agent emits the
  // `data` SSE event for each metric block as it renders the corresponding
  // table in the markdown. If the writer ever interleaves narrative-only
  // tables or reorders, this pairing degrades silently — the warn() above
  // is the canary.
  tables.forEach((table, index) => {
    if (table.nextElementSibling?.classList.contains("table-export-btn")) return;
    const rows = extractTableData(table);
    if (rows.length <= 1) return;

    const btn = document.createElement("button");
    btn.className =
      "table-export-btn mt-3 mb-4 inline-flex items-center gap-2 px-3 py-1.5 text-xs font-medium text-text-chart bg-white hover:bg-text-chart hover:text-white border border-text-chart transition-all duration-200";

    // Pick the structured payload for this table by index when available.
    const structured =
      metrics[index] ||
      (
        metrics.length === 1 &&
        metrics[0]?.csv_export &&
        Array.isArray(metrics[0].csv_export.rows) &&
        metrics[0].csv_export.rows.length > 0
          ? metrics[0]
          : null
      );
    const lazyExport = exportRefs[index] || (exportRefs.length === 1 ? exportRefs[0] : null);
    const useStructured =
      structured &&
      (
        (Array.isArray(structured.results) && structured.results.length > 0) ||
        (
          structured.csv_export &&
          Array.isArray(structured.csv_export.rows) &&
          structured.csv_export.rows.length > 0
        )
      );
    const useLazy = lazyExport && lazyExport.message_id && lazyExport.export_id;

    btn.innerHTML = `
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
        <polyline points="7 10 12 15 17 10"></polyline>
        <line x1="12" y1="15" x2="12" y2="3"></line>
      </svg>
      ${useStructured || useLazy ? "Export complete data (CSV)" : "Export CSV"}
    `;

    btn.onclick = () => {
      if (useLazy) {
        exportLazyTable(lazyExport, table, index, sessionId, btn);
      } else if (useStructured) {
        exportStructuredTable(structured, table, index, sessionId);
      } else {
        exportTableToCsv(table, index, sessionId);
      }
    };
    table.after(btn);
  });
}

async function exportLazyTable(exportRef, table, index, sessionId, btn) {
  const originalHtml = btn.innerHTML;
  try {
    btn.disabled = true;
    btn.textContent = "Preparing CSV...";
    const url =
      `/api/message-export.php?message_id=${encodeURIComponent(exportRef.message_id)}` +
      `&export_id=${encodeURIComponent(exportRef.export_id)}`;
    const response = await fetch(url);
    if (!response.ok) throw new Error(`Export failed with status ${response.status}`);
    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    const filename = filenameFromDisposition(response.headers.get("Content-Disposition")) ||
      buildTableFilename(table, index, sessionId);
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = filename;
    anchor.style.display = "none";
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
    setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
  } catch (error) {
    console.error("Lazy CSV export failed:", error);
    exportTableToCsv(table, index, sessionId);
  } finally {
    btn.disabled = false;
    btn.innerHTML = originalHtml;
  }
}

function filenameFromDisposition(headerValue) {
  if (!headerValue || typeof headerValue !== "string") return null;
  const quoted = headerValue.match(/filename="([^"]+)"/i);
  if (quoted && quoted[1]) return quoted[1];
  const bare = headerValue.match(/filename=([^;]+)/i);
  return bare && bare[1] ? bare[1].trim() : null;
}

function buildTableFilename(table, index, _sessionId) {
  // Best-available human title: the table's preceding heading; fall back to the
  // existing short slug, then a positional "Table N" when no heading exists.
  const heading = findPrecedingHeading(table);
  const title = heading || twoWordSlug(heading) || `Table ${index + 1}`;
  return buildExportFilename({ title, ext: "csv" });
}

function exportStructuredTable(structured, table, index, sessionId) {
  try {
    // generateMetricCSV expects an array of MetricResultData objects
    // (each with .results), NOT a raw results array. Wrap in an array.
    const csv = generateMetricCSV([structured], true);
    const filename = buildTableFilename(table, index, sessionId);
    downloadCSV(csv, filename);
  } catch (error) {
    console.error("Structured CSV export failed:", error);
  }
}

/**
 * Extract all rows (header + body) from a table element as arrays of clean text.
 */
function extractTableData(table) {
  const data = [];

  // Headers
  const headerCells = table.querySelectorAll("thead th");
  if (headerCells.length > 0) {
    data.push([...headerCells].map((th) => cleanCellText(th.textContent)));
  }

  // Body rows (or all tr if no thead)
  const bodyRows =
    headerCells.length > 0
      ? table.querySelectorAll("tbody tr")
      : table.querySelectorAll("tr");

  bodyRows.forEach((tr, i) => {
    const cells = tr.querySelectorAll("td, th");
    // Skip the first row if it's headers and we didn't find thead
    if (headerCells.length === 0 && i === 0 && tr.querySelector("th")) {
      data.push([...cells].map((c) => cleanCellText(c.textContent)));
    } else {
      data.push([...cells].map((c) => cleanCellText(c.textContent)));
    }
  });

  return data;
}

/**
 * Strip citation markers [N], "View source N" text, and excess whitespace.
 */
function cleanCellText(text) {
  return text
    .replace(/View source [\d,\s]+/g, "") // "View source 34,35"
    .replace(/\s*\[[\d,\s]+\]/g, "") // citation markers [1] or [1,2]
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * RFC 4180 CSV field escaping.
 */
function escapeCSVField(value) {
  if (value == null) return "";
  // Neutralize spreadsheet formula-injection BEFORE RFC-4180 quoting, using
  // the same shared predicate as csvExport.js / the backend ZIP writer.
  const str = neutralizeFormula(value);
  if (
    str.includes(",") ||
    str.includes('"') ||
    str.includes("\n") ||
    str.includes("\r")
  ) {
    return '"' + str.replace(/"/g, '""') + '"';
  }
  return str;
}

/**
 * Find the heading element immediately before a table to use as filename hint.
 */
function findPrecedingHeading(table) {
  let el = table.previousElementSibling;
  while (el) {
    if (/^H[1-6]$/.test(el.tagName)) {
      return el.textContent.trim();
    }
    // Don't look too far back
    if (el.tagName === "TABLE" || el.classList?.contains("table-export-btn")) {
      break;
    }
    el = el.previousElementSibling;
  }
  return null;
}

/**
 * Extract two meaningful words from a heading for a short filename slug.
 */
function twoWordSlug(heading) {
  if (!heading) return null;
  const stopWords = new Set([
    "a", "an", "the", "and", "or", "of", "in", "for", "with", "to", "from",
    "by", "on", "at", "is", "are", "was", "were", "be", "been",
  ]);
  const words = heading
    .toLowerCase()
    .replace(/[^a-z0-9\s]/g, "")
    .split(/\s+/)
    .filter((w) => w.length > 1 && !stopWords.has(w));
  if (words.length === 0) return null;
  return words.slice(0, 2).join("-");
}

/**
 * Export a single table's data as CSV.
 */
function exportTableToCsv(table, index, sessionId) {
  try {
    const rows = extractTableData(table);
    const csv = rows.map((row) => row.map(escapeCSVField).join(",")).join("\n");

    const filename = buildTableFilename(table, index, sessionId);

    downloadCSV(csv, filename);
  } catch (error) {
    console.error("Table CSV export failed:", error);
  }
}
