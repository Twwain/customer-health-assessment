// @vitest-environment jsdom
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { chat, customers, streamChat } from "../api";
import { UiFeedbackProvider } from "../components/UiFeedback";
import type { AssessmentResponse, ChatEvent, ChatMessageItem, ChatSessionDetail } from "../types";
import Chat from "./Chat";

vi.mock("../api", () => ({
  streamChat: vi.fn(),
  customers: { list: vi.fn(), assessment: vi.fn(), trend: vi.fn(), factorConfig: vi.fn().mockRejectedValue(new Error("not needed")) },
  chat: { getSession: vi.fn(), createSession: vi.fn(), feedback: vi.fn() },
}));
const score = (value: number): AssessmentResponse => ({ customer_id: 26, customer_name: "快照客户", total_score: value,
  max_score: 100, level: "风险", level_color: "", dimensions: [], risk_alerts: [], alerts: [], suggestions: [],
  config_version: "v1", assessed_at: "2026-11-01T00:00:00" });
const answer = (id: number, value?: number): ChatMessageItem => ({ id, session_id: 7, role: "assistant", scenario: "assessment",
  content: `回答${id}`, assessment: value === undefined ? undefined : score(value), references: [], strategy_items: [], tokens_used: 0,
  feedback: "", degraded: false, created_at: "" });
const session = (messages: ChatMessageItem[] = []): ChatSessionDetail => ({
  id: 7, title: "快照测试", customer_id: 26, customer_name: "快照客户", scenario: "assessment", streaming: false, message_count: messages.length,
  last_message: "", created_at: "", updated_at: "", system_prompt: "", messages,
});
function renderChat() {
  return render(<UiFeedbackProvider><MemoryRouter initialEntries={["/chat/7"]}><Routes>
    <Route path="/chat/:sessionId" element={<Chat />} />
  </Routes></MemoryRouter></UiFeedbackProvider>);
}
beforeEach(() => {
  vi.mocked(chat.getSession).mockReset().mockResolvedValue(session());
  vi.mocked(customers.assessment).mockReset().mockResolvedValue(score(88));
  vi.mocked(customers.trend).mockReset().mockResolvedValue({ customer_id: 26, customer_name: "快照客户", max_score: 100,
    points: [], latest_score: 88, previous_score: 44, delta: 44, trend: "up", level: "健康", level_color: "", level_lines: [] });
  vi.mocked(customers.list).mockReset();
  vi.mocked(streamChat).mockReset().mockResolvedValue(undefined);
});

describe("message scoring snapshots", () => {
  it("keeps older cards unchanged while a new assessment uses the latest factors and after reload", async () => {
    const older = [answer(1, 42), answer(2, 67)];
    vi.mocked(chat.getSession).mockResolvedValue(session(older));
    let emit!: (event: ChatEvent) => void | Promise<void>;
    let finish!: () => void;
    vi.mocked(streamChat).mockImplementation((_url, _body, onEvent) => {
      emit = onEvent;
      return new Promise<void>((resolve) => { finish = resolve; });
    });
    const { container, unmount } = renderChat();
    await screen.findByText("42");
    fireEvent.click(screen.getByRole("button", { name: "📊 综合评估" }));
    await act(async () => { await emit({ type: "context", data: { assessment: score(91), trend: null } }); });
    expect(within(container.querySelector('[data-message-anchor="1"]') as HTMLElement).getByText("42")).toBeInTheDocument();
    expect(within(container.querySelector('[data-message-anchor="2"]') as HTMLElement).getByText("67")).toBeInTheDocument();
    expect(within(container.querySelector('[data-message-anchor="stream"]') as HTMLElement).getByText("91")).toBeInTheDocument();
    const newest = answer(3, 91);
    await act(async () => { await emit({ type: "done", data: { message: newest } }); finish(); });
    expect(screen.getByText("42")).toBeInTheDocument();
    expect(screen.getByText("67")).toBeInTheDocument();
    expect(screen.getByText("91")).toBeInTheDocument();
    unmount();
    vi.mocked(chat.getSession).mockResolvedValue(session([...older, newest]));
    vi.mocked(customers.assessment).mockResolvedValue(score(99));
    renderChat();
    expect(await screen.findByText("91")).toBeInTheDocument();
    expect(screen.getByText("42")).toBeInTheDocument();
    expect(screen.getByText("67")).toBeInTheDocument();
    expect(screen.queryByText("99")).not.toBeInTheDocument();
  });

  it("displays each saved snapshot independently and never fills missing or free-QA cards from current data", async () => {
    const freeQa = { ...answer(4, 25), scenario: "free_qa" };
    vi.mocked(chat.getSession).mockResolvedValue(session([answer(1, 42), answer(2, 67), answer(3), freeQa]));
    const { container, unmount } = renderChat();
    await screen.findByText("回答4");
    expect(within(container.querySelector('[data-message-anchor="1"]') as HTMLElement).getByText("42")).toBeInTheDocument();
    expect(within(container.querySelector('[data-message-anchor="2"]') as HTMLElement).getByText("67")).toBeInTheDocument();
    expect(screen.queryByText("88")).not.toBeInTheDocument();
    expect(screen.queryByText("25")).not.toBeInTheDocument();
    expect(container.querySelector('[data-message-anchor="3"]')).not.toHaveTextContent("满分");
    unmount();
    vi.mocked(customers.assessment).mockResolvedValue(score(99));
    renderChat();
    expect(await screen.findByText("42")).toBeInTheDocument();
    expect(screen.queryByText("99")).not.toBeInTheDocument();
  });

  it("freezes context including missing trend and keeps the completed snapshot after streaming ends", async () => {
    let emit!: (event: ChatEvent) => void | Promise<void>;
    let finish!: () => void;
    vi.mocked(streamChat).mockImplementation((_url, _body, onEvent) => {
      emit = onEvent;
      onEvent({ type: "start", data: { scenario: "assessment" } });
      return new Promise<void>((resolve) => { finish = resolve; });
    });
    renderChat();
    await screen.findByText("快照测试");
    fireEvent.click(screen.getByRole("button", { name: "📊 综合评估" }));
    expect(await screen.findByText("88")).toBeInTheDocument();
    await act(async () => { await emit({ type: "context", data: { assessment: score(61), trend: null } }); });
    expect(screen.getByText("61")).toBeInTheDocument();
    expect(screen.queryByText(/\+44/)).not.toBeInTheDocument();
    await act(async () => { await emit({ type: "done", data: { message: answer(10, 61) } }); finish(); });
    expect(screen.getByText("61")).toBeInTheDocument();
    expect(screen.queryByText("88")).not.toBeInTheDocument();
  });

  it("does not invent a completed snapshot when the final message has none", async () => {
    vi.mocked(streamChat).mockImplementation(async (_url, _body, onEvent) => {
      await onEvent({ type: "start", data: { scenario: "assessment" } });
      await onEvent({ type: "context", data: { assessment: score(61) } });
      await onEvent({ type: "done", data: { message: answer(10) } });
    });
    renderChat();
    await screen.findByText("快照测试");
    fireEvent.click(screen.getByRole("button", { name: "📊 综合评估" }));
    await screen.findByText("回答10");
    expect(screen.queryByText("61")).not.toBeInTheDocument();
    expect(screen.queryByText("88")).not.toBeInTheDocument();
  });

  it("preserves the original answer and its scoring card after regeneration fails", async () => {
    vi.mocked(chat.getSession).mockResolvedValue(session([answer(1, 42)]));
    vi.mocked(streamChat).mockImplementation(async (_url, _body, onEvent) => {
      await onEvent({ type: "context", data: { assessment: score(61) } });
      throw new Error("network unavailable");
    });
    renderChat();
    await screen.findByText("42");
    fireEvent.click(screen.getByRole("button", { name: "🔄 重新生成" }));
    await screen.findByText(/重新生成未完成，已保留原答案/);
    expect(screen.getByText("回答1")).toBeInTheDocument();
    expect(screen.getByText("42")).toBeInTheDocument();
    expect(screen.queryByText("61")).not.toBeInTheDocument();
    expect(screen.queryByText("88")).not.toBeInTheDocument();
  });
});
