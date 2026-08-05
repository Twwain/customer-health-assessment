import { alertLevelClass, levelColor } from "../lib/ui";

function hexToRgba(hex: string, alpha: number): string {
  const h = hex.replace("#", "");
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

export function LevelBadge({ grade, size = "md" }: { grade: string; size?: "sm" | "md" }) {
  const color = levelColor(grade);
  const pad = size === "sm" ? "1px 7px" : "2px 9px";
  const font = size === "sm" ? 11 : 12;
  return (
    <span
      className="inline-flex items-center rounded-full font-semibold leading-none whitespace-nowrap"
      style={{
        padding: pad,
        fontSize: font,
        color,
        background: hexToRgba(color, 0.12),
        border: `1px solid ${hexToRgba(color, 0.28)}`,
      }}
    >
      {grade}
    </span>
  );
}

export function AlertBadge({ level, message }: { level: string; message: string }) {
  const color =
    level === "high" ? "#E03131" : level === "medium" ? "#DD5B00" : "#0075DE";
  return (
    <span
      className="inline-flex max-w-full min-w-0 items-center gap-1 overflow-hidden rounded-full px-2 py-[2px] text-[11.5px] font-medium"
      style={{ color, background: hexToRgba(color, 0.1), border: `1px solid ${hexToRgba(color, 0.25)}` }}
    >
      <span className="truncate">⚠ {message}</span>
    </span>
  );
}

export function UrgencyBadge({ urgency, label }: { urgency: string; label: string }) {
  const color =
    urgency === "high" ? "#E03131" : urgency === "medium" ? "#DD5B00" : "#0075DE";
  return (
    <span
      className="rounded px-1.5 py-[1px] text-[11px] font-semibold"
      style={{ color, background: hexToRgba(color, 0.12) }}
    >
      {label}
    </span>
  );
}

export { alertLevelClass };
