const levelConfig: Record<string, { color: string; bg: string; text: string }> = {
  "优秀": { color: "#84cc16", bg: "bg-lime-50", text: "text-lime-700" },
  "良好": { color: "#0ea5e9", bg: "bg-sky-50", text: "text-sky-700" },
  "一般": { color: "#d97706", bg: "bg-amber-50", text: "text-amber-700" },
  "风险": { color: "#ef4444", bg: "bg-red-50", text: "text-red-700" },
};

const levelRanges: Record<string, string> = {
  "优秀": "85-100",
  "良好": "70-84",
  "一般": "55-69",
  "风险": "0-54",
};

const rangePcts: Record<string, number> = {
  "优秀": 16,
  "良好": 15,
  "一般": 15,
  "风险": 54,
};

export default function ScoreGauge({
  score,
  level,
}: {
  score: number;
  level: string;
}) {
  const cfg = levelConfig[level] || levelConfig["一般"];
  const pct = Math.min(100, Math.max(0, score));
  const levelOrder = ["风险", "一般", "良好", "优秀"] as const;
  const indicatorPct = Math.min(97, Math.max(3, pct));

  return (
    <div className={`rounded-2xl p-8 text-center ${cfg.bg} border border-slate-100`}>
      {/* 等级 + 分数 — 放大突出 */}
      <div className="flex items-baseline justify-center gap-6 mb-2">
        <span className="text-5xl font-bold tracking-tight" style={{ color: cfg.color }}>
          {level}
        </span>
        <span className="text-5xl font-bold tracking-tight" style={{ color: cfg.color }}>
          {score.toFixed(1)}
          <span className="text-xl font-normal text-slate-400 ml-1">分</span>
        </span>
      </div>

      {/* 小标签 — 置于值下方 */}
      <div className="flex justify-center gap-6 mb-7 text-xs text-slate-400">
        <span>健康等级</span>
        <span>综合健康分</span>
      </div>

      {/* 等级标识条 + 分数位置指示器 */}
      <div className="relative mt-3">
        {/* 指示器圆点 — 超过标尺高度 */}
        <div
          className="absolute z-10"
          style={{ left: `${indicatorPct}%`, transform: "translateX(-50%)", top: "-12px" }}
        >
          <div
            className="w-5 h-5 rounded-full border-2 border-white shadow-md"
            style={{ backgroundColor: cfg.color }}
          />
        </div>
        {/* 等级分段条 */}
        <div className="flex rounded-full overflow-hidden h-4">
          {levelOrder.map((lvl) => {
            const lc = levelConfig[lvl];
            const active = lvl === level;
            return (
              <div
                key={lvl}
                className="flex items-center justify-center text-[11px] font-medium transition"
                style={{
                  width: `${rangePcts[lvl]}%`,
                  backgroundColor: active ? lc.color : `${lc.color}25`,
                  color: active ? "#fff" : lc.color,
                }}
              >
                {lvl}
              </div>
            );
          })}
        </div>
        {/* 范围标注 */}
        <div className="flex mt-1.5">
          {levelOrder.map((lvl) => (
            <div
              key={lvl}
              className="text-[10px] text-slate-400 text-center"
              style={{ width: `${rangePcts[lvl]}%` }}
            >
              {levelRanges[lvl]}分
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
