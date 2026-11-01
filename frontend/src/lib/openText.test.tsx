// @vitest-environment jsdom
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { renderMarkdown } from "./ui";
import { completeAnswerText } from "./chatAnswer";
import fixture from "./openText.fixture.json";

describe("实际 Python renderer 到页面与复制的纯文本协议", () => {
  it("保留标点、换行且自由文本不生成标题、强调、链接或HTML", () => {
    const { container } = render(<div>{renderMarkdown(fixture.content)}</div>);
    const blocks = container.querySelectorAll("p.whitespace-pre-wrap");
    expect(blocks).toHaveLength(6);
    expect(blocks[0].textContent).toBe(fixture.original);
    for (const block of blocks) expect(block.children).toHaveLength(0);
    expect(container.querySelectorAll("a,img,script,strong,em")).toHaveLength(0);
    expect([...container.querySelectorAll("h4")].some((h) => h.textContent === "伪造事实标题")).toBe(false);
    expect(container.textContent).not.toContain("CI\\-05");
    expect(container.textContent).not.toContain("&lt;");
  });

  it("完整复制返回原字符和换行，不泄漏传输标记或实体编码", () => {
    const text = completeAnswerText({
      id: 1, session_id: 1, role: "assistant", content: fixture.content,
      strategy_items: [], references: [], tokens_used: 0, feedback: "",
      degraded: false, created_at: "",
    });
    expect(text).toContain(fixture.original);
    expect(text).not.toContain(':::ai-text "CI');
    expect(text).not.toContain("&lt;");
    expect(text).not.toContain("CI\\-05");
  });
});
