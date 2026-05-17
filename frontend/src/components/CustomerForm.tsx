import { useState, useEffect } from "react";
import { Customer } from "../api";

interface Props {
  data?: Partial<Customer>;
  onChange: (data: Partial<Customer>) => void;
  readOnly?: boolean;
}

const freqOptions = ["每周", "双周", "每月", "每季度", "不定期"];
const paymentOptions = ["正常", "部分逾期", "严重逾期"];
const growthOptions = ["高", "中", "低"];

export default function CustomerForm({ data = {}, onChange, readOnly }: Props) {
  const update = (key: string, value: unknown) => {
    if (!readOnly) onChange({ ...data, [key]: value });
  };

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
      <Field label="客户名称" required>
        <input
          type="text"
          value={data.customer_name ?? ""}
          onChange={(e) => update("customer_name", e.target.value)}
          disabled={readOnly}
          className={inputClass}
          placeholder="请输入客户名称"
        />
      </Field>

      <Field label="所属行业">
        <input
          type="text"
          value={data.industry ?? ""}
          onChange={(e) => update("industry", e.target.value)}
          disabled={readOnly}
          className={inputClass}
          placeholder="如：信息技术、金融"
        />
      </Field>

      <Field label="对接人">
        <input
          type="text"
          value={data.contact_person ?? ""}
          onChange={(e) => update("contact_person", e.target.value)}
          disabled={readOnly}
          className={inputClass}
        />
      </Field>

      <Field label="联系电话">
        <input
          type="text"
          value={data.contact_phone ?? ""}
          onChange={(e) => update("contact_phone", e.target.value)}
          disabled={readOnly}
          className={inputClass}
        />
      </Field>

      <Field label="合作年限">
        <input
          type="number"
          step="0.5"
          min="0"
          value={data.cooperation_years ?? 0}
          onChange={(e) => update("cooperation_years", parseFloat(e.target.value) || 0)}
          disabled={readOnly}
          className={inputClass}
        />
      </Field>

      <Field label="沟通频率">
        <select
          value={data.contact_frequency ?? "每月"}
          onChange={(e) => update("contact_frequency", e.target.value)}
          disabled={readOnly}
          className={inputClass}
        >
          {freqOptions.map((o) => (
            <option key={o} value={o}>{o}</option>
          ))}
        </select>
      </Field>

      <Field label="最近联系日期">
        <input
          type="date"
          value={data.last_contact_date?.split("T")[0] ?? ""}
          onChange={(e) => update("last_contact_date", e.target.value || null)}
          disabled={readOnly}
          className={inputClass}
        />
      </Field>

      <Field label="客户满意度 (1-10)">
        <input
          type="range"
          min="1"
          max="10"
          value={data.customer_satisfaction ?? 5}
          onChange={(e) => update("customer_satisfaction", parseInt(e.target.value))}
          disabled={readOnly}
          className="w-full accent-amber-500"
        />
        <div className="text-center text-lg font-bold text-amber-600">
          {data.customer_satisfaction ?? 5} 分
        </div>
      </Field>

      <Field label="合同金额 (万元)">
        <input
          type="number"
          min="0"
          step="1"
          value={data.contract_amount ?? 0}
          onChange={(e) => update("contract_amount", parseFloat(e.target.value) || 0)}
          disabled={readOnly}
          className={inputClass}
        />
      </Field>

      <Field label="回款情况">
        <select
          value={data.payment_status ?? "正常"}
          onChange={(e) => update("payment_status", e.target.value)}
          disabled={readOnly}
          className={inputClass}
        >
          {paymentOptions.map((o) => (
            <option key={o} value={o}>{o}</option>
          ))}
        </select>
      </Field>

      <Field label="增长潜力">
        <select
          value={data.growth_potential ?? "中"}
          onChange={(e) => update("growth_potential", e.target.value)}
          disabled={readOnly}
          className={inputClass}
        >
          {growthOptions.map((o) => (
            <option key={o} value={o}>{o}</option>
          ))}
        </select>
      </Field>

      <Field label="竞品介入">
        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={data.competitor_involvement ?? false}
            onChange={(e) => update("competitor_involvement", e.target.checked)}
            disabled={readOnly}
            className="w-4 h-4 rounded border-slate-300 accent-amber-500"
          />
          <span className="text-sm text-slate-700">是</span>
        </label>
      </Field>

      <Field label="风险信号" full>
        <textarea
          value={data.risk_signals ?? ""}
          onChange={(e) => update("risk_signals", e.target.value)}
          disabled={readOnly}
          className={inputClass}
          rows={2}
          placeholder="描述客户当前存在的风险信号"
        />
      </Field>

      <Field label="备注" full>
        <textarea
          value={data.notes ?? ""}
          onChange={(e) => update("notes", e.target.value)}
          disabled={readOnly}
          className={inputClass}
          rows={3}
          placeholder="其他备注信息"
        />
      </Field>

      {/* 自定义扩展字段 */}
      {data.custom_fields && Object.keys(data.custom_fields).length > 0 && (
        <>
          <div className="md:col-span-2 mt-2">
            <h3 className="text-sm font-semibold text-slate-600 border-t border-slate-200 pt-4 pb-2 bg-slate-50 -mx-2 px-2 rounded-lg">
              扩展字段
            </h3>
          </div>
          {Object.entries(data.custom_fields).map(([key, value]) => (
            <Field key={key} label={key}>
              <input
                type="text"
                value={value}
                onChange={(e) => {
                  const newFields = { ...data.custom_fields, [key]: e.target.value };
                  onChange({ ...data, custom_fields: newFields });
                }}
                disabled={readOnly}
                className={inputClass}
              />
            </Field>
          ))}
        </>
      )}
    </div>
  );
}

function Field({
  label,
  required,
  full,
  children,
}: {
  label: string;
  required?: boolean;
  full?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className={full ? "md:col-span-2" : ""}>
      <label className="block text-sm font-medium text-slate-700 mb-1.5">
        {label}
        {required && <span className="text-red-500 ml-0.5">*</span>}
      </label>
      {children}
    </div>
  );
}

const inputClass =
  "w-full px-3 py-2 border border-slate-300 rounded-xl text-sm focus:ring-2 focus:ring-amber-500 focus:border-amber-500 outline-none disabled:bg-slate-50 disabled:text-slate-500 transition-colors";
