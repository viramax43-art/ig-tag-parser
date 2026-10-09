import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { api } from "./api";
import "./styles.css";

function reportClientError(
  context: string,
  message: string,
  stack?: string,
) {
  console.error(`[ui:${context}]`, message, stack || "");
  void api.clientLog({
    level: "error",
    context,
    message,
    stack: stack || "",
  });
}

window.addEventListener("error", (ev) => {
  reportClientError(
    "window.onerror",
    ev.message || String(ev.error || "unknown"),
    ev.error instanceof Error ? ev.error.stack : undefined,
  );
});

window.addEventListener("unhandledrejection", (ev) => {
  const reason = ev.reason;
  const message =
    reason instanceof Error
      ? reason.message
      : typeof reason === "string"
        ? reason
        : JSON.stringify(reason);
  const stack = reason instanceof Error ? reason.stack : undefined;
  reportClientError("unhandledrejection", message, stack);
});

const rootEl = document.getElementById("root");
if (!rootEl) {
  reportClientError("boot", "element #root not found");
} else {
  try {
    createRoot(rootEl).render(
      <StrictMode>
        <App />
      </StrictMode>,
    );
  } catch (err) {
    reportClientError(
      "react-boot",
      err instanceof Error ? err.message : String(err),
      err instanceof Error ? err.stack : undefined,
    );
  }
}
