// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { RefsBox } from "./RefsBox";

describe("检索来源与实际引用分别显示", () => {
  it("按真实文档分组，保留仅检索状态并不伪造缺失相关度", () => {
    const refs = Array.from({ length: 5 }, (_, index) => ({
      document_id: index < 3 ? 11 : 22, chunk_id: index + 1,
      title: index < 3 ? "文档甲" : "文档乙", category: "制度",
      snippet: "原文" + index, score: null, used: index === 3,
    }));
    const trace = vi.fn();
    const { container } = render(<RefsBox references={refs} onTrace={trace} />);
    fireEvent.click(screen.getByRole("button", { name: /共检索 2 个文档 \/ 5 段/ }));
    expect(screen.getByText("仅检索")).toBeInTheDocument();
    expect(screen.getByText("已引用 1")).toBeInTheDocument();
    expect(container.textContent).not.toContain("0.00");
    fireEvent.click(screen.getByRole("button", { name: /文档甲/ }));
    expect(trace).toHaveBeenCalledWith(refs.slice(0, 3));
  });
});
