#!/usr/bin/env python3
"""
Окно управления: теги, аккаунты, прокси, запуск сбора и выгрузка Excel.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from queue import Empty, Queue
from tkinter import filedialog, messagebox, ttk

from paths import (
    ACCOUNTS_FILE,
    BASE_DIR,
    EXPORTS_DIR,
    PROXIES_FILE,
    REQ_FILE,
    TAGS_FILE,
    ensure_layout,
    invoke_cmd,
)


class GuiApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("IG Tag Parser")
        self.geometry("960x720")
        self.minsize(800, 560)

        ensure_layout()
        self._worker: threading.Thread | None = None
        self._running = False
        self._ui_queue: Queue = Queue()
        self._poll_ui_queue()

        self._build()
        self._load_all()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self) -> None:
        top = ttk.Frame(self, padding=8)
        top.pack(fill=tk.BOTH, expand=True)

        nb = ttk.Notebook(top)
        nb.pack(fill=tk.BOTH, expand=True)

        self.tags_text = self._make_editor(
            nb, "Теги",
            "Один тег на строку. Строки с // игнорируются.\n"
            "Файл: data/tags.txt",
        )
        self.accounts_text = self._make_editor(
            nb, "Аккаунты",
            "Формат 1: username;password;2FA_SECRET;ip:port:user:pass\n"
            "Формат 2: dump-строка магазина — тогда прокси из вкладки «Прокси» "
            "по номеру строки.\n"
            "Файл: data/accounts.txt",
        )
        self.proxies_text = self._make_editor(
            nb, "Прокси",
            "host:port:user:pass — по одной на строку (для dump-аккаунтов).\n"
            "Файл: proxies.txt рядом с программой",
        )

        run_tab = ttk.Frame(nb, padding=8)
        nb.add(run_tab, text="Запуск")
        self._build_run_tab(run_tab)

        bar = ttk.Frame(self, padding=(8, 0, 8, 8))
        bar.pack(fill=tk.X)
        ttk.Button(bar, text="Сохранить всё", command=self._save_all).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(bar, text="Перезагрузить с диска", command=self._load_all).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(bar, text="Открыть папку данных", command=self._open_data).pack(
            side=tk.LEFT
        )
        self.status = ttk.Label(bar, text=str(BASE_DIR))
        self.status.pack(side=tk.RIGHT)

    def _make_editor(self, nb: ttk.Notebook, title: str, hint: str) -> tk.Text:
        frame = ttk.Frame(nb, padding=8)
        nb.add(frame, text=title)
        ttk.Label(frame, text=hint, wraplength=880, justify=tk.LEFT).pack(
            anchor=tk.W, pady=(0, 6)
        )
        wrap = ttk.Frame(frame)
        wrap.pack(fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(wrap)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        text = tk.Text(
            wrap,
            wrap=tk.NONE,
            font=("Consolas", 10),
            undo=True,
            yscrollcommand=scroll.set,
        )
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.config(command=text.yview)
        xscroll = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=text.xview)
        xscroll.pack(fill=tk.X)
        text.configure(xscrollcommand=xscroll.set)
        return text

    def _build_run_tab(self, frame: ttk.Frame) -> None:
        opts = ttk.Frame(frame)
        opts.pack(fill=tk.X, pady=(0, 8))

        self.retry_var = tk.BooleanVar(value=False)
        self.only_ru_var = tk.BooleanVar(value=True)
        self.max_accounts_var = tk.IntVar(value=1)
        try:
            from settings import load_settings
            saved = load_settings()
            self.max_accounts_var.set(saved.get("max_accounts", 1))
            self.retry_var.set(bool(saved.get("retry_unfinished", False)))
            self.only_ru_var.set(bool(saved.get("only_ru", True)))
        except Exception:
            pass
        ttk.Label(opts, text="Одновременно аккаунтов:").pack(side=tk.LEFT, padx=(0, 4))
        ttk.Spinbox(
            opts, from_=1, to=999, width=6, textvariable=self.max_accounts_var,
            command=self._save_runtime_settings,
        ).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Checkbutton(
            opts, text="Добирать прерванные теги (RETRY_UNFINISHED)",
            variable=self.retry_var, command=self._save_runtime_settings,
        ).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Checkbutton(
            opts, text="Excel (кнопка) только РУ",
            variable=self.only_ru_var, command=self._save_runtime_settings,
        ).pack(side=tk.LEFT)

        btns = ttk.Frame(frame)
        btns.pack(fill=tk.X, pady=(0, 8))
        self.start_btn = ttk.Button(btns, text="▶  Запустить сбор", command=self._start)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.stop_btn = ttk.Button(
            btns, text="■  Остановить", command=self._stop, state=tk.DISABLED
        )
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="Выгрузить Excel", command=self._export).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(btns, text="Вставить req.sh…", command=self._import_req).pack(
            side=tk.LEFT
        )

        hint = (
            "Нужен шаблон GraphQL в data/req.sh (кнопка «Вставить req.sh»).\n"
            "При заполненном accounts.txt сбор идёт в несколько потоков "
            "(логин + 2FA + прокси автоматически)."
        )
        ttk.Label(frame, text=hint, wraplength=880, justify=tk.LEFT).pack(
            anchor=tk.W, pady=(0, 6)
        )

        log_frame = ttk.LabelFrame(frame, text="Лог", padding=4)
        log_frame.pack(fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(log_frame)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text = tk.Text(
            log_frame,
            wrap=tk.WORD,
            font=("Consolas", 9),
            state=tk.DISABLED,
            yscrollcommand=scroll.set,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)
        scroll.config(command=self.log_text.yview)

    # ----- files -----

    def _read_file(self, path: Path) -> str:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def _write_file(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Нормализуем переводы строк; utf-8 без BOM (BOM ломает теги/логин).
        text = content.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
        if text and not text.endswith("\n"):
            text += "\n"
        path.write_text(text, encoding="utf-8")

    def _load_all(self) -> None:
        self._set_text(self.tags_text, self._read_file(TAGS_FILE))
        self._set_text(self.accounts_text, self._read_file(ACCOUNTS_FILE))
        self._set_text(self.proxies_text, self._read_file(PROXIES_FILE))
        self._append_log(f"[i] загружено из {BASE_DIR}")

    def _save_all(self) -> None:
        self._write_file(TAGS_FILE, self.tags_text.get("1.0", tk.END))
        self._write_file(ACCOUNTS_FILE, self.accounts_text.get("1.0", tk.END))
        self._write_file(PROXIES_FILE, self.proxies_text.get("1.0", tk.END))
        self.status.configure(text=f"Сохранено · {BASE_DIR}")
        self._append_log("[i] файлы сохранены")

    def _set_text(self, widget: tk.Text, value: str) -> None:
        widget.delete("1.0", tk.END)
        widget.insert("1.0", value)

    def _open_data(self) -> None:
        path = BASE_DIR
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def _import_req(self) -> None:
        path = filedialog.askopenfilename(
            title="Выберите файл с cURL (req.sh)",
            filetypes=[
                ("Shell / text", "*.sh *.txt *.*"),
                ("Все файлы", "*.*"),
            ],
        )
        if not path:
            return
        data = Path(path).read_text(encoding="utf-8", errors="replace")
        if len(data) < 1000:
            messagebox.showwarning(
                "Мало данных",
                "Файл слишком короткий для GraphQL cURL (< 1000 байт).",
            )
            return
        REQ_FILE.parent.mkdir(parents=True, exist_ok=True)
        REQ_FILE.write_text(data, encoding="utf-8")
        self._append_log(f"[i] req.sh записан ({len(data)} байт)")
        messagebox.showinfo("Готово", f"Шаблон сохранён:\n{REQ_FILE}")

    # ----- log -----

    def _append_log(self, msg: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _save_runtime_settings(self) -> None:
        try:
            from settings import load_settings, save_settings
            data = load_settings()
            data.update({
                "max_accounts": max(1, int(self.max_accounts_var.get())),
                "retry_unfinished": bool(self.retry_var.get()),
                "only_ru": bool(self.only_ru_var.get()),
            })
            save_settings(data)
        except Exception as exc:
            self._append_log(f"[!] настройки: {exc}")

    def _poll_ui_queue(self) -> None:
        try:
            while True:
                kind, value = self._ui_queue.get_nowait()
                if kind == "log":
                    self._append_log(value)
                elif kind == "finished":
                    self._run_finished()
        except Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._poll_ui_queue)

    def _ui_log(self, msg: str) -> None:
        self._ui_queue.put(("log", msg))

    # ----- run / stop / export -----

    def _start(self) -> None:
        if self._running:
            return
        self._save_all()
        if not REQ_FILE.exists() or REQ_FILE.stat().st_size < 1000:
            if not messagebox.askyesno(
                "Нет req.sh",
                "Нет шаблона data/req.sh.\n"
                "Для мультиаккаунта он обязателен.\n\n"
                "Всё равно запустить?",
            ):
                return

        self._running = True
        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self._append_log("")
        self._append_log("=" * 50)
        self._append_log("СТАРТ СБОРА")
        self._append_log("=" * 50)

        self._save_runtime_settings()
        env_extra = {}
        if self.retry_var.get():
            env_extra["RETRY_UNFINISHED"] = "1"
        env_extra["MAX_CONSECUTIVE_FAILURES"] = "10"
        env_extra["MAX_WORKERS"] = str(max(1, int(self.max_accounts_var.get())))

        self._worker = threading.Thread(
            target=self._run_worker, args=(env_extra,), daemon=True
        )
        self._worker.start()

    def _run_worker(self, env_extra: dict) -> None:
        import run_tags

        # Keep log visible in the GUI window.
        def gui_log(msg: str, *, error: bool = False) -> None:
            self._ui_log(msg)

        run_tags.log = gui_log
        run_tags.reset_stop()

        # Prefer child probe processes so stop/terminate is reliable and workers
        # do not share probe module globals. Keep in-process only if already set.
        old_env = {k: os.environ.get(k) for k in env_extra}
        old_inprocess = os.environ.get("IG_INPROCESS_PROBE")
        try:
            os.environ.pop("IG_INPROCESS_PROBE", None)
            for k, v in env_extra.items():
                os.environ[k] = v
            run_tags.MAX_WORKERS = int(env_extra.get("MAX_WORKERS", "1"))
            run_tags.RETRY_UNFINISHED = os.getenv("RETRY_UNFINISHED") == "1"
            run_tags.MAX_CONSECUTIVE_FAILURES = int(
                os.getenv("MAX_CONSECUTIVE_FAILURES", "3")
            )
            code = run_tags.main()
            self._ui_log(f"[i] сбор завершён, код {code}")
        except Exception as exc:
            self._ui_log(f"[!] ошибка: {exc}")
        finally:
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            if old_inprocess is None:
                os.environ.pop("IG_INPROCESS_PROBE", None)
            else:
                os.environ["IG_INPROCESS_PROBE"] = old_inprocess
            self._ui_queue.put(("finished", None))

    def _run_finished(self) -> None:
        self._running = False
        self.start_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)

    def _stop(self) -> None:
        if not self._running:
            return
        try:
            import run_tags
            run_tags.request_stop()
        except Exception:
            pass
        self.stop_btn.configure(state=tk.DISABLED)
        self._append_log("[!] остановка… дождитесь конца текущей страницы (до ~45 с)")

    def _export(self) -> None:
        if self._running:
            messagebox.showinfo("Занято", "Дождитесь окончания сбора или остановите его.")
            return
        self._save_all()
        self._append_log("[i] выгрузка Excel…")

        only_ru = bool(self.only_ru_var.get())

        def work() -> None:
            env = {**os.environ}
            if only_ru:
                env["ONLY_RU"] = "1"
            else:
                env.pop("ONLY_RU", None)
            try:
                proc = subprocess.Popen(
                    invoke_cmd("export_xlsx"),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=str(BASE_DIR),
                    **(
                        {"creationflags": subprocess.CREATE_NO_WINDOW}
                        if sys.platform == "win32"
                        else {}
                    ),
                )
                assert proc.stdout is not None
                for line in proc.stdout:
                    self._ui_log(line.rstrip("\n"))
                code = proc.wait()
                self._ui_log(f"[i] Excel готов (код {code}), папка: {EXPORTS_DIR}")
                if code == 0 and EXPORTS_DIR.exists() and sys.platform == "win32":
                    self.after(0, lambda: os.startfile(EXPORTS_DIR))  # type: ignore
            except Exception as exc:
                self._ui_log(f"[!] экспорт: {exc}")

        threading.Thread(target=work, daemon=True).start()

    def _on_close(self) -> None:
        if self._running:
            if not messagebox.askyesno(
                "Сбор идёт",
                "Остановить сбор и выйти?",
            ):
                return
            self._stop()
            if self._worker is not None:
                self._worker.join(timeout=8)
        self.destroy()


def run_gui() -> int:
    # На Windows консоль/пайпы UTF-8.
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
        except Exception:
            pass
    app = GuiApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    # Запуск как python src/gui_app.py
    src = Path(__file__).resolve().parent
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    raise SystemExit(run_gui())
