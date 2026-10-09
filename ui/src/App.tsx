import { motion } from "framer-motion";
import { useEffect, useMemo, useRef, useState, type ClipboardEvent } from "react";
import { api, type Progress, type Settings, type TabId } from "./api";
import { countAccountLines, normalizeAccountsText } from "./normalizeAccounts";

const EMPTY_PROGRESS: Progress = {
  running: false,
  tags_total: 0,
  tags_finished: 0,
  tags_running: 0,
  tags_pct: 0,
  accounts_collected: 0,
  posts_total: 0,
  pages_total: 0,
  accounts_alive: 0,
  accounts_total: 0,
  proxies_alive: 0,
  proxies_total: 0,
};

const TABS: { id: TabId; label: string; hint: string }[] = [
  {
    id: "tags",
    label: "Теги",
    hint: "Один тег на строку. Строки с // игнорируются.",
  },
  {
    id: "accounts",
    label: "Аккаунты",
    hint: "Вставка dump (user:pass:2FA|Instagram…) сама разобьёт на строки. Или username;password;2FA;прокси.",
  },
  {
    id: "proxies",
    label: "Прокси",
    hint:
      "host:port:user:pass, login:password@ip:port или http://user:pass@host:port. " +
      "1 строка = 1 dump-аккаунт. Обязательны, если Instagram заблокирован — весь трафик к IG только через них.",
  },
  {
    id: "run",
    label: "Запуск",
    hint:
      "Win10/Win11. Интерфейс — localhost; к instagram.com для UI доступ не нужен. " +
      "Сбор по тегам, Excel и шаблон GraphQL req.sh.",
  },
];

export default function App() {
  const [tab, setTab] = useState<TabId>("run");
  const [tags, setTags] = useState("");
  const [accounts, setAccounts] = useState("");
  const [proxies, setProxies] = useState("");
  const [settings, setSettings] = useState<Settings>({
    max_accounts: 1,
    retry_unfinished: false,
    only_ru: true,
    report_every_accounts: 10,
    export_interval_sec: 10,
  });
  const [running, setRunning] = useState(false);
  const [baseDir, setBaseDir] = useState("");
  const [envLabel, setEnvLabel] = useState("");
  const [hasReq, setHasReq] = useState(false);
  const [logText, setLogText] = useState("");
  const [progress, setProgress] = useState<Progress>(EMPTY_PROGRESS);
  const [toast, setToast] = useState("");
  const logCursor = useRef(0);
  const logRef = useRef<HTMLPreElement | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);

  const active = useMemo(() => TABS.find((t) => t.id === tab)!, [tab]);

  const flash = (msg: string) => {
    setToast(msg);
    window.setTimeout(() => setToast(""), 3200);
  };

  const pushLocalLog = (line: string) => {
    const stamp = new Date().toLocaleTimeString("ru-RU", { hour12: false });
    const text = `[${stamp}] [ui] ${line}`;
    setLogText((prev) => prev + (prev && !prev.endsWith("\n") ? "\n" : "") + text + "\n");
    void api.clientLog({ level: "info", context: "ui", message: line });
  };

  const reportErr = (context: string, err: unknown) => {
    const message = err instanceof Error ? err.message : String(err);
    const stack = err instanceof Error ? err.stack : undefined;
    pushLocalLog(`ERROR ${context}: ${message}`);
    if (stack) pushLocalLog(stack);
    void api.clientLog({
      level: "error",
      context,
      message,
      stack: stack || "",
    });
    flash(`${context}: ${message}`);
  };

  useEffect(() => {
    void (async () => {
      try {
        const [st, s, t, a, p] = await Promise.all([
          api.status(),
          api.getSettings(),
          api.getFile("tags"),
          api.getFile("accounts"),
          api.getFile("proxies"),
        ]);
        setRunning(st.running);
        setBaseDir(st.base_dir);
        setEnvLabel(
          [st.os_label, st.webview2 === false ? "нет WebView2" : null]
            .filter(Boolean)
            .join(" · ") || "",
        );
        setHasReq(st.has_req);
        setSettings(s);
        setTags(t.content);
        setAccounts(a.content);
        setProxies(p.content);
        pushLocalLog(
          `boot ok · running=${st.running} hasReq=${st.has_req} dir=${st.base_dir}`,
        );
      } catch (err) {
        reportErr("boot", err);
      }
    })();
  }, []);

  useEffect(() => {
    const id = window.setInterval(() => {
      void api
        .logs(logCursor.current)
        .then((res) => {
          if (res.lines.length) {
            setLogText((prev) => {
              const next =
                prev + (prev && !prev.endsWith("\n") ? "\n" : "") + res.lines.join("\n");
              return next;
            });
            logCursor.current = res.next;
          }
          setRunning(res.running);
        })
        .catch((err) => {
          // Не спамим каждую секунду — только в console + редкий client-log.
          console.warn("[ui] logs poll", err);
        });
      void api.progress().then(setProgress).catch((err) => {
        console.warn("[ui] progress poll", err);
      });
    }, 700);
    return () => window.clearInterval(id);
  }, []);

  useEffect(() => {
    void api.progress().then(setProgress).catch(() => undefined);
  }, []);

  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [logText]);

  const saveAll = async () => {
    try {
      const accountsNorm = normalizeAccountsText(accounts);
      if (accountsNorm !== accounts) setAccounts(accountsNorm);
      const [, accRes] = await Promise.all([
        api.saveFile("tags", tags),
        api.saveFile("accounts", accountsNorm),
        api.saveFile("proxies", proxies),
        api.saveSettings(settings),
      ]);
      if (accRes.content) setAccounts(accRes.content);
      flash(`Сохранено · аккаунтов: ${countAccountLines(accountsNorm)}`);
    } catch (err) {
      reportErr("save", err);
      throw err;
    }
  };

  const onEditorPaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    if (tab !== "accounts") return;
    const pasted = e.clipboardData.getData("text");
    if (!pasted.trim()) return;
    e.preventDefault();
    const ta = e.currentTarget;
    const startPos = ta.selectionStart;
    const end = ta.selectionEnd;
    const merged = accounts.slice(0, startPos) + pasted + accounts.slice(end);
    const normalized = normalizeAccountsText(merged);
    setAccounts(normalized);
    const n = countAccountLines(normalized);
    flash(`Вставлено · аккаунтов в списке: ${n}`);
    requestAnimationFrame(() => {
      try {
        ta.focus();
        const pos = normalized.length;
        ta.setSelectionRange(pos, pos);
      } catch (err) {
        reportErr("paste-caret", err);
      }
    });
  };

  const reload = async () => {
    try {
      const [t, a, p, s, st] = await Promise.all([
        api.getFile("tags"),
        api.getFile("accounts"),
        api.getFile("proxies"),
        api.getSettings(),
        api.status(),
      ]);
      setTags(t.content);
      setAccounts(a.content);
      setProxies(p.content);
      setSettings(s);
      setBaseDir(st.base_dir);
      setHasReq(st.has_req);
      flash("Перезагружено с диска");
    } catch (err) {
      reportErr("reload", err);
    }
  };

  const start = async () => {
    try {
      pushLocalLog("Старт: сохраняю файлы…");
      await saveAll();
      if (!hasReq && !window.confirm("Нет req.sh. Всё равно запустить?")) {
        pushLocalLog("Старт отменён: нет req.sh");
        return;
      }
      setLogText("");
      setProgress(EMPTY_PROGRESS);
      pushLocalLog("Старт: запрос /api/run…");
      const res = await api.start();
      if (typeof res.log_cursor === "number") {
        logCursor.current = res.log_cursor;
      }
      if (res.already) {
        setRunning(true);
        pushLocalLog(`Старт отклонён: уже идёт (${res.error || "already"})`);
        flash(res.error || "Сбор уже выполняется");
        return;
      }
      if (res.idle || res.code === 10) {
        setRunning(false);
        pushLocalLog(res.error || "Все теги уже finished — собирать нечего");
        flash(res.error || "Все теги уже собраны — нужен Сброс БД");
        return;
      }
      if (!res.ok) {
        setRunning(false);
        pushLocalLog(`Старт не удался: ${res.error || "ok=false"}`);
        flash(res.error || "Не удалось запустить");
        return;
      }
      setRunning(true);
      pushLocalLog(
        `Старт OK · log=${res.run_log || "data/run.log"} cursor=${res.log_cursor ?? "?"}`,
      );
      flash("Сбор запущен");
    } catch (err) {
      setRunning(false);
      reportErr("start", err);
    }
  };

  const stop = async () => {
    try {
      pushLocalLog("Стоп: запрос /api/stop…");
      await api.stop();
      flash("Остановка…");
    } catch (err) {
      reportErr("stop", err);
    }
  };

  const resetDb = async () => {
    if (running) {
      flash("Сначала остановите сбор");
      return;
    }
    const ok = window.confirm(
      "Сбросить базу данных?\n\n" +
        "• Excel сохранится (accounts_live → accounts_saved_…)\n" +
        "• Прогресс тегов (БД + data/tags) обнулится\n" +
        "• accounts.txt / tags.txt / прокси / сессии не трогаем",
    );
    if (!ok) return;
    try {
      const res = await api.resetDb();
      setProgress(EMPTY_PROGRESS);
      if (res.archived_excel) {
        flash(`БД сброшена, Excel → ${res.archived_excel}`);
      } else {
        flash("БД сброшена");
      }
      void api.progress().then(setProgress).catch(() => undefined);
    } catch (err) {
      flash(err instanceof Error ? err.message : "Не удалось сбросить БД");
    }
  };

  const copyLog = async () => {
    const text = logText.trim();
    if (!text) {
      flash("Лог пуст");
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      flash("Лог скопирован");
    } catch {
      try {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.left = "-9999px";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
        flash("Лог скопирован");
      } catch {
        flash("Не удалось скопировать");
      }
    }
  };

  const onReqFile = async (file: File | null) => {
    if (!file) return;
    const content = await file.text();
    if (content.length < 1000) {
      flash("Файл слишком короткий для GraphQL cURL");
      return;
    }
    const res = await api.saveReq(content);
    setHasReq(true);
    flash(`req.sh сохранён (${res.size} байт)`);
  };

  const editorValue =
    tab === "tags" ? tags : tab === "accounts" ? accounts : tab === "proxies" ? proxies : "";
  const setEditorValue =
    tab === "tags" ? setTags : tab === "accounts" ? setAccounts : setProxies;

  return (
    <div className="app">
      <aside className="rail">
        <motion.div
          className="brand"
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.55, ease: [0.22, 1, 0.36, 1] }}
        >
          <div className="brand-mark">IG</div>
          <h1>
            Tag
            <br />
            Parser
          </h1>
          <p>Сбор аккаунтов Instagram по хештегам — локально и под контролем.</p>
        </motion.div>

        <nav className="nav">
          {TABS.map((item, i) => (
            <motion.button
              key={item.id}
              type="button"
              className={tab === item.id ? "active" : ""}
              onClick={() => setTab(item.id)}
              initial={{ opacity: 0, x: -10 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ delay: 0.08 * i, duration: 0.35 }}
            >
              <span className="idx">0{i + 1}</span>
              <span>{item.label}</span>
            </motion.button>
          ))}
        </nav>

        <div className="rail-foot">
          {envLabel ? <span className="rail-env">{envLabel}</span> : null}
          <span>{baseDir || "…"}</span>
        </div>
      </aside>

      <main className="main">
        <div className="topbar">
          <div>
            <h2>{active.label}</h2>
            <p className="hint">{active.hint}</p>
          </div>
          <div className="actions">
            <button type="button" className="btn" onClick={() => void reload()}>
              С диска
            </button>
            <button type="button" className="btn" onClick={() => void saveAll()}>
              Сохранить
            </button>
            <button
              type="button"
              className="btn ghost-dark"
              onClick={() => void api.openData().then(() => flash("Папка данных"))}
            >
              Данные
            </button>
          </div>
        </div>

        <motion.section
          key={tab}
          className="panel"
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
        >
          {tab !== "run" ? (
            <textarea
              className="editor"
              spellCheck={false}
              value={editorValue}
              onChange={(e) => setEditorValue(e.target.value)}
              onPaste={onEditorPaste}
              placeholder={
                tab === "accounts"
                  ? "Вставьте dump-аккаунты — разобьются на строки автоматически"
                  : tab === "proxies"
                    ? "host:port:user:pass или login:password@ip:port — по одной на аккаунт. Нужны прокси с доступом к Instagram."
                    : undefined
              }
            />
          ) : (
            <div className="run-layout">
              <div className="run-controls">
                <label className="field">
                  Одновременно аккаунтов
                  <input
                    type="number"
                    min={1}
                    max={999}
                    value={settings.max_accounts}
                    onChange={(e) =>
                      setSettings((s) => ({
                        ...s,
                        max_accounts: Math.max(1, Number(e.target.value) || 1),
                      }))
                    }
                  />
                </label>
                <label className="field">
                  Отчёт каждые N аккаунтов
                  <input
                    type="number"
                    min={1}
                    max={10000}
                    value={settings.report_every_accounts}
                    onChange={(e) =>
                      setSettings((s) => ({
                        ...s,
                        report_every_accounts: Math.max(1, Number(e.target.value) || 10),
                      }))
                    }
                  />
                </label>
                <label className="field">
                  Excel каждые N сек
                  <input
                    type="number"
                    min={5}
                    max={600}
                    value={settings.export_interval_sec}
                    onChange={(e) =>
                      setSettings((s) => ({
                        ...s,
                        export_interval_sec: Math.max(5, Number(e.target.value) || 10),
                      }))
                    }
                  />
                </label>
                <label className="field">
                  <input
                    type="checkbox"
                    checked={settings.retry_unfinished}
                    onChange={(e) =>
                      setSettings((s) => ({
                        ...s,
                        retry_unfinished: e.target.checked,
                      }))
                    }
                  />
                  Добирать прерванные
                </label>
                <label className="field" title="Фильтр только для кнопки Excel. Live-файл всегда полный.">
                  <input
                    type="checkbox"
                    checked={settings.only_ru}
                    onChange={(e) =>
                      setSettings((s) => ({ ...s, only_ru: e.target.checked }))
                    }
                  />
                  Excel (кнопка) только РУ
                </label>

                <div className="run-actions">
                  <button
                    type="button"
                    className="btn"
                    onClick={() => fileRef.current?.click()}
                  >
                    Вставить req.sh
                  </button>
                  <button
                    type="button"
                    className="btn"
                    disabled={running}
                    onClick={() => void api.exportXlsx().then(() => flash("Экспорт запущен"))}
                  >
                    Excel
                  </button>
                  <button
                    type="button"
                    className="btn danger"
                    disabled={running}
                    title="Только SQLite. Excel архивируется и больше не перезаписывается."
                    onClick={() => void resetDb()}
                  >
                    Сброс БД
                  </button>
                  <button
                    type="button"
                    className="btn danger"
                    disabled={!running}
                    onClick={() => void stop()}
                  >
                    Стоп
                  </button>
                  <motion.button
                    type="button"
                    className="btn primary"
                    disabled={running}
                    onClick={() => void start()}
                    whileHover={{ scale: running ? 1 : 1.03 }}
                    whileTap={{ scale: 0.98 }}
                  >
                    Запустить сбор
                  </motion.button>
                </div>
              </div>

              <div className="log-wrap">
                <div className="log-head">
                  <span>Live log</span>
                  <div className="log-head-actions">
                    <button
                      type="button"
                      className="btn ghost-dark log-copy"
                      disabled={!logText}
                      onClick={() => void copyLog()}
                    >
                      Копировать
                    </button>
                    <span className={`badge ${running ? "live" : ""}`}>
                      {running ? <span className="dot" /> : null}
                      {running ? "идёт сбор" : hasReq ? "готово к запуску" : "нет req.sh"}
                    </span>
                  </div>
                </div>
                <div className="run-progress" aria-label="Прогресс по тегам">
                  <div className="run-progress-meta">
                    <span>
                      Теги {progress.tags_finished}/{progress.tags_total || "—"}
                      {progress.tags_running ? ` · в работе ${progress.tags_running}` : ""}
                    </span>
                    <span>{progress.tags_pct.toFixed(0)}%</span>
                  </div>
                  <div className="run-progress-track">
                    <motion.div
                      className="run-progress-fill"
                      initial={false}
                      animate={{ width: `${Math.min(100, Math.max(0, progress.tags_pct))}%` }}
                      transition={{ duration: 0.35, ease: [0.22, 1, 0.36, 1] }}
                    />
                  </div>
                </div>
                <div className="run-stats">
                  <span>акки {progress.accounts_collected}</span>
                  <span className="sep">·</span>
                  <span>посты {progress.posts_total}</span>
                  <span className="sep">·</span>
                  <span>стр. {progress.pages_total}</span>
                  <span className="sep">·</span>
                  <span title="Активные слоты / загружено в пул. Лимит — настройка «Одновременно». attempt=1/5 в логе — это HTTP-повтор, не аккаунты.">
                    слоты {progress.accounts_alive}/{progress.accounts_total}
                    {(progress.max_workers ?? 0) > 0
                      ? ` · лимит ${progress.max_workers}`
                      : ""}
                    {(progress.accounts_file_lines ?? 0) >
                    (progress.accounts_pool_loaded ?? progress.accounts_total)
                      ? ` · в файле ${progress.accounts_file_lines}, загружено ${progress.accounts_pool_loaded ?? progress.accounts_total}`
                      : ""}
                  </span>
                  <span className="sep">·</span>
                  <span>
                    прокси {progress.proxies_alive}/{progress.proxies_total}
                  </span>
                </div>
                <pre className="log" ref={logRef}>
                  {logText || "Лог появится после старта…"}
                </pre>
              </div>
            </div>
          )}
        </motion.section>
      </main>

      <input
        ref={fileRef}
        className="hidden-file"
        type="file"
        accept=".sh,.txt,*"
        onChange={(e) => void onReqFile(e.target.files?.[0] ?? null)}
      />

      {toast ? (
        <motion.div
          className="toast"
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0 }}
        >
          {toast}
        </motion.div>
      ) : null}
    </div>
  );
}
