import type { StrategyItem } from "../types";
import { assessmentPlan } from "../lib/assessmentPresentation";

export function AssessmentPlan({ items }: { items: StrategyItem[] }) {
  return <section className="rounded-xl border border-border bg-surface px-3.5 py-2.5">
    <h4 className="mb-2 font-semibold text-ink">下一步计划</h4>
    <div className="overflow-x-auto"><table className="w-full min-w-[580px] text-left text-[13px]">
      <thead><tr>{["行动", "责任人", "时限", "交付物"].map((title) =>
        <th key={title} className="p-2 font-medium text-muted">{title}</th>)}</tr></thead>
      <tbody>{assessmentPlan(items).map((item, index) => <tr key={index} className="border-t border-border-soft">
        <td className="p-2 align-top"><span>{item.action}</span>
          {item.detail && <details className="mt-1 text-muted">
            <summary className="cursor-pointer text-[12px]">查看完整步骤</summary>
            <div className="mt-1 whitespace-pre-wrap">{item.detail}</div>
          </details>}
        </td>
        <td className="p-2 align-top">{item.owner}</td>
        <td className="p-2 align-top">{item.timing}</td>
        <td className="p-2 align-top">{item.deliverable}</td>
      </tr>)}</tbody>
    </table></div>
  </section>;
}
