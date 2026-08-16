// @vitest-environment jsdom
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { UiFeedbackProvider } from "../components/UiFeedback";
import Chat from "./Chat";

const { deleteSession } = vi.hoisted(() => ({ deleteSession: vi.fn() }));

vi.mock("../api", () => ({
  streamChat: vi.fn(),
  customers: {
    assessment: vi.fn().mockResolvedValue(null),
    trend: vi.fn().mockResolvedValue(null),
  },
  chat: {
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
  beforeEach(() => deleteSession.mockReset().mockResolvedValue(undefined));

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
});
