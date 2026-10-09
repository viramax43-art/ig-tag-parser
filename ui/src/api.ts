export type TabId = "tags" | "accounts" | "proxies" | "run";

export type Settings = {
  max_accounts: number;
  retry_unfinished: boolean;
  only_ru: boolean;
  report_every_accounts: number;
  export_interval_sec: number;
};

export type Status = {
  running: boolean;
  base_dir: string;
  has_req: boolean;
  req_size: number;
  os_label?: string;
  os_build?: number | null;
  webview2?: boolean | null;
  platform_note?: string;
};

export type Progress = {
  running: boolean;
  tags_total: number;
  tags_finished: number;
  tags_running: number;
  tags_pct: number;
  accounts_collected: number;
  posts_total: number;
  pages_total: number;
  accounts_alive: number;
  accounts_total: number;
  accounts_pool_loaded?: number;
  accounts_file_lines?: number;
  max_workers?: number;
  max_workers_setting?: number;
  proxies_alive: number;
  proxies_total: number;
};

export type StartResult = {
  ok: boolean;
  already?: boolean;
  idle?: boolean;
  code?: number | null;
  error?: string;
  log_cursor?: number;
  run_log?: string;
};

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${text || "(empty body)"}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  status: () => fetch("/api/status").then((r) => json<Status>(r)),
  getFile: (kind: "tags" | "accounts" | "proxies") =>
    fetch(`/api/files/${kind}`).then((r) => json<{ content: string }>(r)),
  saveFile: (kind: "tags" | "accounts" | "proxies", content: string) =>
    fetch(`/api/files/${kind}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    }).then((r) => json<{ ok: boolean; content?: string | null }>(r)),
  getSettings: () => fetch("/api/settings").then((r) => json<Settings>(r)),
  saveSettings: (data: Settings) =>
    fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    }).then((r) => json<{ ok: boolean }>(r)),
  saveReq: (content: string) =>
    fetch("/api/req", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    }).then((r) => json<{ ok: boolean; size: number }>(r)),
  start: () =>
    fetch("/api/run", { method: "POST" }).then((r) => json<StartResult>(r)),
  stop: () =>
    fetch("/api/stop", { method: "POST" }).then((r) =>
      json<{ ok: boolean; running?: boolean }>(r),
    ),
  exportXlsx: () =>
    fetch("/api/export", { method: "POST" }).then((r) =>
      json<{ ok: boolean }>(r),
    ),
  openData: () =>
    fetch("/api/open-data", { method: "POST" }).then((r) =>
      json<{ ok: boolean }>(r),
    ),
  resetDb: () =>
    fetch("/api/reset-db", { method: "POST" }).then((r) =>
      json<{ ok: boolean; removed: string[]; archived_excel: string | null }>(r),
    ),
  logs: (after: number) =>
    fetch(`/api/logs?after=${after}`).then((r) =>
      json<{ lines: string[]; next: number; running: boolean }>(r),
    ),
  progress: () =>
    fetch("/api/progress").then((r) => json<Progress>(r)),
  clientLog: (payload: {
    level?: string;
    message: string;
    stack?: string;
    context?: string;
  }) =>
    fetch("/api/client-log", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then((r) => json<{ ok: boolean }>(r))
      .catch(() => ({ ok: false })),
};
