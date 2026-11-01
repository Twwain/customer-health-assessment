import type { ChatMessageItem } from "../types";
import { answerPlainText } from "./plainText";
import { assessmentCopyText, assessmentPlan, assessmentSections } from "./assessmentPresentation";

export function completeAnswerText(msg: ChatMessageItem): string {
  const assessment = assessmentSections(msg.content);
  const sections = [assessment ? assessmentCopyText(assessment) : answerPlainText(msg.content).trim()];
  if (msg.strategy_items?.length) {
    sections.push("## 下一步计划\n\n" + assessmentPlan(msg.strategy_items)
      .map((item) => `- 行动：${item.action}\n  责任人：${item.owner}\n  时限：${item.timing}\n  交付物：${item.deliverable}`
        + (item.detail ? `\n  完整步骤：\n${item.detail}` : "")).join("\n\n"));
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
