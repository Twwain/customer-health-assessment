import { plainTextBlock } from "./plainText";
import type { StrategyItem } from "../types";
import { strategyDetails } from "./strategyDetails";

export interface AssessmentSection {
  title: string;
  items: { text: string; detail?: string }[];
}

const marker = "## AI 分析（业务判断，可能有误）";
const titles = ["总体评估", "主要风险点", "优势", "待验证假设", "核实问题"];
const fallbacks = new Set(["未提出额外风险判断。", "未提出额外优势判断。"]);

// Only the exact server-authored evaluation envelope is reshaped. Model text
// stays inside decoded leaves and can never introduce a section or markup.
export function assessmentSections(content: string): AssessmentSection[] | null {
  const lines = content.replace(/\r\n/g, "\n").split("\n");
  if (lines[0] !== "本次按以下任务回答：综合评估") return null;
  const start = lines.indexOf(marker);
  if (start < 0 || lines[start + 1] !== "### 总体评估") return null;
  const sections: AssessmentSection[] = [];
  let current: AssessmentSection | undefined;
  let hasText = false;
  for (const line of lines.slice(start + 1)) {
    if (!line.trim()) continue;
    if (titles.some((title) => line === "### " + title)) {
      const title = line.slice(4);
      if (sections.some((section) => section.title === title)) return null;
      current = { title, items: [] };
      sections.push(current);
      continue;
    }
    if (!current) return null;
    const plain = plainTextBlock(line);
    if (plain === null && !fallbacks.has(line)) return null;
    hasText ||= plain !== null;
    const value = plain ?? line;
    if (current.title === "总体评估") {
      current.items.push(...value.split(/(?<=[。！？])\s*|\n+/u).filter((part) => part.trim()).map((text) => ({ text })));
    } else if (current.title === "待验证假设" && current.items.length
      && !current.items[current.items.length - 1].detail && value.startsWith("验证方式：")) {
      current.items[current.items.length - 1].detail = value;
    } else {
      current.items.push({ text: value });
    }
  }
  return hasText && sections[0]?.items.length
    ? sections.filter((section) => section.title !== "待验证假设" && section.title !== "核实问题") : null;
}

export function assessmentPlan(items: StrategyItem[]) {
  return items.map((item) => ({
    action: item.title || item.steps?.[0] || item.action,
    owner: item.suggested_owner?.trim() || "待明确",
    timing: item.suggested_timing?.trim() || "待明确",
    deliverable: item.deliverable?.trim() || "待明确",
    detail: strategyDetails(item),
  }));
}

export function assessmentCopyText(sections: AssessmentSection[]): string {
  return sections.map((section) => "## " + section.title + "\n\n" +
    section.items.map((item) => "- " + item.text + (item.detail ? "\n  " + item.detail : "")).join("\n")).join("\n\n");
}
