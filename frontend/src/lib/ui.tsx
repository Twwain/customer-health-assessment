import type { ReactNode } from "react";

// 共享 UI 工具：等级配色、趋势箭头、告警配色、日期格式化、Markdown 轻量渲染。

export type Grade = string;

export interface LevelSpec {
  name: string;
  min_score: number;
  color: string;
}

// 默认等级（与后端 scoring_config.yaml 出厂配置一致），仅作兜底；
// 实际等级以 /api/customers/factor-config 下发的 levels 为准（setLevels 注册）。
const DEFAULT_LEVELS: LevelSpec[] = [
  { name: "健康", min_score: 80, color: "#22C55E" },
  { name: "亚健康", min_score: 60, color: "#EAB308" },
  { name: "风险", min_score: 40, color: "#F97316" },
  { name: "高危", min_score: 0, color: "#EF4444" },
];

let _levels: LevelSpec[] = DEFAULT_LEVELS;

/** 注册评分配置中的等级表（按 min_score 降序传入），使等级名/颜色随配置走。 */
export function setLevels(levels: LevelSpec[] | undefined | null): void {
  if (levels && levels.length) {
    _levels = [...levels].sort((a, b) => b.min_score - a.min_score);
  }
}

export function getLevels(): LevelSpec[] {
  return _levels;
}

export function gradeOf(score: number): string {
  for (const lv of _levels) {
    if (score >= lv.min_score) return lv.name;
  }
  return _levels[_levels.length - 1]?.name ?? "风险";
}

export function levelColor(grade: string): string {
  const hit = _levels.find((lv) => lv.name === grade);
  if (hit) return hit.color;
  return (
    { 健康: "#22C55E", 亚健康: "#EAB308", 风险: "#F97316", 高危: "#EF4444" }[grade] || "#3B82F6"
  );
}

export function levelClass(grade: string): string {
  return (
    { 健康: "lv-excellent", 亚健康: "lv-good", 风险: "lv-normal", 高危: "lv-risk" }[grade] ||
    "lv-normal"
  );
}

export interface TrendMeta {
  arrow: string;
  cls: string;
  text: string;
}

export function trendMeta(cur: number, prev: number | null | undefined): TrendMeta {
  if (prev == null) return { arrow: "→", cls: "trend-flat", text: "+0" };
  const d = +(cur - prev).toFixed(1);
  if (d > 2) return { arrow: "↑", cls: "trend-up", text: "+" + d };
  if (d < -2) return { arrow: "↓", cls: "trend-down", text: String(d) };
  return { arrow: "→", cls: "trend-flat", text: (d >= 0 ? "+" : "") + d };
}

// 告警等级配色（评审结论 Q5：高=红 / 中=黄 / 低=蓝）
export function alertLevelClass(level: string): string {
  if (level === "high") return "ab-high";
  if (level === "medium") return "ab-medium";
  return "ab-low";
}

export function alertLevelLabel(level: string): string {
  return level === "high" ? "高" : level === "medium" ? "中" : "低";
}

export function fmtDate(s: string | null | undefined): string {
  if (!s) return "—";
  return String(s).split("T")[0];
}

export function storageLabel(storage: string): string {
  if (!storage) return "未知";
  if (storage.includes("vector")) return "向量库";
  if (storage.includes("struct")) return "SQLite 结构化";
  return storage;
}

export function categoryIcon(cat: string): string {
  if (cat.includes("规范")) return "📘";
  if (cat.includes("数据")) return "📊";
  if (cat.includes("外部")) return "🌐";
  if (cat.includes("沉淀")) return "💬";
  return "📄";
}

// ── 轻量 Markdown 渲染（覆盖标题/列表/引用/表格/分隔线/行内样式）──────────

function renderInline(text: string, keyBase: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const regex = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*\n]+\*)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = regex.exec(text)) !== null) {
    if (m.index > last) nodes.push(text.slice(last, m.index));
    const tok = m[0];
    const key = keyBase + "-i" + i++;
    if (tok.startsWith("**")) nodes.push(<strong key={key}>{tok.slice(2, -2)}</strong>);
    else if (tok.startsWith("`")) nodes.push(<code key={key}>{tok.slice(1, -1)}</code>);
    else nodes.push(<em key={key}>{tok.slice(1, -1)}</em>);
    last = m.index + tok.length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

function tableCells(line: string): string[] {
  let value = line.trim();
  if (value.startsWith("|")) value = value.slice(1);
  if (value.endsWith("|")) {
    let slashes = 0;
    for (let i = value.length - 2; i >= 0 && value[i] === "\\"; i--) slashes++;
    if (slashes % 2 === 0) value = value.slice(0, -1);
  }
  const cells: string[] = [];
  let cell = "";
  let inCode = false;
  for (let i = 0; i < value.length; i++) {
    const ch = value[i];
    if (ch === "\\") {
      let end = i;
      while (value[end] === "\\") end++;
      const slashCount = end - i;
      cell += "\\".repeat(Math.floor(slashCount / 2));
      if (value[end] === "|" && slashCount % 2 === 1) {
        cell += "|";
        i = end;
      } else {
        if (slashCount % 2 === 1) cell += "\\";
        i = end - 1;
      }
    } else if (ch === "`") {
      inCode = !inCode;
      cell += ch;
    } else if (ch === "|" && !inCode) {
      cells.push(cell.trim());
      cell = "";
    } else {
      cell += ch;
    }
  }
  cells.push(cell.trim());
  return cells;
}

function isTableDivider(line: string): boolean {
  const cells = tableCells(line);
  return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
}

export function renderMarkdown(text: string): ReactNode {
  const lines = (text || "").replace(/\r\n/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let para: string[] = [];
  let key = 0;

  const flushPara = () => {
    if (para.length) {
      const k = key++;
      blocks.push(<p key={k}>{renderInline(para.join(" "), "p" + k)}</p>);
      para = [];
    }
  };

  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      flushPara();
      i++;
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && isTableDivider(lines[i + 1])) {
      flushPara();
      const headers = tableCells(line);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        rows.push(tableCells(lines[i]));
        i++;
      }
      const k = key++;
      blocks.push(
        <div className="md-table-wrap" key={k}>
          <table>
            <thead>
              <tr>
                {headers.map((cell, idx) => (
                  <th key={idx}>{renderInline(cell, `th${k}-${idx}`)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, rowIdx) => (
                <tr key={rowIdx}>
                  {headers.map((_, cellIdx) => (
                    <td key={cellIdx}>{renderInline(row[cellIdx] || "", `td${k}-${rowIdx}-${cellIdx}`)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }
    if (/^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/.test(line)) {
      flushPara();
      blocks.push(<hr key={key++} />);
      i++;
      continue;
    }
    const h = /^(#{2,4})\s+(.*)$/.exec(line);
    if (h) {
      flushPara();
      const k = key++;
      blocks.push(<h4 key={k}>{renderInline(h[2], "h" + k)}</h4>);
      i++;
      continue;
    }
    if (line.startsWith("> ")) {
      flushPara();
      const k = key++;
      blocks.push(<blockquote key={k}>{renderInline(line.slice(2), "q" + k)}</blockquote>);
      i++;
      continue;
    }
    if (/^\d+\.\s+/.test(line)) {
      flushPara();
      const items: string[] = [];
      while (i < lines.length && /^\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\d+\.\s+/, ""));
        i++;
      }
      const k = key++;
      blocks.push(
        <ol key={k}>
          {items.map((it, idx) => (
            <li key={idx}>{renderInline(it, "ol" + k + "-" + idx)}</li>
          ))}
        </ol>,
      );
      continue;
    }
    if (/^[-*]\s+/.test(line)) {
      flushPara();
      const items: string[] = [];
      while (i < lines.length && /^[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^[-*]\s+/, ""));
        i++;
      }
      const k = key++;
      blocks.push(
        <ul key={k}>
          {items.map((it, idx) => (
            <li key={idx}>{renderInline(it, "ul" + k + "-" + idx)}</li>
          ))}
        </ul>,
      );
      continue;
    }
    para.push(line.trim());
    i++;
  }
  flushPara();
  return <>{blocks}</>;
}
