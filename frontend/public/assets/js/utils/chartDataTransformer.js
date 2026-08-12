export function transformChartData(backendChart) {
  // Store original backend data for export (always keep this)
  const result = {
    ...backendChart,
    _originalData: backendChart
  };

  if (backendChart.artifact_type === "bar_chart") {
    const series = Array.isArray(backendChart.series) ? backendChart.series : [];
    const points = Array.isArray(backendChart.points) ? backendChart.points : [];
    const firstSeriesPoints = Array.isArray(series[0]?.points) ? series[0].points : [];
    const labelPoints = firstSeriesPoints.length > 0 ? firstSeriesPoints : points;
    const labels = labelPoints.map(point => point.label || "");

    const datasets = series.length > 0
      ? series.map((item, index) => ({
          label: item.label || item.name || `Series ${index + 1}`,
          data: (Array.isArray(item.points) ? item.points : []).map(point => point.value || 0)
        }))
      : [{
          label: backendChart.title || "Value",
          data: points.map(point => point.value || 0)
        }];

    return {
      ...result,
      type: "bar",
      title: backendChart.title,
      subtitle: backendChart.subtitle,
      labels,
      datasets,
      x_axis: { label: backendChart.x_axis_label || "" },
      y_axis: { label: backendChart.y_axis_label || "" }
    };
  }

  if (backendChart.artifact_type === "line_chart") {
    const series = Array.isArray(backendChart.series) ? backendChart.series : [];
    const firstSeriesPoints = Array.isArray(series[0]?.points) ? series[0].points : [];
    const labels = firstSeriesPoints.map(point => point.label || "");
    const datasets = series.map((item, index) => ({
      label: item.label || item.name || `Series ${index + 1}`,
      data: (Array.isArray(item.points) ? item.points : []).map(point => point.value || 0)
    }));

    return {
      ...result,
      type: "line",
      title: backendChart.title,
      subtitle: backendChart.subtitle,
      labels,
      datasets,
      x_axis: { label: backendChart.x_axis_label || "" },
      y_axis: { label: backendChart.y_axis_label || "" }
    };
  }

  // If chart already has labels array (already transformed), return as-is
  if (backendChart.labels && Array.isArray(backendChart.labels) && !backendChart.series) {
    return result;
  }

  // Handle LINE charts with series format
  if (backendChart.type === 'line' && backendChart.series && Array.isArray(backendChart.series)) {
    // Extract labels from first series
    const labels = backendChart.series[0]?.data?.map(point => point.x || point.label || '') || [];
    
    // Transform each series into Chart.js dataset format
    const datasets = backendChart.series.map((series, index) => ({
      label: series.name || `Series ${index + 1}`,
      data: series.data?.map(point => point.y || point.value || 0) || [],
      borderColor: series.color || null,
      backgroundColor: series.color ? `${series.color}33` : null,
      fill: false,
      tension: 0.1
    }));

    return {
      ...result,
      type: backendChart.type,
      title: backendChart.title,
      subtitle: backendChart.subtitle,
      labels: labels,
      datasets: datasets,
      x_axis: backendChart.x_axis,
      y_axis: backendChart.y_axis
    };
  }

  // Handle BAR charts and other charts with simple data array
  if (backendChart.data && Array.isArray(backendChart.data)) {
    // Check if data contains {x, y} objects
    if (backendChart.data.length > 0 && typeof backendChart.data[0] === 'object') {
      const labels = backendChart.data.map(point => point.x || point.label || '');
      const values = backendChart.data.map(point => point.y || point.value || 0);

      return {
        ...result,
        type: backendChart.type,
        title: backendChart.title,
        subtitle: backendChart.subtitle,
        labels: labels,
        datasets: [{
          label: backendChart.title || 'Value',
          data: values
        }],
        x_axis: backendChart.x_axis,
        y_axis: backendChart.y_axis
      };
    }
  }

  return result;
}

export function prepareChartForExport(chart) {
  // If we have the original backend data, use that
  if (chart._originalData) {
    return chart._originalData;
  }

  // Otherwise, return the chart as-is
  return chart;
}

export function prepareMetricForExport(metricData) {
  // If it's already in the correct format, return as-is
  if (metricData.type === "metric_comparison" && metricData.results) {
    return metricData.results;
  }

  // If it's wrapped, unwrap it
  if (Array.isArray(metricData)) {
    return metricData;
  }

  // Single metric result
  return [metricData];
}
