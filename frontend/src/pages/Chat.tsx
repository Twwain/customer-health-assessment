import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { chat, customers, streamChat } from "../api";
import type {
  AssessmentResponse,
  AssessmentTrendResponse,
  ChatMessageItem,
  KnowledgeReference,
  StrategyItem,
} from "../types";
import { useLLMStatus } from "../statusContext";
import { renderMarkdown } from "../lib/ui";
import HealthCard from "../components/HealthCard";
import { RefsBox, StrategyList } from "../components/StrategyList";

interface StreamMsg {
  role: "assistant";
  content: string;
  references: KnowledgeReference[];
  strategy_items: StrategyItem[];
  assessment?: AssessmentResponse | null;
  trend?: AssessmentTrendResponse | null;
  degraded: boolean;
  scenario?: string;
  streaming: true;
}

type Scenario = "assessment" | "strategy" | "alert_analysis";
const EP: Record<Scenario, string> = {
  assessment: "evaluate",
  strategy: "strategy",
  alert_analysis: "alert-analysis",
};

export default function Chat() {
  const { sessionId } = useParams();
  const sessionIdNum = sessionId ? Number(sessionId) : null;
  const location = useLocation();
  const navigate = useNavigate();
  const { status } = useLLMStatus();

  const [messages, setMessages] = useState<ChatMessageItem[]>([]);
  const [stream, setStream] = useState<StreamMsg | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sessionTitle, setSessionTitle] = useState("AI 对话");
  const [sessionCustomerId, setSessionCustomerId] = useState<number | null>(null);
  const [ctx, setCtx] = useState<{ assessment?: AssessmentResponse | null; trend?: AssessmentTrendResponse | null }>({});
  const [trace, setTrace] = useState<KnowledgeReference | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [feedbacks, setFeedbacks] = useState<Record<number, string>>({});
  const [adopted, setAdopted] = useState<Record<number, boolean>>({});

  const busyRef = useRef(false);
  const streamRef = useRef<StreamMsg | null>(null);
  const customerIdRef = useRef<number | null>(null);
  const scenarioRef = useRef<string>("free_qa");
  const lastAutoRef = useRef<string>("");
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const pendingScenario = useRef<Scenario | null>(null);

  // ── 加载会话 ───────────────────────────────────────────────
  const loadSession = async (id: number) => {
    try {
      const s = await chat.getSession(id);
      setMessages(s.messages);
      setSessionTitle(s.title || "AI 对话");
      setSessionCustomerId(s.customer_id);
      customerIdRef.current = s.customer_id;
      scenarioRef.current = s.scenario;
      for (const m of s.messages) if (m.feedback) setFeedbacks((p) => ({ ...p, [m.id]: m.feedback }));
      if (s.customer_id) fetchCtx(s.customer_id);
      const auto = (location.state as { autoScenario?: Scenario } | null)?.autoScenario;
      const key = `${id}:${auto ?? ""}`;
      if (auto && lastAutoRef.current !== key) {
        lastAutoRef.current = key;
        runScenarioEndpoint(auto, s.customer_id);
      }
    } catch {
      setError("会话加载失败");
    }
  };

  const fetchCtx = (cid: number) => {
    customers.assessment(cid).then((a) => setCtx((p) => ({ ...p, assessment: a }))).catch(() => {});
    customers.trend(cid, 12).then((t) => setCtx((p) => ({ ...p, trend: t }))).catch(() => {});
  };

  useEffect(() => {
    // 切换会话前清空上个会话的全部状态，避免旧消息/反馈/溯源闪现
    streamRef.current = null;
    setStream(null);
    setError(null);
    setTrace(null);
    setFeedbacks({});
    setAdopted({});
    if (!sessionIdNum) {
      setMessages([]);
      setCtx({});
      return;
    }
    setMessages([]);
    setCtx({});
    loadSession(sessionIdNum);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionIdNum]);

  // ── SSE 流式发送 ──────────────────────────────────────────
  const handleEvent = (ev: { type: string; data: any }) => {
    if (ev.type === "done") {
      const msg = ev.data.message as ChatMessageItem;
      streamRef.current = null;
      setStream(null);
      setMessages((prev) => [...prev, msg]);
      return;
    }
    if (ev.type === "error") {
      streamRef.current = null;
      setStream(null);
      setError(ev.data?.message || "生成出错");
      return;
    }
    const cur = streamRef.current || {
      role: "assistant" as const,
      content: "",
      references: [] as KnowledgeReference[],
      strategy_items: [] as StrategyItem[],
      degraded: false,
      streaming: true as const,
    };
    let next: StreamMsg = cur;
    switch (ev.type) {
      case "start":
        next = { ...cur, scenario: ev.data.scenario, degraded: !!ev.data.degraded };
        break;
      case "context":
        next = { ...cur, assessment: ev.data.assessment, trend: ev.data.trend };
        break;
      case "delta":
        next = { ...cur, content: cur.content + ev.data.text };
        break;
      case "strategy":
        next = { ...cur, strategy_items: ev.data.items };
        break;
      case "references":
        next = { ...cur, references: ev.data.items };
        break;
      case "warning":
        next = { ...cur, content: (cur.content ? cur.content + "\n\n" : "") + "> ⚠️ " + ev.data.message };
        break;
    }
    streamRef.current = next;
    setStream(next);
  };

  const sendTurn = async (id: number, endpoint: string, body: Record<string, unknown>) => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    if (body.content) {
      const um: ChatMessageItem = {
        id: -Date.now(),
        session_id: id,
        role: "user",
        content: String(body.content),
        references: [],
        strategy_items: [],
        tokens_used: 0,
        feedback: "",
        degraded: false,
        created_at: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, um]);
    }
    streamRef.current = {
      role: "assistant",
      content: "",
      references: [],
      strategy_items: [],
      degraded: false,
      streaming: true,
    };
    setStream(streamRef.current);
    try {
      await streamChat(`/api${endpoint}`, { stream: true, ...body }, handleEvent);
    } catch (e) {
      setError(e instanceof Error ? e.message : "流式请求失败");
      streamRef.current = null;
      setStream(null);
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const runScenarioEndpoint = (scenario: Scenario, customerId: number | null) => {
    if (!sessionIdNum) return;
    sendTurn(sessionIdNum, `/chat/sessions/${sessionIdNum}/${EP[scenario]}`, {
      customer_id: customerId ?? undefined,
    });
  };

  const runQuick = (scenario: Scenario) => {
    if (!sessionIdNum) return;
    if (customerIdRef.current) runScenarioEndpoint(scenario, customerIdRef.current);
    else setPickerOpen(true);
  };

  const sendMessage = () => {
    const text = inputRef.current?.value.trim();
    if (!text || !sessionIdNum) return;
    sendTurn(sessionIdNum, `/chat/sessions/${sessionIdNum}/messages`, {
      content: text,
      customer_id: customerIdRef.current ?? undefined,
    });
    if (inputRef.current) inputRef.current.value = "";
  };

  const regenerate = () => {
    if (!sessionIdNum || busyRef.current) return;
    const idx = [...messages].reverse().findIndex((m) => m.role === "assistant");
    if (idx < 0) return;
    const lastId = messages[messages.length - 1 - idx].id;
    setMessages((prev) => prev.filter((m) => m.id !== lastId));
    sendTurn(sessionIdNum, `/chat/sessions/${sessionIdNum}/regenerate`, {
      scenario: scenarioRef.current,
      customer_id: customerIdRef.current ?? undefined,
    });
  };

  const setFeedback = async (msgId: number, fb: string) => {
    const next = feedbacks[msgId] === fb ? "" : fb;
    setFeedbacks((p) => ({ ...p, [msgId]: next }));
    try {
      await chat.feedback(msgId, next);
    } catch {
      /* ignore */
    }
  };

  const exportPdf = () => {
    if (sessionCustomerId) window.open(`/api/assessment/${sessionCustomerId}/pdf`, "_blank");
    else alert("该会话未关联客户，无法导出报告");
  };

  const deleteSession = async () => {
    if (!sessionIdNum) return;
    if (!confirm("确定删除该会话？")) return;
    try {
      await chat.deleteSession(sessionIdNum);
      navigate("/chat");
    } catch {
      alert("删除失败");
    }
  };

  const handlePick = (cid: number) => {
    const sc = pendingScenario.current || "assessment";
    pendingScenario.current = null;
    setPickerOpen(false);
    chat
      .createSession({ title: `客户评估`, customer_id: cid, scenario: sc })
      .then((s) => navigate(`/chat/${s.id}`, { state: { autoScenario: sc } }))
      .catch(() => alert("创建会话失败"));
  };

  const picker = pickerOpen && (
    <CustomerPicker
      onClose={() => {
        setPickerOpen(false);
        pendingScenario.current = null;
      }}
      onPick={handlePick}
    />
  );

  // ── 渲染 ──────────────────────────────────────────────────
  if (!sessionIdNum) {
    return (
      <>
        <WelcomeScreen
          onPick={(sc) => {
            pendingScenario.current = sc;
            setPickerOpen(true);
          }}
          onKnowledge={() => navigate("/knowledge")}
        />
        {picker}
      </>
    );
  }

  const display = stream ? [...messages, stream as unknown as ChatMessageItem] : messages;

  return (
    <div className="flex h-full flex-col">
      {/* 会话头部 */}
      <div className="flex shrink-0 items-center gap-2 border-b border-border bg-surface px-4 py-2.5">
        <div className="min-w-0 flex-1">
          <div className="truncate text-[14px] font-semibold text-ink">{sessionTitle}</div>
          {sessionCustomerId && (
            <div className="truncate text-[11.5px] text-muted">
              🔗 上下文：客户 #{sessionCustomerId}
            </div>
          )}
        </div>
        <button
          className="rounded-lg border border-border px-2.5 py-1.5 text-[12.5px] text-ink-2 transition hover:border-accent hover:text-accent disabled:opacity-40"
          onClick={regenerate}
          disabled={busy || display.filter((m) => m.role === "assistant").length === 0}
        >
          🔄 重新生成
        </button>
        <button
          className="rounded-lg border border-border px-2.5 py-1.5 text-[12.5px] text-ink-2 transition hover:border-accent hover:text-accent"
          onClick={exportPdf}
        >
          📄 导出报告
        </button>
        <button
          className="rounded-lg border border-border px-2.5 py-1.5 text-[12.5px] text-danger transition hover:border-danger"
          onClick={deleteSession}
        >
          🗑
        </button>
      </div>

      {/* 消息区 */}
      <div className="min-h-0 flex-1 overflow-y-auto bg-bg">
        <div className="mx-auto max-w-[860px] space-y-4 px-4 py-4">
          {display.map((m, i) => {
            const isStream = stream != null && i === display.length - 1;
            const mid = m.id ?? -1;
            return (
              <MessageView
                key={mid}
                msg={m}
                assessment={isStream ? (stream as StreamMsg)?.assessment : ctx.assessment}
                trend={isStream ? (stream as StreamMsg)?.trend : ctx.trend}
                streaming={isStream}
                feedback={feedbacks[mid] || ""}
                adopted={!!adopted[mid]}
                onTrace={setTrace}
                onFeedback={(fb) => setFeedback(mid, fb)}
                onAdopt={() => setAdopted((p) => ({ ...p, [mid]: !p[mid] }))}
              />
            );
          })}
          {error && (
            <div className="rounded-lg border border-danger/30 bg-danger-soft px-3 py-2 text-[13px] text-danger">
              ⚠️ {error}
            </div>
          )}
        </div>
      </div>

      {/* 降级条 */}
      {status?.degraded && (
        <div className="shrink-0 border-t border-warning/30 bg-warning-soft px-4 py-2 text-[12.5px] text-warning">
          ⚠️ <b>LLM 服务暂不可用</b>，已自动降级为规则引擎模式：评分、客户管理、PDF 报告不受影响，AI 对话与策略生成将使用兜底回复。
        </div>
      )}

      {/* 输入区 */}
      <div className="shrink-0 border-t border-border bg-surface px-4 py-3">
        <div className="mx-auto max-w-[860px]">
          <div className="mb-2 flex gap-1.5 overflow-x-auto">
            <QuickChip label="📊 综合评估" onClick={() => runQuick("assessment")} disabled={busy} />
            <QuickChip label="🎯 生成策略" onClick={() => runQuick("strategy")} disabled={busy} />
            <QuickChip label="🚨 风险排查" onClick={() => runQuick("alert_analysis")} disabled={busy} />
            <QuickChip label="📄 生成报告" onClick={exportPdf} disabled={busy} />
          </div>
          <div className="flex items-end gap-2 rounded-xl border border-border-strong bg-surface px-3 py-2 focus-within:border-accent">
            <textarea
              ref={inputRef}
              rows={1}
              placeholder="输入消息，或描述你关心的客户与问题…"
              className="max-h-32 flex-1 resize-none bg-transparent text-[13.5px] text-ink outline-none"
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  sendMessage();
                }
              }}
            />
            <button
              className="rounded-lg bg-accent px-3.5 py-1.5 text-[13px] font-medium text-white transition hover:bg-accent-hover disabled:opacity-50"
              onClick={sendMessage}
              disabled={busy}
            >
              ➤
            </button>
          </div>
        </div>
      </div>

      {trace && <TraceDrawer reference={trace} onClose={() => setTrace(null)} />}
      {picker}
    </div>
  );
}

function QuickChip({ label, onClick, disabled }: { label: string; onClick: () => void; disabled?: boolean }) {
  return (
    <button
      className="shrink-0 rounded-full border border-border-strong bg-surface px-3 py-1.5 text-[12.5px] text-ink-2 transition hover:border-accent hover:bg-surface-2 hover:text-accent disabled:opacity-40"
      onClick={onClick}
      disabled={disabled}
    >
      {label}
    </button>
  );
}

function MessageView({
  msg,
  assessment,
  trend,
  streaming,
  feedback,
  adopted,
  onTrace,
  onFeedback,
  onAdopt,
}: {
  msg: ChatMessageItem;
  assessment?: AssessmentResponse | null;
  trend?: AssessmentTrendResponse | null;
  streaming?: boolean;
  feedback: string;
  adopted: boolean;
  onTrace: (r: KnowledgeReference) => void;
  onFeedback: (fb: string) => void;
  onAdopt: () => void;
}) {
  if (msg.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[80%] rounded-xl rounded-tr-sm bg-ink-2 px-3.5 py-2.5 text-[13.5px] text-white">
          {msg.content}
        </div>
      </div>
    );
  }
  return (
    <div className="flex gap-2.5">
      <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-brand/10 text-[15px]">🤖</div>
      <div className="min-w-0 flex-1">
        {assessment && <div className="mb-2"><HealthCard assessment={assessment} trend={trend ?? null} compact onAlertAI={undefined} /></div>}
        {msg.content && (
          <div className="md-text rounded-xl rounded-tl-sm border border-border bg-surface px-3.5 py-2.5">
            <div className="md">
              {renderMarkdown(msg.content)}
              {streaming && <span className="cursor-blink ml-0.5" />}
            </div>
          </div>
        )}
        {msg.strategy_items && msg.strategy_items.length > 0 && (
          <div className="mt-2">
            <StrategyList items={msg.strategy_items} references={msg.references} onTrace={onTrace} />
          </div>
        )}
        <RefsBox references={msg.references} onTrace={onTrace} />
        <div className="mt-1.5 flex items-center gap-1.5 pl-1">
          <ToolBtn active={feedback === "up"} onClick={() => onFeedback("up")} label="👍" />
          <ToolBtn active={feedback === "down"} onClick={() => onFeedback("down")} label="👎" />
          <span className="mx-1 h-3.5 w-px bg-border" />
          <button
            className={`rounded-md px-2 py-1 text-[12px] transition ${adopted ? "bg-accent-soft text-accent" : "text-ink-2 hover:bg-surface-2"}`}
            onClick={onAdopt}
          >
            {adopted ? "✓ 已采纳（proposed）" : "⭐ 采纳策略"}
          </button>
          <button
            className="rounded-md px-2 py-1 text-[12px] text-ink-2 transition hover:bg-surface-2"
            onClick={() => navigator.clipboard?.writeText(msg.content)}
          >
            📋 复制
          </button>
          {msg.degraded && <span className="ml-auto text-[11px] text-warning">规则引擎兜底</span>}
        </div>
      </div>
    </div>
  );
}

function ToolBtn({ active, onClick, label }: { active: boolean; onClick: () => void; label: string }) {
  return (
    <button
      className={`rounded-md px-2 py-1 text-[13px] transition ${active ? "bg-accent-soft text-accent" : "text-ink-2 hover:bg-surface-2"}`}
      onClick={onClick}
    >
      {label}
    </button>
  );
}

function TraceDrawer({ reference, onClose }: { reference: KnowledgeReference; onClose: () => void }) {
  const snippet = reference.snippet || "（无切片预览，可在知识库中查看原文）";
  return (
    <>
      <div className="overlay-mask" onClick={onClose} />
      <div className="drawer-panel">
        <div className="flex items-center border-b border-border px-4 py-3">
          <h3 className="flex-1 text-[15px] font-semibold text-ink">📎 知识溯源</h3>
          <button className="ml-2 flex h-7 w-7 items-center justify-center rounded-lg text-muted hover:bg-surface-2" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          <div className="rounded-xl border border-border bg-surface-2 p-3">
            <div className="text-[14px] font-semibold text-ink">{reference.title || "—"}</div>
            <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[12px] text-muted">
              {reference.category && <span>分类：{reference.category}</span>}
              {reference.score ? <span>相似度：<b className="text-accent">{reference.score.toFixed(2)}</b></span> : null}
            </div>
          </div>
          <div className="mb-1.5 mt-3 text-[11.5px] text-muted">命中切片原文</div>
          <div className="rounded-xl border border-border-soft bg-surface p-3 text-[13px] leading-relaxed text-ink-2">{snippet}</div>
          <div className="mt-3 rounded-lg border border-border-soft bg-surface-2 px-3 py-2 text-[12px] text-muted">
            💡 该片段由检索链路召回：metadata 过滤 → dense 向量召回 → Rerank 重排。
          </div>
        </div>
        <div className="flex items-center gap-2 border-t border-border px-4 py-3">
          <span className="mr-auto" />
          <button className="rounded-lg border border-border px-3 py-2 text-[13px] text-ink-2" onClick={onClose}>
            关闭
          </button>
        </div>
      </div>
    </>
  );
}

function CustomerPicker({ onClose, onPick }: { onClose: () => void; onPick: (cid: number) => void }) {
  const [list, setList] = useState<{ id: number; customer_name: string; industry: string }[]>([]);
  const [q, setQ] = useState("");
  useEffect(() => {
    customers.list({ page_size: 200 }).then((r) => setList(r.items.map((c) => ({ id: c.id, customer_name: c.customer_name, industry: c.industry })))).catch(() => {});
  }, []);
  const filtered = list.filter((c) => c.customer_name.toLowerCase().includes(q.toLowerCase()) || c.industry.toLowerCase().includes(q.toLowerCase()));
  return (
    <>
      <div className="overlay-mask" onClick={onClose} />
      <div className="modal-panel">
        <div className="flex items-center border-b border-border px-4 py-3">
          <h3 className="flex-1 text-[15px] font-semibold text-ink">选择客户</h3>
          <button className="ml-2 flex h-7 w-7 items-center justify-center rounded-lg text-muted hover:bg-surface-2" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          <input
            autoFocus
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="搜索客户…"
            className="mb-3 w-full rounded-lg border border-border-strong px-3 py-2 text-[13px] outline-none focus:border-accent"
          />
          <div className="max-h-[50vh] space-y-1.5 overflow-y-auto">
            {filtered.map((c) => (
              <button
                key={c.id}
                className="flex w-full items-center justify-between rounded-lg border border-border bg-surface px-3 py-2.5 text-left text-[13.5px] transition hover:border-accent"
                onClick={() => onPick(c.id)}
              >
                <span className="font-medium text-ink">{c.customer_name}</span>
                <span className="text-[12px] text-muted">{c.industry || "—"}</span>
              </button>
            ))}
            {filtered.length === 0 && <div className="py-6 text-center text-[13px] text-muted">无匹配客户</div>}
          </div>
        </div>
      </div>
    </>
  );
}

function WelcomeScreen({
  onPick,
  onKnowledge,
}: {
  onPick: (sc: Scenario) => void;
  onKnowledge: () => void;
}) {
  const entries: { icon: string; title: string; desc: string; sc?: Scenario; nav?: string }[] = [
    { icon: "📊", title: "评估某个客户", desc: "选择客户 → 自动评分 + AI 综合分析", sc: "assessment" },
    { icon: "🚨", title: "排查风险客户", desc: "聚焦预警成因与止损动作", sc: "alert_analysis" },
    { icon: "🎯", title: "制定客户策略", desc: "基于评估结果生成三层可执行策略", sc: "strategy" },
    { icon: "📚", title: "查询内部知识", desc: "分级标准 / SLA / 行业基准 / 竞品动态", nav: "knowledge" },
  ];
  return (
    <div className="flex min-h-full items-center justify-center overflow-y-auto bg-bg">
      <div className="w-full max-w-[820px] px-6 py-10">
        <div className="mb-8 flex flex-col items-center text-center">
          <div className="flex items-center gap-3">
            <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-xl bg-accent-soft text-[22px]">🤖</div>
            <h1 className="text-[24px] font-semibold tracking-tight text-ink">你好，我是客情分析助手</h1>
          </div>
          <p className="mt-2 text-[13.5px] leading-relaxed text-muted">
            结合<strong className="font-medium text-ink">量化评估引擎</strong>（因子评分）与<strong className="font-medium text-ink">知识增强引擎</strong>（RAG 检索），帮你分析客户健康度、排查风险、制定可执行策略。
          </p>
        </div>

        <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
          {entries.map((e) => (
            <button
              key={e.title}
              className="flex flex-col items-center gap-2.5 rounded-xl border border-border bg-surface p-5 text-center transition hover:border-accent/60 hover:bg-surface-3"
              onClick={() => (e.nav ? onKnowledge() : onPick(e.sc!))}
            >
              <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-accent-soft/70 text-[18px]">
                {e.icon}
              </div>
              <div>
                <div className="text-[14px] font-medium text-ink">{e.title}</div>
                <div className="mt-1 text-[12.5px] leading-relaxed text-muted">{e.desc}</div>
              </div>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
