// @vitest-environment jsdom
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { UiFeedbackProvider } from "../components/UiFeedback";
import type { ChatEvent } from "../types";
import Chat from "./Chat";

const { createSession, deleteSession, streamChat } = vi.hoisted(() => ({
  createSession: vi.fn(),
  deleteSession: vi.fn(),
  streamChat: vi.fn(),
}));

vi.mock("../api", () => ({
  streamChat,
  customers: {
    assessment: vi.fn().mockResolvedValue(null),
    trend: vi.fn().mockResolvedValue(null),
  },
  chat: {
    createSession,
    getSession: vi.fn().mockResolvedValue({
      id: 7,
      title: "测试会话",
      customer_id: null,
      scenario: "free_qa",
      streaming: false,
      messages: [],
    }),
    deleteSession,
    feedback: vi.fn(),
  },
}));

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
    streamChat.mockImplementation(async (_url, _body, onEvent) => {
      emit = onEvent;
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
    expect(messageList.scrollTop).toBe(100);
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
