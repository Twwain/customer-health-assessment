import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { chat, customers, streamChat } from "../api";
import type {
  AssessmentResponse,
  AssessmentTrendResponse,
  ChatEvent,
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
const SCENE_LABEL: Record<Scenario, string> = {
  assessment: "综合评估",
  strategy: "策略建议",
  alert_analysis: "风险排查",
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
  const [resuming, setResuming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sessionTitle, setSessionTitle] = useState("AI 对话");
  const [sessionCustomerId, setSessionCustomerId] = useState<number | null>(null);
  const [exporting, setExporting] = useState(false);
  const [exportJob, setExportJob] = useState<{ id: string; status: string } | null>(null);
  const [ctx, setCtx] = useState<{ assessment?: AssessmentResponse | null; trend?: AssessmentTrendResponse | null }>({});
  const [trace, setTrace] = useState<KnowledgeReference[] | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [feedbacks, setFeedbacks] = useState<Record<number, string>>({});
  const [adopted, setAdopted] = useState<Record<number, boolean>>({});

  const busyRef = useRef(false);
  const streamRef = useRef<StreamMsg | null>(null);
  const customerIdRef = useRef<number | null>(null);
  const scenarioRef = useRef<string>("free_qa");
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const pendingScenario = useRef<Scenario | null>(null);
  const pollRef = useRef<number | null>(null);

  const stopPolling = () => {
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };
  const startPolling = (id: number) => {
    stopPolling();
    const startedAt = Date.now();
    pollRef.current = window.setInterval(() => {
      if (Date.now() - startedAt > 120000) {
        stopPolling();
        setResuming(false);
        setError("生成超时，请刷新页面或重新发送");
        return;
      }
      void loadSession(id);
    }, 3000);
  };

  // ── SSE 流式发送 ──────────────────────────────────────────
  const handleEvent = (ev: ChatEvent) => {
    if (ev.type === "done") {
      const msg = ev.data.message as ChatMessageItem;
      streamRef.current = null;
      setStream(null);
      setMessages((prev) => [...prev, msg]);
      stopPolling();
      setResuming(false);
      return;
    }
    if (ev.type === "error") {
      streamRef.current = null;
      setStream(null);
      stopPolling();
      setResuming(false);
      setError((ev.data.message as string | undefined) || "生成出错");
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
        next = { ...cur, scenario: ev.data.scenario as string, degraded: !!ev.data.degraded };
        break;
      case "context":
        next = {
          ...cur,
          assessment: ev.data.assessment as AssessmentResponse,
          trend: ev.data.trend as AssessmentTrendResponse | undefined,
        };
        break;
      case "delta":
        next = { ...cur, content: cur.content + (ev.data.text as string) };
        break;
      case "strategy":
        next = { ...cur, strategy_items: ev.data.items as StrategyItem[] };
        break;
      case "references":
        next = { ...cur, references: ev.data.items as KnowledgeReference[] };
        break;
      case "warning":
        next = { ...cur, content: (cur.content ? cur.content + "\n\n" : "") + "> ⚠️ " + (ev.data.message as string) };
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

  // ── 加载会话 ───────────────────────────────────────────────
  const fetchCtx = (cid: number) => {
    customers.assessment(cid).then((a) => setCtx((p) => ({ ...p, assessment: a }))).catch(() => {});
    customers.trend(cid, 12).then((t) => setCtx((p) => ({ ...p, trend: t }))).catch(() => {});
  };

  const loadSession = (id: number) =>
    chat
      .getSession(id)
      .then((s) => {
        setMessages(s.messages);
        setSessionTitle(s.title || "AI 对话");
        setSessionCustomerId(s.customer_id);
        customerIdRef.current = s.customer_id;
        scenarioRef.current = s.scenario;
        for (const m of s.messages) if (m.feedback) setFeedbacks((p) => ({ ...p, [m.id]: m.feedback }));
        if (s.customer_id) fetchCtx(s.customer_id);
        // 恢复提示：切走再切回时，若后端标记会话仍在生成则显示「正在生成」并轮询
        if (s.streaming) {
          setResuming(true);
          startPolling(id);
        } else {
          setResuming(false);
          stopPolling();
        }
      })
      .catch(() => setError("会话加载失败"));

  useEffect(() => {
    // 会话切换由路由层 key 重挂载保证状态全新（App.tsx ChatBySession），
    // 这里只负责加载当前会话，不再手动清空上个会话的状态
    if (!sessionIdNum) return;
    const auto = (location.state as { autoScenario?: Scenario } | null)?.autoScenario;
    // 快捷场景（评估/策略/预警）在加载完成后的异步回调里触发，
    // 避免在 effect 的同步链路上触发 setState（react-hooks/set-state-in-effect）
    void loadSession(sessionIdNum).then(() => {
      if (auto && customerIdRef.current) runScenarioEndpoint(auto, customerIdRef.current);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionIdNum]);

  // 组件卸载时停止恢复轮询
  useEffect(() => () => stopPolling(), []);

  const runQuick = (scenario: Scenario) => {
    if (!sessionIdNum) return;
    if (customerIdRef.current) runScenarioEndpoint(scenario, customerIdRef.current);
    else setPickerOpen(true);
  };

  const streamLabel = (scenario?: string) => {
    if (scenario && scenario in SCENE_LABEL) {
      return `正在生成${SCENE_LABEL[scenario as Scenario]}…`;
    }
    return "思考中…";
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

  const exportPdf = async () => {
    if (!sessionCustomerId) {
      alert("该会话未关联客户，无法生成报告");
      return;
    }
    setExporting(true);
    setExportJob(null);
    try {
      // 默认始终使用 AI 生成；AI 不可用时后端自动降级为规则引擎
      const job = await customers.pdfJob(sessionCustomerId, true);
      setExportJob({ id: job.job_id, status: job.status });
    } catch {
      alert("导出任务创建失败，请稍后重试");
      setExporting(false);
    }
  };

  // 后台生成：轮询任务状态，成功后切换为「下载报告」
  useEffect(() => {
    if (
      !exportJob ||
      !sessionCustomerId ||
      exportJob.status === "ready" ||
      exportJob.status === "error"
    ) {
      return;
    }
    const jobId = exportJob.id;
    const timer = setInterval(async () => {
      try {
        const s = await customers.pdfJobStatus(sessionCustomerId, jobId);
        if (s.status === "ready" || s.status === "error") {
          clearInterval(timer);
          setExportJob({ id: jobId, status: s.status });
          setExporting(false);
          if (s.status === "error") {
            alert("报告生成失败：" + (s.error || "未知错误"));
          }
        }
      } catch {
        clearInterval(timer);
        setExporting(false);
        alert("导出任务状态获取失败，请重试");
      }
    }, 2000);
    return () => clearInterval(timer);
  }, [exportJob, sessionCustomerId]);

  const cancelExport = () => {
    // 仅停止前端轮询与状态，后端任务会在 TTL 内自行回收
    setExportJob(null);
    setExporting(false);
  };

  const downloadPdf = async () => {
    if (!exportJob || !sessionCustomerId) return;
    try {
      const blob = await customers.pdfDownload(sessionCustomerId, exportJob.id);
      const name = ctx.assessment?.customer_name || `客户${sessionCustomerId}`;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${name}_客情评估报告.pdf`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      // 延迟释放，避免个别浏览器在下载开始前就回收 blob URL
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      setExportJob(null);
    } catch {
      alert("报告下载失败，请重试");
    }
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

  const handlePick = (c: { id: number; customer_name: string }) => {
    const sc = pendingScenario.current || "assessment";
    pendingScenario.current = null;
    setPickerOpen(false);
    chat
      .createSession({ title: `${c.customer_name} · ${SCENE_LABEL[sc]}`, customer_id: c.id, scenario: sc })
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
  const showResume = resuming && !stream && !busy;

  return (
    <div className="flex h-full flex-col">
      {/* 会话头部 */}
      <div className="flex shrink-0 items-center gap-2 border-b border-border bg-surface px-4 py-2.5">
        <div className="min-w-0 flex-1">
          <div className="truncate text-[15px] font-semibold text-ink">{sessionTitle}</div>
          {sessionCustomerId && (
            <div className="truncate text-[12.5px] text-muted">
              🔗 上下文：客户 #{sessionCustomerId}
            </div>
          )}
        </div>
        <button
          className="rounded-lg border border-border px-2.5 py-1.5 text-[13.5px] text-ink-2 transition hover:border-accent hover:text-accent disabled:opacity-40"
          onClick={regenerate}
          disabled={busy || display.filter((m) => m.role === "assistant").length === 0}
        >
          🔄 重新生成
        </button>
        {exportJob?.status === "ready" ? (
          <button
            className="rounded-lg bg-accent px-2.5 py-1.5 text-[13.5px] font-medium text-white transition hover:bg-accent-hover"
            onClick={downloadPdf}
          >
            ⬇️ 下载报告
          </button>
        ) : (
          <>
            <button
              className="rounded-lg border border-border px-2.5 py-1.5 text-[13.5px] text-ink-2 transition hover:border-accent hover:text-accent disabled:opacity-40"
              onClick={exportPdf}
              disabled={exporting || !sessionCustomerId}
            >
              {exporting ? "⏳ 生成中…" : "📄 生成报告"}
            </button>
            {exporting && exportJob?.status === "running" && (
              <button
                className="rounded-lg border border-border px-2.5 py-1.5 text-[13.5px] text-ink-2 transition hover:border-danger hover:text-danger"
                onClick={cancelExport}
              >
                取消
              </button>
            )}
          </>
        )}
        <button
          className="rounded-lg border border-border px-2.5 py-1.5 text-[13.5px] text-danger transition hover:border-danger"
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
                streamHint={isStream ? streamLabel((stream as StreamMsg)?.scenario) : undefined}
                feedback={feedbacks[mid] || ""}
                adopted={!!adopted[mid]}
                onTrace={setTrace}
                onFeedback={(fb) => setFeedback(mid, fb)}
                onAdopt={() => setAdopted((p) => ({ ...p, [mid]: !p[mid] }))}
              />
            );
          })}
          {showResume && (
            <div className="rounded-xl border border-border bg-surface px-3.5 py-2.5 text-[14px] text-muted">
              <span className="cursor-blink mr-1" />
              正在生成…
            </div>
          )}
          {error && (
            <div className="rounded-lg border border-danger/30 bg-danger-soft px-3 py-2 text-[14px] text-danger">
              ⚠️ {error}
            </div>
          )}
        </div>
      </div>

      {/* 降级条 */}
      {status?.degraded && (
        <div className="shrink-0 border-t border-warning/30 bg-warning-soft px-4 py-2 text-[13.5px] text-warning">
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
              className="max-h-32 flex-1 resize-none bg-transparent text-[14.5px] text-ink outline-none"
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  sendMessage();
                }
              }}
            />
            <button
              className="rounded-lg bg-accent px-3.5 py-1.5 text-[14px] font-medium text-white transition hover:bg-accent-hover disabled:opacity-50"
              onClick={sendMessage}
              disabled={busy}
            >
              ➤
            </button>
          </div>
        </div>
      </div>

      {trace && <TraceDrawer references={trace} onClose={() => setTrace(null)} />}
      {picker}
    </div>
  );
}

function QuickChip({ label, onClick, disabled }: { label: string; onClick: () => void; disabled?: boolean }) {
  return (
    <button
      className="shrink-0 rounded-full border border-border-strong bg-surface px-3 py-1.5 text-[13.5px] text-ink-2 transition hover:border-accent hover:bg-surface-2 hover:text-accent disabled:opacity-40"
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
  streamHint,
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
  streamHint?: string;
  feedback: string;
  adopted: boolean;
  onTrace: (r: KnowledgeReference[]) => void;
  onFeedback: (fb: string) => void;
  onAdopt: () => void;
}) {
  if (msg.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[80%] rounded-xl rounded-tr-sm bg-ink-2 px-3.5 py-2.5 text-[14.5px] text-white">
          {msg.content}
        </div>
      </div>
    );
  }
  return (
    <div className="flex gap-2.5">
      <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-brand/10 text-[16px]">🤖</div>
      <div className="min-w-0 flex-1">
        {assessment && <div className="mb-2"><HealthCard assessment={assessment} trend={trend ?? null} compact showTrendButton={false} onAlertAI={undefined} /></div>}
        {msg.content ? (
          <div className="md-text rounded-xl rounded-tl-sm border border-border bg-surface px-3.5 py-2.5">
            <div className="md">
              {renderMarkdown(msg.content)}
              {streaming && <span className="cursor-blink ml-0.5" />}
            </div>
          </div>
        ) : streaming ? (
          <div className="md-text rounded-xl rounded-tl-sm border border-border bg-surface px-3.5 py-2.5 text-[14.5px] text-muted">
            <span className="cursor-blink mr-1" />
            {streamHint ?? "思考中…"}
          </div>
        ) : null}
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
            className={`rounded-md px-2 py-1 text-[13px] transition ${adopted ? "bg-accent-soft text-accent" : "text-ink-2 hover:bg-surface-2"}`}
            onClick={onAdopt}
          >
            {adopted ? "✓ 已采纳（proposed）" : "⭐ 采纳策略"}
          </button>
          <button
            className="rounded-md px-2 py-1 text-[13px] text-ink-2 transition hover:bg-surface-2"
            onClick={() => navigator.clipboard?.writeText(msg.content)}
          >
            📋 复制
          </button>
          {msg.degraded && <span className="ml-auto text-[12px] text-warning">规则引擎兜底</span>}
        </div>
      </div>
    </div>
  );
}

function ToolBtn({ active, onClick, label }: { active: boolean; onClick: () => void; label: string }) {
  return (
    <button
      className={`rounded-md px-2 py-1 text-[14px] transition ${active ? "bg-accent-soft text-accent" : "text-ink-2 hover:bg-surface-2"}`}
      onClick={onClick}
    >
      {label}
    </button>
  );
}

function TraceDrawer({ references, onClose }: { references: KnowledgeReference[]; onClose: () => void }) {
  const first = references[0];
  return (
    <>
      <div className="overlay-mask" onClick={onClose} />
      <div className="drawer-panel">
        <div className="flex items-center border-b border-border px-4 py-3">
          <h3 className="flex-1 text-[16px] font-semibold text-ink">📎 知识溯源</h3>
          <button className="ml-2 flex h-7 w-7 items-center justify-center rounded-lg text-muted hover:bg-surface-2" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          <div className="rounded-xl border border-border bg-surface-2 p-3">
            <div className="text-[15px] font-semibold text-ink">{first?.title || "—"}</div>
            <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[13px] text-muted">
              {first?.category && <span>分类：{first.category}</span>}
              {references.length > 1 && <span>{references.length} 个命中切片</span>}
            </div>
          </div>
          <div className="mb-1.5 mt-3 text-[12.5px] text-muted">命中切片原文</div>
          <div className="space-y-2">
            {references.map((r, i) => (
              <div key={i} className="rounded-xl border border-border-soft bg-surface p-3 text-[14px] leading-relaxed text-ink-2">
                <div className="mb-1 flex flex-wrap items-center gap-2 text-[11px] text-muted">
                  <span>P.{r.chunk_id ?? "—"} · 相似度 {r.score ? r.score.toFixed(2) : "—"}</span>
                  {r.used ? (
                    <span className="rounded-full bg-accent-soft px-1.5 py-0.5 text-[10.5px] text-accent">已引用</span>
                  ) : (
                    <span className="rounded-full bg-surface-2 px-1.5 py-0.5 text-[10.5px] text-muted">仅检索</span>
                  )}
                </div>
                <div className="whitespace-pre-wrap">{r.snippet || "（无切片预览，可在知识库中查看原文）"}</div>
              </div>
            ))}
          </div>
          <div className="mt-3 rounded-lg border border-border-soft bg-surface-2 px-3 py-2 text-[13px] text-muted">
            💡 该片段由检索链路召回：metadata 过滤 → dense 向量召回 → Rerank 重排。
          </div>
        </div>
        <div className="flex items-center gap-2 border-t border-border px-4 py-3">
          <span className="mr-auto" />
          <button className="rounded-lg border border-border px-3 py-2 text-[14px] text-ink-2" onClick={onClose}>
            关闭
          </button>
        </div>
      </div>
    </>
  );
}

function CustomerPicker({ onClose, onPick }: { onClose: () => void; onPick: (c: { id: number; customer_name: string }) => void }) {
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
          <h3 className="flex-1 text-[16px] font-semibold text-ink">选择客户</h3>
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
            className="mb-3 w-full rounded-lg border border-border-strong px-3 py-2 text-[14px] outline-none focus:border-accent"
          />
          <div className="max-h-[50vh] space-y-1.5 overflow-y-auto">
            {filtered.map((c) => (
              <button
                key={c.id}
                className="flex w-full items-center justify-between rounded-lg border border-border bg-surface px-3 py-2.5 text-left text-[14.5px] transition hover:border-accent"
                onClick={() => onPick(c)}
              >
                <span className="font-medium text-ink">{c.customer_name}</span>
                <span className="text-[13px] text-muted">{c.industry || "—"}</span>
              </button>
            ))}
            {filtered.length === 0 && <div className="py-6 text-center text-[14px] text-muted">无匹配客户</div>}
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
            <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-xl bg-accent-soft text-[23px]">🤖</div>
            <h1 className="text-[25px] font-semibold tracking-tight text-ink">你好，我是客情分析助手</h1>
          </div>
          <p className="mt-2 text-[14.5px] leading-relaxed text-muted">
            结合<strong className="font-medium text-ink">客户库</strong>（客情因子数据）与<strong className="font-medium text-ink">知识库</strong>（内部资料检索），帮你分析客户健康度、排查风险、制定可执行策略。
          </p>
        </div>

        <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
          {entries.map((e) => (
            <button
              key={e.title}
              className="flex flex-col items-center gap-2.5 rounded-xl border border-border bg-surface p-5 text-center transition hover:border-accent/60 hover:bg-surface-3"
              onClick={() => (e.nav ? onKnowledge() : onPick(e.sc!))}
            >
              <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-accent-soft/70 text-[19px]">
                {e.icon}
              </div>
              <div>
                <div className="text-[15px] font-medium text-ink">{e.title}</div>
                <div className="mt-1 text-[13.5px] leading-relaxed text-muted">{e.desc}</div>
              </div>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
