// Toast stack: 4500ms lifetime, ok/err kinds, aria-live region. Errors show a
// red left border, confirmations a green one — no other decoration.
import { createContext, useCallback, useContext, useState } from "react";

const ToastContext = createContext(() => {});

export function useToast() {
  return useContext(ToastContext);
}

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([]);

  const toast = useCallback((message, kind) => {
    const id = Math.random().toString(36).slice(2);
    setToasts((t) => [...t, { id, message, kind: kind === "err" ? "err" : "ok" }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4500);
  }, []);

  return (
    <ToastContext.Provider value={toast}>
      {children}
      <div
        className="fixed bottom-6 right-6 z-50 flex max-w-sm flex-col gap-2"
        data-testid="toast-stack"
        aria-live="polite"
      >
        {toasts.map((t) => (
          <div
            key={t.id}
            data-testid="toast"
            className={`rounded-md border border-line-strong bg-card px-3.5 py-2.5 text-sm ${
              t.kind === "err" ? "border-l-danger border-l-2" : "border-l-success border-l-2"
            }`}
          >
            {t.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
