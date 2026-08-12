/**
 * Chart rendering utilities using Chart.js
 * Handles various chart types including line charts with multiple series
 */

import { generateChartCSV, downloadCSV, buildExportFilename } from "./csvExport.js";

let chartCounter = 0;

/**
 * Main function to render a chart in the chat
 * @param {HTMLElement} container - The chat message container
 * @param {Object} chartData - Chart configuration from backend (transformed)
 * @returns {HTMLElement|undefined} the appended chart wrapper, so a live caller
 *   can scroll it into view (#1467); undefined when the data is invalid.
 */
export function renderChart(container, chartData) {
  if (!chartData || !chartData.type) {
    console.error("Invalid chart data:", chartData);
    return;
  }

  // Create chart wrapper
  const chartWrapper = document.createElement("div");
  chartWrapper.className = "chart-container my-4 p-4 bg-white rounded-lg shadow-sm border border-slate-200";
  chartWrapper.id = `chart-container-${chartData.id || chartCounter}`;

  // Add title if provided
  if (chartData.title) {
    const title = document.createElement("h4");
    title.className = "text-lg font-semibold text-[#0F223D] mb-2";
    title.textContent = chartData.title;
    chartWrapper.appendChild(title);
  }

  // Add subtitle if provided
  if (chartData.subtitle) {
    const subtitle = document.createElement("p");
    subtitle.className = "text-sm text-gray-600 mb-3";
    subtitle.textContent = chartData.subtitle;
    chartWrapper.appendChild(subtitle);
  }

  // Create canvas element
  const canvas = document.createElement("canvas");
  canvas.id = `chart-${chartCounter++}`;
  canvas.style.maxHeight = "400px";
  chartWrapper.appendChild(canvas);

  container.appendChild(chartWrapper);
  
  // Render the chart
  try {
    createChart(canvas, chartData);
    
    // Add export button directly below the chart
    addChartExportButton(chartWrapper, chartData);
  } catch (error) {
    console.error("Error creating chart:", error);
    const errorMsg = document.createElement("p");
    errorMsg.className = "text-status-error text-sm mt-2";
    errorMsg.textContent = "Error rendering chart";
    chartWrapper.appendChild(errorMsg);
  }

  return chartWrapper;
}

/**
 * Add export button to chart container
 * @param {HTMLElement} chartContainer - The chart container element
 * @param {Object} chartData - Original chart data for export
 */
function addChartExportButton(chartContainer, chartData) {
  const exportBtn = document.createElement("button");
  exportBtn.className = "export-csv-btn mt-3 inline-flex items-center gap-2 px-3 py-1.5 text-xs font-medium text-text-chart bg-white hover:bg-text-chart hover:text-white border border-text-chart transition-all duration-200";
  exportBtn.innerHTML = `
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
      <polyline points="7 10 12 15 17 10"></polyline>
      <line x1="12" y1="15" x2="12" y2="3"></line>
    </svg>
    Export CSV
  `;
  exportBtn.onclick = () => exportChartToCsv(chartData);
  chartContainer.appendChild(exportBtn);
}

/**
 * Export chart data as CSV (client-side generation).
 * @param {Object} chartData - Chart data to export
 */
function exportChartToCsv(chartData) {
  try {
    const dataToExport = chartData._originalData || chartData;
    const csv = generateChartCSV(dataToExport);
    // Best-available human title: the chart's title, then its id; otherwise
    // buildExportFilename drops the title segment.
    const title = chartTitle(dataToExport);
    const filename = buildExportFilename({ title, ext: "csv" });
    downloadCSV(csv, filename);
  } catch (error) {
    alert("Failed to export CSV: " + error.message);
  }
}

/**
 * Best human title for a chart export: prefer the chart title, fall back to
 * its id, else "" so the filename builder uses its generic shape.
 */
function chartTitle(chartData) {
  if (typeof chartData?.title === "string" && chartData.title.trim()) {
    return chartData.title.trim();
  }
  if (typeof chartData?.id === "string" && chartData.id.trim()) {
    return chartData.id.trim();
  }
  return "";
}

/**
 * Create Chart.js instance with proper configuration
 * @param {HTMLCanvasElement} canvas - Canvas element
 * @param {Object} chartData - Chart configuration (transformed)
 */
function createChart(canvas, chartData) {
  const ctx = canvas.getContext("2d");

  // Secondary color palette from design guide for charts
  const colorPalette = [
    { bg: 'rgba(74, 145, 208, 0.15)', border: '#4A91D0' },   // Light blue
    { bg: 'rgba(246, 142, 30, 0.15)', border: '#F68E1E' },   // Orange
    { bg: 'rgba(160, 198, 32, 0.15)', border: '#A0C620' },   // Green
    { bg: 'rgba(219, 69, 69, 0.15)', border: '#DB4545' },    // Red
    { bg: 'rgba(153, 124, 35, 0.15)', border: '#997C23' },   // Gold
    { bg: 'rgba(54, 104, 43, 0.15)', border: '#36682B' },    // Dark green
  ];

  const solidColors = [
    'rgba(74, 145, 208, 0.85)',   // Light blue
    'rgba(246, 142, 30, 0.85)',   // Orange
    'rgba(160, 198, 32, 0.85)',   // Green
    'rgba(219, 69, 69, 0.85)',    // Red
    'rgba(153, 124, 35, 0.85)',   // Gold
    'rgba(54, 104, 43, 0.85)',    // Dark green
  ];

  // Build Chart.js configuration
  const config = {
    type: chartData.type,
    data: {
      labels: chartData.labels || [],
      datasets: []
    },
    options: getChartOptions(chartData)
  };

  // Build datasets based on chart type and data structure
  if (chartData.type === 'line') {
    // Line chart - handle datasets array (from transformed series)
    if (chartData.datasets && Array.isArray(chartData.datasets)) {
      config.data.datasets = chartData.datasets.map((dataset, i) => {
        const color = colorPalette[i % colorPalette.length];
        return {
          label: dataset.label || `Series ${i + 1}`,
          data: dataset.data || [],
          backgroundColor: dataset.backgroundColor || color.bg,
          borderColor: dataset.borderColor || color.border,
          borderWidth: 2,
          fill: dataset.fill !== undefined ? dataset.fill : false,
          tension: dataset.tension || 0.1,
          pointRadius: 4,
          pointHoverRadius: 6
        };
      });
    } else if (Array.isArray(chartData.data)) {
      // Fallback: simple data array
      config.data.datasets = [{
        label: chartData.title || 'Value',
        data: chartData.data,
        backgroundColor: colorPalette[0].bg,
        borderColor: colorPalette[0].border,
        borderWidth: 2,
        fill: false,
        tension: 0.1
      }];
    }
  } else if (chartData.type === 'bar') {
    // Bar chart
    if (chartData.datasets && Array.isArray(chartData.datasets)) {
      config.data.datasets = chartData.datasets.map((dataset, i) => ({
        label: dataset.label || `Dataset ${i + 1}`,
        data: dataset.data || [],
        backgroundColor: solidColors[i % solidColors.length],
        borderColor: solidColors[i % solidColors.length].replace('0.7', '1'),
        borderWidth: 1,
        borderRadius: 4
      }));
    } else if (Array.isArray(chartData.data)) {
      // Fallback: simple data array
      config.data.datasets = [{
        label: chartData.title || 'Value',
        data: chartData.data,
        backgroundColor: 'rgba(74, 145, 208, 0.85)',  // Light blue
        borderColor: '#4A91D0',
        borderWidth: 1,
        borderRadius: 4
      }];
    }
  } else if (chartData.type === 'pie' || chartData.type === 'doughnut') {
    // Pie/Doughnut chart
    const data = chartData.datasets?.[0]?.data || chartData.data || [];
    config.data.datasets = [{
      data: data,
      backgroundColor: solidColors.slice(0, data.length),
      borderWidth: 1
    }];
  }

  new Chart(ctx, config);
}

/**
 * Get chart options with consistent styling
 * @param {Object} chartData - Chart configuration
 */
function getChartOptions(chartData) {
  const yFormat = chartData.y_axis?.format;
  
  const baseOptions = {
    responsive: true,
    maintainAspectRatio: true,
    plugins: {
      legend: {
        display: chartData.datasets && chartData.datasets.length > 1,
        position: 'top',
        labels: {
          font: {
            family: "'Inter', sans-serif",
            size: 12,
          },
          color: '#0F223D',
          padding: 15,
          usePointStyle: true,
          boxWidth: 8,
        },
      },
      tooltip: {
        backgroundColor: 'rgba(15, 34, 61, 0.95)',  // Dark navy
        titleColor: '#fff',
        bodyColor: '#fff',
        borderColor: '#4A91D0',  // Light blue
        borderWidth: 1,
        padding: 12,
        displayColors: true,
        callbacks: {
          label: function(context) {
            let label = context.dataset.label || '';
            if (label) {
              label += ': ';
            }
            const value = context.parsed.y !== undefined ? context.parsed.y : context.parsed;
            
            if (yFormat === 'currency') {
              label += '$' + value.toLocaleString();
            } else if (yFormat === 'percent') {
              label += value + '%';
            } else {
              label += value.toLocaleString();
            }
            return label;
          }
        }
      },
    },
  };

  // Add scales for line and bar charts
  if (chartData.type === 'line' || chartData.type === 'bar') {
    baseOptions.scales = {
      x: {
        title: {
          display: !!chartData.x_axis?.label,
          text: chartData.x_axis?.label || '',
          color: '#0F223D',
          font: { weight: '500' }
        },
        grid: {
          display: false,
        },
        ticks: {
          color: '#64748B',
          font: {
            size: 11,
          },
        },
      },
      y: {
        title: {
          display: !!chartData.y_axis?.label,
          text: chartData.y_axis?.label || '',
          color: '#0F223D',
          font: { weight: '500' }
        },
        beginAtZero: true,
        grid: {
          color: 'rgba(122, 145, 178, 0.15)',  // Gray blue with transparency
        },
        ticks: {
          color: '#64748B',
          font: {
            size: 11,
          },
          callback: function(value) {
            if (yFormat === 'currency') {
              return '$' + value.toLocaleString();
            } else if (yFormat === 'percent') {
              return value + '%';
            }
            return value.toLocaleString();
          }
        },
      },
    };
  }

  return baseOptions;
}