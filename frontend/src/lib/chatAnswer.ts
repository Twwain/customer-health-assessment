import type { ChatMessageItem, StrategyItem } from "../types";

// 只折叠正文已完整包含的卡片，避免历史回答中的独有说明被删除。
function comparable(text: string): string {
  return text.normalize("NFKC").replace(/[\s*`_#]/gu, "").toLowerCase();
}

export function additionalStrategies(content: string, items: StrategyItem[]): StrategyItem[] {
  const lines = content.split("\n");
  return items.filter((item) => {
    const fields = [item.title, item.reason, item.action, item.expected_outcome, item.reference]
      .map(comparable).filter(Boolean);
    if (fields.length === 0 || !priorities[item.priority] || !urgency[item.urgency]) return true;
    let group = "";
    for (let index = 0; index < lines.length; index++) {
      const label = comparable(lines[index]).replace(/^[✅□💡]/u, "");
      if (Object.values(priorities).includes(label)) group = label;
      if (!comparable(lines[index]).includes(comparable(item.title)) || !item.title) continue;
      let end = index + 1;
      while (end < lines.length) {
        const next = comparable(lines[end]).replace(/^[✅□💡]/u, "");
        if (Object.values(priorities).includes(next) || items.some((other) =>
          other !== item && other.title && next.includes(comparable(other.title)))) break;
        end++;
      }
      const block = comparable(lines.slice(index, end).join("\n"));
      if (group === priorities[item.priority]
        && block.includes(`紧急度:${urgency[item.urgency]}`)
        && fields.every((field) => block.includes(field))) return false;
    }
    return true;
  });
}

const priorities: Record<string, string> = {
  recommended: "推荐策略", alternative: "备选策略", long_term: "长期建议",
};
const urgency: Record<string, string> = { high: "高", medium: "中", low: "低" };

export function completeAnswerText(msg: ChatMessageItem): string {
  const sections = [msg.content.trim()];
  const remaining = additionalStrategies(msg.content, msg.strategy_items ?? []);
  const groups = [...new Set(["recommended", "alternative", "long_term", ...remaining.map((item) => item.priority)])];
  for (const priority of groups) {
    const items = remaining.filter((item) => item.priority === priority);
    if (!items.length) continue;
    sections.push(`## ${priorities[priority] ?? "其他策略"}\n\n` + items.map((item, index) => [
      `${index + 1}. ${item.title}（紧急度：${urgency[item.urgency] ?? item.urgency}）`,
      item.reason && `原因：${item.reason}`,
      item.action && `行动：${item.action}`,
      item.expected_outcome && `预期：${item.expected_outcome}`,
      item.reference && `依据：${item.reference}`,
    ].filter(Boolean).join("\n")).join("\n\n"));
  }
  if (msg.references?.length) {
    const references = [...new Set(msg.references.map((ref) =>
      `- ${ref.title}${ref.chunk_id != null ? ` · 切片 ${ref.chunk_id}` : ""}（${ref.used ? "已引用" : "仅检索，未作为结论依据"}）`,
    ))];
    sections.push("## 知识来源\n\n" + references.join("\n"));
  }
  if (msg.degraded) sections.push("说明：本次包含规则引擎建议。");
  return sections.filter(Boolean).join("\n\n");
}
