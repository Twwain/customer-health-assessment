import { useEffect, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { LLMStatusProvider, useLLMStatus } from "../statusContext";
import { useIsMobile } from "../hooks";
import { customers } from "../api";
import { setLevels } from "../lib/ui";
import Sidebar from "./Sidebar";

function Topbar({ onMenu }: { onMenu?: () => void }) {
  const { status } = useLLMStatus();
  const degraded = status?.degraded ?? false;
  return (
    <div className="flex h-12 shrink-0 items-center gap-2.5 border-b border-border bg-surface px-5">
      <button
        onClick={onMenu}
        className="mr-1 flex h-7 w-7 items-center justify-center rounded-lg text-[16px] text-ink-2 md:hidden"
      >
        ☰
      </button>
      <div className="flex items-center gap-2 text-[14.5px] font-semibold tracking-tight text-ink">
        <span className="h-[18px] w-[18px] rounded-[5px] bg-accent" />
        客情评估智能体
      </div>
      <div className="flex-1" />
      {status && (
        <div
          className={`flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[12px] ${
            degraded
              ? "border-warning/25 bg-warning-soft text-warning"
              : "border-border bg-surface-2 text-ink-2"
          }`}
        >
          <span className={`h-[7px] w-[7px] rounded-full ${degraded ? "bg-warning" : "bg-success"}`} />
          {degraded ? "LLM 不可用 · 已降级为规则引擎" : `AI 就绪${status.model ? ` · ${status.model}` : ""}`}
        </div>
      )}
    </div>
  );
}

function BottomTabBar() {
  const tabs = [
    { icon: "💬", label: "对话", path: "/chat" },
    { icon: "👥", label: "客户库", path: "/customers" },
    { icon: "📚", label: "知识库", path: "/knowledge" },
  ];
  return (
    <div className="flex h-[58px] shrink-0 border-t border-border bg-surface pb-1">
      {tabs.map((t) => (
        <NavLink
          key={t.path}
          to={t.path}
          className={({ isActive }) =>
            `flex flex-1 flex-col items-center justify-center gap-0.5 text-[10.5px] ${
              isActive ? "text-brand font-semibold" : "text-muted"
            }`
          }
        >
          <span className="text-[18px]">{t.icon}</span>
          {t.label}
        </NavLink>
      ))}
    </div>
  );
}

function Shell() {
  const isMobile = useIsMobile();
  const [drawerOpen, setDrawerOpen] = useState(false);

  if (isMobile) {
    return (
      <div className="flex h-full flex-col">
        <Topbar onMenu={() => setDrawerOpen(true)} />
        <div className="min-h-0 flex-1 overflow-y-auto bg-bg">
          <Outlet />
        </div>
        <BottomTabBar />
        {drawerOpen && (
          <>
            <div className="overlay-mask" onClick={() => setDrawerOpen(false)} />
            <div className="fixed inset-y-0 left-0 z-[700] w-[268px] bg-sidebar shadow-[6px_0_24px_rgba(15,15,15,.16)]">
              <Sidebar onNavigate={() => setDrawerOpen(false)} />
            </div>
          </>
        )}
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <Topbar />
      <div className="flex min-h-0 flex-1">
        <aside className="w-[260px] shrink-0 border-r border-border bg-sidebar">
          <Sidebar />
        </aside>
        <main className="min-w-0 flex-1 overflow-y-auto">
          <Outlet />
        </main>
      </div>
    </div>
  );
}

export default function Layout() {
  // 启动时拉取评分配置中的等级表，注册到全局，使等级名/颜色随 scoring_config.yaml 走
  useEffect(() => {
    customers
      .factorConfig()
      .then((cfg) => setLevels(cfg.levels))
      .catch(() => {
        /* 配置拉取失败时用内置默认等级 */
      });
  }, []);

  return (
    <LLMStatusProvider>
      <Shell />
    </LLMStatusProvider>
  );
}
