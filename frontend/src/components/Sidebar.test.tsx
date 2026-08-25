// @vitest-environment jsdom
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ChatSessionItem } from "../types";
import Sidebar from "./Sidebar";

const { createSession, sessions } = vi.hoisted(() => ({
  createSession: vi.fn(),
  sessions: vi.fn(),
}));

vi.mock("../api", () => ({
  chat: { createSession, sessions },
  knowledge: { status: vi.fn().mockResolvedValue({ count: 0 }) },
}));

function makeSession(partial: Partial<ChatSessionItem>): ChatSessionItem {
  return {
    id: 0,
    title: "",
    customer_id: null,
    customer_name: "",
    scenario: "free_qa",
    streaming: false,
    message_count: 0,
    last_message: "",
    created_at: "",
    updated_at: "",
    ...partial,
  };
}

function renderSidebar(initialPath = "/chat") {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <Routes>
        <Route
          path="*"
          element={
            <>
              <Sidebar />
              <div>当前路径标记</div>
            </>
          }
        />
        <Route path="/chat/new" element={<div>草稿页</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("Sidebar 会话列表", () => {
  beforeEach(() => {
    createSession.mockReset();
    sessions.mockReset().mockResolvedValue({
      items: [
        makeSession({ id: 1, title: "有消息的会话", message_count: 3 }),
        makeSession({ id: 2, title: "新对话", message_count: 0 }),
        makeSession({ id: 3, title: "生成中的会话", message_count: 0, streaming: true }),
      ],
      total: 3,
    });
    // /api/assessment/all/overview 的 fetch 在 jsdom 下无后端，catch 后置空即可
  });

  it("隐藏从未发言的空会话，保留有消息与流式中的会话", async () => {
    renderSidebar();
    expect(await screen.findByText("有消息的会话")).toBeInTheDocument();
    expect(screen.getByText("生成中的会话")).toBeInTheDocument();
    expect(screen.queryByText("新对话")).not.toBeInTheDocument();
  });

  it("当前激活的空会话仍然显示", async () => {
    renderSidebar("/chat/2");
    expect(await screen.findByText("新对话")).toBeInTheDocument();
  });

  it("点新建对话只跳转草稿页，不立即创建会话", async () => {
    renderSidebar();
    fireEvent.click(screen.getByRole("button", { name: /新建对话/ }));
    expect(await screen.findByText("草稿页")).toBeInTheDocument();
    expect(createSession).not.toHaveBeenCalled();
    await waitFor(() => expect(sessions).toHaveBeenCalled());
  });
});
