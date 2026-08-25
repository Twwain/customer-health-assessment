import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { chat, knowledge } from "../api";
import type { ChatSessionItem } from "../types";

interface SidebarProps {
  onNavigate?: () => void;
}

export default function Sidebar({ onNavigate }: SidebarProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const [sessions, setSessions] = useState<ChatSessionItem[]>([]);
  const [riskCount, setRiskCount] = useState<number | null>(null);
  const [knowledgeCount, setKnowledgeCount] = useState<number | null>(null);

  const activeScreen = location.pathname.startsWith("/chat")
    ? "chat"
    : location.pathname.startsWith("/customers")
      ? "customers"
      : location.pathname.startsWith("/knowledge")
        ? "knowledge"
        : "";

  const activeSessionId = location.pathname.startsWith("/chat/")
    ? Number(location.pathname.split("/")[2])
    : null;

  // 隐藏从未发言的空会话（历史「新对话」占位），当前激活/流式中的除外
  const visibleSessions = sessions.filter(
    (s) => s.message_count > 0 || s.streaming || s.id === activeSessionId
  );

  useEffect(() => {
    // 路由变化（如删除/新建会话后 navigate）时重新拉取，避免已删会话残留在侧边栏
    chat.sessions().then((r) => setSessions(r.items)).catch(() => setSessions([]));
  }, [location.pathname]);

  useEffect(() => {
    // 风险客户数（客户库徽标）
    fetch("/api/assessment/all/overview")
      .then((r) => r.json())
      .then((d) => setRiskCount(d.risk_count))
      .catch(() => setRiskCount(null));
    knowledge
      .status()
      .then((s) => setKnowledgeCount(s.count))
      .catch(() => setKnowledgeCount(null));
  }, []);

  const newChat = () => {
    // 草稿态：只跳转不建库，首发消息时才真正创建会话，避免空「新对话」占位
    navigate("/chat/new");
    onNavigate?.();
  };

  const go = (path: string) => {
    navigate(path);
    onNavigate?.();
  };

  return (
    <div className="flex h-full flex-col bg-sidebar">
      <button
        onClick={newChat}
        className="mx-3.5 mt-3.5 mb-2.5 flex h-9.5 items-center justify-center gap-1.5 rounded-lg bg-accent text-[14.5px] font-medium text-white transition hover:bg-accent-hover"
      >
        ＋ 新建对话
      </button>

      <div className="px-4.5 pb-1.5 pt-2 text-[12px] font-semibold tracking-wide text-muted">
        最近会话
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-2">
        {visibleSessions.map((s) => (
          <button
            key={s.id}
            onClick={() => go(`/chat/${s.id}`)}
            className={`mb-0.5 flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-[14px] transition ${
              s.id === activeSessionId
                ? "bg-accent-soft font-medium text-accent"
                : "text-ink-2 hover:bg-[#EBEBEB]"
            }`}
          >
            <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${s.id === activeSessionId ? "bg-accent" : "bg-[#C4C4C4]"}`} />
            <span className="flex-1 truncate">{s.title}</span>
          </button>
        ))}
        {visibleSessions.length === 0 && (
          <div className="px-2.5 py-2 text-[13px] text-muted">暂无会话</div>
        )}
      </div>

      <div className="border-t border-border px-2 py-2.5">
        <NavItem
          icon="💬"
          label="对话"
          on={activeScreen === "chat"}
          onClick={() => go("/chat")}
        />
        <NavItem
          icon="👥"
          label="客户库"
          on={activeScreen === "customers"}
          badge={riskCount ?? undefined}
          onClick={() => go("/customers")}
        />
        <NavItem
          icon="📚"
          label="知识库"
          on={activeScreen === "knowledge"}
          count={knowledgeCount ?? undefined}
          onClick={() => go("/knowledge")}
        />
      </div>

    </div>
  );
}

function NavItem({
  icon,
  label,
  on,
  badge,
  count,
  onClick,
}: {
  icon: string;
  label: string;
  on: boolean;
  badge?: number;
  count?: number;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`relative flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-[14.5px] transition ${
        on ? "bg-surface font-semibold text-ink" : "text-ink-2 hover:bg-[#EBEBEB] hover:text-accent"
      }`}
    >
      {on && <span className="absolute left-0 top-1/2 h-4 w-[3px] -translate-y-1/2 rounded-r-full bg-accent" />}
      <span>{icon}</span>
      <span>{label}</span>
      {badge !== undefined && (
        <span className="ml-auto flex h-[17px] min-w-[17px] items-center justify-center rounded-full bg-danger px-1 text-[11px] text-white">
          {badge}
        </span>
      )}
      {count !== undefined && count > 0 && !badge && (
        <span className="ml-auto text-[12px] text-muted">{count}</span>
      )}
    </button>
  );
}
