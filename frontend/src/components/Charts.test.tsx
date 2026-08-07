// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { TrendChart } from "./Charts";
import type { AssessmentTrendResponse } from "../types";

function trend(points: Array<[string, number]>): AssessmentTrendResponse {
  const items = points.map(([label, total_score]) => ({
    assessed_at: "2026-07-01T00:00:00",
    label,
    total_score,
    level: "健康",
    dimensions: {},
  }));
  return {
    customer_id: 1,
    customer_name: "测试客户",
    max_score: 100,
    points: items,
    latest_score: items[items.length - 1]?.total_score ?? 0,
    previous_score: items.length >= 2 ? items[items.length - 2].total_score : null,
    delta: 0,
    trend: "flat",
    level: "健康",
    level_color: "#22c55e",
    level_lines: [],
  };
}

describe("TrendChart", () => {
  it("单点数据只渲染末点，不产生非法曲线路径", () => {
    const { container } = render(<TrendChart trend={trend([["07-01", 42]])} color="#22c55e" width={200} height={100} />);
    expect(screen.getByText("42")).toBeInTheDocument();
    expect(container.querySelector("svg circle")).not.toBeNull();
    expect(container.querySelector("path")?.getAttribute("d") ?? "").not.toContain(" L");
  });

  it("多点数据渲染曲线与末点标注", () => {
    const { container } = render(
      <TrendChart trend={trend([["07-01", 42], ["08-01", 66]])} color="#22c55e" width={200} height={100} />,
    );
    const d = container.querySelector("path")?.getAttribute("d") ?? "";
    expect(d.startsWith("M")).toBe(true);
    expect(screen.getByText("66")).toBeInTheDocument();
  });
});
