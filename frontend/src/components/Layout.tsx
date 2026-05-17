import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useState } from "react";

const navItems = [
  { to: "/", label: "仪表盘", icon: "📊" },
  { to: "/customers", label: "客情列表", icon: "👥" },
  { to: "/import", label: "数据导入", icon: "📥" },
];

export default function Layout() {
  const [menuOpen, setMenuOpen] = useState(false);
  const loc = useLocation();

  return (
    <div className="min-h-screen bg-[#faf9f6] flex flex-col">
      {/* Top bar - mobile */}
      <header className="lg:hidden bg-white shadow-sm sticky top-0 z-30">
        <div className="flex items-center justify-between px-4 h-14">
          <div>
            <span className="font-bold text-slate-800 text-lg">客情健康度</span>
            <div className="h-0.5 w-8 bg-amber-500 rounded-full mt-0.5" />
          </div>
          <button
            onClick={() => setMenuOpen(!menuOpen)}
            className="p-2 text-slate-500"
          >
            <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              {menuOpen ? (
                <path strokeLinecap="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              ) : (
                <path strokeLinecap="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
              )}
            </svg>
          </button>
        </div>
        {menuOpen && (
          <nav className="border-t border-slate-100 bg-white px-2 py-2">
            {navItems.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === "/"}
                onClick={() => setMenuOpen(false)}
                className={({ isActive }) =>
                  `flex items-center gap-2 px-4 py-3 rounded-lg text-sm font-medium border-l-2 ${
                    isActive
                      ? "border-amber-500 bg-amber-50 text-amber-700"
                      : "border-transparent text-slate-500 hover:bg-slate-50 hover:text-slate-700"
                  }`
                }
              >
                <span>{item.icon}</span>
                {item.label}
              </NavLink>
            ))}
          </nav>
        )}
      </header>

      <div className="flex flex-1">
        {/* Sidebar - desktop */}
        <aside className="hidden lg:flex flex-col w-56 bg-white shadow-sm shrink-0">
          <div className="h-14 flex items-center px-6 border-b border-slate-100">
            <div>
              <span className="font-bold text-slate-800 text-lg">客情健康度</span>
              <div className="h-0.5 w-10 bg-amber-500 rounded-full mt-0.5" />
            </div>
          </div>
          <nav className="flex-1 px-3 py-4 space-y-1">
            {navItems.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === "/"}
                className={({ isActive }) =>
                  `flex items-center gap-3 px-3 py-2.5 rounded-r-lg text-sm font-medium transition-colors border-l-2 ${
                    isActive
                      ? "border-amber-500 bg-amber-50 text-amber-700"
                      : "border-transparent text-slate-500 hover:bg-slate-50 hover:text-slate-800"
                  }`
                }
              >
                <span>{item.icon}</span>
                {item.label}
              </NavLink>
            ))}
          </nav>
          <div className="p-4 border-t border-slate-100 text-xs text-slate-400">
            客情健康度评估系统 v1.0
          </div>
        </aside>

        {/* Main content */}
        <main className="flex-1 overflow-auto">
          <div className="max-w-6xl mx-auto p-4 lg:p-8">
            <Outlet />
          </div>
        </main>
      </div>

      {/* Bottom nav - mobile */}
      <nav className="lg:hidden fixed bottom-0 inset-x-0 bg-white border-t border-slate-200 z-30 flex">
        {navItems.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.to === "/"}
            className={({ isActive }) =>
              `flex-1 flex flex-col items-center gap-0.5 py-2 text-xs font-medium ${
                isActive ? "text-amber-600" : "text-slate-400"
              }`
            }
          >
            <span className="text-lg">{item.icon}</span>
            {item.label}
          </NavLink>
        ))}
      </nav>
    </div>
  );
}
