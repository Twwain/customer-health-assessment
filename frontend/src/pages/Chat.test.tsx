// @vitest-environment jsdom
import { MemoryRouter, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { UiFeedbackProvider } from "../components/UiFeedback";
import type { ChatEvent, ChatMessageItem } from "../types";
import Chat from "./Chat";

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

beforeEach(() => getSession.mockReset().mockResolvedValue(session()));

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
  beforeEach(() => {
    deleteSession.mockReset().mockResolvedValue(undefined);
    streamChat.mockReset().mockResolvedValue(undefined);
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
    expect(writeText).toHaveBeenCalledWith(expect.stringContaining("行动：本月安排拜访"));
    expect(writeText.mock.calls[0][0]).toContain("部分结论尚待核实");
    expect(writeText.mock.calls[0][0]).toContain("经营指南（已引用）");
    writeText.mockRejectedValueOnce(new Error("denied"));
    fireEvent.click(screen.getByRole("button", { name: "📋 复制完整结果" }));
    expect(await screen.findByText(/复制失败，请允许浏览器访问剪贴板/)).toBeInTheDocument();
  });

  it("历史正文已有完整策略时不重复显示卡片", async () => {
    const msg = answer(1, "结论需要关注。\n\n## 推荐策略\n\n### 高层拜访（紧急度：高）\n原因：高层联系不足\n行动：本月安排拜访\n预期：建立联系\n依据：经营指南");
    msg.strategy_items = [{ priority: "recommended", title: "高层拜访", urgency: "high", reason: "高层联系不足", action: "本月安排拜访", expected_outcome: "建立联系", reference: "经营指南" }];
    getSession.mockResolvedValue(session([msg]));
    renderChat();
    await screen.findByText("结论需要关注。");
    expect(screen.getByTestId("chat-message-list").textContent?.match(/高层拜访/g)).toHaveLength(1);
    expect(screen.queryByText("✅ 推荐策略")).not.toBeInTheDocument();
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
