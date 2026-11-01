import { describe, expect, it } from "vitest";
import type { ChatMessageItem, FactorConfigResponse } from "../types";
import { completeAnswerText } from "./chatAnswer";
import { factorNames, nameFactors, presentFactorNames } from "./factorNames";
import { plainTextBlock } from "./plainText";

const config = { dimensions: [{ factors: [
  { label: "SVC-03 故障响应时效" }, { label: "SVC-04 故障解决效率" },
  { label: "SVC-05 主动服务频次" }, { label: "KCR-10B 决策流程影响力" },
] }] } as FactorConfigResponse;
const names = factorNames(config);

describe("分析中的因子业务名称", () => {
  it("直接问答只改开放文本中的编号，不改普通事实行", () => {
    const content = '记录原值为「SVC-03」。\n:::ai-text "先提升SVC-03，再改善SVC-05。"';
    const message = { role: 'assistant', scenario: 'free_qa', content, strategy_items: [], references: [] } as unknown as ChatMessageItem;
    const displayed = presentFactorNames(message, names);
    expect(displayed.content.split('\n')[0]).toBe('记录原值为「SVC-03」。');
    expect(plainTextBlock(displayed.content.split('\n')[1])).toBe('先提升故障响应时效，再改善主动服务频次。');
    expect(message.content).toBe(content);
  });

  it("从配置映射完整编号，保留未知编号和更长标识", () => {
    expect(nameFactors("SVC-03、SVC-04、SVC-05和KCR-10B；SVC-99、XSVC-03、SVC-030、SVC-03A", names))
      .toBe("故障响应时效、故障解决效率、主动服务频次和决策流程影响力；SVC-99、XSVC-03、SVC-030、SVC-03A");
  });

  it("已有业务名称不重复，保留段落缩进和特殊字符", () => {
    expect(nameFactors("  SVC-03 故障响应时效；SVC-04（故障解决效率）；SVC-05(主动服务频次)\n<>&", names))
      .toBe("  故障响应时效；故障解决效率；主动服务频次\n<>&");
  });

  it("只改展示分析和行动，事实、知识原文、消息原值与用户文字不变；复制同步", () => {
    const body = '本次按以下任务回答：综合评估\n### 总览\nSVC-03 事实原文\n## AI 分析（业务判断，可能有误）\n### 总体评估\n:::ai-text "SVC-03有基础。<img src=x>\\n##标题"\n### 主要风险点\n:::ai-text "SVC-04需要改善"\n### 优势\n:::ai-text "SVC-05稳定"';
    const msg = {
      role: "assistant", content: body, strategy_items: [{ title: "提升SVC-04", reason: "SVC-03", action: "SVC-05", expected_outcome: "SVC-04", reference: "SVC-03原文" }],
      references: [{ title: "SVC-03原文", snippet: "SVC-04原文" }],
    } as ChatMessageItem;
    const displayed = presentFactorNames(msg, names);
    expect(displayed.content).toContain("SVC-03 事实原文");
    expect(plainTextBlock(displayed.content.split("\n")[5])).toBe("故障响应时效有基础。<img src=x>\n##标题");
    expect(displayed.strategy_items[0].title).toBe("提升故障解决效率");
    expect(displayed.strategy_items[0].reference).toBe("SVC-03原文");
    expect(displayed.references).toBe(msg.references);
    expect(msg.content).toBe(body);
    expect(completeAnswerText(displayed)).toContain("行动：提升故障解决效率");
    expect(presentFactorNames({ ...msg, role: "user" }, names).content).toBe(body);
  });
});
