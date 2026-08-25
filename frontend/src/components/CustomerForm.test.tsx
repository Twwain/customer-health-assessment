// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import CustomerForm from "./CustomerForm";
import type { CustomerResponse, FactorConfigResponse } from "../types";

const customer: CustomerResponse = {
  id: 1,
  customer_name: "测试客户",
  industry: "金融",
  contact_person: "张三",
  contact_phone: "13800000000",
  cooperation_years: 2,
  contact_frequency: "每月",
  customer_satisfaction: 8,
  contract_amount: 100,
  growth_potential: "高",
  notes: "",
  custom_fields: {},
  created_at: "2026-01-01T00:00:00",
  updated_at: "2026-01-01T00:00:00",
};

const textInput = {
  type: "text",
  options: [] as string[],
  min: null,
  max: null,
  step: null,
  unit: "",
  placeholder: "",
};

const config: FactorConfigResponse = {
  version: "2026.08",
  updated_at: "",
  description: "",
  strategy: "config",
  total_max_score: 20,
  dimensions: [
    {
      key: "kcr",
      name: "客户关系网络",
      max_score: 20,
      enabled: true,
      description: "",
      factors: [
        {
          field: "kcr_01",
          label: "已识别决策链人数占比",
          weight: 2.1,
          source: "model",
          source_role: "AR",
          description: "原子指标：已识别决策链人数占比",
          sub_dimension: "决策链覆盖度",
          rule_text: "",
          rule_type: "threshold",
          editable: true,
          input: { ...textInput },
        },
        {
          field: "kcr_02",
          label: "关键人客情等级",
          weight: 2.1,
          source: "model",
          source_role: "AR",
          description: "各关键人等级加权平均",
          sub_dimension: "信息互通",
          rule_text: "",
          rule_type: "mapping",
          editable: true,
          input: { ...textInput, type: "key_person_levels", options: ["3", "2", "1", "0", "-1"] },
        },
      ],
    },
  ],
  levels: [],
};

describe("CustomerForm 二级维度分组", () => {
  it("维度默认收起，展开后按 sub_dimension 分组展示因子", async () => {
    const user = userEvent.setup();
    render(
      <CustomerForm
        customer={customer}
        config={config}
        value={{}}
        onChange={vi.fn()}
        readOnly={false}
      />,
    );

    // 维度默认收起：因子不可见
    expect(screen.queryByText("已识别决策链人数占比")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /客户关系网络/ }));

    // 二级维度分组头与因子明细可见
    expect(screen.getByText("决策链覆盖度")).toBeInTheDocument();
    expect(screen.getByText("信息互通")).toBeInTheDocument();
    expect(screen.getByText("已识别决策链人数占比")).toBeInTheDocument();
    expect(screen.getByText("关键人客情等级")).toBeInTheDocument();
    // 描述按后端清洗后原样展示
    expect(screen.getByText("原子指标：已识别决策链人数占比")).toBeInTheDocument();
  });

  it("收起某个二级维度分组后其因子隐藏", async () => {
    const user = userEvent.setup();
    render(
      <CustomerForm
        customer={customer}
        config={config}
        value={{}}
        onChange={vi.fn()}
        readOnly={false}
      />,
    );
    await user.click(screen.getByRole("button", { name: /客户关系网络/ }));

    await user.click(screen.getByRole("button", { name: /信息互通/ }));
    expect(screen.queryByText("关键人客情等级")).not.toBeInTheDocument();
    expect(screen.getByText("已识别决策链人数占比")).toBeInTheDocument();
  });

  it("KCR-02 以等级量表点选五位关键人，再点已选档位取消", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const { rerender } = render(
      <CustomerForm customer={customer} config={config} value={{}} onChange={onChange} />,
    );
    await user.click(screen.getByRole("button", { name: /客户关系网络/ }));

    // 点选关键人 5 的反对档
    await user.click(screen.getByRole("button", { name: "关键人 5 -1 - 反对" }));
    expect(onChange).toHaveBeenLastCalledWith({ kcr_02: ["", "", "", "", "-1"] });

    // 已填值渲染：关键人 5 的 -1 档位为选中态
    rerender(
      <CustomerForm
        customer={customer}
        config={config}
        value={{ kcr_02: ["3", "2", "1", "0", "-1"] }}
        onChange={onChange}
      />,
    );
    expect(screen.getByRole("button", { name: "关键人 5 -1 - 反对" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "关键人 1 3 - 教练级" })).toHaveAttribute("aria-pressed", "true");

    // 再点已选中的档位 = 取消选择
    await user.click(screen.getByRole("button", { name: "关键人 1 3 - 教练级" }));
    expect(onChange).toHaveBeenLastCalledWith({ kcr_02: ["", "2", "1", "0", "-1"] });
  });
});
