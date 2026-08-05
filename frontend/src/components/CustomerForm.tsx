import type { CustomerResponse, FactorConfigItem, FactorConfigResponse } from "../types";

/** 客户基本信息字段（与后端 CustomerUpdate 一致），与客情因子区分开 */
export const BASIC_FIELDS = [
  "customer_name",
  "industry",
  "contact_person",
  "contact_phone",
  "notes",
] as const;

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

export default function CustomerForm({ customer, config, value, onChange, readOnly }: CustomerFormProps) {
  const base = getBase(customer, config);
  const merged: Record<string, unknown> = { ...base, ...value };

  const set = (field: string, v: unknown) => onChange({ ...value, [field]: v });

  return (
    <div className="space-y-3">
      {/* 区块一：客户基本信息 */}
      <div className="rounded-xl border border-border bg-surface p-3">
        <div className="mb-2.5 flex items-center gap-2">
          <span className="text-[13.5px] font-semibold text-ink">📇 客户基本信息</span>
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
          <span className="text-[13.5px] font-semibold text-ink">🧩 客情因子信息</span>
          <span className="rounded bg-surface-2 px-1.5 py-[1px] text-[11px] text-muted">维度驱动评分</span>
        </div>
        <div className="divide-y divide-border-soft">
          {config.dimensions
            .filter((d) => d.enabled)
            .map((dim, di) => (
              <div key={dim.key} className={di === 0 ? "pb-3" : "py-3"}>
            <div className="mb-2.5 flex items-center gap-2">
              <span className="text-[13.5px] font-semibold text-ink">
                {["①", "②", "③", "④", "⑤", "⑥"][di] ?? "•"} {dim.name}
              </span>
              <span className="rounded bg-surface-2 px-1.5 py-[1px] text-[11px] text-muted">
                维度权重 {dim.max_score} 分
              </span>
            </div>
            <div className="space-y-2.5">
              {dim.factors.map((f) => (
                <FactorField
                  key={f.field}
                  factor={f}
                  value={merged[f.field]}
                  readOnly={readOnly}
                  onChange={(v) => set(f.field, v)}
                />
              ))}
            </div>
              </div>
            ))}
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
    "w-full rounded-lg border border-border-strong bg-surface px-3 py-2 text-[13px] text-ink outline-none focus:border-accent focus:ring-2 focus:ring-accent/20 disabled:bg-surface-2 disabled:text-muted";
  return (
    <div className={wide ? "sm:col-span-2" : ""}>
      <label className="mb-1 block text-[12.5px] font-medium text-ink-2">{label}</label>
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
    "w-full rounded-lg border border-border-strong bg-surface px-3 py-2 text-[13px] text-ink outline-none focus:border-accent focus:ring-2 focus:ring-accent/20 disabled:bg-surface-2 disabled:text-muted";
  const label = (
    <label className="mb-1 block text-[12.5px] font-medium text-ink-2">
      {factor.label}
      {factor.description && <span className="ml-1 text-[11px] font-normal text-muted">{factor.description}</span>}
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
        className={inputCls}
        min={factor.input.min ?? undefined}
        max={factor.input.max ?? undefined}
        step={factor.input.step ?? undefined}
        value={value === "" || value == null ? "" : Number(value)}
        disabled={readOnly}
        onChange={(e) => onChange(e.target.value === "" ? "" : Number(e.target.value))}
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
        <span className="w-12 text-right text-[14px] font-bold text-accent">{num}</span>
        {factor.input.unit && <span className="text-[12px] text-muted">{factor.input.unit}</span>}
      </div>
    );
  } else if (t === "select") {
    control = (
      <select
        className={inputCls}
        value={String(value ?? "")}
        disabled={readOnly}
        onChange={(e) => onChange(e.target.value)}
      >
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
      <label className="flex items-center gap-2 text-[13px] text-ink-2">
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
      {control}
    </div>
  );
}
