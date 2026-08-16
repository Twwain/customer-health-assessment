import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from "react";

type Tone = "info" | "success" | "error";
type ConfirmOptions = { title?: string; confirmText?: string; danger?: boolean };
type UiFeedback = {
  notify: (message: string, tone?: Tone) => void;
  confirm: (message: string, options?: ConfirmOptions) => Promise<boolean>;
};

const Context = createContext<UiFeedback | null>(null);

export function UiFeedbackProvider({ children }: { children: ReactNode }) {
  const [toast, setToast] = useState<{ message: string; tone: Tone } | null>(null);
  const [dialog, setDialog] = useState<{ message: string; options: ConfirmOptions } | null>(null);
  const resolver = useRef<((value: boolean) => void) | null>(null);
  const timer = useRef<number | null>(null);

  const notify = useCallback((message: string, tone: Tone = "info") => {
    if (timer.current !== null) window.clearTimeout(timer.current);
    setToast({ message, tone });
    timer.current = window.setTimeout(() => setToast(null), 3600);
  }, []);

  const confirm = useCallback((message: string, options: ConfirmOptions = {}) => {
    resolver.current?.(false);
    setDialog({ message, options });
    return new Promise<boolean>((resolve) => {
      resolver.current = resolve;
    });
  }, []);

  const finish = (answer: boolean) => {
    resolver.current?.(answer);
    resolver.current = null;
    setDialog(null);
  };

  return (
    <Context.Provider value={{ notify, confirm }}>
      {children}
      {toast && (
        <div
          role="status"
          className={`fixed right-5 top-5 z-[1100] max-w-sm rounded-xl border px-4 py-3 text-sm shadow-xl ${
            toast.tone === "error"
              ? "border-red-200 bg-red-50 text-red-700"
              : toast.tone === "success"
                ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                : "border-blue-200 bg-blue-50 text-blue-700"
          }`}
        >
          {toast.message}
        </div>
      )}
      {dialog && (
        <div className="fixed inset-0 z-[1000] flex items-center justify-center bg-black/35 p-4" role="presentation">
          <div role="dialog" aria-modal="true" aria-labelledby="confirm-title" className="w-full max-w-md rounded-2xl bg-surface p-5 shadow-2xl">
            <h2 id="confirm-title" className="text-base font-semibold text-ink">
              {dialog.options.title ?? "请确认"}
            </h2>
            <p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-muted">{dialog.message}</p>
            <div className="mt-5 flex justify-end gap-2">
              <button className="rounded-lg border border-border px-4 py-2 text-sm text-ink-2" onClick={() => finish(false)}>
                取消
              </button>
              <button
                autoFocus
                className={`rounded-lg px-4 py-2 text-sm font-medium text-white ${dialog.options.danger ? "bg-red-600 hover:bg-red-700" : "bg-accent hover:bg-accent-hover"}`}
                onClick={() => finish(true)}
              >
                {dialog.options.confirmText ?? "确定"}
              </button>
            </div>
          </div>
        </div>
      )}
    </Context.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components -- Provider 与配套 hook 共同构成该组件 API
export function useUiFeedback(): UiFeedback {
  const value = useContext(Context);
  if (!value) throw new Error("useUiFeedback 必须在 UiFeedbackProvider 内使用");
  return value;
}
