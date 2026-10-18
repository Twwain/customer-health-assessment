import { describe, expect, it } from "vitest";
import type { ChatMessageItem, StrategyItem } from "../types";
import { additionalStrategies, completeAnswerText } from "./chatAnswer";

const strategy: StrategyItem = {
  priority: "recommended", title: "加强高层沟通", urgency: "high",
  reason: "尚未建立高层联系", action: "本月安排一次高层拜访",
  expected_outcome: "建立季度沟通机制", reference: "客户经营指南",
};
const message = (content: string, strategy_items = [strategy]): ChatMessageItem => ({
  id: 1, session_id: 7, role: "assistant", content, strategy_items,
  references: [], tokens_used: 0, feedback: "", degraded: false, created_at: "",
});

describe("完整答案展示和复制", () => {
  it("复制已核实AI概况与规则行动时保留各自说明，不把全部内容冒称规则生成", () => {
    const rule = { ...strategy, title: "保持服务回访", reference: "评分规则建议" };
    const overview = "已核实的AI概况：客户需要持续经营。";
    const notice = "已保留核实通过的概况；以下行动来自规则建议。";
    const msg = { ...message(`${overview}\n\n${notice}`, [rule]), degraded: true };
    const copied = completeAnswerText(msg);
    expect(copied).toContain(overview);
    expect(copied).toContain(notice);
    expect(copied).toContain(rule.action);
    expect(copied).toContain("评分规则建议");
    expect(copied).toContain("本次包含规则引擎建议");
    expect(copied).not.toContain("本次结果由规则引擎生成");
  });

  it("历史正文完整覆盖卡片时只展示一份，仍保留原有结论和核实提示", () => {
    const body = `## 客户结论\n需要持续关注\n\n## 推荐策略\n### 加强高层沟通（紧急度：高）\n原因：尚未建立高层联系\n行动：本月安排一次高层拜访\n预期：建立季度沟通机制\n依据：客户经营指南\n\n> 部分结论尚待核实`;
    expect(additionalStrategies(body, [strategy])).toEqual([]);
    const result = completeAnswerText(message(body));
    expect(result).toBe(body);
    expect(result.match(/本月安排一次高层拜访/g)).toHaveLength(1);
  });

  it("不能因正文只提到了策略标题就丢弃完整卡片", () => {
    expect(additionalStrategies("建议加强高层沟通", [strategy])).toEqual([strategy]);
    const result = completeAnswerText(message("建议加强高层沟通"));
    for (const field of [strategy.reason, strategy.action, strategy.expected_outcome, strategy.reference]) {
      expect(result).toContain(field);
    }
  });

  it("不将正负增益或不同数字的策略误判为相同", () => {
    const item = { ...strategy, expected_outcome: "总分+3" };
    const body = [item.title, item.reason, item.action, "总分-3", item.reference].join("\n");
    expect(additionalStrategies(body, [item])).toEqual([item]);
  });

  it("正文缺少分层或紧急度时仍保留卡片及完整复制信息", () => {
    const fields = [strategy.title, strategy.reason, strategy.action, strategy.expected_outcome, strategy.reference].join("\n");
    for (const body of [fields, `## 推荐策略\n${fields}`, `紧急度：高\n${fields}`]) {
      expect(additionalStrategies(body, [strategy])).toEqual([strategy]);
      expect(completeAnswerText(message(body))).toContain("加强高层沟通（紧急度：高）");
      expect(completeAnswerText(message(body))).toContain("## 推荐策略");
    }
  });

  it("不会借用另一层级或另一条策略的紧急度来隐藏卡片", () => {
    const fields = [strategy.title, strategy.reason, strategy.action, strategy.expected_outcome, strategy.reference].join("\n");
    const second = { ...strategy, title: "其他策略", urgency: "high" };
    expect(additionalStrategies(`## 备选策略\n紧急度：高\n${fields}`, [strategy])).toEqual([strategy]);
    const body = `## 推荐策略\n${fields}\n### 其他策略（紧急度：高）\n其他内容`;
    expect(additionalStrategies(body, [strategy, second])).toContain(strategy);
  });

  it("混合新旧条目仅去除完全重复项，复制包含全部层级和引用状态", () => {
    const second = { ...strategy, priority: "alternative", title: "技术交流", action: "组织产品演示" };
    const third = { ...strategy, priority: "long_term", title: "长期伙伴", action: "制定年度合作计划" };
    const body = "## 推荐策略\n" + [strategy.title, "紧急度：高", strategy.reason, strategy.action, strategy.expected_outcome, strategy.reference].join("\n");
    const msg = message(body, [strategy, second, third]);
    msg.references = [
      { title: "客户经营指南", category: "", snippet: "", score: 0.8, chunk_id: 3, used: true },
      { title: "行业参考", category: "", snippet: "", score: 0.5, used: false },
    ];
    expect(additionalStrategies(body, msg.strategy_items)).toEqual([second, third]);
    const result = completeAnswerText(msg);
    expect(result.match(/本月安排一次高层拜访/g)).toHaveLength(1);
    expect(result).toContain("## 备选策略");
    expect(result).toContain("## 长期建议");
    expect(result).toContain("客户经营指南 · 切片 3（已引用）");
    expect(result).toContain("行业参考（仅检索，未作为结论依据）");
  });
});
