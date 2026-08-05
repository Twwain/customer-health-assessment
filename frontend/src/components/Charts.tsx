import type { AssessmentTrendResponse, DimensionScore, LevelConfigItem } from "../types";

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
        <span style={{ fontSize: 10, color: "#666666" }}>健康分</span>
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

export function RadarChart({
  dimensions,
  color,
  size = 190,
  labels,
}: {
  dimensions: DimensionScore[];
  color: string;
  size?: number;
  labels?: string[];
}) {
  const cx = size / 2;
  const cy = size / 2 + 4;
  const R = size / 2 - 34;
  const ang = [-90, 0, 90, 180].map((a) => (a * Math.PI) / 180);
  const maxOf = (i: number) => dimensions[i]?.max_score || 25;
  const pt = (i: number, ratio: number): [number, number] => [
    cx + Math.cos(ang[i]) * R * ratio,
    cy + Math.sin(ang[i]) * R * ratio,
  ];

  const grid = [0.25, 0.5, 0.75, 1]
    .map((rt) => {
      const p = [0, 1, 2, 3]
        .map((i) => pt(i, rt).map((n) => n.toFixed(1)).join(","))
        .join(" ");
      return <polygon key={rt} points={p} fill="none" stroke="#E5E5E5" strokeWidth={1} />;
    });
  const axes = [0, 1, 2, 3].map((i) => {
    const p = pt(i, 1);
    return (
      <line key={i} x1={cx} y1={cy} x2={p[0].toFixed(1)} y2={p[1].toFixed(1)} stroke="#E5E5E5" strokeWidth={1} />
    );
  });
  const dp = dimensions
    .map((d, i) => pt(i, Math.max(d.score / maxOf(i), 0.02)).map((n) => n.toFixed(1)).join(","))
    .join(" ");
  const dots = dimensions.map((d, i) => {
    const p = pt(i, Math.max(d.score / maxOf(i), 0.02));
    return <circle key={i} cx={p[0].toFixed(1)} cy={p[1].toFixed(1)} r={3} fill={color} />;
  });
  const anchors: Array<"middle" | "start" | "end"> = ["middle", "start", "middle", "end"];
  const dy = [-9, 4, 15, 4];
  const dx = [0, 8, 0, -8];
  const labelsEls = dimensions.map((d, i) => {
    const p = pt(i, 1);
    const name = labels?.[i] ?? d.name;
    return (
      <g key={i}>
        <text
          x={(p[0] + dx[i]).toFixed(1)}
          y={(p[1] + dy[i]).toFixed(1)}
          textAnchor={anchors[i]}
          fontSize={10.5}
          fill="#555555"
        >
          {name}
        </text>
        <text
          x={(p[0] + dx[i]).toFixed(1)}
          y={(p[1] + dy[i] + 12).toFixed(1)}
          textAnchor={anchors[i]}
          fontSize={10}
          fontWeight={700}
          fill={color}
        >
          {Math.round(d.score * 10) / 10}
        </text>
      </g>
    );
  });

  return (
    <svg width={size} height={size + 14} viewBox={`0 0 ${size} ${size + 14}`}>
      {grid}
      {axes}
      <polygon points={dp} fill={color} fillOpacity={0.16} stroke={color} strokeWidth={1.8} />
      {dots}
      {labelsEls}
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
  if (vals.length === 0) return <div style={{ color: "#666666", fontSize: 13 }}>暂无趋势数据</div>;
  const labels = trend.points.map((p) => p.label);
  const padL = 30;
  const padR = 12;
  const padT = 12;
  const padB = 22;
  const iw = width - padL - padR;
  const ih = height - padT - padB;
  const y = (v: number) => padT + ih - (v / 100) * ih;
  const x = (i: number) => padL + (i / Math.max(vals.length - 1, 1)) * iw;

  const gridLines = [0, 25, 50, 75, 100].map((v) => (
    <g key={v}>
      <line x1={padL} y1={y(v)} x2={width - padR} y2={y(v)} stroke="#EFEFEF" strokeWidth={1} />
      <text x={padL - 6} y={y(v) + 3.5} textAnchor="end" fontSize={9.5} fill="#9C9C9C">
        {v}
      </text>
    </g>
  ));

  const levelLines = (trend.level_lines || []).map((lv: LevelConfigItem) => {
    if (lv.min_score <= 0 || lv.min_score >= 100) return null;
    const isRisk = lv.name.includes("风险");
    const isGood = lv.name.includes("良好");
    const stroke = isRisk ? "#EF4444" : isGood ? "#3B82F6" : "#B5B5B5";
    return (
      <g key={lv.name}>
        <line
          x1={padL}
          y1={y(lv.min_score)}
          x2={width - padR}
          y2={y(lv.min_score)}
          stroke={stroke}
          strokeWidth={1}
          strokeDasharray="4 3"
          opacity={isRisk ? 0.5 : 0.35}
        />
        <text
          x={width - padR - 2}
          y={y(lv.min_score) - 4}
          textAnchor="end"
          fontSize={9}
          fill={stroke}
        >
          {lv.name}线 {lv.min_score}
        </text>
      </g>
    );
  });

  const linePts = vals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const area = `${linePts} ${x(vals.length - 1).toFixed(1)},${padT + ih} ${padL},${padT + ih}`;
  const dots = vals.map((v, i) => {
    const last = i === vals.length - 1;
    return (
      <g key={i}>
        <circle cx={x(i).toFixed(1)} cy={y(v).toFixed(1)} r={3.2} fill="#fff" stroke={color} strokeWidth={2} />
        {last && (
          <text x={x(i).toFixed(1)} y={(y(v) - 10).toFixed(1)} textAnchor="middle" fontSize={11} fontWeight={700} fill={color}>
            {v}
          </text>
        )}
      </g>
    );
  });
  const xl = labels.map((lb, i) => (
    <text key={i} x={x(i).toFixed(1)} y={height - 6} textAnchor="middle" fontSize={9.5} fill="#666666">
      {lb}
    </text>
  ));

  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
      {gridLines}
      {levelLines}
      <polygon points={area} fill={color} opacity={0.08} />
      <polyline points={linePts} fill="none" stroke={color} strokeWidth={2.2} strokeLinejoin="round" />
      {dots}
      {xl}
    </svg>
  );
}
