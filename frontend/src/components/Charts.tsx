import type { AssessmentTrendResponse } from "../types";
import { computeTrendYRange } from "../lib/trendRange";

// 图表组件（SVG 自绘，1:1 还原 docs/prototype/app.js 的 SVG 逻辑，确保与冻结基线一致）。
// 注：recharts 为项目依赖，但手写 SVG 可保证与评审基线像素级一致，避免主题化偏移。

export function Ring({
  score,
  color,
  size = 76,
}: {
  score: number;
  color: string;
  size?: number;
}) {
  const r = size / 2 - 6;
  const c = 2 * Math.PI * r;
  const off = c * (1 - Math.max(0, Math.min(100, score)) / 100);
  return (
    <div style={{ width: size, height: size, position: "relative", flexShrink: 0 }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="#F0F0F0" strokeWidth={7} />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={color}
          strokeWidth={7}
          strokeLinecap="round"
          strokeDasharray={c}
          strokeDashoffset={off}
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
        />
      </svg>
      <div
        style={{
          position: "absolute",
          inset: 0,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        <b style={{ color, fontSize: size * 0.26, lineHeight: 1 }}>
          {Math.round(score * 10) / 10}
        </b>
        <span style={{ fontSize: 11, color: "#666666" }}>客情评分</span>
      </div>
    </div>
  );
}

export function Sparkline({
  values,
  color,
  width = 92,
  height = 30,
}: {
  values: number[];
  color: string;
  width?: number;
  height?: number;
}) {
  if (!values || values.length === 0) {
    return <svg width={width} height={height} />;
  }
  if (values.length === 1) {
    return (
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
        <circle cx={width / 2} cy={height / 2} r={2.6} fill={color} />
      </svg>
    );
  }
  const mx = Math.max(...values);
  const mn = Math.min(...values);
  const span = Math.max(mx - mn, 1);
  const pts = values.map((v, i) => {
    const x = (i / (values.length - 1)) * (width - 4) + 2;
    const y = height - 3 - ((v - mn) / span) * (height - 8);
    return [x, y] as [number, number];
  });
  const line = pts.map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
  const area = `${line} ${width - 2},${height} 2,${height}`;
  const last = pts[pts.length - 1];
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
      <polygon points={area} fill={color} opacity={0.09} />
      <polyline
        points={line}
        fill="none"
        stroke={color}
        strokeWidth={1.8}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      <circle cx={last[0].toFixed(1)} cy={last[1].toFixed(1)} r={2.6} fill={color} />
    </svg>
  );
}

export function TrendChart({
  trend,
  color,
  width = 470,
  height = 168,
}: {
  trend: AssessmentTrendResponse;
  color: string;
  width?: number;
  height?: number;
}) {
  const vals = trend.points.map((p) => p.total_score);
  if (vals.length === 0) return <div style={{ color: "#666666", fontSize: 14 }}>暂无趋势数据</div>;
  if (vals.length === 1) {
    // 只有一次评估：无曲线可画，仅渲染末点
    return (
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label="客情评分趋势">
        <circle cx={width / 2} cy={height / 2} r={3.5} fill="#fff" stroke={color} strokeWidth={2} />
        <text x={width / 2} y={height / 2 - 12} textAnchor="middle" fontSize={12} fontWeight={700} fill={color}>
          {vals[0]}
        </text>
      </svg>
    );
  }
  const labels = trend.points.map((p) => p.label);
  const padL = 8;
  const padR = 14;
  const padT = 20;
  const padB = 26;
  const iw = width - padL - padR;
  const ih = height - padT - padB;
  // y 轴按数据范围自适应（与列表 Sparkline 一致），避免固定 0-100 刻度让波动显得平缓
  const { yMin, ySpan } = computeTrendYRange(vals);
  const y = (v: number) => padT + ih - ((v - yMin) / ySpan) * ih;
  const x = (i: number) => padL + (i / Math.max(vals.length - 1, 1)) * iw;

  // 平滑曲线：Catmull-Rom 转三次贝塞尔
  const smoothPath = (pts: Array<[number, number]>): string => {
    if (pts.length < 2) return "";
    let d = `M ${pts[0][0].toFixed(1)} ${pts[0][1].toFixed(1)}`;
    for (let i = 0; i < pts.length - 1; i++) {
      const p0 = pts[Math.max(i - 1, 0)];
      const p1 = pts[i];
      const p2 = pts[i + 1];
      const p3 = pts[Math.min(i + 2, pts.length - 1)];
      const c1x = p1[0] + (p2[0] - p0[0]) / 6;
      const c1y = p1[1] + (p2[1] - p0[1]) / 6;
      const c2x = p2[0] - (p3[0] - p1[0]) / 6;
      const c2y = p2[1] - (p3[1] - p1[1]) / 6;
      d += ` C ${c1x.toFixed(1)} ${c1y.toFixed(1)}, ${c2x.toFixed(1)} ${c2y.toFixed(1)}, ${p2[0].toFixed(1)} ${p2[1].toFixed(1)}`;
    }
    return d;
  };

  const pts: Array<[number, number]> = vals.map((v, i) => [x(i), y(v)]);
  const linePath = smoothPath(pts);
  const areaPath = `${linePath} L ${x(vals.length - 1).toFixed(1)} ${padT + ih} L ${padL} ${padT + ih} Z`;
  const lastIdx = vals.length - 1;
  const lastDot = (
    <g>
      <circle cx={x(lastIdx).toFixed(1)} cy={y(vals[lastIdx]).toFixed(1)} r={3.5} fill="#fff" stroke={color} strokeWidth={2} />
      <text x={x(lastIdx).toFixed(1)} y={(y(vals[lastIdx]) - 12).toFixed(1)} textAnchor="middle" fontSize={12} fontWeight={700} fill={color}>
        {vals[lastIdx]}
      </text>
    </g>
  );
  const step = vals.length > 6 ? Math.ceil(vals.length / 5) : 1;
  const xl = labels.map((lb, i) =>
    i % step === 0 || i === vals.length - 1 ? (
      <text key={i} x={x(i).toFixed(1)} y={height - 8} textAnchor="middle" fontSize={10} fill="#666666">
        {lb}
      </text>
    ) : null
  );

  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label="客情评分趋势">
      <path d={areaPath} fill={color} opacity={0.09} />
      <path d={linePath} fill="none" stroke={color} strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" />
      {lastDot}
      {xl}
    </svg>
  );
}
