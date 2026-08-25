import { useState } from "react";
import type { CustomerResponse, FactorConfigItem, FactorConfigResponse } from "../types";
import { groupFactors } from "../lib/factorGroups";
import { BASIC_FIELDS } from "../lib/customerFields";

interface CustomerFormProps {
  customer: CustomerResponse;
  config: FactorConfigResponse;
  value: Record<string, unknown>;
  onChange: (next: Record<string, unknown>) => void;
  readOnly?: boolean;
}

function getBase(customer: CustomerResponse, config: FactorConfigResponse): Record<string, unknown> {
  const base: Record<string, unknown> = {};
  for (const f of BASIC_FIELDS) {
    base[f] = (customer as unknown as Record<string, unknown>)[f] ?? "";
  }
  for (const dim of config.dimensions) {
    for (const f of dim.factors) {
      if (f.source === "custom_fields") {
        base[f.field] = customer.custom_fields?.[f.field] ?? "";
      } else {
        const v = (customer as unknown as Record<string, unknown>)[f.field];
        base[f.field] = v ?? (f.input.type === "bool" ? false : "");
      }
    }
  }
  return base;
}

const KEY_PERSON_OPTIONS = [
  { value: "3", label: "3 - 教练级", color: "#1AAE39" },
  { value: "2", label: "2 - 强支持", color: "#5CB85C" },
  { value: "1", label: "1 - 支持", color: "#A6CE39" },
  { value: "0", label: "0 - 中立", color: "#9E9E9E" },
  { value: "-1", label: "-1 - 反对", color: "#E03131" },
];

function parseKeyPersonLevels(value: unknown): string[] {
  let raw: unknown = value;
  if (typeof value === "string" && value.trim()) {
    try {
      raw = JSON.parse(value);
    } catch {
      raw = value.replace(/^\[|\]$/g, "").split(",").map((item) => item.trim());
    }
  }
  const allowed = new Set(KEY_PERSON_OPTIONS.map((option) => option.value));
  const values = Array.isArray(raw) ? raw.slice(0, 5).map(String) : [];
  return Array.from({ length: 5 }, (_, index) => allowed.has(values[index]) ? values[index] : "");
}

export default function CustomerForm({ customer, config, value, onChange, readOnly }: CustomerFormProps) {
  const base = getBase(customer, config);
  const merged: Record<string, unknown> = { ...base, ...value };
  const enabledDims = config.dimensions.filter((d) => d.enabled);
  // 客情因子按维度折叠，默认全部收起（28 个因子全展开仍会影响录入体验）
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(enabledDims.map((d) => [d.key, true]))
  );
  // 二级维度组：默认展开（一级维度默认收起，展开后直接看到因子分组）
  const [subCollapsed, setSubCollapsed] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(
      enabledDims.flatMap((d) => groupFactors(d.factors).map((g) => [`${d.key}::${g.name}`, false])),
    ),
  );
  const allCollapsed = enabledDims.every((d) => collapsed[d.key]);
  const toggleAll = () => {
    const next = !allCollapsed;
    setCollapsed(Object.fromEntries(enabledDims.map((d) => [d.key, next])));
    // 全部展开 / 收起时二级维度组同步重置为展开，避免手动收起的分组残留
    setSubCollapsed(
      Object.fromEntries(
        enabledDims.flatMap((d) => groupFactors(d.factors).map((g) => [`${d.key}::${g.name}`, false])),
      ),
    );
  };
  const filledCount = (dim: (typeof enabledDims)[number]) =>
    dim.factors.filter((f) => {
      const v = merged[f.field];
      if (v === undefined || v === null || v === "" || v === false) return false;
      // select：只有新版配置中的合法选项才算已填。
      if (f.input.type === "select") {
        return typeof v === "string" && f.input.options.includes(v);
      }
      if (f.input.type === "key_person_levels") {
        return parseKeyPersonLevels(v).every(Boolean);
      }
      // slider：0 表示未选择（后端清空时存 0）
      if (f.input.type === "slider") return v !== 0 && v !== "0";
      return true;
    }).length;

  const set = (field: string, v: unknown) => onChange({ ...value, [field]: v });

  return (
    <div className="space-y-3">
      {/* 区块一：客户基本信息 */}
      <div className="rounded-xl border border-border bg-surface p-3">
        <div className="mb-2.5 flex items-center gap-2">
          <span className="text-[14.5px] font-semibold text-ink">📇 客户基本信息</span>
        </div>
        <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
          <BasicField
            label="客户名称 *"
            type="text"
            value={String(merged.customer_name ?? "")}
            readOnly={readOnly}
            placeholder="如：示例汽车集团"
            onChange={(v) => set("customer_name", v)}
          />
          <BasicField
            label="行业"
            type="text"
            value={String(merged.industry ?? "")}
            readOnly={readOnly}
            placeholder="如：制造业"
            onChange={(v) => set("industry", v)}
          />
          <BasicField
            label="对接人"
            type="text"
            value={String(merged.contact_person ?? "")}
            readOnly={readOnly}
            placeholder="客户方联系人"
            onChange={(v) => set("contact_person", v)}
          />
          <BasicField
            label="联系电话"
            type="text"
            value={String(merged.contact_phone ?? "")}
            readOnly={readOnly}
            placeholder="13800000000"
            onChange={(v) => set("contact_phone", v)}
          />
          <BasicField
            label="备注"
            type="textarea"
            value={String(merged.notes ?? "")}
            readOnly={readOnly}
            placeholder="重点跟进客户等"
            onChange={(v) => set("notes", v)}
            wide
          />
        </div>
      </div>

      {/* 区块二：客情因子信息 */}
      <div className="rounded-xl border border-border bg-surface p-3">
        <div className="mb-2.5 flex items-center gap-2">
          <span className="text-[14.5px] font-semibold text-ink">🧩 客情因子信息</span>
          <span className="rounded bg-surface-2 px-1.5 py-[1px] text-[12px] text-muted">维度驱动评分</span>
          <button
            type="button"
            onClick={toggleAll}
            className="ml-auto rounded-lg border border-border bg-surface px-2 py-1 text-[12px] text-ink-2 transition hover:border-accent hover:text-accent"
          >
            {allCollapsed ? "全部展开" : "全部收起"}
          </button>
        </div>
        <div className="divide-y divide-border-soft">
          {enabledDims.map((dim, di) => {
            const isOpen = !collapsed[dim.key];
            const filled = filledCount(dim);
            return (
              <div key={dim.key} className={di === 0 ? "pb-3" : "py-3"}>
                <button
                  type="button"
                  onClick={() => setCollapsed((s) => ({ ...s, [dim.key]: !s[dim.key] }))}
                  className="mb-2.5 flex w-full items-center gap-2 text-left"
                >
                  <span className={`text-[11px] text-muted transition-transform ${isOpen ? "rotate-90" : ""}`}>▶</span>
                  <span className="text-[14.5px] font-semibold text-ink">
                    {["①", "②", "③", "④", "⑤", "⑥", "⑦"][di] ?? "•"} {dim.name}
                  </span>
                  <span className="rounded bg-surface-2 px-1.5 py-[1px] text-[12px] text-muted">
                    维度权重 {dim.max_score} 分
                  </span>
                  {filled > 0 && (
                    <span className="rounded bg-accent-soft px-1.5 py-[1px] text-[12px] text-accent">
                      已填 {filled}/{dim.factors.length}
                    </span>
                  )}
                </button>
                {isOpen && (
                  <div className="space-y-2.5">
                    {groupFactors(dim.factors).map((g) => {
                      const subKey = `${dim.key}::${g.name}`;
                      const subOpen = !subCollapsed[subKey];
                      return (
                        <div key={subKey}>
                          <button
                            type="button"
                            onClick={() => setSubCollapsed((s) => ({ ...s, [subKey]: !s[subKey] }))}
                            className="mb-2 flex w-full items-center gap-2 text-left"
                          >
                            <span className={`text-[11px] text-muted transition-transform ${subOpen ? "rotate-90" : ""}`}>▶</span>
                            <span className="text-[13px] font-semibold text-ink-2">{g.name}</span>
                            <span className="rounded bg-surface-2 px-1.5 py-[1px] text-[12px] text-muted">{g.factors.length} 项</span>
                          </button>
                          {subOpen && (
                            <div className="space-y-2.5">
                              {g.factors.map((f) => (
                                <FactorField
                                  key={f.field}
                                  factor={f}
                                  value={merged[f.field]}
                                  readOnly={readOnly}
                                  onChange={(v) => set(f.field, v)}
                                />
                              ))}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function BasicField({
  label,
  type,
  value,
  readOnly,
  placeholder,
  onChange,
  wide,
}: {
  label: string;
  type: "text" | "textarea";
  value: string;
  readOnly?: boolean;
  placeholder?: string;
  onChange: (v: string) => void;
  wide?: boolean;
}) {
  const inputCls =
    "w-full rounded-lg border border-border-strong bg-surface px-3 py-2 text-[14px] text-ink outline-none focus:border-accent focus:ring-2 focus:ring-accent/20 disabled:bg-surface-2 disabled:text-muted";
  return (
    <div className={wide ? "sm:col-span-2" : ""}>
      <label className="mb-1 block text-[13.5px] font-medium text-ink-2">{label}</label>
      {type === "textarea" ? (
        <textarea
          className={inputCls}
          rows={2}
          value={value}
          disabled={readOnly}
          placeholder={placeholder}
          onChange={(e) => onChange(e.target.value)}
        />
      ) : (
        <input
          type="text"
          className={inputCls}
          value={value}
          disabled={readOnly}
          placeholder={placeholder}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
    </div>
  );
}

/** 因子输入有效性校验：数字/滑杆需在 min-max 范围内。 */
function validateFactor(factor: FactorConfigItem, value: unknown): string {
  const t = factor.input.type;
  if ((t === "number" || t === "slider") && value !== "" && value != null) {
    const n = Number(value);
    if (Number.isNaN(n)) return "请输入有效数字";
    if (factor.input.min != null && n < factor.input.min) return `不能小于 ${factor.input.min}`;
    if (factor.input.max != null && n > factor.input.max) return `不能大于 ${factor.input.max}`;
  }
  return "";
}

function FactorField({
  factor,
  value,
  readOnly,
  onChange,
}: {
  factor: FactorConfigItem;
  value: unknown;
  readOnly?: boolean;
  onChange: (v: unknown) => void;
}) {
  const inputCls =
    "w-full rounded-lg border border-border-strong bg-surface px-3 py-2 text-[14px] text-ink outline-none focus:border-accent focus:ring-2 focus:ring-accent/20 disabled:bg-surface-2 disabled:text-muted";
  const err = validateFactor(factor, value);
  const label = (
    <label className="mb-1 block text-[13.5px] font-medium text-ink-2">
      {factor.label}
    </label>
  );

  let control: React.ReactNode;
  const t = factor.input.type;
  if (t === "textarea") {
    control = (
      <textarea
        className={inputCls}
        rows={2}
        value={String(value ?? "")}
        disabled={readOnly}
        placeholder={factor.input.placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  } else if (t === "number") {
    control = (
      <input
        type="number"
        className={`${inputCls} ${err ? "border-danger focus:border-danger focus:ring-danger/20" : ""}`}
        min={factor.input.min ?? undefined}
        max={factor.input.max ?? undefined}
        step={factor.input.step ?? undefined}
        value={value === "" || value == null ? "" : Number(value)}
        disabled={readOnly}
        onChange={(e) => onChange(e.target.value === "" ? "" : Number(e.target.value))}
        onBlur={() => {
          if (value === "" || value == null) return;
          const n = Number(value);
          if (Number.isNaN(n)) return;
          let next = n;
          if (factor.input.min != null && next < factor.input.min) next = factor.input.min;
          if (factor.input.max != null && next > factor.input.max) next = factor.input.max;
          if (next !== n) onChange(next);
        }}
      />
    );
  } else if (t === "slider" || t === "range") {
    const num = typeof value === "number" ? value : Number(value) || 0;
    control = (
      <div className="flex items-center gap-3">
        <input
          type="range"
          className="flex-1 accent-accent"
          min={factor.input.min ?? 0}
          max={factor.input.max ?? 10}
          step={factor.input.step ?? 1}
          value={num}
          disabled={readOnly}
          onChange={(e) => onChange(Number(e.target.value))}
        />
        <span className="w-12 text-right text-[15px] font-bold text-accent">{num}</span>
        {factor.input.unit && <span className="text-[13px] text-muted">{factor.input.unit}</span>}
      </div>
    );
  } else if (t === "key_person_levels") {
    const levels = parseKeyPersonLevels(value);
    control = (
      <div>
        {/* 等级量表：每位关键人一列，高等级在上；点选置色，再点取消 */}
        <div className="grid grid-cols-5 gap-2">
          {levels.map((level, index) => (
            <div key={index} className="flex flex-col items-center gap-1">
              <span className="mb-0.5 text-[12px] text-muted">关键人 {index + 1}</span>
              {KEY_PERSON_OPTIONS.map((option) => {
                const active = level === option.value;
                return (
                  <button
                    key={option.value}
                    type="button"
                    disabled={readOnly}
                    aria-label={`关键人 ${index + 1} ${option.label}`}
                    aria-pressed={active}
                    title={option.label}
                    onClick={() => {
                      const next = [...levels];
                      next[index] = active ? "" : option.value;
                      onChange(next);
                    }}
                    className={`flex h-7 w-7 items-center justify-center rounded-full border text-[12.5px] font-semibold transition disabled:opacity-50 ${
                      active
                        ? "border-transparent text-white"
                        : "border-border-strong bg-surface text-muted hover:border-accent hover:text-accent"
                    }`}
                    style={active ? { background: option.color } : undefined}
                  >
                    {option.value}
                  </button>
                );
              })}
            </div>
          ))}
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] text-muted">
          {KEY_PERSON_OPTIONS.map((option) => (
            <span key={option.value} className="flex items-center gap-1">
              <span className="h-2 w-2 rounded-full" style={{ background: option.color }} />
              {option.label}
            </span>
          ))}
        </div>
        <div className="mt-1 text-[12px] text-muted">平均等级及最终映射分数由后端自动计算</div>
      </div>
    );
  } else if (t === "select") {
    // 非新版合法选项统一显示为“未选择”，保存时只能提交标准下拉值。
    const raw = String(value ?? "");
    const val = factor.input.options.includes(raw) ? raw : "";
    control = (
      <select
        className={inputCls}
        value={val}
        disabled={readOnly}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">未选择</option>
        {factor.input.options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    );
  } else if (t === "date") {
    control = (
      <input
        type="date"
        className={inputCls}
        value={String(value ?? "")}
        disabled={readOnly}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  } else if (t === "bool") {
    control = (
      <label className="flex items-center gap-2 text-[14px] text-ink-2">
        <input
          type="checkbox"
          className="h-4 w-4 rounded border-border accent-accent"
          checked={Boolean(value)}
          disabled={readOnly}
          onChange={(e) => onChange(e.target.checked)}
        />
        是
      </label>
    );
  } else {
    control = (
      <input
        type="text"
        className={inputCls}
        value={String(value ?? "")}
        disabled={readOnly}
        placeholder={factor.input.placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }

  return (
    <div>
      {label}
      {factor.description && <div className="mb-1 text-[12px] text-muted">{factor.description}</div>}
      {control}
      {err && <div className="mt-1 text-[12px] text-danger">{err}</div>}
    </div>
  );
}
