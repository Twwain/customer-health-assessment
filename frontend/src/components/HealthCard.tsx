import { useState } from "react";
import type { AssessmentResponse, AssessmentTrendResponse, DimensionScore } from "../types";
import { fmtDate, levelColor, trendMeta } from "../lib/ui";
import { AlertBadge, LevelBadge } from "./Badges";
import { Ring, TrendChart } from "./Charts";

interface HealthCardProps {
  assessment: AssessmentResponse;
  trend?: AssessmentTrendResponse | null;
  compact?: boolean;
  showTrendButton?: boolean;
  onAlertAI?: () => void;
  onEditFactors?: () => void;
}

const ALERT_PREVIEW_LIMIT = 8;
const ALERT_LEVEL_ORDER: Record<string, number> = { high: 0, medium: 1, low: 2 };

function dimColor(pct: number): string {
  return pct >= 70 ? "#1AAE39" : pct >= 40 ? "#DD5B00" : "#E03131";
}

export default function HealthCard({
  assessment,
  trend,
  compact,
  showTrendButton = true,
  onAlertAI,
  onEditFactors,
}: HealthCardProps) {
  const [trendOpen, setTrendOpen] = useState(false);
  const [alertsExpanded, setAlertsExpanded] = useState(false);
  const color = assessment.level_color || levelColor(assessment.level);
  const t = trend ? trendMeta(trend.latest_score, trend.previous_score) : null;
  const prioritizedAlerts = assessment.alerts
    .map((alert, index) => ({ alert, index }))
    .sort((a, b) => (ALERT_LEVEL_ORDER[a.alert.level] ?? 9) - (ALERT_LEVEL_ORDER[b.alert.level] ?? 9) || a.index - b.index)
    .map(({ alert }) => alert);
  const visibleAlerts = alertsExpanded ? prioritizedAlerts : prioritizedAlerts.slice(0, ALERT_PREVIEW_LIMIT);
  const hiddenAlertCount = prioritizedAlerts.length - visibleAlerts.length;

  return (
    <div className="rounded-xl border border-border bg-surface p-4">
      <div className="flex items-start gap-3">
        <Ring score={assessment.total_score} color={color} size={compact ? 62 : 76} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-[15px] font-semibold text-ink">{assessment.customer_name}</span>
            <LevelBadge grade={assessment.level} />
            {t && (
              <span className={`text-[13px] font-medium ${t.cls === "trend-up" ? "text-success" : t.cls === "trend-down" ? "text-danger" : "text-muted"}`}>
                {t.arrow} {t.text}
              </span>
            )}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[12px] text-muted">
            <span>满分 {assessment.max_score}</span>
            <span>·</span>
            <span>评估于 {fmtDate(assessment.assessed_at)}</span>
          </div>
        </div>
        {trend && (
          <div className="hidden shrink-0 text-right sm:block">
            <div className="text-[10px] text-muted">近 {trend.points.length} 次评估</div>
            <SparkMini points={trend.points.map((p) => p.total_score)} color={color} />
          </div>
        )}
      </div>

      <div className="mt-3 space-y-1.5">
        {assessment.dimensions.map((d: DimensionScore) => {
          const pct = d.max_score ? (d.score / d.max_score) * 100 : 0;
          const dc = dimColor(pct);
          return (
            <div key={d.key} className="flex items-center gap-2">
              <span className="w-[150px] shrink-0 whitespace-nowrap text-[13px] text-ink-2">{d.name}</span>
              <div className="h-[6px] flex-1 overflow-hidden rounded-full bg-[#F0F0F0]">
                <div className="h-full rounded-full" style={{ width: `${pct}%`, background: dc }} />
              </div>
              <span className="w-[44px] shrink-0 text-right text-[12px] font-medium text-ink">
                {Math.round(d.score * 10) / 10}
                <span className="text-muted">/{d.max_score}</span>
              </span>
            </div>
          );
        })}
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-1.5">
        {assessment.alerts.length > 0 ? (
          <>
            {visibleAlerts.map((a) => <AlertBadge key={a.id} level={a.level} message={a.message} />)}
            {assessment.alerts.length > ALERT_PREVIEW_LIMIT && (
              <button
                type="button"
                className="rounded border border-border px-1.5 py-0.5 text-[12px] text-muted transition hover:border-accent hover:text-accent"
                onClick={() => setAlertsExpanded((value) => !value)}
              >
                {alertsExpanded ? "收起预警" : `查看其余 ${hiddenAlertCount} 项`}
              </button>
            )}
          </>
        ) : (
          <span className="text-[12px] text-muted">— 无预警</span>
        )}
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        {showTrendButton && (
          <button
            className="rounded-lg border border-border bg-surface px-2.5 py-1.5 text-[12.5px] text-ink-2 transition hover:border-accent hover:text-accent"
            onClick={() => setTrendOpen((v) => !v)}
            disabled={!trend}
          >
            {trendOpen ? "收起" : "查看"}历史趋势
          </button>
        )}
        {onAlertAI && (
          <button
            className="rounded-lg bg-accent px-2.5 py-1.5 text-[12.5px] font-medium text-white transition hover:bg-accent-hover"
            onClick={onAlertAI}
          >
            ✨ AI 一键解读预警
          </button>
        )}
        {onEditFactors && (
          <button
            className="rounded-lg border border-border bg-surface px-2.5 py-1.5 text-[12.5px] text-ink-2 transition hover:border-accent hover:text-accent"
            onClick={onEditFactors}
          >
            编辑因子
          </button>
        )}
      </div>

      {showTrendButton && trendOpen && trend && (
        <div className="mt-3 rounded-xl border border-border-soft bg-surface-2 p-3">
          <div className="mb-1 text-[13px] font-semibold text-ink">📈 客情评分趋势</div>
          <div className="overflow-x-auto">
            <TrendChart trend={trend} color={color} width={compact ? 360 : 470} height={168} />
          </div>
          {trend.delta !== 0 && (
            <div className="mt-2 text-[12px] text-muted">
              近 {trend.points.length} 次评估
              <b className={trend.trend === "down" ? "text-danger" : trend.trend === "up" ? "text-success" : ""}>
                {" "}
                {trend.trend === "down" ? "持续下滑" : trend.trend === "up" ? "持续回升" : "基本持平"}{" "}
                {trend.delta > 0 ? "+" : ""}
                {Math.round(trend.delta * 10) / 10} 分
              </b>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function SparkMini({ points, color }: { points: number[]; color: string }) {
  if (points.length < 2) {
    return <div style={{ width: 84, height: 22 }} />;
  }
  const w = 84;
  const h = 22;
  const mx = Math.max(...points);
  const mn = Math.min(...points);
  const span = Math.max(mx - mn, 1);
  const pts = points.map((v, i) => {
    const x = (i / (points.length - 1)) * (w - 4) + 2;
    const y = h - 3 - ((v - mn) / span) * (h - 8);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} className="mx-auto">
      <polyline points={pts.join(" ")} fill="none" stroke={color} strokeWidth={1.6} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}
