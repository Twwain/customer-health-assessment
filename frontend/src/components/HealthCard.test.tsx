// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import HealthCard from "./HealthCard";
import type { AssessmentResponse } from "../types";

function assessmentWithAlerts(count: number): AssessmentResponse {
  return {
    customer_id: 1,
    customer_name: "测试客户",
    total_score: 50,
    max_score: 100,
    level: "亚健康",
    level_color: "#eab308",
    dimensions: [],
    risk_alerts: Array.from({ length: count }, (_, i) => `预警 ${i + 1}`),
    alerts: Array.from({ length: count }, (_, i) => ({
      id: `alert_${i + 1}`,
      level: i < 5 ? "high" : "medium",
      message: `预警 ${i + 1}`,
    })),
    suggestions: [],
    config_version: "test",
    assessed_at: "2026-08-22T12:00:00Z",
  };
}

describe("HealthCard alert preview", () => {
  it("shows eight alerts by default and can expand the full list", async () => {
    const user = userEvent.setup();
    render(<HealthCard assessment={assessmentWithAlerts(10)} showTrendButton={false} />);

    expect(screen.getByText(/预警 8/)).toBeInTheDocument();
    expect(screen.queryByText(/预警 9/)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "查看其余 2 项" }));

    expect(screen.getByText(/预警 9/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "收起预警" })).toBeInTheDocument();
  });
});
