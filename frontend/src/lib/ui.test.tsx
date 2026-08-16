// @vitest-environment jsdom

import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { renderMarkdown } from "./ui";

describe("renderMarkdown", () => {
  it("renders GFM-style tables with inline formatting", () => {
    const markdown = [
      "| 观察维度 | 关注点 |",
      "|---|---|",
      "| **RISK 竞争态势** | 持续跟踪竞品动态 |",
      "| KCR 关键人关系 | 关注岗位变动 |",
    ].join("\n");

    render(<div>{renderMarkdown(markdown)}</div>);

    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("columnheader")).toHaveLength(2);
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    expect(within(table).getByText("RISK 竞争态势").tagName).toBe("STRONG");
    expect(within(table).getByText("持续跟踪竞品动态")).toBeInTheDocument();
  });

  it("renders horizontal rules instead of plain text", () => {
    const { container } = render(<div>{renderMarkdown("上文\n\n---\n\n下文")}</div>);
    expect(container.querySelector("hr")).toBeInTheDocument();
    expect(screen.getByText("上文")).toBeInTheDocument();
    expect(screen.getByText("下文")).toBeInTheDocument();
  });

  it("keeps escaped and inline-code pipes inside a table cell", () => {
    const markdown = [
      "| 类型 | 内容 |",
      "|---|---|",
      "| 转义 | A \\| B |",
      "| 代码 | `a|b` |",
    ].join("\n");

    render(<div>{renderMarkdown(markdown)}</div>);

    const rows = within(screen.getByRole("table")).getAllByRole("row");
    expect(within(rows[1]).getAllByRole("cell")).toHaveLength(2);
    expect(within(rows[1]).getByText("A | B")).toBeInTheDocument();
    expect(within(rows[2]).getByText("a|b").tagName).toBe("CODE");
  });

  it("keeps an escaped pipe at the end of a row without a border pipe", () => {
    const markdown = ["| 类型 | 内容 |", "|---|---|", "| 转义 | A \\|"].join("\n");
    render(<div>{renderMarkdown(markdown)}</div>);
    const rows = within(screen.getByRole("table")).getAllByRole("row");
    expect(within(rows[1]).getAllByRole("cell")).toHaveLength(2);
    expect(within(rows[1]).getByText("A |")).toBeInTheDocument();
  });
});
