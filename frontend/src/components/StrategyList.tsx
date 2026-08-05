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
  onTrace?: (ref: KnowledgeReference) => void;
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
            <div className="mb-1.5 flex items-center gap-1.5 text-[13px] font-semibold text-brand">
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
                              onTrace?.(ref || { title: it.reference, category: "", score: 0, snippet: "" })
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
  onTrace?: (ref: KnowledgeReference) => void;
}) {
  if (!references || references.length === 0) return null;
  return (
    <div className="mt-3 rounded-xl border border-border-soft bg-surface-2 p-3">
      <div className="mb-1.5 text-[12px] text-muted">
        📚 本次回答共检索 {references.length} 条知识（metadata 过滤 → 双路召回 → Rerank）
      </div>
      <div className="flex flex-wrap gap-1.5">
        {references.map((r, i) => (
          <button
            key={i}
            className="inline-flex items-center gap-1 rounded-full border border-border bg-surface px-2 py-1 text-[11.5px] text-ink-2 transition hover:border-accent hover:text-accent"
            onClick={() => onTrace?.(r)}
          >
            📄 {r.title}
            {r.score ? <span className="text-accent">· {r.score.toFixed(2)}</span> : null}
          </button>
        ))}
      </div>
    </div>
  );
}
