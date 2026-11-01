import type { StrategyItem } from "../types";

export function strategyDetails(item: StrategyItem): string {
  if (!item.suggested_owner || !item.suggested_timing || !item.deliverable) return item.action;
  // Remove only the exact renderer-appended metadata already shown in columns.
  // Preserve model-authored steps, dependencies, and unknown legacy formats.
  const metadata = `\n建议参与方：${item.suggested_owner}；建议期限：${item.suggested_timing}；交付物：${item.deliverable}。`;
  const index = item.action.lastIndexOf(metadata);
  if (index < 0) return item.action;
  const tail = item.action.slice(index + metadata.length);
  if (tail && !tail.startsWith("\n前置行动：")) return item.action;
  return item.action.slice(0, index) + tail;
}
