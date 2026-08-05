import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { chat } from "./api";
import type { LLMStatusResponse } from "./types";

interface StatusCtx {
  status: LLMStatusResponse | null;
  loading: boolean;
  refresh: () => void;
}

const Ctx = createContext<StatusCtx>({ status: null, loading: true, refresh: () => {} });

export function LLMStatusProvider({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<LLMStatusResponse | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(() => {
    chat
      .status()
      .then(setStatus)
      .catch(() => setStatus(null))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    refresh();
    // 周期轮询：LLM 恢复后降级提示条可自动消失，无需手动刷新页面
    const timer = window.setInterval(refresh, 30_000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  return <Ctx.Provider value={{ status, loading, refresh }}>{children}</Ctx.Provider>;
}

export function useLLMStatus() {
  return useContext(Ctx);
}
