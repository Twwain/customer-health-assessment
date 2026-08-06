import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { chat, customers } from "../api";
import type {
  AssessmentResponse,
  AssessmentTrendResponse,
  CustomerResponse,
  FactorConfigResponse,
} from "../types";
import { getLevels, levelColor, trendMeta, type LevelSpec } from "../lib/ui";
import { useIsMobile } from "../hooks";
import { AlertBadge, LevelBadge } from "../components/Badges";
import { Sparkline, TrendChart } from "../components/Charts";
import CustomerForm, { BASIC_FIELDS } from "../components/CustomerForm";

interface Row {
  customer: CustomerResponse;
  a?: AssessmentResponse;
  t?: AssessmentTrendResponse;
}

// 等级表：优先本页已加载的 factorConfig（React 状态，避免依赖全局注册时序），
// 兜底用 Layout 启动时注册的全局等级表；均不写死"优秀/良好/一般/风险"。
const resolveLevels = (config: FactorConfigResponse | null): LevelSpec[] =>
  config?.levels?.length ? config.levels : getLevels();

export default function CustomerList() {
  const navigate = useNavigate();
  const isMobile = useIsMobile();

  const [rows, setRows] = useState<CustomerResponse[]>([]);
  const [assess, setAssess] = useState<Record<number, AssessmentResponse | null>>({});
  const [trends, setTrends] = useState<Record<number, AssessmentTrendResponse | null>>({});
  const [industries, setIndustries] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [search, setSearch] = useState("");
  const [industry, setIndustry] = useState("");
  const [level, setLevel] = useState("");
  const [trendFilter, setTrendFilter] = useState("");
  const [sort, setSort] = useState("score_desc");

  const [config, setConfig] = useState<FactorConfigResponse | null>(null);
  const [factorDrawer, setFactorDrawer] = useState<{ customer: CustomerResponse; readOnly: boolean } | null>(null);
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [saving, setSaving] = useState(false);
  const [trendDrawer, setTrendDrawer] = useState<CustomerResponse | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [editMode, setEditMode] = useState(false);

  const enrich = async (list: CustomerResponse[]) => {
    // 并发请求每个客户的评分与趋势；内层 promise 必须返回给 Promise.all，
    // 否则 await 立即完成、loading 提前结束（此前 loading 语义不准）
    await Promise.all(
      list.flatMap((c) => [
        customers
          .assessment(c.id)
          .then((a) => setAssess((p) => ({ ...p, [c.id]: a })))
          .catch(() => setAssess((p) => ({ ...p, [c.id]: null }))),
        customers
          .trend(c.id, 12)
          .then((t) => setTrends((p) => ({ ...p, [c.id]: t })))
          .catch(() => setTrends((p) => ({ ...p, [c.id]: null }))),
      ]),
    );
  };

  const fetchData = () => {
    setLoading(true);
    setError(null);
    customers
      .list({ page_size: 200 })
      .then(async (r) => {
        setRows(r.items);
        await enrich(r.items);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "加载失败"))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    fetchData();
    customers.industries().then(setIndustries).catch(() => {});
    customers.factorConfig().then(setConfig).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const data: Row[] = useMemo(
    () =>
      rows.map((c) => ({ customer: c, a: assess[c.id] ?? undefined, t: trends[c.id] ?? undefined })),
    [rows, assess, trends],
  );

  const filtered = useMemo(() => {
    let list = data;
    const q = search.trim().toLowerCase();
    if (q)
      list = list.filter(
        (x) =>
          x.customer.customer_name.toLowerCase().includes(q) ||
          x.customer.industry.toLowerCase().includes(q) ||
          x.customer.contact_person.toLowerCase().includes(q),
      );
    if (industry) list = list.filter((x) => x.customer.industry === industry);
    if (level) list = list.filter((x) => x.a?.level === level);
    if (trendFilter === "up") list = list.filter((x) => x.t?.trend === "up");
    else if (trendFilter === "down") list = list.filter((x) => x.t?.trend === "down");
    else if (trendFilter === "flat") list = list.filter((x) => x.t?.trend === "flat" || !x.t);
    const sorted = [...list];
    if (sort === "score_asc") sorted.sort((a, b) => (a.a?.total_score ?? 0) - (b.a?.total_score ?? 0));
    else if (sort === "delta") sorted.sort((a, b) => (a.t?.delta ?? 0) - (b.t?.delta ?? 0));
    else sorted.sort((a, b) => (b.a?.total_score ?? 0) - (a.a?.total_score ?? 0));
    return sorted;
  }, [data, search, industry, level, trendFilter, sort]);

  const stats = useMemo(() => {
    const scores = rows.map((c) => assess[c.id]?.total_score).filter((v): v is number => typeof v === "number");
    const avg = scores.length ? scores.reduce((s, v) => s + v, 0) / scores.length : 0;
    // 最低档等级（配置驱动，不假定叫"风险"）
    const lv = resolveLevels(config);
    const riskName = lv.length ? lv[lv.length - 1].name : "风险";
    const risk = rows.filter((c) => assess[c.id]?.level === riskName).length;
    const down = rows.filter((c) => trends[c.id]?.trend === "down").length;
    return { total: rows.length, avg: Math.round(avg * 10) / 10, risk, down, riskName };
  }, [rows, assess, trends, config]);

  const openFactor = (c: CustomerResponse) => {
    setDraft({});
    setFactorDrawer({ customer: c, readOnly: isMobile });
  };

  const saveFactors = async () => {
    if (!factorDrawer || !config) return;
    setSaving(true);
    let basicSaved = false;
    try {
      const id = factorDrawer.customer.id;
      const basic: Record<string, unknown> = {};
      const factors: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(draft)) {
        if ((BASIC_FIELDS as readonly string[]).includes(k)) basic[k] = v;
        else factors[k] = v;
      }
      if (Object.keys(basic).length) {
        await customers.update(id, basic);
        basicSaved = true;
      }
      const r = await customers.updateFactors(id, factors);
      setRows((prev) => prev.map((c) => (c.id === r.customer.id ? r.customer : c)));
      setAssess((p) => ({ ...p, [r.customer.id]: r.assessment }));
      // 因子变更后重算已写入历史，刷新该客户趋势（箭头 / sparkline / 趋势抽屉）
      customers
        .trend(r.customer.id, 12)
        .then((t) => setTrends((p) => ({ ...p, [r.customer.id]: t })))
        .catch(() => {});
      setFactorDrawer(null);
    } catch (e) {
      alert(
        `${basicSaved ? "基本信息已保存；" : ""}保存失败：` +
          (e instanceof Error ? e.message : "未知错误"),
      );
      // 基本信息已落库时刷新列表同步显示，抽屉保留以便重试因子部分
      if (basicSaved) fetchData();
    } finally {
      setSaving(false);
    }
  };

  const evalCustomer = async (c: CustomerResponse) => {
    try {
      const s = await chat.createSession({
        title: `${c.customer_name} · AI 评估`,
        customer_id: c.id,
        scenario: "assessment",
      });
      navigate(`/chat/${s.id}`, { state: { autoScenario: "assessment" } });
    } catch {
      alert("创建评估会话失败");
    }
  };

  return (
    <div className="mx-auto max-w-[1280px] px-6 py-7">
      {/* 页头 */}
      <div className="mb-5 flex flex-wrap items-center gap-3">
        <div className="mr-auto">
          <div className="flex items-center gap-2.5">
            <span className="h-[16px] w-[3px] rounded-full bg-accent" />
            <h1 className="text-[22px] font-semibold tracking-tight text-ink">客户库</h1>
          </div>
          <div className="mt-0.5 text-[13px] text-muted">量化评估引擎入口 · 编辑客情因子后可一键触发 AI 评估</div>
        </div>
        <button
          className="rounded-lg bg-accent px-3 py-2 text-[13px] font-medium text-white transition hover:bg-accent-hover"
          onClick={() => setAddOpen(true)}
        >
          ＋ 添加客户
        </button>
      </div>

      {/* 筛选工具栏（无盒化，直接悬浮在画布上） */}
      <div className="mb-5 flex flex-col gap-2 lg:flex-row lg:items-center">
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="搜索客户名称 / 行业 / 对接人…"
          className="max-w-[300px] flex-1 rounded-lg border border-border-strong bg-surface px-3 py-2 text-[13px] outline-none focus:border-accent focus:ring-2 focus:ring-accent/20"
        />
        <select value={industry} onChange={(e) => setIndustry(e.target.value)} className={selCls}>
          <option value="">全部行业</option>
          {industries.map((i) => (
            <option key={i} value={i}>
              {i}
            </option>
          ))}
        </select>
        <select value={level} onChange={(e) => setLevel(e.target.value)} className={selCls}>
          <option value="">全部等级</option>
          {resolveLevels(config).map((l) => (
            <option key={l.name} value={l.name}>
              {l.name}
            </option>
          ))}
        </select>
        <select value={trendFilter} onChange={(e) => setTrendFilter(e.target.value)} className={selCls}>
          <option value="">全部趋势</option>
          <option value="up">↑ 上升</option>
          <option value="down">↓ 下降</option>
          <option value="flat">→ 持平</option>
        </select>
        <select value={sort} onChange={(e) => setSort(e.target.value)} className={selCls}>
          <option value="score_desc">按健康分降序</option>
          <option value="score_asc">按健康分升序</option>
          <option value="delta">按跌幅排序</option>
        </select>
      </div>

      {/* 统计条 */}
      <div className="mb-6 flex flex-wrap items-center gap-x-7 gap-y-2 rounded-xl border border-border bg-surface px-5 py-3">
        <Stat label="客户总数" value={String(stats.total)} />
        <span className="hidden h-7 w-px bg-border sm:block" />
        <Stat label="平均健康分" value={String(stats.avg)} />
        <span className="hidden h-7 w-px bg-border sm:block" />
        <Stat label={`风险客户（${stats.riskName}级）`} value={String(stats.risk)} danger />
        <span className="hidden h-7 w-px bg-border sm:block" />
        <Stat label="趋势下滑客户" value={String(stats.down)} warning />
      </div>

      {loading ? (
        <div className="py-20 text-center text-muted">加载中…</div>
      ) : error ? (
        <div className="py-20 text-center">
          <div className="mb-2 text-danger">数据加载失败</div>
          <div className="mb-3 text-[13px] text-muted">{error}</div>
          <button className="rounded-lg bg-accent px-4 py-2 text-[13px] text-white" onClick={fetchData}>
            重试
          </button>
        </div>
      ) : filtered.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-24 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-surface-2 text-[22px]">👥</div>
          <div className="mt-3 text-[14px] font-medium text-ink-2">没有匹配的客户</div>
          <div className="mt-1 text-[12.5px] text-muted">试试调整搜索关键词或筛选条件</div>
        </div>
      ) : (
        <>
          {/* 桌面表格（无外框，仅行分隔） */}
          <div className="hidden md:block">
            {editMode && (
              <div className="mb-2 flex items-center gap-2 rounded-lg border border-accent/25 bg-accent-soft/50 px-3 py-2 text-[12.5px] text-accent">
                ✏️ 编辑模式已开启：点击任意客户行即可修改其信息，完成后点右上角「完成」退出。
              </div>
            )}
            <table className="w-full table-fixed border-collapse">
              <thead className="border-b border-border text-left text-[12px] text-muted">
                <tr>
                  <th className="w-[160px] whitespace-nowrap px-4 pb-2.5 font-medium">客户名称</th>
                  <th className="w-[90px] whitespace-nowrap px-4 pb-2.5 font-medium">对接人</th>
                  <th className="w-[90px] whitespace-nowrap px-4 pb-2.5 font-medium">行业</th>
                  <th className="w-[110px] whitespace-nowrap px-4 pb-2.5 font-medium">健康分</th>
                  <th className="w-[120px] whitespace-nowrap px-4 pb-2.5 font-medium">趋势</th>
                  <th className="w-[70px] whitespace-nowrap px-4 pb-2.5 font-medium">等级</th>
                  <th className="w-[280px] whitespace-nowrap px-4 pb-2.5 font-medium">预警</th>
                  <th className="w-[80px] whitespace-nowrap px-4 pb-2.5 text-right font-medium">
                    <button
                      className={`whitespace-nowrap rounded-md border px-2 py-1 text-[11.5px] font-medium transition ${
                        editMode
                          ? "border-accent bg-accent text-white"
                          : "border-border text-ink-2 hover:border-accent hover:text-accent"
                      }`}
                      onClick={() => setEditMode((v) => !v)}
                    >
                      {editMode ? "完成" : "编辑"}
                    </button>
                  </th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((x) => (
                  <tr
                    key={x.customer.id}
                    className={`group border-b border-border-soft transition last:border-0 hover:bg-surface-3 ${
                      editMode ? "cursor-pointer" : ""
                    }`}
                    onClick={() => {
                      if (editMode) openFactor(x.customer);
                    }}
                  >
                    <td className="px-4 py-3.5">
                      <span className="whitespace-nowrap font-medium text-ink">{x.customer.customer_name}</span>
                    </td>
                    <td className="px-4 py-3.5">
                      <span className="whitespace-nowrap text-[12.5px] text-ink-2">
                        {x.customer.contact_person || "—"}
                      </span>
                    </td>
                    <td className="px-4 py-3.5">
                      <div className="max-w-[108px] truncate whitespace-nowrap text-[13px] text-ink-2">
                        {x.customer.industry || "—"}
                      </div>
                    </td>
                    <td className="px-4 py-3.5">
                      {x.a ? (
                        <ScoreCell score={x.a.total_score} level={x.a.level} />
                      ) : (
                        <span className="text-[12px] text-muted">计算中…</span>
                      )}
                    </td>
                    <td className="px-4 py-3.5">
                      {x.t ? (
                        <button
                          className="group/spark flex items-center rounded-lg px-1 py-0.5 transition hover:bg-surface-2"
                          title="点击查看趋势详情"
                          onClick={(e) => {
                            e.stopPropagation();
                            setTrendDrawer(x.customer);
                          }}
                        >
                          <Sparkline values={x.t.points.map((p) => p.total_score)} color={levelColor(x.a?.level || "一般")} width={92} height={26} />
                          <span className="ml-0.5 text-[10px] text-muted opacity-0 transition group-hover/spark:opacity-100">⤢</span>
                        </button>
                      ) : (
                        <span className="text-[12px] text-muted">—</span>
                      )}
                    </td>
                    <td className="px-4 py-3.5">{x.a ? <LevelBadge grade={x.a.level} size="sm" /> : "—"}</td>
                    <td className="px-4 py-3.5">
                      {x.a && x.a.alerts.length > 0 ? (
                        <div className="flex items-center gap-1 overflow-hidden">
                          {x.a.alerts.slice(0, 2).map((al, i) => (
                            <AlertBadge key={i} level={al.level} message={al.message} />
                          ))}
                          {x.a.alerts.length > 2 && (
                            <span className="shrink-0 rounded-full bg-surface-2 px-1.5 py-[2px] text-[11px] text-muted">
                              +{x.a.alerts.length - 2}
                            </span>
                          )}
                        </div>
                      ) : (
                        <span className="text-[12px] text-muted">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* 移动卡片 */}
          <div className="space-y-3 md:hidden">
            {filtered.map((x) => (
              <div key={x.customer.id} className="rounded-xl border border-border bg-surface p-3.5">
                <div className="flex items-start gap-2">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="truncate text-[15px] font-semibold text-ink">{x.customer.customer_name}</span>
                      {x.a && <LevelBadge grade={x.a.level} size="sm" />}
                    </div>
                    <div className="mt-0.5 text-[11.5px] text-muted">
                      {x.customer.industry || "未填写行业"}
                    </div>
                  </div>
                  <div className="text-right">
                    <div className="text-[19px] font-bold" style={{ color: x.a ? levelColor(x.a.level) : "#787671" }}>
                      {x.a ? Math.round(x.a.total_score * 10) / 10 : "—"}
                    </div>
                    {x.t && <TrendArrow trend={x.t} />}
                  </div>
                </div>
                {x.t && (
                  <div className="mt-2 flex justify-center">
                    <button
                      className="rounded-lg px-1 py-0.5 transition hover:bg-surface-2"
                      title="点击查看趋势详情"
                      onClick={() => setTrendDrawer(x.customer)}
                    >
                      <Sparkline values={x.t.points.map((p) => p.total_score)} color={levelColor(x.a?.level || "一般")} width={120} height={28} />
                    </button>
                  </div>
                )}
                {x.a && x.a.alerts.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-1">
                    {x.a.alerts.slice(0, 2).map((al, i) => (
                      <AlertBadge key={i} level={al.level} message={al.message} />
                    ))}
                  </div>
                )}
                <div className="mt-2.5 flex gap-2">
                  <button className="flex-1 rounded-lg border border-border py-2 text-[12.5px] text-ink-2" onClick={() => openFactor(x.customer)}>
                    编辑
                  </button>
                </div>
              </div>
            ))}
          </div>
          <div className="mt-3 text-[12px] text-muted">
            共 {filtered.length} 条
          </div>
        </>
      )}

      {factorDrawer && config && (
        <Drawer
          title={factorDrawer.readOnly ? `客户信息 — ${factorDrawer.customer.customer_name}` : `编辑客户 — ${factorDrawer.customer.customer_name}`}
          onClose={() => setFactorDrawer(null)}
          narrow={factorDrawer.readOnly}
          footer={
            factorDrawer.readOnly ? (
              <>
                <span className="mr-auto text-[12px] text-muted">
                  当前基础客情分{" "}
                  <b className="text-danger">
                    {factorDrawer.customer ? (assess[factorDrawer.customer.id]?.total_score ?? "—") : "—"}
                  </b>
                </span>
                <button className="rounded-lg border border-border px-3 py-2 text-[13px] text-ink-2" onClick={() => setFactorDrawer(null)}>
                  关闭
                </button>
                <button className="rounded-lg bg-accent px-3 py-2 text-[13px] font-medium text-white" onClick={() => evalCustomer(factorDrawer.customer)}>
                  ✨ AI 评估
                </button>
              </>
            ) : (
              <>
                <span className="mr-auto text-[12px] text-muted">保存后重算基础客情分并写入评估历史</span>
                <button className="rounded-lg border border-border px-3 py-2 text-[13px] text-ink-2" onClick={() => setFactorDrawer(null)}>
                  取消
                </button>
                <button className="rounded-lg bg-accent px-3 py-2 text-[13px] font-medium text-white disabled:opacity-50" onClick={saveFactors} disabled={saving}>
                  {saving ? "保存中…" : "保存并重新评分"}
                </button>
              </>
            )
          }
        >
          {factorDrawer.readOnly && (
            <div className="mb-3 rounded-lg border border-warning/30 bg-warning-soft px-3 py-2 text-[12px] text-warning">
              📱 移动端为只读视图。因子编辑请在桌面端完成。
            </div>
          )}
          <CustomerForm
            customer={factorDrawer.customer}
            config={config}
            value={draft}
            onChange={setDraft}
            readOnly={factorDrawer.readOnly}
          />
        </Drawer>
      )}

      {trendDrawer && (
        <Drawer title={`📈 ${trendDrawer.customer_name} — 健康分历史趋势`} onClose={() => setTrendDrawer(null)}>
          <TrendDrawerBody a={assess[trendDrawer.id] ?? undefined} t={trends[trendDrawer.id] ?? undefined} />
        </Drawer>
      )}

      {addOpen && <AddCustomerModal config={config} onClose={() => setAddOpen(false)} onDone={() => { setAddOpen(false); fetchData(); }} />}
    </div>
  );
}

const selCls =
  "rounded-lg border border-border-strong bg-surface px-3 py-2 text-[13px] text-ink outline-none focus:border-accent focus:ring-2 focus:ring-accent/20";

function Stat({ label, value, danger, warning }: { label: string; value: string; danger?: boolean; warning?: boolean }) {
  const color = danger ? "text-danger" : warning ? "text-warning" : "text-ink";
  return (
    <div className="flex items-baseline gap-2">
      <div className={`text-[20px] font-semibold leading-none ${color}`}>{value}</div>
      <div className="text-[12px] text-muted">{label}</div>
    </div>
  );
}

function ScoreCell({ score, level }: { score: number; level: string }) {
  const color = levelColor(level);
  return (
    <span className="whitespace-nowrap text-[16px] font-bold" style={{ color }}>
      {Math.round(score * 10) / 10}
    </span>
  );
}

function TrendArrow({ trend }: { trend: AssessmentTrendResponse }) {
  const t = trendMeta(trend.latest_score, trend.previous_score);
  return (
    <span className={`text-[11px] font-medium ${t.cls === "trend-up" ? "text-success" : t.cls === "trend-down" ? "text-danger" : "text-muted"}`}>
      {t.arrow} {t.text}
    </span>
  );
}

function Drawer({
  title,
  onClose,
  children,
  footer,
  narrow,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
  footer?: React.ReactNode;
  narrow?: boolean;
}) {
  return (
    <>
      <div className="overlay-mask" onClick={onClose} />
      <div className={`drawer-panel ${narrow ? "narrow" : ""}`}>
        <div className="flex items-center border-b border-border px-4 py-3">
          <h3 className="flex-1 truncate text-[15px] font-semibold text-ink">{title}</h3>
          <button className="ml-2 flex h-7 w-7 items-center justify-center rounded-lg text-muted hover:bg-surface-2" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">{children}</div>
        {footer && <div className="flex items-center gap-2 border-t border-border px-4 py-3">{footer}</div>}
      </div>
    </>
  );
}

function TrendDrawerBody({
  a,
  t,
}: {
  a?: AssessmentResponse;
  t?: AssessmentTrendResponse;
}) {
  const color = a ? levelColor(a.level) : "var(--color-primary)";
  const history = t ? [...t.points].reverse() : [];
  return (
    <div>
      {a && (
        <div className="mb-3 flex items-center gap-3">
          <div>
            <div className="text-[20px] font-bold" style={{ color }}>
              {Math.round(a.total_score * 10) / 10}
            </div>
            <LevelBadge grade={a.level} size="sm" />
          </div>
          <div className="text-[12px] text-muted">
            满分 {a.max_score} · 共 {t?.points.length ?? 0} 次评估
          </div>
        </div>
      )}
      {t ? (
        <div className="overflow-x-auto rounded-xl border border-border-soft bg-surface-2 p-3">
          <TrendChart trend={t} color={color} width={420} height={180} />
        </div>
      ) : (
        <div className="rounded-xl border border-border-soft bg-surface-2 p-4 text-[13px] text-muted">暂无趋势数据</div>
      )}
      {history.length > 0 && (
        <div className="mt-3 overflow-hidden rounded-xl border border-border">
          <table className="w-full text-[12.5px]">
            <thead className="bg-surface-2 text-muted">
              <tr>
                <th className="px-3 py-2 text-left font-medium">评估时间</th>
                <th className="px-3 py-2 text-right font-medium">总分</th>
                <th className="px-3 py-2 text-right font-medium">变化</th>
                <th className="px-3 py-2 text-left font-medium">等级</th>
              </tr>
            </thead>
            <tbody>
              {history.map((p, i) => {
                const d = i < history.length - 1 ? +(history[i + 1].total_score - p.total_score).toFixed(1) : null;
                return (
                  <tr key={i} className="border-t border-border-soft">
                    <td className="px-3 py-2 text-ink-2">{p.label}</td>
                    <td className="px-3 py-2 text-right font-medium" style={{ color: levelColor(p.level) }}>
                      {p.total_score}
                    </td>
                    <td className="px-3 py-2 text-right">
                      {d == null ? (
                        "—"
                      ) : (
                        <span className={d > 0 ? "text-success" : d < 0 ? "text-danger" : "text-muted"}>
                          {d > 0 ? "+" : ""}
                          {d}
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2">
                      <LevelBadge grade={p.level} size="sm" />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <div className="mt-3 rounded-lg border border-border-soft bg-surface-2 px-3 py-2 text-[12px] text-muted">
        曲线基于 AssessmentHistory 全量记录；点击行内任意位置可编辑客户信息。
      </div>
    </div>
  );
}

const EMPTY_CUSTOMER: CustomerResponse = {
  id: 0,
  customer_name: "",
  industry: "",
  contact_person: "",
  contact_phone: "",
  cooperation_years: 0,
  contact_frequency: "",
  last_contact_date: null,
  customer_satisfaction: 0,
  contract_amount: 0,
  payment_status: "",
  risk_signals: "",
  competitor_involvement: false,
  growth_potential: "",
  notes: "",
  custom_fields: {},
  created_at: "",
  updated_at: "",
};

// 批量导入模板表头：须与 backend/routers/customers.py 的 field_map 保持一致
const IMPORT_TEMPLATE_HEADERS = [
  "客户名称",
  "行业",
  "对接人",
  "联系电话",
  "合作年限",
  "沟通频率",
  "最近联系日期",
  "客户满意度",
  "合同金额(万元)",
  "回款情况",
  "风险信号",
  "竞品介入",
  "增长潜力",
  "备注",
];
const IMPORT_TEMPLATE_SAMPLE_MAP: Record<string, string> = {
  客户名称: "示例科技",
  行业: "制造业",
  对接人: "张三",
  联系电话: "13800000000",
  备注: "重点跟进客户",
  合作年限: "3",
  沟通频率: "每月",
  最近联系日期: "2026-07-01",
  满意度评分: "8",
  合同金额: "500",
  回款情况: "正常",
  风险信号: "",
  竞品介入: "否",
  增长潜力: "高",
  交付质量: "良",
  技术适配度: "高",
  需求响应时效: "24小时内响应",
  团队稳定性: "稳定",
};

// 由配置动态生成模板表头：基础字段 + 各因子 label（与后端导入识别一致）
const buildTemplateHeaders = (cfg: FactorConfigResponse | null): string[] => {
  if (!cfg) return IMPORT_TEMPLATE_HEADERS;
  const base = ["客户名称", "行业", "对接人", "联系电话", "备注"];
  const labels: string[] = [];
  for (const dim of cfg.dimensions) {
    for (const f of dim.factors) {
      if (f.input.type === "readonly") continue;
      labels.push(f.label);
    }
  }
  return [...base, ...labels];
};

function AddCustomerModal({
  config,
  onClose,
  onDone,
}: {
  config: FactorConfigResponse | null;
  onClose: () => void;
  onDone: () => void;
}) {
  const [tab, setTab] = useState<"single" | "import">("single");
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);

  const downloadTemplate = () => {
    const headers = buildTemplateHeaders(config);
    const sample = headers.map((h) => IMPORT_TEMPLATE_SAMPLE_MAP[h] ?? "");
    const csv = "﻿" + [headers.join(","), sample.join(",")].join("\r\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "客户导入模板.csv";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const submitSingle = async () => {
    const name = String(draft.customer_name ?? "").trim();
    if (!name) return;
    setBusy(true);
    try {
      // 基本信息走 create，因子走 factors 接口（未注册字段会被后端忽略）
      const basic: Record<string, unknown> = {};
      const factors: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(draft)) {
        if ((BASIC_FIELDS as readonly string[]).includes(k)) basic[k] = v;
        else factors[k] = v;
      }
      const c = await customers.create({ ...basic, customer_name: name });
      if (Object.keys(factors).length) await customers.updateFactors(c.id, factors);
      onDone();
    } catch (e) {
      alert("创建失败：" + (e instanceof Error ? e.message : "未知错误"));
      setBusy(false);
    }
  };

  const submitImport = async () => {
    if (!file) return;
    setBusy(true);
    try {
      const r = await customers.import(file);
      alert(`导入完成：新建 ${r.created ?? 0} 条${r.errors?.length ? `，${r.errors.length} 条出错` : ""}`);
      onDone();
    } catch (e) {
      alert("导入失败：" + (e instanceof Error ? e.message : "未知错误"));
      setBusy(false);
    }
  };

  return (
    <>
      <div className="overlay-mask" onClick={onClose} />
      <div className="modal-panel">
        <div className="flex items-center border-b border-border px-4 py-3">
          <h3 className="flex-1 text-[15px] font-semibold text-ink">＋ 添加客户</h3>
          <button className="ml-2 flex h-7 w-7 items-center justify-center rounded-lg text-muted hover:bg-surface-2" onClick={onClose}>
            ✕
          </button>
        </div>

        {/* Tab 切换：逐个新建 / 批量导入 */}
        <div className="flex gap-1 border-b border-border px-4 pt-2">
          {([
            { k: "single", t: "新建客户" },
            { k: "import", t: "批量导入（Excel/CSV）" },
          ] as const).map((item) => (
            <button
              key={item.k}
              onClick={() => setTab(item.k)}
              className={`rounded-t-lg px-3 py-2 text-[13px] transition ${
                tab === item.k ? "border-b-2 border-accent font-medium text-accent" : "text-muted hover:text-ink-2"
              }`}
            >
              {item.t}
            </button>
          ))}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          {tab === "single" ? (
            <div className="space-y-3">
              {config ? (
                <CustomerForm customer={EMPTY_CUSTOMER} config={config} value={draft} onChange={setDraft} />
              ) : (
                <div className="text-[12.5px] text-muted">因子配置加载中…</div>
              )}
            </div>
          ) : (
            <div className="space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <button
                  onClick={downloadTemplate}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-surface px-3 py-2 text-[12.5px] text-ink-2 transition hover:border-accent hover:text-accent"
                >
                  ⬇️ 下载模板
                </button>
                <label className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-border bg-surface px-3 py-2 text-[12.5px] text-ink-2 transition hover:border-accent hover:text-accent">
                  📁 选择文件
                  <input
                    type="file"
                    accept=".csv,.xlsx"
                    className="hidden"
                    onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                  />
                </label>
                {file && (
                  <span className="inline-flex items-center gap-1.5 rounded-lg bg-surface-2 px-3 py-1.5 text-[12.5px] text-ink-2">
                    已选择：{file.name}
                    <button
                      type="button"
                      onClick={() => setFile(null)}
                      className="ml-0.5 text-muted transition hover:text-[#E60012]"
                    >
                      ✕
                    </button>
                  </span>
                )}
              </div>
            </div>
          )}
        </div>

        <div className="flex items-center gap-2 border-t border-border px-4 py-3">
          <span className="mr-auto" />
          <button className="rounded-lg border border-border px-3 py-2 text-[13px] text-ink-2" onClick={onClose}>
            取消
          </button>
          {tab === "single" ? (
            <button
              className="rounded-lg bg-accent px-3 py-2 text-[13px] font-medium text-white disabled:opacity-50"
              onClick={submitSingle}
              disabled={busy || !String(draft.customer_name ?? "").trim()}
            >
              {busy ? "创建中…" : "创建客户"}
            </button>
          ) : (
            <button
              className="rounded-lg bg-accent px-3 py-2 text-[13px] font-medium text-white disabled:opacity-50"
              onClick={submitImport}
              disabled={busy || !file}
            >
              {busy ? "导入中…" : "开始导入"}
            </button>
          )}
        </div>
      </div>
    </>
  );
}
