import { useState } from "react";
import type { KnowledgeReference, StrategyItem } from "../types";
import { UrgencyBadge } from "./Badges";

const PRIORITY_GROUPS: { key: string; title: string }[] = [
  { key: "recommended", title: "✅ 推荐策略" },
  { key: "alternative", title: "□ 备选策略" },
  { key: "long_term", title: "💡 长期建议" },
];

function urgencyLabel(u: string): string {
  return u === "high" ? "紧急度：高" : u === "medium" ? "紧急度：中" : "紧急度：低";
}

function matchReference(item: StrategyItem, refs?: KnowledgeReference[]): KnowledgeReference | null {
  if (!refs || refs.length === 0) return null;
  const needle = item.reference || "";
  return (
    refs.find((r) => r.title && (needle.includes(r.title) || r.title.includes(needle.replace(/^.*·\s*/, "")))) ||
    null
  );
}

export function StrategyList({
  items,
  references,
  onTrace,
}: {
  items: StrategyItem[];
  references?: KnowledgeReference[];
  onTrace?: (refs: KnowledgeReference[]) => void;
}) {
  if (!items || items.length === 0) return null;
  const sorted = [...items].sort((a, b) => priorityRank(a.priority) - priorityRank(b.priority));

  return (
    <div className="space-y-3">
      {PRIORITY_GROUPS.map((g) => {
        const list = sorted.filter((it) => it.priority === g.key);
        if (list.length === 0) return null;
        return (
          <div key={g.key}>
            <div className="mb-1.5 flex items-center gap-1.5 text-[13px] font-semibold text-ink">
              {g.title}
              <span className="rounded-full bg-surface-2 px-1.5 text-[11px] font-normal text-muted">
                {list.length} 条
              </span>
            </div>
            <div className="space-y-2">
              {list.map((it, idx) => {
                const ref = matchReference(it, references);
                return (
                  <div
                    key={idx}
                    className={`rounded-xl border border-border bg-surface p-3 ${
                      g.key === "alternative" ? "border-l-[3px] border-l-warning" : g.key === "long_term" ? "border-l-[3px] border-l-info" : "border-l-[3px] border-l-accent"
                    }`}
                  >
                    <div className="flex items-start gap-2">
                      <span className="mt-[1px] flex h-[18px] w-[18px] shrink-0 items-center justify-center rounded-full bg-accent-soft text-[11px] font-bold text-accent">
                        {idx + 1}
                      </span>
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-[13.5px] font-semibold text-ink">{it.title}</span>
                          <UrgencyBadge urgency={it.urgency} label={urgencyLabel(it.urgency)} />
                        </div>
                        <div className="mt-1.5 space-y-1 text-[12.5px] leading-relaxed">
                          {it.reason && <Row k="原因" v={it.reason} />}
                          {it.action && <Row k="行动" v={it.action} />}
                          {it.expected_outcome && <Row k="预期" v={it.expected_outcome} />}
                        </div>
                        {it.reference && (
                          <button
                            className="mt-1.5 inline-flex items-center gap-1 text-[12px] text-accent hover:underline"
                            onClick={() =>
                              onTrace?.(ref ? [ref] : [{ title: it.reference, category: "", score: 0, snippet: "" }])
                            }
                          >
                            📎 {it.reference} <span className="font-medium">溯源 ›</span>
                          </button>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function priorityRank(p: string): number {
  return p === "recommended" ? 0 : p === "alternative" ? 1 : 2;
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex gap-2">
      <span className="shrink-0 font-medium text-muted">{k}</span>
      <span className="text-ink-2">{v}</span>
    </div>
  );
}

export function RefsBox({
  references,
  onTrace,
}: {
  references?: KnowledgeReference[];
  onTrace?: (refs: KnowledgeReference[]) => void;
}) {
  const [open, setOpen] = useState(false);
  if (!references || references.length === 0) return null;
  const groups = new Map<number, KnowledgeReference[]>();
  for (const r of references) {
    const key = r.document_id ?? r.item_id ?? 0;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(r);
  }
  const sorted = [...groups.entries()].sort(
    (a, b) => (b[1][0].score ?? 0) - (a[1][0].score ?? 0),
  );
  return (
    <div className="mt-3 rounded-xl border border-border-soft bg-surface-2 p-3">
      <button
        type="button"
        className="flex w-full items-center gap-1.5 text-left text-[12px] text-muted transition hover:text-ink-2"
        onClick={() => setOpen((v) => !v)}
      >
        <span className="shrink-0">{open ? "▾" : "▸"}</span>
        <span>📚 本次回答共检索 {groups.size} 个文档 / {references.length} 段（点击{open ? "收起" : "展开"}）</span>
      </button>
      {open && (
        <div className="mt-2 space-y-1.5">
          {sorted.map(([docId, refs]) => {
            const first = refs[0];
            const usedCount = refs.filter((r) => r.used).length;
            return (
              <button
                key={docId}
                type="button"
                className="flex w-full items-center gap-1.5 rounded-lg border border-border bg-surface px-2.5 py-2 text-left text-[12.5px] text-ink-2 transition hover:border-accent hover:text-accent"
                onClick={() => onTrace?.(refs)}
              >
                <span className="shrink-0">📄</span>
                <span className="truncate">{first.title}</span>
                <span className="ml-auto shrink-0 text-muted">{refs.length} 段</span>
                {first.score ? <span className="shrink-0 text-accent">· {first.score.toFixed(2)}</span> : null}
                {usedCount > 0 ? (
                  <span className="shrink-0 rounded-full bg-accent-soft px-1.5 py-0.5 text-[11px] text-accent">
                    已引用 {usedCount}
                  </span>
                ) : (
                  <span className="shrink-0 rounded-full bg-surface-2 px-1.5 py-0.5 text-[11px] text-muted">
                    仅检索
                  </span>
                )}
                <span className="shrink-0 text-accent">查看 ›</span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
