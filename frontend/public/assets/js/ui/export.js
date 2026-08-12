/**
 * CSV Export functionality for metrics data.
 * Generates CSV client-side — no server round-trip needed.
 * Chart export is handled in chartRenderer.js.
 */

import { generateMetricCSV, downloadCSV, buildExportFilename } from "../utils/csvExport.js";

/**
 * Add export button for metrics data to a message container.
 * Charts have their own export buttons added by chartRenderer.js.
 */
export function addExportButton(container, exportData) {
  const hasMetrics = exportData.metrics && exportData.metrics.length > 0;
  if (!hasMetrics) return;

  const exportWrapper = document.createElement("div");
  exportWrapper.className = "mt-4 mb-2";

  const btnExport = document.createElement("button");
  btnExport.className = "inline-flex items-center gap-2 px-4 py-2 text-sm font-medium text-text-chart bg-white hover:bg-text-chart hover:text-white border border-text-charttransition-all duration-200 shadow-sm hover:shadow";
  btnExport.innerHTML = `
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
      <polyline points="7 10 12 15 17 10"></polyline>
      <line x1="12" y1="15" x2="12" y2="3"></line>
    </svg>
    Export Data (CSV)
  `;
  btnExport.onclick = () => exportMetricCSV(exportData.metrics);
  exportWrapper.appendChild(btnExport);

  container.appendChild(exportWrapper);
}

/**
 * Export metric comparison data as CSV (client-side generation).
 */
function exportMetricCSV(metricData) {
  try {
    // Normalize into array of MetricResultData-like objects
    let normalized = [];

    if (Array.isArray(metricData)) {
      metricData.forEach((item) => {
        if (item.results) {
          normalized.push(item);
        } else {
          // Direct metric data — wrap it
          normalized.push({ results: [item] });
        }
      });
    } else if (metricData.results) {
      normalized.push(metricData);
    } else {
      normalized.push({ results: [metricData] });
    }

    const csv = generateMetricCSV(normalized, true);
    // Best-available human title: the first metric's name, if any; otherwise
    // buildExportFilename drops the title segment (generic "NCTQ Compass - <date>").
    const title = firstMetricName(normalized);
    downloadCSV(csv, buildExportFilename({ title, ext: "csv" }));
  } catch (error) {
    console.error("Export failed:", error.message);
    alert("Failed to export CSV. Error: " + error.message);
  }
}

/**
 * First non-empty metric_name across the normalized metric payloads, used as
 * the human title for the export filename. Returns "" when none is present so
 * the filename builder falls back to its generic shape.
 */
function firstMetricName(normalized) {
  for (const item of normalized) {
    const results = Array.isArray(item?.results) ? item.results : [];
    for (const result of results) {
      const name = result?.metric_name;
      if (typeof name === "string" && name.trim()) return name.trim();
    }
  }
  return "";
}

/**
 * Restore export buttons when loading conversation from history.
 * Only restores metrics button — charts get their buttons from renderChart.
 */
export function restoreExportButton(container, exportData) {
  if (!exportData) return;

  const hasMetrics = exportData.metrics && exportData.metrics.length > 0;
  if (hasMetrics) {
    addExportButton(container, exportData);
  }
}
