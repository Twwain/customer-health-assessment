export interface TrendYRange {
  yMin: number;
  yMax: number;
  ySpan: number;
}

/**
 * 计算趋势图 y 轴范围：按数据范围自适应，并保证最小高度与 0-100 边界。
 * 全等值等近平数据以中点为轴补足最小高度，避免曲线贴边。
 * 注意：PDF 版在 pdf_report_strategy_trend.py 有同源实现，改动需同步。
 */
export function computeTrendYRange(vals: number[]): TrendYRange {
  if (vals.length === 0) {
    return { yMin: 0, yMax: 100, ySpan: 100 };
  }
  const rawMin = Math.min(...vals);
  const rawMax = Math.max(...vals);
  const rawSpan = Math.max(rawMax - rawMin, 8);
  let yMin = Math.max(0, rawMin - rawSpan * 0.18);
  let yMax = Math.min(100, rawMax + rawSpan * 0.18);
  if (yMax - yMin < 8) {
    const mid = (yMin + yMax) / 2;
    yMin = Math.max(0, mid - 4);
    yMax = Math.min(100, mid + 4);
  }
  return { yMin, yMax, ySpan: Math.max(yMax - yMin, 1) };
}
