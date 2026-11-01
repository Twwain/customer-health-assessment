// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AssessmentPlan } from "./AssessmentPlan";

describe("下一步计划精简表格", () => {
  const item = { priority: "recommended", title: "形成客户价值测算表", urgency: "medium",
    reason: "重复原因", action: "选择口径\n收集基线\n前置行动：取得业务方认可",
    expected_outcome: "重复预期", reference: "", suggested_owner: "客户经理与方案经理",
    suggested_timing: "两周内", deliverable: "<b>投入|收益表</b>" };

  it("四列突出重点，完整步骤按需展开且保持文本安全", () => {
    const { container } = render(<AssessmentPlan items={[item]} />);
    expect(screen.getAllByRole("columnheader").map((node) => node.textContent))
      .toEqual(["行动", "责任人", "时限", "交付物"]);
    expect(screen.getByText(item.suggested_owner)).toBeVisible();
    expect(screen.getByText(item.deliverable)).toBeVisible();
    expect(screen.queryByText(item.reason)).not.toBeInTheDocument();
    expect(screen.getByText(/前置行动/)).not.toBeVisible();
    fireEvent.click(screen.getByText("查看完整步骤"));
    expect(screen.getByText(/前置行动/)).toBeVisible();
    expect(container.querySelector("b")).toBeNull();
  });
});
