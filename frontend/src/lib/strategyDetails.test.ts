import { describe, expect, it } from "vitest";
import { strategyDetails } from "./strategyDetails";
import { assessmentPlan } from "./assessmentPresentation";
import type { StrategyItem } from "../types";

const item: StrategyItem = { title: "关键人复盘", priority: "recommended", urgency: "medium", reason: "",
  action: "", expected_outcome: "", reference: "", suggested_owner: "客户经理\n方案经理",
  suggested_timing: "两周内", deliverable: "关系图" };
const metadata = '\n建议参与方：客户经理\n方案经理；建议期限：两周内；交付物：关系图。';

describe("完整步骤去重", () => {
  it("计划仅移除重复元数据，保留步骤与依赖", () => {
    const action = "1. 梳理关键人\n2. 制定交付物验收标准";
    const full = { ...item, action: action + metadata + "\n前置行动：确认范围" };
    const expected = action + "\n前置行动：确认范围";
    expect(strategyDetails(full)).toBe(expected);
    expect(assessmentPlan([full])[0]).toMatchObject({ detail: expected, owner: item.suggested_owner, deliverable: item.deliverable });
    expect(full.action).toContain(metadata);
  });

  it("元数据恰好在末尾时也移除，正文相同内容与旧格式仍保留", () => {
    const action = '1. 讨论示例' + metadata + '\n2. 继续推进';
    expect(strategyDetails({ ...item, action: action + metadata })).toBe(action);
    expect(strategyDetails({ ...item, action: '1. 推进\n参与方：客户经理；期限：两周' }))
      .toBe('1. 推进\n参与方：客户经理；期限：两周');
    expect(strategyDetails({ ...item, action: '1. 推进' + metadata + '\n未知段落' }))
      .toBe('1. 推进' + metadata + '\n未知段落');
  });
});
