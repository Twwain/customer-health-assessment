import { describe, expect, it } from "vitest";
import type { ChatMessageItem, StrategyItem } from "../types";
import { completeAnswerText } from "./chatAnswer";

const action: StrategyItem = {
  priority: "recommended", title: "形成价值测算表", urgency: "high",
  reason: "已有定性价值", action: "1. 确认价值口径\n2. 收集基线\n建议参与方：客户经理；建议期限：两周内；交付物：测算表。\n前置行动：客户认可口径",
  expected_outcome: "提交评审", reference: "", suggested_owner: "客户经理",
  suggested_timing: "两周内", deliverable: "测算表", steps: ["确认价值口径", "收集基线"],
};
const message = (content: string, strategy_items = [action]): ChatMessageItem => ({
  id: 1, session_id: 7, role: "assistant", content, strategy_items,
  references: [], tokens_used: 0, feedback: "", degraded: false, created_at: "",
});

describe("完整答案复制", () => {
  it("综合评估和自由问答统一包含计划、完整步骤与依赖，元数据只出现一次", () => {
    for (const body of ["建议先量化价值。", '本次按以下任务回答：综合评估\n## AI 分析（业务判断，可能有误）\n### 总体评估\n:::ai-text "建议先量化价值。"']) {
      const copied = completeAnswerText(message(body));
      for (const text of ["建议先量化价值。", "## 下一步计划", "行动：形成价值测算表", "责任人：客户经理", "时限：两周内", "交付物：测算表", "1. 确认价值口径", "2. 收集基线", "前置行动：客户认可口径"]) {
        expect(copied).toContain(text);
      }
      expect(copied.match(/交付物：测算表/g)).toHaveLength(1);
      expect(copied).not.toContain("建议参与方：");
      expect(copied).not.toContain("AI 分析（业务判断，可能有误）");
    }
  });

  it("保留引用与仅检索的区别，并标注规则兜底", () => {
    const msg = message("现有概况");
    msg.degraded = true;
    msg.references = [
      { title: "经营指南", category: "", snippet: "", score: 0.8, chunk_id: 3, used: true },
      { title: "行业参考", category: "", snippet: "", score: 0.5, used: false },
    ];
    const copied = completeAnswerText(msg);
    expect(copied).toContain("现有概况");
    expect(copied).toContain("经营指南 · 切片 3（已引用）");
    expect(copied).toContain("行业参考（仅检索，未作为结论依据）");
    expect(copied).toContain("本次包含规则引擎建议");
  });

  it("没有行动或引用时不追加空段落，解码安全文本", () => {
    expect(completeAnswerText(message(':::ai-text "原文 <b>与符号</b>"', [])))
      .toBe("原文 <b>与符号</b>");
  });

  it("直接问答复制只包含理由、唯一行动及引用，不增加报告模板", () => {
    const msg = { ...message(':::ai-text "先量化价值，方便客户明确试点目标。"'), scenario: 'free_qa' };
    const copied = completeAnswerText(msg);
    expect(copied).toContain('先量化价值，方便客户明确试点目标。');
    expect(copied.match(/行动：形成价值测算表/g)).toHaveLength(1);
    for (const section of ['总览', '总体评估', '完整预警清单', '重点解读', 'AI 分析', ':::ai-text']) {
      expect(copied).not.toContain(section);
    }
  });
});
