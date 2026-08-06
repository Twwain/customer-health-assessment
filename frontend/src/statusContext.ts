import { createContext, useContext } from "react";
import type { LLMStatusResponse } from "./types";

export interface StatusCtx {
  status: LLMStatusResponse | null;
  loading: boolean;
  refresh: () => void;
}

export const Ctx = createContext<StatusCtx>({
  status: null,
  loading: true,
  refresh: () => {},
});

export function useLLMStatus() {
  return useContext(Ctx);
}
