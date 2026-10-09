"""Local HTTP API + desktop UI shell for IG Tag Parser."""
from __future__ import annotations

import mimetypes
import os
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from pathlib import Path
from typing import Deque

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from paths import (
    ACCOUNTS_FILE,
    BASE_DIR,
    EXPORTS_DIR,
    PROXIES_FILE,
    REQ_FILE,
    TAGS_FILE,
    ensure_layout,
    invoke_cmd,
    resolve_proxies_file,
)
from settings import load_settings, save_settings

# Windows often maps .js -> text/plain; browsers then refuse ES modules.
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/wasm", ".wasm")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("application/json", ".json")
mimetypes.add_type("text/html", ".html")

UI_DIST = BASE_DIR / "ui" / "dist"


def resolve_ui_dist() -> Path:
    """Locate built React UI for both dev and frozen exe layouts."""
    candidates = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "ui" / "dist")
        candidates.append(Path(sys.executable).resolve().parent / "ui" / "dist")
        candidates.append(Path(sys.executable).resolve().parent / "_internal" / "ui" / "dist")
    candidates.append(BASE_DIR / "ui" / "dist")
    candidates.append(Path(__file__).resolve().parent.parent / "ui" / "dist")
    for path in candidates:
        if (path / "index.html").exists():
            return path
    return candidates[0]


UI_DIST = resolve_ui_dist()

FILE_MAP = {
    "tags": TAGS_FILE,
    "accounts": ACCOUNTS_FILE,
    "proxies": PROXIES_FILE,
}

_log_lock = threading.Lock()
_log_lines: Deque[tuple[int, str]] = deque(maxlen=5000)
_log_seq = 0
_worker: threading.Thread | None = None
_running = False
_run_lock = threading.Lock()
_run_file_lock = threading.Lock()


def _run_log_path() -> Path:
    return BASE_DIR / "data" / "run.log"


def _write_run_file(line: str) -> None:
    """Дублируем лог на диск — видно даже если UI «молчит»."""
    try:
        ensure_layout()
        path = _run_log_path()
        with _run_file_lock:
            with path.open("a", encoding="utf-8", errors="replace") as fh:
                fh.write(line + "\n")
                fh.flush()
    except Exception as exc:
        # Не глотаем молча — хотя бы в stderr / crash.
        try:
            sys.stderr.write(f"[run.log write fail] {exc}\n")
        except Exception:
            pass


def _append_log(msg: str) -> None:
    global _log_seq
    stamp = time.strftime("%H:%M:%S")
    to_file: list[str] = []
    with _log_lock:
        for line in str(msg).splitlines() or [""]:
            _log_seq += 1
            # Avoid double timestamps if a source already prefixed one.
            if len(line) >= 10 and line[0] == "[" and line[3] == ":" and line[6] == ":":
                text = line
            else:
                text = f"[{stamp}] {line}"
            _log_lines.append((_log_seq, text))
            to_file.append(text)
    # Пишем вне lock — иначе легко словить блокировку/потерю строк.
    for text in to_file:
        _write_run_file(text)


def _append_exception(prefix: str, exc: BaseException | None = None) -> None:
    import traceback

    if exc is not None:
        _append_log(f"{prefix}: {type(exc).__name__}: {exc}")
        _append_log(traceback.format_exc())
    else:
        _append_log(prefix)
        _append_log(traceback.format_exc())


def _clear_logs_for_new_run() -> int:
    """Очищает буфер UI-лога перед новым прогоном. Seq не сбрасываем."""
    with _log_lock:
        _log_lines.clear()
        marker = _log_seq
    try:
        ensure_layout()
        with _run_file_lock:
            with _run_log_path().open("a", encoding="utf-8", errors="replace") as fh:
                fh.write("\n" + "=" * 60 + "\n")
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} NEW RUN\n")
                fh.write("=" * 60 + "\n")
    except Exception:
        pass
    return marker


def _media_type(path: Path) -> str:
    ext = path.suffix.lower()
    forced = {
        ".js": "application/javascript",
        ".mjs": "application/javascript",
        ".css": "text/css",
        ".html": "text/html",
        ".svg": "image/svg+xml",
        ".json": "application/json",
        ".wasm": "application/wasm",
        ".map": "application/json",
    }
    if ext in forced:
        return forced[ext]
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _ui_file(rel: str) -> Path:
    global UI_DIST
    UI_DIST = resolve_ui_dist()
    base = UI_DIST.resolve()
    path = (UI_DIST / rel).resolve()
    if not str(path).startswith(str(base)) or not path.is_file():
        raise HTTPException(404, "not found")
    return path


def _append_log(msg: str) -> None:
    global _log_seq
    stamp = time.strftime("%H:%M:%S")
    with _log_lock:
        for line in str(msg).splitlines() or [""]:
            _log_seq += 1
            # Avoid double timestamps if a source already prefixed one.
            if len(line) >= 10 and line[0] == "[" and line[3] == ":" and line[6] == ":":
                text = line
            else:
                text = f"[{stamp}] {line}"
            _log_lines.append((_log_seq, text))


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = content.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8")


class FileBody(BaseModel):
    content: str = ""


class SettingsBody(BaseModel):
    max_accounts: int = Field(1, ge=1)
    retry_unfinished: bool = False
    only_ru: bool = True
    report_every_accounts: int = Field(10, ge=1)
    export_interval_sec: int = Field(10, ge=5)


app = FastAPI(title="IG Tag Parser")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_ui_file("index.html"), media_type="text/html")


@app.get("/assets/{file_path:path}")
def assets(file_path: str) -> FileResponse:
    path = _ui_file(f"assets/{file_path}")
    return FileResponse(path, media_type=_media_type(path))


@app.on_event("startup")
def _startup() -> None:
    ensure_layout()
    try:
        from platform_info import log_runtime_banner

        log_runtime_banner(_append_log)
    except Exception as exc:
        _append_log(f"[i] не удалось определить ОС: {exc}")


@app.get("/api/status")
def status() -> dict:
    from platform_info import runtime_summary

    size = REQ_FILE.stat().st_size if REQ_FILE.exists() else 0
    env = runtime_summary()
    return {
        "running": _running,
        "base_dir": str(BASE_DIR),
        "has_req": size >= 1000,
        "req_size": size,
        "os_label": env["os_label"],
        "os_build": env.get("os_build"),
        "webview2": env.get("webview2"),
        "platform_note": env["instagram_note"],
    }


@app.get("/api/files/{kind}")
def get_file(kind: str) -> dict:
    if kind == "proxies":
        path = resolve_proxies_file()
    else:
        path = FILE_MAP.get(kind)
    if path is None:
        raise HTTPException(404, "unknown file")
    return {"content": _read_text(path)}


@app.put("/api/files/{kind}")
def put_file(kind: str, body: FileBody) -> dict:
    path = FILE_MAP.get(kind)
    if path is None:
        raise HTTPException(404, "unknown file")
    content = body.content
    if kind == "accounts":
        from accounts import normalize_accounts_text
        content = normalize_accounts_text(content)
    _write_text(path, content)
    # Keep data/proxies.txt in sync when UI saves root proxies.txt
    if kind == "proxies":
        data_proxies = BASE_DIR / "data" / "proxies.txt"
        try:
            _write_text(data_proxies, body.content)
        except Exception:
            pass
    return {"ok": True, "content": content if kind == "accounts" else None}


@app.get("/api/settings")
def get_settings() -> dict:
    return load_settings()


@app.put("/api/settings")
def put_settings(body: SettingsBody) -> dict:
    data = body.model_dump()
    data["max_accounts"] = max(1, int(data["max_accounts"]))
    data["report_every_accounts"] = max(1, int(data["report_every_accounts"]))
    data["export_interval_sec"] = max(5, int(data["export_interval_sec"]))
    save_settings(data)
    return {"ok": True}


@app.put("/api/req")
def put_req(body: FileBody) -> dict:
    if len(body.content) < 1000:
        raise HTTPException(400, "req too short")
    _write_text(REQ_FILE, body.content)
    return {"ok": True, "size": len(body.content.encode("utf-8"))}


@app.get("/api/logs")
def get_logs(after: int = 0) -> dict:
    with _log_lock:
        lines = [text for seq, text in _log_lines if seq > after]
        next_seq = _log_seq
    return {"lines": lines, "next": next_seq, "running": _running}


def _progress_snapshot() -> dict:
    """Aggregate scrape progress for the GUI progress bar / stats line."""
    from accounts import load_accounts, load_proxy_lines
    from run_tags import get_runtime_accounts, read_tags, _count_accounts_file_lines
    from settings import load_settings
    from state_db import connect_readonly

    tags = read_tags(TAGS_FILE)
    tags_total = len(tags)
    accounts_file_n = len(load_accounts(ACCOUNTS_FILE))
    accounts_file_lines = _count_accounts_file_lines(ACCOUNTS_FILE)
    max_workers_setting = max(1, int(load_settings().get("max_accounts", 1)))
    proxies_file_n = len(load_proxy_lines(resolve_proxies_file()))

    accounts_collected = 0
    posts_total = 0
    pages_total = 0
    tags_finished = 0
    tags_running = 0
    proxies_alive = 0
    proxies_total = proxies_file_n
    accounts_alive = 0
    accounts_total = accounts_file_n

    con = connect_readonly()
    if con is not None:
        try:
            row = con.execute("SELECT COUNT(*) AS c FROM profiles").fetchone()
            accounts_collected = int(row["c"] or 0)

            tag_rows = con.execute(
                "SELECT query,status,page_num,total_items,total_accounts FROM tags"
            ).fetchall()
            by_query = {str(r["query"]): r for r in tag_rows}
            for tag in tags:
                query = tag if str(tag).startswith("#") else f"#{tag}"
                row = by_query.get(query) or by_query.get(tag)
                if row is None:
                    continue
                pages_total += int(row["page_num"] or 0)
                posts_total += int(row["total_items"] or 0)
                status = str(row["status"] or "")
                if status == "finished":
                    tags_finished += 1
                elif status == "running":
                    tags_running += 1

            proxy_rows = con.execute(
                "SELECT status, COUNT(*) AS c FROM proxies GROUP BY status"
            ).fetchall()
            proxy_counts = {str(r["status"]): int(r["c"] or 0) for r in proxy_rows}
            db_proxy_total = sum(proxy_counts.values())
            if db_proxy_total:
                proxies_total = db_proxy_total
                proxies_alive = (
                    proxy_counts.get("available", 0)
                    + proxy_counts.get("assigned", 0)
                )
            else:
                proxies_alive = proxies_file_n

            acct_rows = con.execute(
                "SELECT status, COUNT(*) AS c FROM accounts GROUP BY status"
            ).fetchall()
            acct_counts = {str(r["status"]): int(r["c"] or 0) for r in acct_rows}
            db_acct_total = sum(acct_counts.values())
            if db_acct_total:
                accounts_total = max(accounts_file_n, db_acct_total)
                accounts_alive = (
                    acct_counts.get("ready", 0)
                    + acct_counts.get("waiting_proxy", 0)
                )
            else:
                accounts_alive = accounts_file_n
        except Exception:
            pass
        finally:
            try:
                con.close()
            except Exception:
                pass
    else:
        proxies_alive = proxies_file_n
        accounts_alive = accounts_file_n

    runtime_alive, runtime_total, runtime_max_workers = get_runtime_accounts()
    if runtime_total > 0:
        accounts_alive = runtime_alive
        accounts_total = runtime_total

    pct = 0.0
    if tags_total > 0:
        # Running tag counts as half-done so the bar moves before finish.
        pct = min(100.0, 100.0 * (tags_finished + 0.5 * tags_running) / tags_total)

    workers_limit = runtime_max_workers if runtime_max_workers > 0 else min(
        accounts_file_n, max_workers_setting
    )

    return {
        "running": _running,
        "tags_total": tags_total,
        "tags_finished": tags_finished,
        "tags_running": tags_running,
        "tags_pct": round(pct, 1),
        "accounts_collected": accounts_collected,
        "posts_total": posts_total,
        "pages_total": pages_total,
        "accounts_alive": accounts_alive,
        "accounts_total": accounts_total,
        "accounts_pool_loaded": accounts_file_n,
        "accounts_file_lines": accounts_file_lines,
        "max_workers": workers_limit,
        "max_workers_setting": max_workers_setting,
        "proxies_alive": proxies_alive,
        "proxies_total": proxies_total,
    }


@app.get("/api/progress")
def get_progress() -> dict:
    return _progress_snapshot()


@app.post("/api/run")
def start_run() -> dict:
    global _worker, _running
    with _run_lock:
        alive = bool(_worker and _worker.is_alive())
        if _running and alive:
            _append_log(
                "[!] повторный Старт: сбор УЖЕ идёт "
                f"(thread={getattr(_worker, 'name', '?')} alive={alive})"
            )
            return {
                "ok": False,
                "already": True,
                "error": "Сбор уже выполняется — нажмите Стоп, затем Старт",
            }
        if _running and not alive:
            _append_log(
                "[!] флаг running=True, но поток мёртв — принудительный перезапуск"
            )
            _running = False
        _running = True

    cursor_marker = _clear_logs_for_new_run()
    result_box: dict = {"code": None}

    def work() -> None:
        global _running
        import traceback

        def gui_log(msg: str, *, error: bool = False) -> None:
            _append_log(msg)

        try:
            _append_log(f"[dbg] worker thread start pid={os.getpid()}")
            settings = load_settings()
            _append_log(f"[dbg] settings={settings!r}")
            env_extra = {
                "MAX_WORKERS": str(max(1, int(settings.get("max_accounts", 1)))),
                "MAX_CONSECUTIVE_FAILURES": "10",
                "REPORT_EVERY_ACCOUNTS": str(
                    max(1, int(settings.get("report_every_accounts", 10)))
                ),
                "EXPORT_INTERVAL_SEC": str(
                    max(5, int(settings.get("export_interval_sec", 10)))
                ),
                "STALL_WINDOW": str(
                    max(1, int(settings.get("stall_window", 20)))
                ),
                "STALL_MIN_ACCOUNTS": str(
                    max(1, int(settings.get("stall_min_accounts", 3)))
                ),
            }
            if settings.get("retry_unfinished"):
                env_extra["RETRY_UNFINISHED"] = "1"
            if settings.get("only_ru", True):
                env_extra["ONLY_RU"] = "1"

            old_env = {k: os.environ.get(k) for k in env_extra}
            old_inprocess = os.environ.get("IG_INPROCESS_PROBE")
            try:
                import run_tags

                max_workers = int(env_extra["MAX_WORKERS"])
                if max_workers <= 1:
                    os.environ["IG_INPROCESS_PROBE"] = "1"
                else:
                    os.environ.pop("IG_INPROCESS_PROBE", None)
                for k, v in env_extra.items():
                    os.environ[k] = v
                run_tags.log = gui_log
                run_tags.reset_stop()
                run_tags.MAX_WORKERS = max_workers
                run_tags.RETRY_UNFINISHED = os.getenv("RETRY_UNFINISHED") == "1"
                run_tags.MAX_CONSECUTIVE_FAILURES = int(
                    os.getenv("MAX_CONSECUTIVE_FAILURES", "3")
                )
                run_tags._last_live_count = -1
                _append_log("=" * 50)
                _append_log("СТАРТ СБОРА")
                try:
                    from platform_info import log_runtime_banner

                    log_runtime_banner(_append_log)
                except Exception:
                    pass
                _append_log(f"[dbg] BASE_DIR={BASE_DIR}")
                _append_log(f"[dbg] TAGS_FILE={TAGS_FILE} exists={TAGS_FILE.exists()}")
                _append_log(
                    f"[dbg] ACCOUNTS_FILE={ACCOUNTS_FILE} exists={ACCOUNTS_FILE.exists()}"
                )
                _append_log(
                    f"[dbg] REQ_FILE={REQ_FILE} size="
                    f"{REQ_FILE.stat().st_size if REQ_FILE.exists() else 0}"
                )
                _append_log(f"[dbg] env_extra={env_extra}")
                if max_workers > 1:
                    _append_log(
                        f"[i] параллельность: {max_workers} процессов probe"
                    )
                _append_log("=" * 50)
                _append_log("[dbg] вызываю run_tags.main()…")
                code = int(run_tags.main())
                result_box["code"] = code
                _append_log(f"[i] сбор завершён, код {code}")
                if code == 10:
                    _append_log(
                        "[!] IDLE: все теги уже finished — нажмите «Сброс БД», "
                        "чтобы собрать заново"
                    )
            except BaseException as exc:  # noqa: BLE001
                result_box["code"] = -1
                _append_exception("[!] ошибка в run_tags.main()", exc)
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
        except BaseException as exc:  # noqa: BLE001
            result_box["code"] = -1
            _append_exception("[!] критическая ошибка worker-потока", exc)
            _append_log(traceback.format_exc())
        finally:
            with _run_lock:
                _running = False
            _append_log("[dbg] worker thread end, running=False")

    _worker = threading.Thread(target=work, name="ig-run-worker", daemon=True)
    _worker.start()
    # Коротко ждём мгновенный IDLE (все теги finished) — чтобы UI сразу
    # получил понятный ответ, а не «запущен» на пустом месте.
    _worker.join(timeout=2.5)
    finished_fast = not _worker.is_alive()
    code = result_box.get("code")
    _append_log(
        f"[dbg] thread started name={_worker.name} ident={_worker.ident} "
        f"log_cursor_marker={cursor_marker} fast_done={finished_fast} code={code}"
    )
    if finished_fast and code == 10:
        return {
            "ok": False,
            "already": False,
            "idle": True,
            "code": 10,
            "error": (
                "Все теги уже собраны (finished). "
                "Нажмите «Сброс БД», затем Старт — либо добавьте новые теги."
            ),
            "log_cursor": cursor_marker,
            "run_log": str(_run_log_path()),
        }
    return {
        "ok": True,
        "already": False,
        "idle": False,
        "code": code,
        "log_cursor": cursor_marker,
        "run_log": str(_run_log_path()),
    }


@app.post("/api/stop")
def stop_run() -> dict:
    global _running
    try:
        import run_tags

        run_tags.request_stop()
        _append_log("[!] остановка запрошена…")
        alive = bool(_worker and _worker.is_alive())
        _append_log(f"[dbg] stop: running={_running} worker_alive={alive}")
        if _running and not alive:
            with _run_lock:
                _running = False
            _append_log("[dbg] stop: сброшен зависший running=False")
    except Exception as exc:
        _append_exception("[!] stop", exc)
    return {"ok": True, "running": _running}


class ClientLogBody(BaseModel):
    level: str = "error"
    message: str = ""
    stack: str = ""
    context: str = ""


@app.post("/api/client-log")
def client_log(body: ClientLogBody) -> dict:
    level = (body.level or "error").upper()
    _append_log(f"[ui:{level}] {body.context or 'ui'}: {body.message}")
    if body.stack:
        _append_log(body.stack)
    return {"ok": True}


@app.post("/api/export")
def export_xlsx() -> dict:
    if _running:
        raise HTTPException(409, "busy")

    def work() -> None:
        env = {**os.environ}
        if load_settings().get("only_ru", True):
            env["ONLY_RU"] = "1"
        else:
            env.pop("ONLY_RU", None)
        try:
            kwargs = {
                "env": env,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "cwd": str(BASE_DIR),
            }
            if sys.platform == "win32":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.Popen(invoke_cmd("export_xlsx"), **kwargs)
            assert proc.stdout is not None
            for line in proc.stdout:
                _append_log(line.rstrip("\n"))
            code = proc.wait()
            _append_log(f"[i] Excel готов (код {code}), папка: {EXPORTS_DIR}")
            if code == 0 and EXPORTS_DIR.exists() and sys.platform == "win32":
                os.startfile(EXPORTS_DIR)  # type: ignore[attr-defined]
        except Exception as exc:
            _append_log(f"[!] экспорт: {exc}")

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True}


@app.post("/api/open-data")
def open_data() -> dict:
    path = BASE_DIR
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])
    return {"ok": True}


@app.post("/api/reset-db")
def reset_db() -> dict:
    """Wipe SQLite only. Archive live Excel so it is not overwritten later."""
    if _running:
        raise HTTPException(409, "Сначала остановите сбор")

    archived = None
    try:
        from export_xlsx import archive_live_excel

        archived_path = archive_live_excel()
        if archived_path is not None:
            archived = archived_path.name
            _append_log(f"[i] Excel сохранён как {archived}")
    except Exception as exc:
        _append_log(f"[!] не удалось архивировать Excel: {exc}")

    try:
        from state_db import reset_database

        removed = reset_database()
    except Exception as exc:
        raise HTTPException(500, f"Не удалось сбросить БД: {exc}") from exc

    try:
        import run_tags

        run_tags.clear_runtime_progress()
        run_tags._last_live_count = -1
    except Exception:
        pass

    _append_log("[i] БД и прогресс тегов сброшены (Excel и конфиги на месте)")
    return {
        "ok": True,
        "removed": removed,
        "archived_excel": archived,
    }


def _wait_http(url: str, timeout: float = 15.0) -> bool:
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status < 500:
                    return True
        except Exception:
            time.sleep(0.15)
    return False


def _port_free(host: str, port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def _write_crash(message: str) -> Path:
    ensure_layout()
    path = BASE_DIR / "data" / "crash.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n{message}\n"
    try:
        prev = path.read_text(encoding="utf-8") if path.exists() else ""
    except Exception:
        prev = ""
    path.write_text(text + "\n" + prev[:8000], encoding="utf-8")
    return path


def _show_error(title: str, message: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
    except Exception:
        try:
            print(f"[!] {title}: {message}", flush=True)
        except Exception:
            pass


def run_ui_server(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False) -> int:
    import uvicorn

    global UI_DIST

    # Windowed PyInstaller exe sets stdout/stderr to None; uvicorn logging
    # calls sys.stdout.isatty() and crashes without a real stream.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")

    ensure_layout()
    UI_DIST = resolve_ui_dist()
    if not (UI_DIST / "index.html").exists():
        msg = (
            f"Не найден интерфейс UI:\n{UI_DIST}\n\n"
            "Запускайте exe из папки dist\\IGTagParser целиком "
            "(нужна папка _internal)."
        )
        _write_crash(msg)
        _show_error("IG Tag Parser", msg)
        return 1

    chosen = None
    for candidate in range(port, port + 15):
        if _port_free(host, candidate):
            chosen = candidate
            break
    if chosen is None:
        msg = f"Нет свободного порта около {port}. Закройте старый IGTagParser и повторите."
        _write_crash(msg)
        _show_error("IG Tag Parser", msg)
        return 1

    url = f"http://{host}:{chosen}/"
    config = uvicorn.Config(
        app,
        host=host,
        port=chosen,
        log_level="warning",
        log_config=None,
    )
    server = uvicorn.Server(config)
    boot_error: list[BaseException] = []

    def _serve() -> None:
        try:
            server.run()
        except BaseException as exc:  # noqa: BLE001 — capture bind failures
            boot_error.append(exc)

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    if not _wait_http(url):
        detail = f"{boot_error[0]!r}" if boot_error else "таймаут старта"
        msg = f"Не удалось запустить локальный сервер UI ({url}): {detail}"
        _write_crash(msg)
        _show_error("IG Tag Parser", msg)
        return 1

    try:
        print(f"[i] desktop UI: {url}", flush=True)
    except Exception:
        pass

    # Native desktop window (not a browser tab).
    used_browser = False
    started_at = time.time()
    try:
        import webview

        webview.create_window(
            "IG Tag Parser",
            url,
            width=1200,
            height=800,
            min_size=(920, 620),
            background_color="#EEF1F5",
        )
        webview.start()
        # Instant return usually means WebView2 failed to show a window.
        if time.time() - started_at < 1.5:
            raise RuntimeError("webview closed immediately")
    except Exception as exc:
        used_browser = True
        try:
            print(f"[!] webview unavailable ({exc}); fallback browser", flush=True)
        except Exception:
            pass
        webbrowser.open(url)
        try:
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            from platform_info import runtime_summary, webview2_installed

            env = runtime_summary()
            wv2_tip = ""
            if webview2_installed() is False:
                wv2_tip = (
                    "\n\nНа Windows 10 часто нужен «Microsoft Edge WebView2 Runtime» "
                    "(Evergreen Standalone) — тогда откроется встроенное окно, "
                    "а не браузер."
                )
            messagebox.showinfo(
                "IG Tag Parser",
                f"Окно WebView не открылось — интерфейс запущен в браузере.\n"
                f"{url}\n\nОС: {env['short']}.{wv2_tip}\n\n"
                "Сервер продолжит работу, пока открыт этот диалог "
                "или пока вы не закроете процесс.",
            )
            root.destroy()
        except Exception:
            pass
        while thread.is_alive():
            time.sleep(0.5)

    if not used_browser:
        server.should_exit = True
        thread.join(timeout=3)
    return 0
