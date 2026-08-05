import { useEffect, useState } from "react";
import { knowledge } from "../api";
import type {
  KnowledgeItemResponse,
  KnowledgeSearchResult,
  KnowledgeStatusResponse,
} from "../types";
import { categoryIcon, storageLabel } from "../lib/ui";

// 知识分类与后端 models.KNOWLEDGE_CATEGORIES 保持一致（分类权重按此表生效）
const KNOWLEDGE_CATEGORIES = ["内部规范", "内部指标", "外部指标", "对话沉淀"] as const;

export default function Knowledge() {
  const [items, setItems] = useState<KnowledgeItemResponse[]>([]);
  const [categories, setCategories] = useState<string[]>([]);
  const [activeCat, setActiveCat] = useState("全部");
  const [loading, setLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchStatus, setSearchStatus] = useState("all");
  const [searchResults, setSearchResults] = useState<KnowledgeSearchResult[] | null>(null);
  const [searching, setSearching] = useState(false);

  const [uploadOpen, setUploadOpen] = useState(false);
  const [metaItem, setMetaItem] = useState<KnowledgeItemResponse | null>(null);
  const [deleteItem, setDeleteItem] = useState<KnowledgeItemResponse | null>(null);
  const [reindexing, setReindexing] = useState(false);

  const loadItems = (cat: string) => {
    setLoading(true);
    knowledge
      .items({ category: cat === "全部" ? undefined : cat, limit: 200 })
      .then((r) => setItems(r.items))
      .catch(() => setItems([]))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    loadItems(activeCat);
    knowledge
      .status()
      .then((s: KnowledgeStatusResponse) => setCategories(s.categories))
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeCat]);

  const runSearch = async () => {
    if (!searchQuery.trim()) return;
    setSearching(true);
    try {
      const r = await knowledge.search({
        query: searchQuery.trim(),
        status: searchStatus === "all" ? "all" : searchStatus,
        top_k: 5,
      });
      setSearchResults(r.results);
    } catch {
      setSearchResults([]);
    } finally {
      setSearching(false);
    }
  };

  const approve = async (id: number) => {
    try {
      const r = await knowledge.approve(id);
      setItems((prev) => prev.map((it) => (it.id === id ? r : it)));
    } catch {
      alert("审核失败");
    }
  };

  const remove = async () => {
    if (!deleteItem) return;
    try {
      await knowledge.remove(deleteItem.id);
      setItems((prev) => prev.filter((it) => it.id !== deleteItem.id));
      setDeleteItem(null);
    } catch {
      alert("删除失败");
    }
  };

  const reindex = async () => {
    setReindexing(true);
    try {
      const r = await knowledge.reindex();
      alert(`重建索引完成：重新索引 ${r.reindexed ?? 0} 条`);
    } catch {
      alert("重建索引失败");
    } finally {
      setReindexing(false);
    }
  };

  const tabs = ["全部", ...categories];

  return (
    <div className="mx-auto max-w-[1280px] px-6 py-7">
      <div className="mb-5 flex flex-wrap items-center gap-3">
        <div className="mr-auto">
          <div className="flex items-center gap-2.5">
            <span className="h-[16px] w-[3px] rounded-full bg-accent" />
            <h1 className="text-[22px] font-semibold tracking-tight text-ink">知识库</h1>
          </div>
          <div className="mt-0.5 text-[13px] text-muted">知识增强引擎入口 · 支持浏览 / 检索 / 上传 / 删除 / 编辑元数据</div>
        </div>
        <button
          className="rounded-lg border border-border-strong bg-surface px-3 py-2 text-[13px] text-ink-2 transition hover:border-accent hover:text-accent"
          onClick={reindex}
          disabled={reindexing}
        >
          🔄 重建索引
        </button>
        <button
          className="rounded-lg bg-accent px-3 py-2 text-[13px] font-medium text-white transition hover:bg-accent-hover"
          onClick={() => setUploadOpen(true)}
        >
          ⬆ 上传文档
        </button>
      </div>

      <div className="mb-5 flex flex-col gap-2 lg:flex-row lg:items-center">
        <input
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && runSearch()}
          placeholder="语义检索：输入你的问题，如「竞品介入后如何挽回制造业客户」…"
          className="max-w-[560px] flex-1 rounded-lg border border-border-strong bg-surface px-3 py-2 text-[13px] outline-none focus:border-accent focus:ring-2 focus:ring-accent/20"
        />
        <select value={searchStatus} onChange={(e) => setSearchStatus(e.target.value)} className={selCls}>
          <option value="all">全部状态</option>
          <option value="canonical">canonical 已审核</option>
          <option value="proposed">proposed 待审核</option>
        </select>
        <button className="rounded-lg bg-accent px-3 py-2 text-[13px] font-medium text-white transition hover:bg-accent-hover" onClick={runSearch} disabled={searching}>
          {searching ? "检索中…" : "🔍 检索"}
        </button>
      </div>

      {searchResults && (
        <div className="mb-4 rounded-xl border border-accent/30 bg-accent-soft/40 p-3">
          <div className="mb-2 flex items-center">
            <span className="text-[13px] text-ink-2">
              🔍 检索到 <b>{searchResults.length}</b> 条相关片段
            </span>
            <span className="ml-2 text-[11.5px] text-muted">（metadata 过滤 → dense 向量召回 → Rerank 重排）</span>
            <button className="ml-auto rounded-lg border border-border bg-surface px-2.5 py-1 text-[12px] text-ink-2" onClick={() => setSearchResults(null)}>
              清除
            </button>
          </div>
          <div className="space-y-2">
            {searchResults.map((r, i) => (
              <div key={i} className="rounded-xl border border-border bg-surface p-3">
                <div className="mb-1 flex flex-wrap items-center gap-2 text-[12px]">
                  <span className="font-medium text-ink">{r.item_title}</span>
                  <span className="rounded bg-surface-2 px-1.5 py-[1px] text-muted">{r.category}</span>
                  <span className="rounded bg-surface-2 px-1.5 py-[1px] text-muted">P.{r.chunk_index}</span>
                  <span className="ml-auto text-accent">相似度 {r.score.toFixed(2)}</span>
                </div>
                <div className="text-[12.5px] leading-relaxed text-ink-2">{r.content}</div>
              </div>
            ))}
            {searchResults.length === 0 && <div className="py-3 text-center text-[13px] text-muted">未检索到相关片段</div>}
          </div>
        </div>
      )}

      <div className="mb-4 flex gap-1.5 overflow-x-auto pb-1">
        {tabs.map((c) => (
          <button
            key={c}
            className={`shrink-0 rounded-full px-3 py-1.5 text-[13px] transition ${
              activeCat === c ? "bg-accent text-white" : "border border-border bg-surface text-ink-2 hover:border-accent hover:text-accent"
            }`}
            onClick={() => setActiveCat(c)}
          >
            {c} <span className={activeCat === c ? "opacity-80" : "text-muted"}>{c === "全部" ? items.length : ""}</span>
          </button>
        ))}
      </div>

      <div className="mb-3 rounded-lg border border-border-soft bg-surface-2 px-3 py-2 text-[12px] text-muted">
        💡 <span className="font-medium text-ink-2">知识分层存储：</span>叙事/文本类知识进<span className="font-medium">向量库</span>；精确数值类指标进<span className="font-medium">SQLite 结构化表</span>，评估时按行业/规模精确查询，避免向量检索数值误差。
      </div>

      {loading ? (
        <div className="py-20 text-center text-muted">加载中…</div>
      ) : items.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-24 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-surface-2 text-[22px]">📄</div>
          <div className="mt-3 text-[14px] font-medium text-ink-2">暂无知识条目</div>
          <div className="mt-1 text-[12.5px] text-muted">点击右上角「上传文档」添加第一条知识</div>
        </div>
      ) : (
        <div className="overflow-hidden rounded-xl border border-border bg-surface">
          {items.map((k) => (
            <Row
              key={k.id}
              item={k}
              onApprove={() => approve(k.id)}
              onMeta={() => setMetaItem(k)}
              onDelete={() => setDeleteItem(k)}
            />
          ))}
        </div>
      )}

      <div className="mt-3 rounded-lg border border-warning/30 bg-warning-soft px-3 py-2 text-[12px] text-warning">
        ⚠️ <span className="font-medium">正文不可编辑：</span>修改正文需重新切片 + 向量化，本期仅支持编辑元数据（标题 / 分类 / 标签）。内容更新请重新上传文档覆盖。
      </div>

      {uploadOpen && <UploadModal onClose={() => setUploadOpen(false)} onDone={() => { setUploadOpen(false); loadItems(activeCat); }} />}
      {metaItem && <MetaModal item={metaItem} onClose={() => setMetaItem(null)} onDone={(updated) => { setItems((prev) => prev.map((it) => (it.id === updated.id ? updated : it))); setMetaItem(null); }} />}
      {deleteItem && (
        <>
          <div className="overlay-mask" onClick={() => setDeleteItem(null)} />
          <div className="modal-panel" style={{ width: 420 }}>
            <div className="flex items-center border-b border-border px-4 py-3">
              <h3 className="flex-1 text-[15px] font-semibold text-ink">确认删除</h3>
              <button className="ml-2 flex h-7 w-7 items-center justify-center rounded-lg text-muted hover:bg-surface-2" onClick={() => setDeleteItem(null)}>
                ✕
              </button>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto p-4">
              <p className="text-[13.5px] leading-relaxed">
                确定删除知识条目 <b>「{deleteItem.title}」</b>？
              </p>
              <div className="mt-3 rounded-lg border border-warning/30 bg-warning-soft px-3 py-2 text-[12px] text-warning">
                ⚠️ 将同时删除 SQLite 条目记录与向量库中的 {deleteItem.chunk_count} 条向量（级联清理，不可恢复）。
              </div>
            </div>
            <div className="flex items-center gap-2 border-t border-border px-4 py-3">
              <span className="mr-auto" />
              <button className="rounded-lg border border-border px-3 py-2 text-[13px] text-ink-2" onClick={() => setDeleteItem(null)}>
                取消
              </button>
              <button className="rounded-lg bg-danger px-3 py-2 text-[13px] font-medium text-white" onClick={remove}>
                确认删除
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

const selCls =
  "rounded-lg border border-border-strong bg-surface px-3 py-2 text-[13px] text-ink outline-none focus:border-accent focus:ring-2 focus:ring-accent/20";

function Row({
  item,
  onApprove,
  onMeta,
  onDelete,
}: {
  item: KnowledgeItemResponse;
  onApprove: () => void;
  onMeta: () => void;
  onDelete: () => void;
}) {
  const canonical = item.status === "canonical";
  return (
    <div className="flex flex-col gap-2.5 border-b border-border-soft px-4 py-3.5 transition last:border-0 hover:bg-surface-3 sm:flex-row sm:items-center">
      <div className="flex min-w-0 flex-1 items-start gap-2.5">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-surface-2 text-[18px]">
          {categoryIcon(item.category)}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="truncate text-[14px] font-medium text-ink">{item.title}</span>
            <span
              className={`shrink-0 rounded-full px-2 py-[2px] text-[11px] font-medium ${
                canonical ? "bg-success-soft text-success" : "bg-warning-soft text-warning"
              }`}
            >
              {canonical ? "✓ 已审核" : "待审核"}
            </span>
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-[11.5px] text-muted">
            <span>{item.category}</span>
            <span>·</span>
            <span>{item.chunk_count} 切片</span>
            <span>·</span>
            <span>{storageLabel(item.storage)}</span>
            {item.tags.slice(0, 3).map((t) => (
              <span key={t} className="rounded-full bg-surface-2 px-2 py-[1px] text-[11px] text-ink-2">
                #{t}
              </span>
            ))}
            {item.adoption_count > 0 && (
              <span>· 采纳 {item.adoption_count} 次</span>
            )}
          </div>
          {item.summary && <div className="mt-1 truncate text-[12px] text-muted">{item.summary}</div>}
        </div>
      </div>
      <div className="flex shrink-0 flex-wrap items-center gap-1.5">
        {!canonical && (
          <button className="rounded-lg bg-accent px-2.5 py-1.5 text-[12px] font-medium text-white transition hover:bg-accent-hover" onClick={onApprove}>
            通过审核
          </button>
        )}
        <button className="rounded-lg border border-border px-2.5 py-1.5 text-[12px] text-ink-2 transition hover:border-accent hover:text-accent" onClick={onMeta}>
          编辑元数据
        </button>
        <button className="rounded-lg border border-border px-2.5 py-1.5 text-[12px] text-danger transition hover:border-danger" onClick={onDelete}>
          删除
        </button>
      </div>
    </div>
  );
}

function UploadModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [category, setCategory] = useState<string>(KNOWLEDGE_CATEGORIES[0]);
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    if (!file) return;
    setBusy(true);
    try {
      await knowledge.upload(file, category, title.trim() || undefined);
      onDone();
    } catch (e) {
      alert("上传失败：" + (e instanceof Error ? e.message : "未知错误"));
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <div className="overlay-mask" onClick={onClose} />
      <div className="modal-panel">
        <div className="flex items-center border-b border-border px-4 py-3">
          <h3 className="flex-1 text-[15px] font-semibold text-ink">⬆ 上传知识文档</h3>
          <button className="ml-2 flex h-7 w-7 items-center justify-center rounded-lg text-muted hover:bg-surface-2" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          <div className="rounded-xl border-2 border-dashed border-border bg-surface-2 p-6 text-center">
            <div className="text-[28px]">📄</div>
            <div className="mt-2 text-[13px] text-ink-2">点击选择，或拖拽文件到此处</div>
            <div className="mt-1 text-[11.5px] text-muted">支持 PDF / Markdown / Word / Excel / TXT，单文件 ≤ 50MB</div>
            <input type="file" className="mt-3 block w-full text-[12.5px]" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
            {file && <div className="mt-2 text-[12.5px] text-accent">已选择：{file.name}</div>}
          </div>
          <div className="mt-3">
            <label className="mb-1 block text-[12.5px] font-medium text-ink-2">知识分类</label>
            <select value={category} onChange={(e) => setCategory(e.target.value)} className={`${selCls} w-full`}>
              {KNOWLEDGE_CATEGORIES.map((c) => (
                <option key={c}>{c}</option>
              ))}
            </select>
          </div>
          <div className="mt-3">
            <label className="mb-1 block text-[12.5px] font-medium text-ink-2">标题（可选）</label>
            <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="留空则使用文件名" className="w-full rounded-lg border border-border-strong px-3 py-2 text-[13px] outline-none focus:border-accent" />
          </div>
          <div className="mt-3 rounded-lg border border-border-soft bg-surface-2 px-3 py-2 text-[12px] text-muted">
            💡 上传后自动执行：解析 → 切片 → 向量化 → 写入向量库 + SQLite。数值型指标表将同时抽取为结构化字段。
          </div>
        </div>
        <div className="flex items-center gap-2 border-t border-border px-4 py-3">
          <span className="mr-auto" />
          <button className="rounded-lg border border-border px-3 py-2 text-[13px] text-ink-2" onClick={onClose}>
            取消
          </button>
          <button className="rounded-lg bg-accent px-3 py-2 text-[13px] font-medium text-white disabled:opacity-50" onClick={submit} disabled={busy || !file}>
            {busy ? "上传中…" : "开始上传并索引"}
          </button>
        </div>
      </div>
    </>
  );
}

function MetaModal({
  item,
  onClose,
  onDone,
}: {
  item: KnowledgeItemResponse;
  onClose: () => void;
  onDone: (updated: KnowledgeItemResponse) => void;
}) {
  const [title, setTitle] = useState(item.title);
  const [category, setCategory] = useState(item.category);
  const [tags, setTags] = useState(item.tags.join(", "));
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true);
    try {
      const r = await knowledge.update(item.id, {
        title: title.trim() || item.title,
        category,
        tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
      });
      onDone(r);
    } catch (e) {
      alert("保存失败：" + (e instanceof Error ? e.message : "未知错误"));
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <div className="overlay-mask" onClick={onClose} />
      <div className="modal-panel">
        <div className="flex items-center border-b border-border px-4 py-3">
          <h3 className="flex-1 text-[15px] font-semibold text-ink">编辑元数据</h3>
          <button className="ml-2 flex h-7 w-7 items-center justify-center rounded-lg text-muted hover:bg-surface-2" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          <div className="mb-3">
            <label className="mb-1 block text-[12.5px] font-medium text-ink-2">标题</label>
            <input value={title} onChange={(e) => setTitle(e.target.value)} className="w-full rounded-lg border border-border-strong px-3 py-2 text-[13px] outline-none focus:border-accent" />
          </div>
          <div className="mb-3">
            <label className="mb-1 block text-[12.5px] font-medium text-ink-2">分类</label>
            <select value={category} onChange={(e) => setCategory(e.target.value)} className={`${selCls} w-full`}>
              {/* 历史/非法分类值也展示为选项，避免打开弹窗时当前值不可见而误改 */}
              {Array.from(new Set([...KNOWLEDGE_CATEGORIES, category]).values()).map((c) => (
                <option key={c}>{c}</option>
              ))}
            </select>
          </div>
          <div className="mb-3">
            <label className="mb-1 block text-[12.5px] font-medium text-ink-2">标签（逗号分隔）</label>
            <input value={tags} onChange={(e) => setTags(e.target.value)} className="w-full rounded-lg border border-border-strong px-3 py-2 text-[13px] outline-none focus:border-accent" />
          </div>
          <div className="mb-3">
            <label className="mb-1 block text-[12.5px] font-medium text-ink-2">审核状态</label>
            <div className={`rounded-lg border border-border bg-surface-2 px-3 py-2 text-[13px] ${item.status === "canonical" ? "text-success" : "text-warning"}`}>
              {item.status === "canonical" ? "✓ canonical（已审核）" : "proposed（待审核）— 通过审核按钮切换"}
            </div>
          </div>
          <div className="rounded-lg border border-border-soft bg-surface-2 px-3 py-2 text-[12px] text-muted">
            💡 元数据修改会同步更新向量库中所有切片的 metadata，但不会触发重新向量化。
          </div>
        </div>
        <div className="flex items-center gap-2 border-t border-border px-4 py-3">
          <span className="mr-auto" />
          <button className="rounded-lg border border-border px-3 py-2 text-[13px] text-ink-2" onClick={onClose}>
            取消
          </button>
          <button className="rounded-lg bg-accent px-3 py-2 text-[13px] font-medium text-white disabled:opacity-50" onClick={submit} disabled={busy}>
            {busy ? "保存中…" : "保存"}
          </button>
        </div>
      </div>
    </>
  );
}
