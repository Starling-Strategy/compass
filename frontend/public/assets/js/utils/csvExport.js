/**
 * Client-side CSV generation for metric and chart data.
 * Replaces the PHP proxy (export.php) → FastAPI round-trip.
 *
 * Mirrors backend logic from:
 *   src/archive/compass_api/export/csv.py (legacy CSV generation)
 *   src/archive/compass_api/ina.py        (five-state coverage predicates + phrases)
 *
 * Browser CSV cell text MUST match the backend byte-for-byte. The five-state
 * model lives in the archived legacy INA module; this module ports the predicates and
 * renderer. Any drift here means CSV exports diverge from chat / table cells.
 */

// --- Five-state coverage handling (mirrors archived legacy INA logic) ---

export const INA_PHRASE = "Issue not addressed in the documents reviewed";

/**
 * True iff `value` represents an INA answer.
 * Recognizes raw "INA" (the v1 DB marker) and the TCD-API-mapped display
 * form "Issue not addressed [...]". The pipeline maps raw markers upstream
 * before Compass sees the row, so the predicate must match either form.
 */
export function isINA(value) {
  if (typeof value !== "string") return false;
  const stripped = value.trim().toLowerCase();
  if (stripped === "ina") return true;
  if (stripped.startsWith("issue not addressed")) return true;
  return false;
}

/**
 * True iff `value` represents an NA answer ("not applicable", an affirmative
 * answer NCTQ recorded — distinct from INA which means "didn't address it").
 * Recognizes raw "N/A" / "N/A - <qualifier>" AND TCD-API-mapped
 * "Not applicable for district".
 */
export function isNA(value) {
  if (typeof value !== "string") return false;
  const stripped = value.trim().toLowerCase();
  if (stripped === "n/a") return true;
  if (stripped.startsWith("n/a -")) return true;
  if (stripped.startsWith("not applicable")) return true;
  return false;
}

/**
 * Extract the qualifier text from "N/A - <qualifier>". Returns null for
 * bare or TCD-mapped N/A (which have no qualifier preserved upstream).
 */
export function parseNAQualifier(value) {
  if (!isNA(value)) return null;
  const stripped = value.trim();
  if (!stripped.toLowerCase().startsWith("n/a -")) return null;
  const qualifier = stripped.split("-").slice(1).join("-").trim();
  return qualifier || null;
}

/**
 * Render the deterministic table-cell text for a given (value, district_name)
 * pair. Matches backend csv.py behavior — INA / NA / COVERED rows produce
 * byte-identical output to the server-rendered CSV.
 *
 * NOT_REVIEWED rows are detected separately at the CSV row layer (heuristic
 * based on missing value + missing source) because the canonical phrase needs
 * year + topic context the renderer doesn't always have here.
 */
export function renderCell(value, districtName) {
  if (typeof value !== "string") return "";
  if (isINA(value)) return INA_PHRASE;
  if (isNA(value)) {
    const q = parseNAQualifier(value);
    return q
      ? `Not applicable for ${districtName}: ${q}`
      : `Not applicable for ${districtName}`;
  }
  return value;
}

// --- Legacy-name compatibility shim ---
// Older call-sites invoke `formatINA(rawValue)` without a district. Keep it
// working but route through the new renderer when possible.
function formatINA(value) {
  // formatINA was used for both INA and the lumped-N/A case before the split.
  // Now it handles only the INA case (NA rows go through renderCell with a
  // district name). If a legacy caller still sends N/A here, keep five-state
  // semantics and render the NA phrase with a generic district placeholder
  // rather than collapsing it into INA.
  if (isINA(value)) return INA_PHRASE;
  if (isNA(value)) {
    // Shouldn't be hit in new code paths.
    return renderCell(value, "this district");
  }
  return INA_PHRASE;
}

// --- Formula-injection neutralization (links #1496) ---

// A bare number: optional sign, then digits-with-optional-trailing-dot OR
// leading-dot decimal, optional exponent. Anything else with a leading +/-
// (e.g. "-3.2%", "-2+3", a lone "-") is NOT a well-formed number and gets
// neutralized; "-3.2", "-5.", "-.5" pass through untouched. This pattern is
// the canonical shared shape — it MUST stay byte-identical to Python's
// `_BARE_NUMBER` in frontend_contracts.py so the single-table (JS) and ZIP
// (Python) CSV sinks neutralize every value the same way.
const BARE_NUMBER = /^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/;

/**
 * Neutralize a spreadsheet formula-injection payload by prefixing a single
 * quote so Excel/Sheets treat the cell as text, not a formula.
 *
 * Applied BEFORE RFC-4180 quoting. The Python ZIP CSV writer implements the
 * same rule (frontend_contracts.py) — the predicates must match for the two
 * CSV sinks to stay in parity.
 *
 * Neutralize when the first character is `=`, `@`, TAB (0x09), or CR (0x0D).
 * For a leading `-` or `+`, neutralize only when the cell is NOT a bare number
 * (security default: err toward neutralizing — a `%`-suffixed value like
 * "-3.2%" is not a bare number and IS neutralized).
 */
export function neutralizeFormula(value) {
  if (value == null) return "";
  const str = String(value);
  if (str === "") return str;
  const first = str[0];
  if (first === "=" || first === "@" || first === "\t" || first === "\r") {
    return "'" + str;
  }
  if ((first === "-" || first === "+") && !BARE_NUMBER.test(str)) {
    return "'" + str;
  }
  return str;
}

// --- CSV escaping (RFC 4180) ---

function escapeField(value) {
  if (value == null) return "";
  const str = neutralizeFormula(value);
  if (str.includes(",") || str.includes('"') || str.includes("\n") || str.includes("\r")) {
    return '"' + str.replace(/"/g, '""') + '"';
  }
  return str;
}

function rowToCSV(fields) {
  return fields.map(escapeField).join(",");
}

// --- Metric CSV generation (mirrors export.py:generate_metric_csv) ---

/**
 * Generate CSV string from metric data array.
 * @param {Array} metricDataArray - Array of MetricResultData objects (each has .results)
 * @param {boolean} includeCitations - Whether to include citation columns
 * @returns {string} CSV content
 */
export function generateMetricCSV(metricDataArray, includeCitations = true) {
  const backendExports = metricDataArray
    .map((item) => item?.csv_export)
    .filter((artifact) =>
      artifact &&
      Array.isArray(csvExportColumns(artifact)) &&
      Array.isArray(csvExportRows(artifact))
    );
  if (backendExports.length > 0) {
    return generateBackendCsvExport(backendExports);
  }

  const hasSortBasis = metricDataArray.some((item) =>
    Array.isArray(item.results) &&
    item.results.some((result) =>
      result.sort_metric_name || result.sort_display_value || result.sort_value !== undefined
    )
  );
  const headers = ["District", "State", "Metric", "Value", "Academic Year", "Topic"];
  if (hasSortBasis) {
    headers.splice(2, 0, "Sort Metric", "Sort Value");
  }
  if (includeCitations) {
    // Mirror archived legacy export/csv.py byte-for-byte: 6 source columns
    // (Document Type was missing pre-PR #371 review; restored for parity).
    headers.push(
      "Source Document",
      "Document Type",
      "Page",
      "Document URL",
      "Valid From",
      "Valid To",
    );
  }

  const rows = [rowToCSV(headers)];

  // Flatten results from all MetricResultData objects
  const allResults = [];
  for (const item of metricDataArray) {
    if (item.results && Array.isArray(item.results)) {
      allResults.push(...item.results);
    }
  }

  for (const result of allResults) {
    const rawValue = result.value ?? "";
    const districtName = result.district_name ?? "";
    const valueIsINA = typeof rawValue === "string" && isINA(rawValue);
    const valueIsNA = typeof rawValue === "string" && isNA(rawValue);
    const sourceDoc = result.source_document ?? "";
    const sourceType = result.source_document_type ?? result.document_type ?? "";
    const sourcePage = result.source_page ?? "";
    const sourceUrl = result.source_url ?? "";
    const validFrom = result.source_valid_from ?? result.valid_from ?? "";
    const validTo = result.source_valid_to ?? result.valid_to ?? "";
    const citations = result.citations || [];

    // NOT_REVIEWED: backend leaves value+sources empty on purpose (tools/data.py:181)
    // because the Writer narrates it from rendered_cell. The CSV never sees
    // the narrative, so label it here to match what the on-screen table shows.
    const isNotReviewed =
      rawValue === "" &&
      !valueIsINA &&
      !valueIsNA &&
      !sourceDoc &&
      !sourceUrl &&
      citations.length === 0;

    let displayValue;
    if (isNotReviewed) {
      displayValue = "Not reviewed";
    } else if (valueIsINA || valueIsNA) {
      // Use the unified renderer so CSV cells match chat/table cells byte-
      // for-byte. NA cells include the district name and any qualifier text.
      displayValue = renderCell(rawValue, districtName);
    } else {
      displayValue = rawValue;
    }

    const row = [result.district_name ?? "", result.state ?? ""];
    if (hasSortBasis) {
      row.push(
        result.sort_metric_name ?? "",
        result.sort_display_value ?? result.sort_value ?? "",
      );
    }
    row.push(
      result.metric_name ?? "",
      displayValue,
      result.academic_year ?? "",
      result.topic ?? "",
    );

    if (includeCitations) {
      // Mirror archived legacy export/csv.py: 6 source columns
      // (Source Document, Document Type, Page, URL, Valid From, Valid To)
      // and the same wording on the unsourced-INA fallback so browser
      // exports match server exports byte-for-byte.
      if (sourceDoc || sourceUrl) {
        row.push(sourceDoc, sourceType, sourcePage, sourceUrl, validFrom, validTo);
      } else if (citations.length > 0) {
        const cit = citations[0];
        row.push(
          cit.document || cit.document_name || "",
          cit.document_type || "",
          cit.page || cit.page_number || "",
          cit.url || "",
          cit.valid_from || "",
          cit.valid_to || "",
        );
      } else if (isNotReviewed || valueIsINA || valueIsNA) {
        // Backend writes "No source available" for unsourced INA rows.
        // NA / NOT_REVIEWED rows likewise have no bibliographic source —
        // labeling all three with the same string keeps export parity
        // simple and matches the server-rendered CSV.
        row.push("No source available", "", "", "", "", "");
      } else {
        row.push("", "", "", "", "", "");
      }
    }

    rows.push(rowToCSV(row));
  }

  return rows.join("\n");
}

function generateBackendCsvExport(artifacts) {
  const columns = [];
  const seen = new Set();
  for (const artifact of artifacts) {
    for (const column of csvExportColumns(artifact)) {
      if (!seen.has(column)) {
        seen.add(column);
        columns.push(column);
      }
    }
  }

  const rows = [rowToCSV(columns)];
  for (const artifact of artifacts) {
    for (const row of csvExportRows(artifact)) {
      rows.push(rowToCSV(columns.map((column) => row?.[column] ?? "")));
    }
  }
  return rows.join("\n");
}

function csvExportColumns(artifact) {
  if (
    Array.isArray(artifact?.public_columns) &&
    artifact.public_columns.length > 0 &&
    Array.isArray(artifact?.public_rows) &&
    artifact.public_rows.length > 0
  ) {
    return artifact.public_columns;
  }
  return Array.isArray(artifact?.columns) ? artifact.columns : [];
}

function csvExportRows(artifact) {
  if (
    Array.isArray(artifact?.public_columns) &&
    artifact.public_columns.length > 0 &&
    Array.isArray(artifact?.public_rows) &&
    artifact.public_rows.length > 0
  ) {
    return artifact.public_rows;
  }
  return Array.isArray(artifact?.rows) ? artifact.rows : [];
}

// --- Chart CSV generation (mirrors export.py:generate_chart_csv) ---

/**
 * Generate CSV string from chart data.
 * @param {Object} chartData - ChartData object with type, data, series, x_axis, y_axis
 * @returns {string} CSV content
 */
export function generateChartCSV(chartData) {
  const rows = [];

  if (chartData.artifact_type === "line_chart" && chartData.series) {
    const xLabel = chartData.x_axis_label || "Academic year";
    const seriesNames = chartData.series.map((s) => s.label || s.name || "Series");
    rows.push(rowToCSV([xLabel, ...seriesNames]));

    const xSet = new Set();
    for (const series of chartData.series) {
      for (const point of series.points || []) {
        xSet.add(point.label);
      }
    }

    const xValues = [...xSet].sort((a, b) => String(a).localeCompare(String(b)));
    const lookups = chartData.series.map((s) => {
      const map = {};
      for (const point of s.points || []) {
        map[point.label] = point.value;
      }
      return map;
    });

    for (const x of xValues) {
      const values = lookups.map((lookup) => lookup[x] ?? "");
      rows.push(rowToCSV([x, ...values]));
    }
  } else if (chartData.artifact_type === "bar_chart" && chartData.points) {
    const xLabel = chartData.x_axis_label || "Category";
    const yLabel = chartData.y_axis_label || "Value";
    rows.push(rowToCSV([xLabel, yLabel]));

    for (const point of chartData.points) {
      rows.push(rowToCSV([point.label, point.value]));
    }
  } else if (chartData.type === "bar" && chartData.data) {
    const xLabel = chartData.x_axis?.label || "Category";
    const yLabel = chartData.y_axis?.label || "Value";
    rows.push(rowToCSV([xLabel, yLabel]));

    for (const point of chartData.data) {
      rows.push(rowToCSV([point.x, point.y]));
    }
  } else if (chartData.type === "line" && chartData.series) {
    const xLabel = chartData.x_axis?.label || "X";
    const seriesNames = chartData.series.map((s) => s.name);
    rows.push(rowToCSV([xLabel, ...seriesNames]));

    // Collect all unique x values
    const xSet = new Set();
    for (const series of chartData.series) {
      for (const point of series.data) {
        xSet.add(point.x);
      }
    }

    // Sort numerically where possible
    const xValues = [...xSet].sort((a, b) => {
      const na = Number(a);
      const nb = Number(b);
      if (!isNaN(na) && !isNaN(nb)) return na - nb;
      return String(a).localeCompare(String(b));
    });

    // Build lookup per series
    const lookups = chartData.series.map((s) => {
      const map = {};
      for (const point of s.data) {
        map[point.x] = point.y;
      }
      return map;
    });

    for (const x of xValues) {
      const values = lookups.map((lookup) => lookup[x] ?? "");
      rows.push(rowToCSV([x, ...values]));
    }
  }

  return rows.join("\n");
}

// --- Shared download filename builder ---

/**
 * Build a human-readable export filename of the shape
 *   `NCTQ Compass - {title} - {YYYY-MM-DD}.{ext}`
 * shared by every download site (single table, chart, conversation ZIP) and
 * mirroring the backend `_export_filename` shape.
 *
 * Sanitization: strip OS-illegal characters (`\ / : * ? " < > |`) and control
 * chars, collapse runs of whitespace to a single space, trim, then cap at
 * ~120 chars. Spaces, commas, parens, and apostrophes are kept (all legal on
 * macOS/Windows/Linux). A blank/missing title drops the title segment rather
 * than emitting a double separator.
 *
 * @param {{title?: string, date?: string, ext?: string}} opts
 * @returns {string}
 */
export function buildExportFilename({ title, date, ext = "csv" } = {}) {
  const day = date || new Date().toISOString().slice(0, 10);
  const cleanTitle = sanitizeFilenameTitle(title);
  const stem = cleanTitle
    ? `NCTQ Compass - ${cleanTitle} - ${day}`
    : `NCTQ Compass - ${day}`;
  return `${stem}.${ext}`;
}

function sanitizeFilenameTitle(title) {
  if (title == null) return "";
  let str = String(title)
    // OS-illegal characters (\ / : * ? " < > |).
    .replace(/[\\/:*?"<>|]/g, "")
    // Collapse all whitespace (incl. tabs/newlines) to single spaces FIRST so a
    // "lineone\ttwo" heading reads "lineone two", not "lineonetwo".
    .replace(/\s+/g, " ")
    // Drop any remaining (non-whitespace) control chars: 0x00-0x08, 0x0B-0x0C,
    // 0x0E-0x1F, 0x7F. \s already consumed 0x09-0x0A and 0x0D above.
    // eslint-disable-next-line no-control-regex
    .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, "")
    .trim();
  // Cap at ~120 chars; re-trim because the slice can leave a trailing space.
  if (str.length > 120) str = str.slice(0, 120).trim();
  return str;
}

// --- Download trigger ---

/**
 * Trigger browser download of CSV content.
 * @param {string} csvString - CSV content
 * @param {string} filename - Download filename
 */
export function downloadCSV(csvString, filename) {
  // UTF-8 BOM for Excel compatibility
  const BOM = "\uFEFF";
  const blob = new Blob([BOM + csvString], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
