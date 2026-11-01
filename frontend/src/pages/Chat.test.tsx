// @vitest-environment jsdom
import { MemoryRouter, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { UiFeedbackProvider } from "../components/UiFeedback";
import type { ChatEvent, ChatMessageItem } from "../types";
import Chat from "./Chat";
import { customers } from "../api";
import type { AssessmentResponse } from "../types";

const { createSession, deleteSession, streamChat, getSession } = vi.hoisted(() => ({
  createSession: vi.fn(),
  deleteSession: vi.fn(),
  streamChat: vi.fn(),
  getSession: vi.fn(),
}));

vi.mock("../api", () => ({
  streamChat,
  customers: {
    assessment: vi.fn().mockResolvedValue(null),
    trend: vi.fn().mockResolvedValue(null),
  },
  chat: {
    createSession,
    getSession,
    deleteSession,
    feedback: vi.fn(),
  },
}));

const session = (messages: ChatMessageItem[] = [], id = 7) => ({
  id, title: "测试会话", customer_id: null, scenario: "free_qa", streaming: false, messages,
});
const answer = (id = 1, content = "旧的完整答案"): ChatMessageItem => ({
  id, session_id: 7, role: "assistant", content, strategy_items: [], references: [],
  tokens_used: 0, feedback: "", degraded: false, created_at: "",
});

beforeEach(() => {
  getSession.mockReset().mockResolvedValue(session());
  vi.mocked(customers.assessment).mockReset().mockResolvedValue(null as unknown as AssessmentResponse);
});

function renderChat() {
  return render(
    <UiFeedbackProvider>
      <MemoryRouter initialEntries={["/chat/7"]}>
        <Routes>
          <Route path="/chat/:sessionId" element={<Chat />} />
          <Route path="/chat" element={<div>会话首页</div>} />
        </Routes>
      </MemoryRouter>
    </UiFeedbackProvider>,
  );
}

describe("Chat 核心操作", () => {
  it("对话只保留综合评估和报告快捷入口", async () => {
    renderChat();
    await screen.findByText("测试会话");
    expect(screen.getByRole("button", { name: "📊 综合评估" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "📄 生成报告" }).length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "🎯 生成策略" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "🚨 风险排查" })).not.toBeInTheDocument();
  });

  it("欢迎页只提供综合评估和知识查询", () => {
    render(<UiFeedbackProvider><MemoryRouter initialEntries={["/chat"]}>
      <Routes><Route path="/chat" element={<Chat />} /></Routes>
    </MemoryRouter></UiFeedbackProvider>);
    expect(screen.getByRole("button", { name: /评估某个客户/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /查询内部知识/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /排查风险客户|制定客户策略/ })).not.toBeInTheDocument();
  });

  it("重新生成由后端恢复最近请求，不以会话初始场景覆盖", async () => {
    getSession.mockResolvedValue({ ...session([answer()]), scenario: "free_qa" });
    renderChat();
    await screen.findByText("旧的完整答案");
    fireEvent.click(screen.getByRole("button", { name: "🔄 重新生成" }));
    await waitFor(() => expect(streamChat).toHaveBeenCalledWith("/api/chat/sessions/7/regenerate",
      expect.not.objectContaining({ scenario: expect.anything() }), expect.any(Function)));
  });
  it("客户入口创建的空会话自动执行综合评估", async () => {
    getSession.mockResolvedValue({ ...session(), customer_id: 26, scenario: "assessment", message_count: 0 });
    streamChat.mockReset().mockResolvedValue(undefined);
    render(<UiFeedbackProvider><MemoryRouter initialEntries={[{ pathname: "/chat/7", state: { autoAssess: true } }]}>
      <Routes><Route path="/chat/:sessionId" element={<Chat />} /></Routes>
    </MemoryRouter></UiFeedbackProvider>);
    await waitFor(() => expect(streamChat).toHaveBeenCalledWith("/api/chat/sessions/7/evaluate",
      expect.objectContaining({ customer_id: 26 }), expect.any(Function)));
    expect(streamChat).toHaveBeenCalledOnce();
  });
  it("计划等待期间先显示独立评分，后续采用流中的冻结评分", async () => {
    const score: AssessmentResponse = { customer_id: 26, customer_name: "先展示评分客户", total_score: 48.4,
      max_score: 100, level: "风险", level_color: "#f97316", dimensions: [], risk_alerts: [], alerts: [],
      suggestions: [], config_version: "test", assessed_at: "2026-11-01T00:00:00" };
    let resolveScore!: (value: AssessmentResponse) => void;
    vi.mocked(customers.assessment).mockReturnValue(new Promise((resolve) => { resolveScore = resolve; }));
    getSession.mockResolvedValue({ ...session(), customer_id: 26, scenario: "assessment" });
    let emit!: (event: ChatEvent) => void;
    let finish!: () => void;
    streamChat.mockImplementation((_url, _body, onEvent) => {
      emit = onEvent;
      onEvent({ type: "start", data: { scenario: "assessment" } });
      onEvent({ type: "stage", data: { message: "正在选择并验证计划。" } });
      return new Promise<void>((resolve) => { finish = resolve; });
    });
    renderChat();
    await screen.findByText("测试会话");
    fireEvent.change(screen.getByPlaceholderText("输入消息，或描述你关心的客户与问题…"), { target: { value: "评估客户" } });
    fireEvent.click(screen.getByRole("button", { name: "➤" }));
    await screen.findByText(/正在选择并验证计划/);
    await act(async () => resolveScore(score));
    expect(await screen.findByText("先展示评分客户")).toBeInTheDocument();
    expect(screen.getByText("48.4")).toBeInTheDocument();
    expect(screen.getByText(/正在选择并验证计划/)).toBeInTheDocument();
    act(() => emit({ type: "context", data: { assessment: { ...score, total_score: 50.2 } } }));
    expect(screen.getByText("50.2")).toBeInTheDocument();
    expect(screen.queryByText("48.4")).not.toBeInTheDocument();
    await act(async () => finish());
  });
  it("问答中的行动统一展示计划表，知识来源仍展示", async () => {
    const msg = answer(1, '结合客户情况推进');
    msg.strategy_items = ["recommended", "alternative", "long_term"].map((priority) => ({
      priority, title: priority + "行动", urgency: "medium", reason: "依据", action: "步骤", expected_outcome: "产出", reference: "", steps: ["步骤"],
    }));
    msg.references = [{ document_id: 1, title: "参考资料", category: "", snippet: "资料原文", score: 1, used: false }];
    getSession.mockResolvedValue(session([msg]));
    renderChat();
    expect(await screen.findByText("recommended行动")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "行动" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "交付物" })).toBeInTheDocument();
    expect(screen.getByText("alternative行动")).toBeInTheDocument();
    expect(screen.getByText("long_term行动")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /本次回答共检索 1 个文档/ })).toBeInTheDocument();
  });
  it("已保存综合评估从总体评估分点开始，显示下一步计划且复制一致", async () => {
    const raw = '本次按以下任务回答：综合评估\n\n### 总览\n客户事实仅在资料库看\n## AI 分析（业务判断，可能有误）\n### 总体评估\n:::ai-text "先明确价值口径。再安排评审。<>& *原文*"\n### 主要风险点\n:::ai-text "采购路径待确认"\n### 优势\n:::ai-text "已有定性价值描述"\n### 待验证假设\n:::ai-text "业务方可能支持试点"\n:::ai-text "验证方式：邀请业务方评审"';
    const msg = answer(1, raw);
    msg.strategy_items = [{ priority: "recommended", title: "形成评审测算表", urgency: "medium",
      reason: "先形成讨论材料", action: "1. 选择口径\n2. 收集基线\n依赖：业务认可\n交付物：测算表",
      expected_outcome: "提交客户评审", reference: "", steps: ["选择口径", "收集基线"] }];
    msg.references = [{ document_id: 11, chunk_id: 1, title: "业务资料", category: "方法", snippet: "原文", score: 1, used: false }];
    getSession.mockResolvedValue(session([msg]));
    const copy = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: copy } });
    const { container } = renderChat();
    expect(await screen.findByText("总体评估")).toBeInTheDocument();
    expect(screen.queryByText("客户事实仅在资料库看")).not.toBeInTheDocument();
    expect(screen.queryByText("AI 分析（业务判断，可能有误）")).not.toBeInTheDocument();
    expect(screen.getByText("先明确价值口径。").closest("li")).not.toBeNull();
    expect(screen.getByText("再安排评审。").closest("li")).not.toBeNull();
    expect(screen.getByText("采购路径待确认").closest("li")).not.toBeNull();
    expect(screen.getByText("已有定性价值描述").closest("li")).not.toBeNull();
    expect(screen.queryByText("待验证假设")).not.toBeInTheDocument();
    expect(screen.queryByText("业务方可能支持试点")).not.toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "行动" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "责任人" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "时限" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "交付物" })).toBeInTheDocument();
    expect(screen.getByText(/依赖：业务认可/)).not.toBeVisible();
    fireEvent.click(screen.getByText("查看完整步骤"));
    expect(screen.getByText(/依赖：业务认可/)).toBeVisible();
    expect(screen.queryByText("✅ 推荐策略")).not.toBeInTheDocument();
    expect(screen.getByText("下一步计划")).toBeInTheDocument();
    expect(screen.getAllByText("形成评审测算表")).toHaveLength(1);
    expect(container.querySelector("img,script")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /本次回答共检索 1 个文档/ }));
    expect(screen.getByText("仅检索")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "📋 复制完整结果" }));
    await waitFor(() => expect(copy).toHaveBeenCalledOnce());
    const copied = copy.mock.calls[0][0];
    expect(copied).toContain("- 先明确价值口径。");
    expect(copied).toContain("## 下一步计划");
    expect(copied).toContain("行动：形成评审测算表");
    expect(copied).toContain("责任人：待明确");
    expect(copied).toContain("交付物：测算表");
    expect(copied).toContain("依赖：业务认可");
    expect(copied).not.toContain("待验证假设");
    expect(copied).not.toContain("客户事实仅在资料库看");
    expect(copied).not.toContain("AI 分析（业务判断，可能有误）");
  });

  it("自由问答直接显示请求的事实，不追加报告章节", async () => {
    getSession.mockResolvedValue(session([{ ...answer(1, ':::ai-text "必须保留的评分事实"'), scenario: 'free_qa' }]));
    renderChat();
    expect(await screen.findByText("必须保留的评分事实")).toBeInTheDocument();
    for (const heading of ['总览', '完整预警清单', '重点解读', '总体评估', 'AI 分析（业务判断，可能有误）']) {
      expect(screen.queryByText(heading)).not.toBeInTheDocument();
    }
  });

  it("同会话按每条消息场景显示评分卡，自由问答仅保留理由和指定行动", async () => {
    const score: AssessmentResponse = { customer_id: 26, customer_name: '评分卡客户', total_score: 49.6,
      max_score: 100, level: '风险', level_color: '#f97316', dimensions: [], risk_alerts: [], alerts: [],
      suggestions: [], config_version: 'test', assessed_at: '2026-11-01T00:00:00' };
    vi.mocked(customers.assessment).mockResolvedValue(score);
    const assessment = { ...answer(1, '综合评估正文'), scenario: 'assessment' };
    const qa = { ...answer(2, ':::ai-text "先修复关键人关系，这是当前推进的主要依赖。"'), scenario: 'free_qa',
      strategy_items: [{ priority: 'recommended', title: '关键人工作坊', urgency: 'medium', reason: '关系是推进依赖',
        action: '邀请关键人', expected_outcome: '确认下一次沟通', reference: '', steps: ['邀请关键人'],
        suggested_owner: '客户经理', suggested_timing: '两周内', deliverable: '关键人地图' }] };
    getSession.mockResolvedValue({ ...session([assessment, qa]), customer_id: 26, scenario: 'free_qa' });
    const { container } = renderChat();
    expect(await screen.findByText('评分卡客户')).toBeInTheDocument();
    expect(screen.getAllByText('49.6')).toHaveLength(1);
    expect(screen.getAllByText('关键人工作坊')).toHaveLength(1);
    expect(screen.getByText('先修复关键人关系，这是当前推进的主要依赖。')).toBeInTheDocument();
    expect(container.querySelector('[data-message-anchor="2"]')!.textContent).not.toContain('客情评分');
  });

  it("没有报告标题的问答正文也回放，中间帧不显示传输标记", async () => {
    let emit!: (event: ChatEvent) => void | Promise<void>;
    let finish!: () => void;
    const request = new Promise<void>((resolve) => { finish = resolve; });
    streamChat.mockImplementation((_url, _body, onEvent) => { emit = onEvent; return request; });
    const { container } = renderChat();
    await screen.findByText('测试会话');
    fireEvent.change(screen.getByPlaceholderText('输入消息，或描述你关心的客户与问题…'), { target: { value: '给我一个行动' } });
    fireEvent.click(screen.getByRole('button', { name: '➤' }));
    await waitFor(() => expect(streamChat).toHaveBeenCalled());
    const body = '优先推进关键人工作坊，明确支持与决策路径。'.repeat(6);
    const text = ':::ai-text ' + JSON.stringify(body);
    vi.useFakeTimers();
    try {
      let playback: void | Promise<void>;
      act(() => {
        emit({ type: 'start', data: { scenario: 'free_qa' } });
        playback = emit({ type: 'delta', data: { text } });
      });
      const first = container.querySelector('.md')!.textContent!;
      expect(first.length).toBeGreaterThan(0);
      expect(first.length).toBeLessThan(body.length);
      expect(first).not.toContain(':::ai-text');
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); await playback; });
      expect(container.querySelector('.md')!.textContent).toBe(body);
      act(() => emit({ type: 'done', data: { message: { ...answer(2, text), scenario: 'free_qa' } } }));
      await act(async () => { finish(); await request; });
    } finally { vi.useRealTimers(); }
  });
  beforeEach(() => {
    deleteSession.mockReset().mockResolvedValue(undefined);
    streamChat.mockReset().mockResolvedValue(undefined);
  });

  it("执行进程丢失后停止恢复状态，展示中断提示并保留旧答案", async () => {
    getSession.mockResolvedValue({ ...session([answer()]), run_notice: "上次生成已中断，已保留原答案。" });
    renderChat();
    expect(await screen.findByText(/上次生成已中断，已保留原答案/)).toBeInTheDocument();
    expect(screen.getByText("旧的完整答案")).toBeInTheDocument();
    expect(streamChat).not.toHaveBeenCalled();
  });

  it("删除会话先经过应用内确认框，取消时不调用接口", async () => {
    renderChat();
    await screen.findByText("测试会话");
    fireEvent.click(screen.getByRole("button", { name: "🗑" }));
    expect(screen.getByRole("dialog", { name: "删除会话" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(deleteSession).not.toHaveBeenCalled();
  });

  it("确认删除后调用接口并回到会话首页", async () => {
    renderChat();
    await screen.findByText("测试会话");
    fireEvent.click(screen.getByRole("button", { name: "🗑" }));
    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
    await waitFor(() => expect(deleteSession).toHaveBeenCalledWith(7));
    expect(await screen.findByText("会话首页")).toBeInTheDocument();
  });

  it("发送消息和流式内容更新时自动滚动到最新位置", async () => {
    streamChat.mockImplementation(async (_url, _body, onEvent) => {
      onEvent({ type: "delta", data: { text: "流式回复" } });
    });
    renderChat();
    await screen.findByText("测试会话");

    const messageList = screen.getByTestId("chat-message-list");
    Object.defineProperty(messageList, "scrollHeight", { configurable: true, value: 800 });
    Object.defineProperty(messageList, "clientHeight", { configurable: true, value: 200 });

    fireEvent.change(screen.getByPlaceholderText("输入消息，或描述你关心的客户与问题…"), {
      target: { value: "你好" },
    });
    fireEvent.click(screen.getByRole("button", { name: "➤" }));

    await screen.findByText("流式回复");
    await waitFor(() => expect(messageList.scrollTop).toBe(800));
  });

  it("用户向上查看历史时不强制滚回底部", async () => {
    let emit: ((event: ChatEvent) => void) | undefined;
    let finish!: () => void;
    const activeStream = new Promise<void>((resolve) => { finish = resolve; });
    streamChat.mockImplementation((_url, _body, onEvent) => {
      emit = onEvent;
      return activeStream;
    });
    renderChat();
    await screen.findByText("测试会话");

    const messageList = screen.getByTestId("chat-message-list");
    Object.defineProperty(messageList, "scrollHeight", { configurable: true, value: 1000 });
    Object.defineProperty(messageList, "clientHeight", { configurable: true, value: 200 });

    fireEvent.change(screen.getByPlaceholderText("输入消息，或描述你关心的客户与问题…"), {
      target: { value: "你好" },
    });
    fireEvent.click(screen.getByRole("button", { name: "➤" }));
    await waitFor(() => expect(streamChat).toHaveBeenCalled());
    await waitFor(() => expect(messageList.scrollTop).toBe(1000));

    messageList.scrollTop = 100;
    fireEvent.scroll(messageList);
    act(() => emit?.({ type: "delta", data: { text: "继续生成" } }));

    await screen.findByText("继续生成");
    // React 提交会安排滚动 rAF，文字出现并不表示该帧已经执行。
    await act(async () => {
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    });
    expect(messageList.scrollTop).toBe(100);
    // 回到底部后应重新跟随；同一活动流继续发出内容。
    messageList.scrollTop = 800;
    fireEvent.scroll(messageList);
    act(() => emit?.({ type: "delta", data: { text: "，生成完成" } }));
    await act(async () => {
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    });
    expect(messageList.scrollTop).toBe(1000);
    await act(async () => { finish(); await activeStream; });
  });
});

describe("最终答案阅读位置", () => {
  afterEach(() => vi.restoreAllMocks());

  async function startTurn(scenario = "assessment") {
    let emit!: (event: ChatEvent) => void;
    let finish!: () => void;
    const request = new Promise<void>((resolve) => { finish = resolve; });
    streamChat.mockReset().mockImplementation((_url, _body, onEvent) => { emit = onEvent; return request; });
    renderChat();
    await screen.findByText("测试会话");
    const list = screen.getByTestId("chat-message-list");
    Object.defineProperty(list, "scrollHeight", { configurable: true, value: 1200 });
    Object.defineProperty(list, "clientHeight", { configurable: true, value: 400 });
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
      const top = this.hasAttribute("data-message-anchor") ? 100 + 220 - list.scrollTop : 100;
      return { top, bottom: top + 1000, left: 0, right: 800, width: 800, height: 1000, x: 0, y: top, toJSON() {} };
    });
    fireEvent.change(screen.getByPlaceholderText("输入消息，或描述你关心的客户与问题…"), { target: { value: "分析一下" } });
    fireEvent.click(screen.getByRole("button", { name: "➤" }));
    await waitFor(() => expect(streamChat).toHaveBeenCalled());
    act(() => emit({ type: "start", data: { scenario } }));
    await waitFor(() => expect(list.scrollTop).toBe(1200));
    return { list, emit, finish, request };
  }

  it.each(['assessment', 'free_qa'])("%s没有final_sync事件也在完成后定位答案开头", async (scenario) => {
    const { list, emit, finish, request } = await startTurn(scenario);
    act(() => {
      emit({ type: "delta", data: { text: "已校验答案" } });
      emit({ type: "done", data: { message: answer(2, "已校验答案") } });
    });
    await act(async () => { finish(); await request; await new Promise(requestAnimationFrame); });
    expect(list.scrollTop).toBe(220);
  });

  it.each([false, true])("最终替换后定位答案开头，完成事件不再拉到底部（同批完成=%s）", async (batched) => {
    const { list, emit, finish, request } = await startTurn();
    act(() => {
      emit({ type: "replace", data: { text: "最终完整分析", reason: "final_sync" } });
      if (batched) emit({ type: "done", data: { message: answer(2, "最终完整分析") } });
    });
    await waitFor(() => expect(list.scrollTop).toBe(220));
    // Even a programmatic near-bottom scroll must not re-enable tail following.
    Object.defineProperty(list, "scrollHeight", { configurable: true, value: 620 });
    fireEvent.scroll(list);
    if (!batched) act(() => emit({ type: "done", data: { message: answer(2, "最终完整分析") } }));
    await act(async () => { finish(); await request; await new Promise(requestAnimationFrame); });
    expect(list.scrollTop).toBe(220);
    Object.defineProperty(list, "scrollHeight", { configurable: true, value: 1200 });
    streamChat.mockResolvedValue(undefined);
    fireEvent.change(screen.getByPlaceholderText("输入消息，或描述你关心的客户与问题…"), { target: { value: "继续提问" } });
    fireEvent.click(screen.getByRole("button", { name: "➤" }));
    await waitFor(() => expect(list.scrollTop).toBe(1200));
  });

  it("用户上滑阅读历史后最终替换和完成均不抢位置", async () => {
    const { list, emit, finish, request } = await startTurn();
    list.scrollTop = 80;
    fireEvent.scroll(list);
    act(() => {
      emit({ type: "replace", data: { text: "最终完整分析", reason: "final_sync" } });
      emit({ type: "done", data: { message: answer(2, "最终完整分析") } });
    });
    await act(async () => { finish(); await request; await new Promise(requestAnimationFrame); });
    expect(list.scrollTop).toBe(80);
  });

  it("自由问答的最终同步定位答案开头", async () => {
    const { list, emit, finish, request } = await startTurn("free_qa");
    act(() => emit({ type: "replace", data: { text: "普通问答", reason: "final_sync" } }));
    await act(async () => { finish(); await request; await new Promise(requestAnimationFrame); });
    expect(list.scrollTop).toBe(220);
  });

  it("内容增高产生的scroll事件不会被当作用户上滑而取消最终定位", async () => {
    const { list, emit, finish, request } = await startTurn();
    Object.defineProperty(list, "scrollHeight", { configurable: true, value: 1669 });
    fireEvent.scroll(list);
    act(() => {
      emit({ type: "replace", data: { text: "最终完整分析", reason: "final_sync" } });
      emit({ type: "done", data: { message: answer(2, "最终完整分析") } });
    });
    await act(async () => { finish(); await request; });
    await waitFor(() => expect(list.scrollTop).toBe(220));
  });

  it("草稿转正式会话重挂载后保留答案开头", async () => {
    const final = answer(99, "新会话的最终分析");
    createSession.mockResolvedValue({ id: 42, title: "测试会话" });
    getSession.mockResolvedValue(session([final], 42));
    streamChat.mockReset().mockImplementation(async (_url, _body, emit) => {
      emit({ type: "start", data: { scenario: "assessment" } });
      emit({ type: "replace", data: { text: final.content, reason: "final_sync" } });
      emit({ type: "done", data: { message: final } });
    });
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
      const list = screen.getByTestId("chat-message-list");
      const top = this.hasAttribute("data-message-anchor") ? 220 - list.scrollTop : 0;
      return { top, bottom: top + 1000, left: 0, right: 800, width: 800, height: 1000, x: 0, y: top, toJSON() {} };
    });
    function KeyedChat() {
      const { sessionId } = useParams();
      return <Chat key={sessionId} />;
    }
    render(<UiFeedbackProvider><MemoryRouter initialEntries={["/chat/new"]}>
      <Routes><Route path="/chat/:sessionId" element={<KeyedChat />} /></Routes>
    </MemoryRouter></UiFeedbackProvider>);
    fireEvent.change(screen.getByPlaceholderText("输入消息，或描述你关心的客户与问题…"), { target: { value: "分析一下" } });
    fireEvent.click(screen.getByRole("button", { name: "➤" }));
    await screen.findByText(final.content);
    await waitFor(() => expect(getSession).toHaveBeenCalledWith(42));
    await waitFor(() => expect(screen.getByTestId("chat-message-list").scrollTop).toBe(220));
  });

  it.each(["wheel", "touch", "keyboard", "scrollbar"])("草稿最终替换后%s手动阅读，done重挂载保留新位置", async (input) => {
    const final = answer(99, "新会话的最终分析");
    createSession.mockResolvedValue({ id: 42, title: "测试会话" });
    getSession.mockResolvedValue(session([final], 42));
    let emit!: (event: ChatEvent) => void;
    let finish!: () => void;
    const request = new Promise<void>((resolve) => { finish = resolve; });
    streamChat.mockReset().mockImplementation((_url, _body, onEvent) => { emit = onEvent; return request; });
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
      const list = screen.getByTestId("chat-message-list");
      const top = this.hasAttribute("data-message-anchor") ? 220 - list.scrollTop : 0;
      return { top, bottom: top + 1000, left: 0, right: 800, width: 800, height: 1000, x: 0, y: top, toJSON() {} };
    });
    function KeyedChat() {
      const { sessionId } = useParams();
      return <Chat key={sessionId} />;
    }
    render(<UiFeedbackProvider><MemoryRouter initialEntries={["/chat/new"]}>
      <Routes><Route path="/chat/:sessionId" element={<KeyedChat />} /></Routes>
    </MemoryRouter></UiFeedbackProvider>);
    fireEvent.change(screen.getByPlaceholderText("输入消息，或描述你关心的客户与问题…"), { target: { value: "分析一下" } });
    fireEvent.click(screen.getByRole("button", { name: "➤" }));
    await waitFor(() => expect(streamChat).toHaveBeenCalled());
    act(() => {
      emit({ type: "start", data: { scenario: "assessment" } });
      emit({ type: "replace", data: { text: final.content, reason: "final_sync" } });
    });
    const list = screen.getByTestId("chat-message-list");
    await waitFor(() => expect(list.scrollTop).toBe(220));
    if (input === "wheel") fireEvent.wheel(list, { deltaY: -140 });
    if (input === "touch") fireEvent.touchMove(list);
    if (input === "keyboard") fireEvent.keyDown(list, { key: "PageUp" });
    list.scrollTop = 80;
    fireEvent.scroll(list);
    act(() => emit({ type: "done", data: { message: final } }));
    await act(async () => { finish(); await request; });
    await waitFor(() => expect(getSession).toHaveBeenCalledWith(42));
    await waitFor(() => expect(screen.getByTestId("chat-message-list")).not.toBe(list));
    await act(async () => { await new Promise(requestAnimationFrame); });
    expect(screen.getByTestId("chat-message-list").scrollTop).toBe(80);
  });
});

describe("重新生成与完整复制", () => {
  beforeEach(() => {
    streamChat.mockReset();
    getSession.mockResolvedValue(session([answer()]));
  });

  it.each(["error", "throw", "eof"])("重新生成 %s 后旧答案仍可查看复制", async (failure) => {
    let emit!: (event: ChatEvent) => void;
    let finish!: () => void;
    const request = new Promise<void>((resolve) => { finish = resolve; });
    streamChat.mockImplementation(async (_url, _body, onEvent) => {
      emit = onEvent;
      await request;
      if (failure === "throw") throw new Error("网络中断");
    });
    renderChat();
    await screen.findByText("旧的完整答案");
    fireEvent.click(screen.getByRole("button", { name: "🔄 重新生成" }));
    act(() => emit({ type: "delta", data: { text: "未完成的新草稿" } }));
    expect(screen.getByText("旧的完整答案")).toBeInTheDocument();
    expect(screen.getByText(/草稿预览 · /)).toBeInTheDocument();
    if (failure === "error") act(() => emit({ type: "error", data: { message: "重新生成未完成，已保留原答案", retained_message_id: 1 } }));
    await act(async () => { finish(); await request; });
    await waitFor(() => expect(screen.queryByText("未完成的新草稿")).not.toBeInTheDocument());
    expect(screen.getAllByText("旧的完整答案")).toHaveLength(1);
    expect(screen.getByText(/重新生成未完成，已保留原答案/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "📋 复制完整结果" })).toBeEnabled();
  });

  it("成功收到完整 done 后替换旧答案且不重复追加", async () => {
    let emit!: (event: ChatEvent) => void;
    let finish!: () => void;
    const request = new Promise<void>((resolve) => { finish = resolve; });
    streamChat.mockImplementation((_url, _body, onEvent) => { emit = onEvent; return request; });
    renderChat();
    await screen.findByText("旧的完整答案");
    fireEvent.click(screen.getByRole("button", { name: "🔄 重新生成" }));
    act(() => emit({ type: "done", data: { message: answer(2, "新的完整答案") } }));
    act(() => emit({ type: "done", data: { message: answer(2, "新的完整答案") } }));
    expect(screen.queryByText("旧的完整答案")).not.toBeInTheDocument();
    expect(screen.getAllByText("新的完整答案")).toHaveLength(1);
    await act(async () => { finish(); await request; });
  });

  it("完整 done 后连接报错不会误报原答案已保留", async () => {
    streamChat.mockImplementation(async (_url, _body, onEvent) => {
      onEvent({ type: "done", data: { message: answer(2, "新的完整答案") } });
      throw new Error("完成后的连接关闭");
    });
    renderChat();
    await screen.findByText("旧的完整答案");
    fireEvent.click(screen.getByRole("button", { name: "🔄 重新生成" }));
    await screen.findByText("新的完整答案");
    await waitFor(() => expect(screen.getByRole("button", { name: "🔄 重新生成" })).toBeEnabled());
    expect(screen.queryByText(/已保留原答案/)).not.toBeInTheDocument();
  });

  it("切换会话后旧流的 done 不会替换新会话内容", async () => {
    let emit!: (event: ChatEvent) => void;
    let finish!: () => void;
    const request = new Promise<void>((resolve) => { finish = resolve; });
    streamChat.mockImplementation((_url, _body, onEvent) => { emit = onEvent; return request; });
    getSession.mockImplementation(async (id) => session([answer(id, id === 7 ? "旧的完整答案" : "另一会话答案")], id));
    function TestRoute() {
      const { sessionId } = useParams();
      const navigate = useNavigate();
      return <><button onClick={() => navigate("/chat/8")}>切换会话</button><Chat key={sessionId} /></>;
    }
    render(<UiFeedbackProvider><MemoryRouter initialEntries={["/chat/7"]}><Routes>
      <Route path="/chat/:sessionId" element={<TestRoute />} />
    </Routes></MemoryRouter></UiFeedbackProvider>);
    await screen.findByText("旧的完整答案");
    fireEvent.click(screen.getByRole("button", { name: "🔄 重新生成" }));
    fireEvent.click(screen.getByRole("button", { name: "切换会话" }));
    await screen.findByText("另一会话答案");
    await act(async () => {
      emit({ type: "done", data: { message: answer(2, "过时的新答案") } });
      finish(); await request;
    });
    expect(screen.queryByText("过时的新答案")).not.toBeInTheDocument();
    expect(screen.getByText("另一会话答案")).toBeInTheDocument();
  });

  it("复制包含独立策略、知识来源和核实提示，失败有可理解提示", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    const msg = answer(1, "客户概况\n\n> 部分结论尚待核实");
    msg.strategy_items = [{ priority: "recommended", title: "高层拜访", urgency: "high", reason: "高层联系不足", action: "本月安排拜访", expected_outcome: "建立联系", reference: "经营指南" }];
    msg.references = [{ title: "经营指南", category: "", score: 0.9, snippet: "", used: true }];
    getSession.mockResolvedValue(session([msg]));
    renderChat();
    await screen.findByText("高层拜访");
    fireEvent.click(screen.getByRole("button", { name: "📋 复制完整结果" }));
    await screen.findByText("已复制完整结果");
    expect(writeText).toHaveBeenCalledWith(expect.stringContaining("行动：高层拜访"));
    expect(writeText.mock.calls[0][0]).toContain("本月安排拜访");
    expect(writeText.mock.calls[0][0]).toContain("部分结论尚待核实");
    expect(writeText.mock.calls[0][0]).toContain("经营指南（已引用）");
    writeText.mockRejectedValueOnce(new Error("denied"));
    fireEvent.click(screen.getByRole("button", { name: "📋 复制完整结果" }));
    expect(await screen.findByText(/复制失败，请允许浏览器访问剪贴板/)).toBeInTheDocument();
  });

});

describe("Chat 草稿态（/chat/new）", () => {
  beforeEach(() => {
    createSession.mockReset().mockResolvedValue({
      id: 42,
      title: "帮我看看客情",
      customer_id: null,
      scenario: "free_qa",
      streaming: false,
      message_count: 0,
      last_message: "",
      created_at: "",
      updated_at: "",
    });
    streamChat.mockReset().mockResolvedValue(undefined);
  });

  function renderDraft() {
    return render(
      <UiFeedbackProvider>
        <MemoryRouter initialEntries={["/chat/new"]}>
          <Routes>
            <Route path="/chat/:sessionId" element={<Chat />} />
            <Route path="/chat" element={<div>会话首页</div>} />
          </Routes>
        </MemoryRouter>
      </UiFeedbackProvider>,
    );
  }

  it("进入草稿页直接展示输入框，不创建会话", () => {
    renderDraft();
    expect(screen.getByPlaceholderText("输入消息，或描述你关心的客户与问题…")).toBeInTheDocument();
    expect(screen.getByText("新对话")).toBeInTheDocument();
    expect(createSession).not.toHaveBeenCalled();
  });

  it("草稿页隐藏依赖已建库会话的操作按钮", () => {
    renderDraft();
    expect(screen.queryByRole("button", { name: "🗑" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "🔄 重新生成" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /生成报告/ })).not.toBeInTheDocument();
    // 快捷场景芯片保留（走选客户建会话流程）
    expect(screen.getByRole("button", { name: "📊 综合评估" })).toBeInTheDocument();
  });

  it("首发消息时才创建会话，并以首条消息命名", async () => {
    renderDraft();
    fireEvent.change(screen.getByPlaceholderText("输入消息，或描述你关心的客户与问题…"), {
      target: { value: "帮我看看客情" },
    });
    fireEvent.click(screen.getByRole("button", { name: "➤" }));

    await waitFor(() =>
      expect(createSession).toHaveBeenCalledWith({ title: "帮我看看客情", scenario: "free_qa" }),
    );
    await waitFor(() =>
      expect(streamChat).toHaveBeenCalledWith(
        "/api/chat/sessions/42/messages",
        expect.objectContaining({ content: "帮我看看客情", stream: true }),
        expect.any(Function),
      ),
    );
  });
});
