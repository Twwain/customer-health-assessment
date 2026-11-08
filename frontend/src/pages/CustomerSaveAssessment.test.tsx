// @vitest-environment jsdom
import { MemoryRouter, useLocation } from "react-router-dom";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { chat, customers } from "../api";
import { UiFeedbackProvider } from "../components/UiFeedback";
import type { AssessmentResponse, AssessmentTrendResponse, CustomerResponse, FactorConfigResponse } from "../types";
import CustomerList from "./CustomerList";

vi.mock("../hooks", () => ({ useIsMobile: () => false }));
vi.mock("../api", () => ({
  chat: { createSession: vi.fn() },
  customers: { list: vi.fn(), industries: vi.fn(), factorConfig: vi.fn(), assessment: vi.fn(), trend: vi.fn(),
    create: vi.fn(), update: vi.fn(), updateFactors: vi.fn() },
}));
const customer = (id: number): CustomerResponse => ({
  id, customer_name: `客户${id}`, industry: "制造", contact_person: "", contact_phone: "", cooperation_years: 0,
  contact_frequency: "", customer_satisfaction: 0, contract_amount: 0, growth_potential: "", notes: "", custom_fields: {}, created_at: "", updated_at: "",
});
const score = (id: number): AssessmentResponse => ({ customer_id: id, customer_name: `客户${id}`, total_score: id === 201 ? 99 : 50,
  max_score: 100, level: "健康", level_color: "", dimensions: [], risk_alerts: [], alerts: [], suggestions: [], config_version: "v1", assessed_at: "" });
const trend = (id: number, previous: number | null = 50): AssessmentTrendResponse => ({
  customer_id: id, customer_name: `客户${id}`, max_score: 100, points: [], latest_score: score(id).total_score,
  previous_score: previous, delta: id === 201 ? 49 : 0, trend: id === 201 ? "up" : "flat", level: "健康", level_color: "", level_lines: [],
});
const config: FactorConfigResponse = {
  version: "v1", updated_at: "", description: "", strategy: "", total_max_score: 100, levels: [], dimensions: [{
    key: "kcr", name: "客户关系网络", enabled: true, max_score: 100, description: "", factors: [
      { field: "relation_note", label: "关系备注", input: { type: "text", options: [], min: null, max: null, step: null, unit: "", placeholder: "补充关系备注" } },
      { field: "kcr_02", label: "关键人客情等级", input: { type: "key_person_levels", options: ["3", "2", "1", "0", "-1"], min: null, max: null, step: null, unit: "", placeholder: "" } },
    ].map((factor) => ({ ...factor, weight: 1, source: "model", source_role: "AR", description: "", sub_dimension: "", rule_text: "", rule_type: "", editable: true })),
  }],
};
const page = (items: CustomerResponse[], total = items.length, number = 1) => ({ items, total, page: number, page_size: 200 });

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(customers.list).mockResolvedValue(page([]));
  vi.mocked(customers.industries).mockResolvedValue(["制造"]);
  vi.mocked(customers.factorConfig).mockResolvedValue(config);
  vi.mocked(customers.assessment).mockImplementation(async (id) => score(id));
  vi.mocked(customers.trend).mockImplementation(async (id) => trend(id));
  vi.mocked(customers.create).mockResolvedValue(customer(10));
  vi.mocked(customers.update).mockResolvedValue(customer(10));
  vi.mocked(customers.updateFactors).mockResolvedValue({ customer: customer(10), assessment: score(10), updated_fields: [], ignored_fields: [] });
});
function RouteState() {
  const location = useLocation();
  return <output data-testid="route-state">{JSON.stringify({ path: location.pathname, state: location.state })}</output>;
}
function renderList() {
  return render(<UiFeedbackProvider><MemoryRouter initialEntries={["/customers"]}><CustomerList /><RouteState /></MemoryRouter></UiFeedbackProvider>);
}
describe("CustomerList 保存后综合评估", () => {
  async function openEditor() {
    vi.mocked(customers.list).mockResolvedValue(page([customer(10)]));
    renderList();
    await screen.findAllByText("客户10");
    fireEvent.click(within(screen.getByRole("table")).getByText("客户10"));
    await screen.findByRole("button", { name: "保存并重新评分" });
  }

  it("保存完成后使用最新客户信息创建综合评估并携带自动评估状态跳转", async () => {
    let finishSave!: (value: Awaited<ReturnType<typeof customers.updateFactors>>) => void;
    vi.mocked(customers.updateFactors).mockReturnValueOnce(new Promise((resolve) => { finishSave = resolve; }));
    vi.mocked(chat.createSession).mockResolvedValue({ id: 77 } as Awaited<ReturnType<typeof chat.createSession>>);
    await openEditor();
    fireEvent.change(screen.getByDisplayValue("客户10"), { target: { value: "更名客户" } });
    fireEvent.click(screen.getByRole("button", { name: "保存并重新评分" }));
    await waitFor(() => expect(customers.updateFactors).toHaveBeenCalledWith(10, {}));
    expect(customers.update).toHaveBeenCalledWith(10, { customer_name: "更名客户" });
    expect(chat.createSession).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "保存中…" })).toBeDisabled();
    finishSave({ customer: { ...customer(10), customer_name: "更名客户" }, assessment: score(10), updated_fields: [], ignored_fields: [] });
    await waitFor(() => expect(screen.getByTestId("route-state")).toHaveTextContent('"path":"/chat/77","state":{"autoAssess":true}'));
    expect(chat.createSession).toHaveBeenCalledExactlyOnceWith({ title: "更名客户 · 综合评估", customer_id: 10, scenario: "assessment" });
    expect(screen.queryByRole("button", { name: "保存并重新评分" })).not.toBeInTheDocument();
  });

  it("保存失败保留编辑内容且不创建评估或跳转", async () => {
    vi.mocked(customers.updateFactors).mockRejectedValueOnce(new Error("评分失败"));
    await openEditor();
    fireEvent.click(screen.getByRole("button", { name: /客户关系网络/ }));
    fireEvent.change(screen.getByPlaceholderText("补充关系备注"), { target: { value: "待保存内容" } });
    fireEvent.click(screen.getByRole("button", { name: "保存并重新评分" }));
    expect(await screen.findByText("保存失败：评分失败")).toBeInTheDocument();
    expect(screen.getByDisplayValue("待保存内容")).toBeInTheDocument();
    expect(chat.createSession).not.toHaveBeenCalled();
    expect(screen.getByTestId("route-state")).toHaveTextContent('"path":"/customers"');
  });

  it("评估会话创建失败时保留已保存的评分，不误报保存失败", async () => {
    vi.mocked(chat.createSession).mockRejectedValueOnce(new Error("offline"));
    await openEditor();
    fireEvent.click(screen.getByRole("button", { name: "保存并重新评分" }));
    expect(await screen.findByText("创建评估会话失败")).toBeInTheDocument();
    expect(screen.queryByText(/保存失败/)).not.toBeInTheDocument();
    expect(customers.updateFactors).toHaveBeenCalledOnce();
    expect(screen.getByTestId("route-state")).toHaveTextContent('"path":"/customers"');
  });
});
