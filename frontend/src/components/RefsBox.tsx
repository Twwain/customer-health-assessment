import { useState } from "react";
import type { KnowledgeReference } from "../types";

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
