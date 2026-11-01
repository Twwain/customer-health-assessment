import type { ChatMessageItem, FactorConfigResponse, StrategyItem } from "../types";
import { plainTextBlock } from "./plainText";

export type FactorNames = ReadonlyMap<string, string>;

export function factorNames(config: FactorConfigResponse): FactorNames {
  const names = new Map<string, string>();
  for (const dimension of config.dimensions) {
    for (const factor of dimension.factors) {
      const match = /^([A-Z]+-\d+[A-Z]?)\s+(.+)$/u.exec(factor.label);
      if (match) names.set(match[1], match[2].trim());
    }
  }
  return names;
}

export function nameFactors(text: string, names: FactorNames): string {
  const escape = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  for (const [code, name] of names) {
    // Consume an already-present label too, including its optional parentheses.
    const label = escape(name);
    const pattern = new RegExp(`(?<![A-Za-z0-9_-])${escape(code)}(?![A-Za-z0-9_-])(?:[ \\t]*${label}|[ \\t]*[（(]${label}[）)])?`, "gu");
    text = text.replace(pattern, () => name);
  }
  return text;
}

function strategyNames(item: StrategyItem, names: FactorNames): StrategyItem {
  const display = { ...item };
  for (const key of ["title", "reason", "action", "expected_outcome"] as const) {
    display[key] = nameFactors(item[key], names);
  }
  for (const key of ["suggested_owner", "suggested_timing", "deliverable"] as const) {
    if (item[key]) display[key] = nameFactors(item[key], names);
  }
  if (item.steps) display.steps = item.steps.map((step) => nameFactors(step, names));
  return display;
}

// Rewrite only decoded analysis leaves and action display fields. The factual
// envelope, source titles/snippets and stored message remain untouched.
export function presentFactorNames(msg: ChatMessageItem, names: FactorNames): ChatMessageItem {
  if (msg.role !== "assistant" || names.size === 0) return msg;
  let analysis = msg.scenario === "free_qa";
  const content = msg.content.split("\n").map((line) => {
    if (line === "## AI 分析（业务判断，可能有误）") analysis = true;
    const plain = analysis ? plainTextBlock(line) : null;
    return plain === null ? line : ":::ai-text " + JSON.stringify(nameFactors(plain, names));
  }).join("\n");
  return { ...msg, content, strategy_items: (msg.strategy_items ?? []).map((item) => strategyNames(item, names)) };
}
