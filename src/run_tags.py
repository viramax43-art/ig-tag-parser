#!/usr/bin/env python3
"""
Прогон по списку тегов. Читает data/tags.txt, гонит probe.py по каждому тегу,
пропускает уже добитые, переживает обрывы.

  python src/run_tags.py                — все теги из data/tags.txt
  python src/run_tags.py data/pilot.txt — свой файл со списком
  RETRY_UNFINISHED=1 python src/run_tags.py — добить прерванные теги

Если есть data/accounts.txt (username;password;2fa;proxy) — параллельный
режим: один поток на строку, свой логин + свой IP.

Формат файла тегов: один тег на строку, решётка необязательна.
Строки, начинающиеся с //, игнорируются.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from paths import (
    ACCOUNTS_FILE,
    BASE_DIR,
    DATA_DIR,
    REQ_FILE as TEMPLATE_REQ,
    SRC_DIR,
    TAGS_DIR,
    TAGS_FILE as DEFAULT_LIST,
    invoke_cmd,
)
from proxy_pool import assign_account_proxy, replace_account_proxy, sync_proxy_pool
from state_db import DB

sys.path.insert(0, str(SRC_DIR))

PROBE = SRC_DIR / "probe.py"
REFRESH = SRC_DIR / "refresh_tokens.py"

STALL_WINDOW = os.getenv("STALL_WINDOW", "20")
STALL_MIN_ACCOUNTS = os.getenv("STALL_MIN_ACCOUNTS", "3")
PAUSE_BETWEEN = int(os.getenv("PAUSE_BETWEEN", "5"))
RETRY_UNFINISHED = os.getenv("RETRY_UNFINISHED") == "1"
MAX_CONSECUTIVE_FAILURES = int(os.getenv("MAX_CONSECUTIVE_FAILURES", "3"))
AUTO_REFRESH = os.getenv("AUTO_REFRESH", "1") == "1"
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "0"))  # legacy override; GUI/settings has priority
EXPORT_INTERVAL_SEC = int(os.getenv("EXPORT_INTERVAL_SEC", "15"))


def configured_max_workers() -> int:
    """Active account concurrency; GUI/settings can change before the next run."""
    try:
        from settings import load_settings
        value = int(load_settings().get("max_accounts", 1))
    except Exception:
        value = 1
    if MAX_WORKERS > 0:
        value = MAX_WORKERS
    return max(1, value)


def configured_export_interval() -> int:
    env = int(os.getenv("EXPORT_INTERVAL_SEC", "0") or 0)
    if env > 0:
        return max(5, env)
    try:
        from settings import load_settings
        return max(5, int(load_settings().get("export_interval_sec", 10)))
    except Exception:
        return max(5, EXPORT_INTERVAL_SEC)


def _apply_export_env_from_settings() -> None:
    try:
        from settings import load_settings
        settings = load_settings()
        if settings.get("only_ru", True):
            os.environ["ONLY_RU"] = "1"
        else:
            os.environ.pop("ONLY_RU", None)
        os.environ["REPORT_EVERY_ACCOUNTS"] = str(
            max(1, int(settings.get("report_every_accounts", 10)))
        )
        os.environ["EXPORT_INTERVAL_SEC"] = str(
            max(5, int(settings.get("export_interval_sec", 10)))
        )
    except Exception:
        os.environ.setdefault("REPORT_EVERY_ACCOUNTS", "10")
        os.environ.setdefault("EXPORT_INTERVAL_SEC", "10")


_last_live_count = -1
_export_busy = threading.Lock()


def _live_export_once(*, quiet: bool = True) -> None:
    global _last_live_count
    if not _export_busy.acquire(blocking=False):
        return
    try:
        from export_xlsx import LIVE_NAME, export_live
        # Offload Excel rewrite so scrape workers are not stalled by GIL/IO.
        code, path, count = export_live(quiet=True)
        if code == 0 and count and count != _last_live_count:
            _last_live_count = count
            log(f"[excel] {LIVE_NAME}: {count} аккаунтов")
        elif code == 2:
            log(f"[excel] файл занят Excel — пропуск тика ({LIVE_NAME})")
    except Exception as exc:
        log(f"[!] live excel: {exc}", error=True)
    finally:
        _export_busy.release()


def start_live_excel_exporter() -> threading.Event:
    """Background rewrite of accounts_live.xlsx from SQLite every N seconds."""
    stop = threading.Event()
    interval = configured_export_interval()

    def loop() -> None:
        log(f"[excel] автовыгрузка из SQLite каждые {interval} с -> data/exports/accounts_live.xlsx")
        _live_export_once(quiet=True)
        while not stop.wait(interval):
            _live_export_once(quiet=True)

    threading.Thread(target=loop, name="live-excel", daemon=True).start()
    return stop

print_lock = threading.Lock()
_active_procs: list = []
_procs_lock = threading.Lock()
_stop_flag = threading.Event()
_log_file = None

# Live worker health for GUI progress.
_progress_lock = threading.Lock()
_runtime_alive: dict[str, bool] = {}
_runtime_accounts_total = 0
_runtime_accounts_active = 0
_runtime_max_workers = 0


def clear_runtime_progress() -> None:
    global _runtime_alive, _runtime_accounts_total, _runtime_accounts_active, _runtime_max_workers
    with _progress_lock:
        _runtime_alive = {}
        _runtime_accounts_total = 0
        _runtime_accounts_active = 0
        _runtime_max_workers = 0


def set_runtime_workers(
    alive: dict[str, bool],
    *,
    pool_total: int | None = None,
    max_workers: int | None = None,
) -> None:
    """alive = currently healthy active slots; pool_total = accounts in file."""
    global _runtime_alive, _runtime_accounts_total, _runtime_accounts_active, _runtime_max_workers
    with _progress_lock:
        _runtime_alive = dict(alive)
        _runtime_accounts_active = sum(1 for ok in alive.values() if ok)
        if pool_total is not None:
            _runtime_accounts_total = max(0, int(pool_total))
        elif _runtime_accounts_total <= 0:
            _runtime_accounts_total = len(alive)
        if max_workers is not None:
            _runtime_max_workers = max(0, int(max_workers))


def sync_runtime_alive(
    alive: dict[str, bool],
    *,
    pool_total: int | None = None,
) -> None:
    global _runtime_alive, _runtime_accounts_active, _runtime_accounts_total
    with _progress_lock:
        _runtime_alive = dict(alive)
        _runtime_accounts_active = sum(1 for ok in alive.values() if ok)
        if pool_total is not None:
            _runtime_accounts_total = max(0, int(pool_total))


def get_runtime_accounts() -> tuple[int, int, int]:
    """Return (active_alive, pool_total, max_workers)."""
    with _progress_lock:
        total = _runtime_accounts_total
        if total <= 0 and not _runtime_alive:
            return 0, 0, _runtime_max_workers
        alive_n = _runtime_accounts_active
        if total <= 0:
            total = len(_runtime_alive)
        return alive_n, total, _runtime_max_workers


def _ensure_log_file():
    global _log_file
    if _log_file is not None:
        return _log_file
    try:
        from paths import DATA_DIR
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _log_file = open(DATA_DIR / "run.log", "a", encoding="utf-8", buffering=1)
    except Exception:
        _log_file = False
    return _log_file


def log(msg: str, *, error: bool = False) -> None:
    with print_lock:
        # В windowed .exe print в «никуда» может зависнуть на заполненном буфере.
        if not getattr(sys, "frozen", False):
            stream = sys.stderr if error else sys.stdout
            try:
                print(msg, file=stream, flush=True)
            except Exception:
                try:
                    enc = getattr(stream, "encoding", None) or "utf-8"
                    safe = msg.encode(enc, errors="replace").decode(enc, errors="replace")
                    print(safe, file=stream, flush=True)
                except Exception:
                    pass
        fh = _ensure_log_file()
        if fh:
            try:
                fh.write(msg + "\n")
                fh.flush()
            except Exception:
                pass


def request_stop() -> None:
    _stop_flag.set()
    try:
        import probe as probe_mod
        probe_mod.set_stop_checker(lambda: True)
    except Exception:
        pass
    with _procs_lock:
        procs = list(_active_procs)
    for proc in procs:
        try:
            proc.terminate()
        except Exception:
            pass
    for proc in procs:
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass


def reset_stop() -> None:
    _stop_flag.clear()
    clear_runtime_progress()
    try:
        import probe as probe_mod
        probe_mod.set_stop_checker(lambda: _stop_flag.is_set())
    except Exception:
        pass


def _kill_proc(proc) -> None:
    try:
        if proc.poll() is not None:
            return
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


def _popen_kwargs() -> dict:
    kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if sys.platform == "win32":
        # Не всплывать чёрные окна консоли из GUI / windowed exe.
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return kwargs


def _run_streaming(cmd: list, env: dict = None, timeout: int = None) -> int:
    """Запуск дочернего probe-процесса. Лог в файл + live-tail в GUI."""
    if _stop_flag.is_set():
        return 130

    from paths import DATA_DIR
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / (
        f"_worker_{os.getpid()}_{threading.get_ident()}_{time.time_ns()}.log"
    )

    out_f = open(out_path, "w", encoding="utf-8", errors="replace", buffering=1)
    kwargs: dict = {
        "stdout": out_f,
        "stderr": subprocess.STDOUT,
        "close_fds": False,
    }
    if sys.platform == "win32":
        # Нужен и для frozen/windowed exe — иначе дочерний процесс зависает.
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        proc = subprocess.Popen(cmd, env=env or os.environ, **kwargs)
    except Exception as exc:
        try:
            out_f.close()
        except Exception:
            pass
        log(f"[!] не удалось запустить {cmd}: {exc}", error=True)
        return 1

    with _procs_lock:
        _active_procs.append(proc)

    last_pos = 0

    def _tail() -> None:
        nonlocal last_pos
        try:
            with open(out_path, "r", encoding="utf-8", errors="replace") as rf:
                rf.seek(last_pos)
                chunk = rf.read()
                last_pos = rf.tell()
            if chunk:
                for line in chunk.splitlines():
                    log(line)
        except Exception:
            pass

    try:
        t0 = time.time()
        deadline = (t0 + timeout) if timeout else (t0 + 3600)
        last_beat = t0
        beat_tag = ""
        try:
            beat_tag = (env or {}).get("TAG") or ""
        except Exception:
            beat_tag = ""
        while proc.poll() is None:
            if _stop_flag.is_set():
                _kill_proc(proc)
                break
            if time.time() > deadline:
                try:
                    proc.kill()
                except Exception:
                    pass
                break
            _tail()
            now = time.time()
            if now - last_beat >= 45:
                suffix = f" tag=#{beat_tag}" if beat_tag else ""
                log(f"[i] probe ещё работает… ({int(now - t0)}с){suffix}")
                last_beat = now
            time.sleep(0.2)
        try:
            code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                code = proc.wait(timeout=5)
            except Exception:
                code = 124
        _tail()
        if _stop_flag.is_set():
            return 130
        return code if code is not None else 124
    finally:
        with _procs_lock:
            if proc in _active_procs:
                _active_procs.remove(proc)
        try:
            out_f.close()
        except Exception:
            pass
        try:
            _tail()
        except Exception:
            pass
        try:
            out_path.unlink(missing_ok=True)
        except Exception:
            pass


def slugify(query: str) -> str:
    cleaned = [c if c.isalnum() else "_" for c in query.lstrip("#").lower()]
    return "".join(cleaned).strip("_") or "unnamed"


def read_tags(path: Path) -> list:
    if not path.exists():
        log(f"[!] нет файла {path}", error=True)
        log("    создайте его: один тег на строку", error=True)
        return []
    raw = path.read_text(encoding="utf-8-sig")
    tags, seen = [], set()
    for line in raw.splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line or line.startswith("//"):
            continue
        tag = line.lstrip("#").strip().replace("\ufeff", "").strip()
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            tags.append(tag)
    return tags


def tag_state(tag: str) -> dict:
    """Состояние тега. SQLite — источник истины; state.json только fallback."""
    query = tag if str(tag).startswith("#") else f"#{tag}"
    try:
        durable = DB.load_tag(query)
        if durable:
            return {
                "query": query,
                "cursor_key": durable.get("cursor_key"),
                "end_cursor": durable.get("end_cursor"),
                "page_num": int(durable.get("page_num") or 0),
                "total_items": int(durable.get("total_items") or 0),
                "total_videos": int(durable.get("total_videos") or 0),
                "total_accounts": int(durable.get("total_accounts") or 0),
                "finished": durable.get("status") == "finished",
                "stop_reason": durable.get("stop_reason"),
            }
    except Exception:
        pass

    path = TAGS_DIR / slugify(tag) / "state.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}

    # Orphan JSON: finished без строки в SQLite (пример: «тег пуст» с 0 аккаунтов)
    # блокировал тег, которого нет в Excel/БД.
    if data.get("finished"):
        data = dict(data)
        data["finished"] = False
        reason = (data.get("stop_reason") or "").strip()
        data["stop_reason"] = (
            f"{reason} [stale state.json ignored: нет в SQLite]".strip()
        )
    return data


def refresh_tokens() -> bool:
    """Обновляет fb_dtsg/lsd в req.sh (однопоточный режим)."""
    if not AUTO_REFRESH:
        return False
    if not getattr(sys, "frozen", False) and not REFRESH.exists():
        return False
    try:
        code = _run_streaming(invoke_cmd("refresh_tokens") + ["--write"], timeout=120)
    except Exception as exc:
        log(f"    [t] обновление токенов не удалось: {exc}", error=True)
        return False

    if code == 0:
        log("    [t] токены обновлены")
        return True

    log(f"    [t] токены не обновились: код {code}", error=True)
    log("        продолжаю со старыми — может потребоваться свежий cURL",
        error=True)
    return False


def run_one(tag: str, curl_file: Path = None, proxy_url: str = "") -> int:
    if _stop_flag.is_set():
        return 130

    # In-process только по явному флагу (обычно 1 аккаунт).
    # Мультиаккаунт → отдельные процессы: иначе GIL + общий probe = «однопоток».
    inprocess = os.getenv("IG_INPROCESS_PROBE") == "1"
    if inprocess:
        try:
            import probe as probe_mod
            os.environ["STALL_WINDOW"] = str(STALL_WINDOW)
            os.environ["STALL_MIN_ACCOUNTS"] = str(STALL_MIN_ACCOUNTS)
            probe_mod.set_stop_checker(lambda: _stop_flag.is_set())
            tag_clean = tag.lstrip("#").replace("\ufeff", "").strip()

            def probe_log(msg: str, *, error: bool = False) -> None:
                log(f"[#{tag_clean}] {msg}", error=error)

            probe_mod.set_log_fn(probe_log)
            t0 = time.perf_counter()
            log(
                f"[i] probe start (in-process) tag=#{tag_clean}"
                + (f" curl={curl_file}" if curl_file else "")
                + (
                    f" proxy={proxy_url.split('@')[-1] if proxy_url and '@' in proxy_url else (proxy_url or 'direct')}"
                )
            )
            try:
                if _stop_flag.is_set():
                    return 130
                code = int(
                    probe_mod.main(
                        tag=tag_clean,
                        curl_file=curl_file,
                        proxy_url=proxy_url or "",
                    )
                )
            finally:
                probe_mod.set_log_fn(None)
            elapsed = time.perf_counter() - t0
            if _stop_flag.is_set():
                return 130
            log(
                f"[i] probe finished code={code} tag=#{tag_clean} "
                f"elapsed={elapsed:.1f}s"
            )
            return code
        except Exception as exc:
            if _stop_flag.is_set():
                return 130
            if exc.__class__.__name__ == "StopRequested":
                return 130
            log(f"[!] probe: {exc}", error=True)
            return 1

    tag_clean = tag.lstrip("#").replace("\ufeff", "").strip()
    env = {
        **os.environ,
        "TAG": tag_clean,
        "STALL_WINDOW": str(STALL_WINDOW),
        "STALL_MIN_ACCOUNTS": str(STALL_MIN_ACCOUNTS),
    }
    env.pop("IG_INPROCESS_PROBE", None)
    # Общий cursor key для дочерних процессов (обычно "after").
    cursor_key = (os.getenv("IG_CURSOR_KEY") or "").strip()
    if not cursor_key:
        try:
            from paths import DATA_DIR
            p = DATA_DIR / "cursor_key.txt"
            if p.exists():
                cursor_key = p.read_text(encoding="utf-8").strip().splitlines()[0]
        except Exception:
            cursor_key = ""
    env["IG_CURSOR_KEY"] = cursor_key or "after"
    if curl_file is not None:
        env["CURL_FILE"] = str(curl_file)
    if proxy_url:
        env["PROXY_URL"] = proxy_url
        proxy_shown = proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url
    else:
        env.pop("PROXY_URL", None)
        proxy_shown = "direct"
    log(
        f"[i] probe start (process) tag=#{tag_clean} proxy={proxy_shown}"
        + (f" curl={curl_file}" if curl_file else "")
    )
    t0 = time.perf_counter()
    code = _run_streaming(invoke_cmd("probe"), env=env)
    elapsed = time.perf_counter() - t0
    if _stop_flag.is_set():
        return 130
    log(
        f"[i] probe finished code={code} tag=#{tag_clean} "
        f"elapsed={elapsed:.1f}s"
    )
    return code


# ---------------------------------------------------------------------------
# Однопоточный режим (как раньше) — нет data/accounts.txt
# ---------------------------------------------------------------------------

def run_single(tags: list) -> int:
    log(f"[i] режим: один аккаунт (data/req.sh)")
    log(f"[i] отсечка: {STALL_WINDOW} страниц / {STALL_MIN_ACCOUNTS} аккаунтов")
    log(f"[i] автообновление токенов: {'вкл' if AUTO_REFRESH else 'выкл'}")
    log("")

    started = time.time()
    done = skipped = failed = consecutive_failures = 0

    for index, tag in enumerate(tags, 1):
        state = tag_state(tag)
        if state.get("finished") and not RETRY_UNFINISHED:
            log(f"[{index}/{len(tags)}] #{tag} — уже добит "
                f"({state.get('total_accounts', 0)} аккаунтов), пропуск")
            skipped += 1
            continue
        if state and not state.get("finished"):
            log(f"[{index}/{len(tags)}] #{tag} — продолжаю "
                f"со страницы {state.get('page_num', 0) + 1}")
        else:
            log(f"[{index}/{len(tags)}] #{tag} — старт")

        refresh_tokens()
        if _stop_flag.is_set():
            log("[!] остановлено пользователем")
            break

        code = run_one(tag)

        if code == 130 or _stop_flag.is_set():
            log("[!] остановлено пользователем")
            break

        if code == 6 and AUTO_REFRESH:
            log("    [t] сессия отвалилась, обновляю токены и повторяю тег")
            if refresh_tokens():
                code = run_one(tag)
            if code == 130 or _stop_flag.is_set():
                log("[!] остановлено пользователем")
                break

        if code == 1:
            log("")
            log("=" * 60)
            log("ПРОГОН ОСТАНОВЛЕН: НЕТ ВАЛИДНОГО data/req.sh")
            log("=" * 60)
            return 1

        if code == 6:
            log("")
            log("=" * 60)
            log("ПРОГОН ОСТАНОВЛЕН: СЕССИЯ НЕДЕЙСТВИТЕЛЬНА")
            log("=" * 60)
            log("  Обновите сессию или заполните data/accounts.txt")
            return 6

        after = tag_state(tag)
        if after.get("finished"):
            done += 1
            consecutive_failures = 0
            log(f"    готово: {after.get('total_accounts', 0)} аккаунтов, "
                f"{after.get('page_num', 0)} страниц")
        else:
            failed += 1
            consecutive_failures += 1
            log(f"    ПРЕРВАНО (код {code}). Добить: "
                f"RETRY_UNFINISHED=1 python src/run_tags.py", error=True)
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                log("")
                log(f"[!] {consecutive_failures} тега подряд прервались — "
                    f"останавливаюсь.", error=True)
                break

        if index < len(tags):
            if _stop_flag.is_set():
                log("[!] остановлено пользователем")
                break
            log(f"    пауза {PAUSE_BETWEEN} с перед следующим тегом")
            log("")
            for _ in range(PAUSE_BETWEEN):
                if _stop_flag.is_set():
                    break
                time.sleep(1)
            if _stop_flag.is_set():
                break

    elapsed = (time.time() - started) / 60
    log("")
    log("=" * 60)
    log("ПРОГОН ЗАВЕРШЁН")
    log("=" * 60)
    log(f"  добито:   {done}")
    log(f"  пропущено:{skipped}")
    log(f"  прервано: {failed}")
    log(f"  времени:  {elapsed:.0f} мин")
    return 0


# ---------------------------------------------------------------------------
# Многопоточный режим — data/accounts.txt
# ---------------------------------------------------------------------------

def prepare_worker_session(account, force: bool = False):
    """Login and build a private request template using a persistent proxy binding."""
    if _stop_flag.is_set():
        raise RuntimeError("остановлено")
    from login import login, relogin
    from session_req import build_session_req

    # Preferred proxy from accounts.txt is only a seed. SQLite owns the exclusive
    # assignment and keeps it stable between restarts.
    assigned_raw = assign_account_proxy(account.username, account.proxy_raw)
    if not assigned_raw:
        raise RuntimeError("нет свободного исправного прокси")
    account.proxy_raw = assigned_raw
    if not account.proxy_url:
        raise RuntimeError("прокси не назначен — прямой доступ запрещён")
    cookies = relogin(account) if force else login(account, force=False)
    if _stop_flag.is_set():
        raise RuntimeError("остановлено")
    return build_session_req(account, cookies, template_path=TEMPLATE_REQ)


def _count_accounts_file_lines(path: Path) -> int:
    if not path.exists():
        return 0
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#") and not s.startswith("//"):
            n += 1
    return n


def worker_slot(
    slot_id: int,
    tag_q: "queue.Queue",
    account_q: "queue.Queue",
    stats: dict,
    alive_slots: dict,
    pool_total: int,
    tags_left: list,
    left_lock: threading.Lock,
    tag_attempts: dict,
    attempts_lock: threading.Lock,
    cooldown: list,
    cooldown_lock: threading.Lock,
) -> None:
    """One concurrent slot: pulls tags from a shared queue, swaps accounts on death."""
    import queue as queue_mod

    MAX_TAG_ATTEMPTS = 4
    prefix_slot = f"[slot{slot_id}]"
    account = None
    curl_file = None
    proxy_url = ""

    def publish_alive() -> None:
        sync_runtime_alive(alive_slots, pool_total=pool_total)

    def mark_slot(ok: bool) -> None:
        alive_slots[slot_id] = ok
        publish_alive()

    def finish_tag_permanently() -> None:
        with left_lock:
            tags_left[0] = max(0, tags_left[0] - 1)

    def put_tag_back(tag: str, *, why: str) -> None:
        with attempts_lock:
            tag_attempts[tag] = int(tag_attempts.get(tag, 0)) + 1
            n = tag_attempts[tag]
        if n > MAX_TAG_ATTEMPTS:
            log(
                f"{prefix_slot} #{tag} — {n} неудачных попыток, пропускаем ({why})",
                error=True,
            )
            finish_tag_permanently()
            return
        tag_q.put(tag)
        log(f"{prefix_slot} тег #{tag} возвращён в очередь (попытка {n}/{MAX_TAG_ATTEMPTS})")

    def take_account():
        nonlocal account, curl_file, proxy_url
        while not _stop_flag.is_set():
            candidate = None
            with cooldown_lock:
                now = time.time()
                still = []
                for ready_at, acc in cooldown:
                    if ready_at <= now and candidate is None:
                        candidate = acc
                    else:
                        still.append((ready_at, acc))
                cooldown[:] = still
            if candidate is None:
                try:
                    candidate = account_q.get_nowait()
                except queue_mod.Empty:
                    return False

            account = candidate
            prefix = f"[{account.label}]"
            try:
                curl_file = prepare_worker_session(account, force=False)
                proxy_url = account.proxy_url
                if not proxy_url:
                    raise RuntimeError("пустой proxy_url")
                log(f"{prefix} сессия готова -> {curl_file}")
                log(f"{prefix_slot} слот занял {account.label}")
                mark_slot(True)
                return True
            except Exception as exc:
                log(f"{prefix} логин/прокси не удался: {exc}", error=True)
                try:
                    DB.mark_account(account.username, "error", str(exc))
                except Exception:
                    pass
                msg = str(exc).lower()
                # Любая ошибка логина — в cooldown, не выкидываем из пула навсегда.
                cool = (
                    180.0
                    if "wait a few minutes" in msg or "please wait" in msg
                    else 90.0
                )
                with cooldown_lock:
                    cooldown.append((time.time() + cool, account))
                log(f"{prefix_slot} {account.label} в cooldown {int(cool)}с")
                account = None
                curl_file = None
                proxy_url = ""
                continue
        return False

    def retire_account(
        reason: str,
        *,
        requeue_tag: str | None = None,
        cooldown_sec: float = 0.0,
        discard: bool = False,
    ) -> None:
        nonlocal account, curl_file, proxy_url
        retired = account
        if account is not None:
            log(f"[{account.label}] вывод из слота: {reason}", error=True)
            try:
                DB.mark_account(account.username, "error", reason[:500])
            except Exception:
                pass
        if requeue_tag:
            put_tag_back(requeue_tag, why=reason)
        if retired is not None and not discard:
            if cooldown_sec > 0:
                with cooldown_lock:
                    cooldown.append((time.time() + cooldown_sec, retired))
                log(f"{prefix_slot} {retired.label} в cooldown {int(cooldown_sec)}с")
            else:
                # Сразу обратно в пул — слот возьмёт другого / того же позже.
                try:
                    account_q.put(retired)
                except Exception:
                    pass
        account = None
        curl_file = None
        proxy_url = ""
        mark_slot(False)

    try:
        while not _stop_flag.is_set():
            with left_lock:
                remaining = tags_left[0]
            if remaining <= 0:
                break

            if account is None:
                if not take_account():
                    with left_lock:
                        still = tags_left[0]
                    if still <= 0:
                        break
                    with cooldown_lock:
                        soon = min((t for t, _ in cooldown), default=None)
                        cooling = len(cooldown)
                    if soon is not None:
                        wait = min(5.0, max(0.5, soon - time.time()))
                        # Не спамим лог каждую секунду.
                        if int(time.time()) % 15 < 5:
                            log(
                                f"{prefix_slot} ждём cooldown аккаунтов "
                                f"({cooling} шт., ~{int(max(0, soon - time.time()))}с)"
                            )
                        time.sleep(wait)
                        continue
                    if account_q.empty():
                        log(
                            f"{prefix_slot} нет свободных аккаунтов — слот пауза 5с"
                        )
                        time.sleep(5.0)
                        # Если теги ещё есть, но аккаунтов совсем нет — выходим.
                        if account_q.empty():
                            with cooldown_lock:
                                cooling = len(cooldown)
                            if cooling == 0:
                                log(
                                    f"{prefix_slot} пул аккаунтов исчерпан, "
                                    f"слот останавливается"
                                )
                                break
                        continue
                    continue

            try:
                tag = tag_q.get(timeout=0.8)
            except queue_mod.Empty:
                continue

            # После Стоп не стартуем новые probe — иначе тег из очереди
            # снова подхватывается соседним слотом.
            if _stop_flag.is_set():
                log(f"{prefix_slot} стоп — слот выходит (тег #{tag} не трогаем)")
                break

            prefix = f"[{account.label}]"
            state = tag_state(tag)
            if state.get("finished") and not RETRY_UNFINISHED:
                log(f"{prefix} #{tag} — уже добит, пропуск")
                with print_lock:
                    stats["skipped"] += 1
                finish_tag_permanently()
                continue

            log(
                f"{prefix} #{tag} — старт"
                + (
                    f" (со стр. {state.get('page_num', 0) + 1})"
                    if state and not state.get("finished")
                    else ""
                )
            )

            code = run_one(tag, curl_file=curl_file, proxy_url=proxy_url)

            if code in (3, 124) and not _stop_flag.is_set():
                replacement = replace_account_proxy(
                    account.username,
                    account.proxy_raw,
                    f"worker transport code {code}",
                )
                if replacement:
                    account.proxy_raw = replacement
                    proxy_url = account.proxy_url
                    log(f"{prefix} прокси заменён, повторяю #{tag}")
                    try:
                        curl_file = prepare_worker_session(account, force=False)
                        code = run_one(
                            tag, curl_file=curl_file, proxy_url=proxy_url
                        )
                    except Exception as exc:
                        log(f"{prefix} повтор после замены прокси: {exc}", error=True)
                        retire_account(str(exc), requeue_tag=tag)
                        continue
                else:
                    DB.mark_account(
                        account.username, "waiting_proxy", "нет свободного прокси"
                    )
                    retire_account("нет свободного прокси", requeue_tag=tag)
                    continue

            if _stop_flag.is_set() or code == 130:
                # Не возвращаем тег в очередь: иначе другой слот снова его запустит.
                log(f"{prefix} остановлен на #{tag}")
                break

            if code == 6:
                log(f"{prefix} сессия мертва, перелогин и повтор #{tag}")
                try:
                    curl_file = prepare_worker_session(account, force=True)
                    code = run_one(tag, curl_file=curl_file, proxy_url=proxy_url)
                except Exception as exc:
                    with print_lock:
                        stats["failed"] += 1
                    cool = (
                        180.0
                        if "wait" in str(exc).lower() or "please" in str(exc).lower()
                        else 60.0
                    )
                    retire_account(
                        f"перелогин: {exc}",
                        requeue_tag=tag,
                        cooldown_sec=cool,
                    )
                    continue
                if code == 6:
                    with print_lock:
                        stats["failed"] += 1
                    retire_account(
                        "аккаунт мёртв после перелогина",
                        requeue_tag=tag,
                        cooldown_sec=180.0,
                    )
                    continue

            if code == 7:
                # Rate limit / временное ограничение — меняем аккаунт из пула.
                with print_lock:
                    stats["failed"] += 1
                retire_account(
                    "rate limit / временное ограничение",
                    requeue_tag=tag,
                    cooldown_sec=120.0,
                )
                continue

            if code == 5:
                # Cursor не подобрался — чаще всего «мягкая» смерть сессии/прокси.
                with print_lock:
                    stats["failed"] += 1
                retire_account(
                    "cursor key не подобран (сессия/прокси)",
                    requeue_tag=tag,
                    cooldown_sec=60.0,
                )
                continue

            if code == 1:
                with print_lock:
                    stats["failed"] += 1
                retire_account("нет валидного curl/шаблона", requeue_tag=tag)
                continue

            after = tag_state(tag)
            if after.get("finished"):
                with print_lock:
                    stats["done"] += 1
                log(
                    f"{prefix} #{tag} готово: "
                    f"{after.get('total_accounts', 0)} аккаунтов, "
                    f"{after.get('page_num', 0)} страниц"
                )
                finish_tag_permanently()
            else:
                # Затухание / лимит — тег считаем закрытым для этого прогона.
                # Прочие коды — ещё раз в очередь (с лимитом попыток).
                reason = (after.get("stop_reason") or "") if after else ""
                soft_done = (
                    "затухание" in reason
                    or "has_next_page" in reason
                    or code == 0
                )
                if soft_done:
                    with print_lock:
                        stats["failed"] += 1
                    log(
                        f"{prefix} #{tag} прервано (код {code}"
                        + (f", {reason}" if reason else "")
                        + ") — больше не крутим в этом прогоне",
                        error=True,
                    )
                    finish_tag_permanently()
                else:
                    with print_lock:
                        stats["failed"] += 1
                    log(
                        f"{prefix} #{tag} сбой (код {code}) — тег в очередь, "
                        f"аккаунт меняем",
                        error=True,
                    )
                    retire_account(f"сбой probe код {code}", requeue_tag=tag)
                    continue

            if not _stop_flag.is_set() and tags_left[0] > 0:
                for _ in range(PAUSE_BETWEEN):
                    if _stop_flag.is_set():
                        break
                    time.sleep(1)
    finally:
        mark_slot(False)
        log(f"{prefix_slot} слот завершён")


def run_pool(tags: list, accounts: list) -> int:
    import queue as queue_mod

    if not TEMPLATE_REQ.exists() or TEMPLATE_REQ.stat().st_size < 1000:
        log(
            "[!] для мультиаккаунта нужен шаблон data/req.sh "
            "(скопируйте graphql cURL один раз)",
            error=True,
        )
        return 1

    try:
        synced = sync_proxy_pool()
    except Exception as exc:
        log(f"[!] не удалось прочитать пул прокси: {exc}", error=True)
        return 1

    missing_proxy = [a for a in accounts if not (a.proxy_raw or "").strip()]
    if missing_proxy and not synced:
        log(
            "[!] у аккаунтов нет прокси, а proxies.txt пуст — "
            "мультиаккаунт без прокси запрещён",
            error=True,
        )
        log(
            "    добавьте proxies.txt (рядом с exe или в data/) "
            "либо укажите прокси в строке аккаунта",
            error=True,
        )
        return 1

    pool_total = len(accounts)
    max_workers = configured_max_workers()
    workers_n = min(pool_total, max_workers)
    if workers_n <= 0:
        log("[!] нет аккаунтов для запуска", error=True)
        return 1

    file_lines = _count_accounts_file_lines(ACCOUNTS_FILE)
    if file_lines > pool_total:
        log(
            f"[!] в accounts.txt {file_lines} строк, загружено только {pool_total} — "
            f"проверьте формат и proxies.txt (dump: 1 прокси на строку аккаунта)",
            error=True,
        )
    if max_workers > pool_total:
        log(
            f"[i] лимит «одновременно»={max_workers}, но в пуле только "
            f"{pool_total} аккаунтов → слотов будет {workers_n}"
        )
    elif max_workers < pool_total:
        log(
            f"[i] лимит «одновременно»={max_workers} → слотов {workers_n} "
            f"(в пуле {pool_total} аккаунтов, остальные — резерв/cooldown)"
        )

    # Заранее фиксируем cursor key — дочерние probe не гадают по 20–30с.
    try:
        from paths import DATA_DIR as _data
        _ck = _data / "cursor_key.txt"
        if not _ck.exists() or not _ck.read_text(encoding="utf-8").strip():
            _data.mkdir(parents=True, exist_ok=True)
            _ck.write_text("after\n", encoding="utf-8")
            log("[i] cursor key: after (записан в data/cursor_key.txt)")
        else:
            log(f"[i] cursor key: {_ck.read_text(encoding='utf-8').strip().splitlines()[0]!r}")
    except Exception as exc:
        log(f"[i] cursor key: after (не удалось записать файл: {exc})")

    pending = []
    skipped_ahead = 0
    for tag in tags:
        state = tag_state(tag)
        if state.get("finished") and not RETRY_UNFINISHED:
            skipped_ahead += 1
            reason = state.get("stop_reason") or "finished"
            log(f"[i] пропуск #{tag} — уже finished ({reason})")
            continue
        pending.append(tag)

    if not pending:
        log("[!] все теги уже добиты — собирать нечего")
        log(
            "[!] Сбросьте БД (кнопка «Сброс БД») или добавьте новые теги. "
            "Галочка «Добирать прерванные» НЕ перезапускает finished-теги."
        )
        return 10  # IDLE — специальный код для GUI

    # Не больше слотов, чем незавершённых тегов.
    workers_n = min(workers_n, len(pending))

    tag_q: queue_mod.Queue = queue_mod.Queue()
    for tag in pending:
        tag_q.put(tag)

    account_q: queue_mod.Queue = queue_mod.Queue()
    for acc in accounts:
        account_q.put(acc)

    tags_left = [len(pending)]
    left_lock = threading.Lock()
    tag_attempts: dict = {}
    attempts_lock = threading.Lock()
    cooldown: list = []
    cooldown_lock = threading.Lock()
    alive_slots = {i: False for i in range(1, workers_n + 1)}
    set_runtime_workers(alive_slots, pool_total=pool_total, max_workers=workers_n)
    try:
        import probe as probe_mod
        probe_mod.set_stop_checker(lambda: _stop_flag.is_set())
    except Exception:
        pass

    log(
        f"[i] режим: мультиаккаунт, слотов {workers_n} "
        f"(лимит {max_workers}), пул {pool_total} аккаунтов"
    )
    if os.getenv("IG_INPROCESS_PROBE") == "1":
        log("[i] probe: in-process (для 1 слота)")
    else:
        log("[i] probe: отдельные процессы (реальная параллельность)")
    log(f"[i] прокси в пуле: {len(synced)}")
    log(
        "[i] Instagram: только через прокси аккаунтов "
        "(Win10/Win11, в т.ч. где instagram.com заблокирован); UI — localhost"
    )
    if len(synced) < pool_total:
        log(
            f"[!] прокси {len(synced)} < загружено аккаунтов {pool_total} — "
            f"добавьте строки в proxies.txt (1 прокси на dump-аккаунт)",
            error=True,
        )
    log(f"[i] тегов в очереди: {len(pending)}")
    log(f"[i] отсечка: {STALL_WINDOW} страниц / {STALL_MIN_ACCOUNTS} аккаунтов")
    log("")

    stats = {"done": 0, "failed": 0, "skipped": skipped_ahead}
    started = time.time()

    pool = ThreadPoolExecutor(max_workers=workers_n)
    try:
        futures = [
            pool.submit(
                worker_slot,
                slot_id,
                tag_q,
                account_q,
                stats,
                alive_slots,
                pool_total,
                tags_left,
                left_lock,
                tag_attempts,
                attempts_lock,
                cooldown,
                cooldown_lock,
            )
            for slot_id in range(1, workers_n + 1)
        ]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as exc:
                log(f"[!] слот упал: {exc}", error=True)
    finally:
        sync_runtime_alive(alive_slots, pool_total=pool_total)
        pool.shutdown(wait=False, cancel_futures=False)

    if not any(alive_slots.values()) and tags_left[0] > 0 and stats["failed"]:
        log("[!] все слоты без живых аккаунтов, теги не добиты", error=True)
        return 6

    elapsed = (time.time() - started) / 60
    log("")
    log("=" * 60)
    log("ПРОГОН ЗАВЕРШЁН (мультиаккаунт)")
    log("=" * 60)
    log(f"  добито:   {stats['done']}")
    log(f"  пропущено:{stats['skipped']}")
    log(f"  прервано: {stats['failed']}")
    log(f"  в очереди осталось: {tags_left[0]}")
    log(f"  времени:  {elapsed:.0f} мин")
    log("=" * 60)
    if stats["failed"] or tags_left[0]:
        log("  добить: RETRY_UNFINISHED=1 python src/run_tags.py")
    return 0 if (stats["failed"] == 0 and tags_left[0] == 0) or any(
        alive_slots.values()
    ) else 6


def main() -> int:
    from accounts import load_accounts
    from paths import ensure_layout

    try:
        ensure_layout()
        reset_stop()
        _apply_export_env_from_settings()

        list_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_LIST
        # В frozen-режиме argv[1] может быть --run_tags — список тегов тогда argv[2].
        if list_path.name.startswith("--"):
            list_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_LIST

        log(f"[dbg] run_tags.main enter argv={sys.argv!r}")
        log(f"[dbg] list_path={list_path} exists={list_path.exists()}")
        log(f"[dbg] RETRY_UNFINISHED={RETRY_UNFINISHED} MAX_WORKERS={MAX_WORKERS}")

        tags = read_tags(list_path)
        if not tags:
            log("[!] список тегов пуст — нечего собирать", error=True)
            return 1

        log(f"[i] список: {list_path}")
        log(f"[i] тегов в списке: {len(tags)}")
        log(f"[i] отчёт каждые {os.getenv('REPORT_EVERY_ACCOUNTS', '10')} аккаунтов")

        export_stop = start_live_excel_exporter()
        try:
            accounts = load_accounts(ACCOUNTS_FILE)
            file_lines = _count_accounts_file_lines(ACCOUNTS_FILE)
            log(
                f"[dbg] аккаунтов загружено: {len(accounts)}, "
                f"строк в файле: {file_lines}"
            )
            if accounts:
                code = run_pool(tags, accounts)
            elif file_lines > 0:
                log(
                    f"[!] в accounts.txt есть {file_lines} строк(и), "
                    f"но ни один аккаунт не загружен",
                    error=True,
                )
                log(
                    "    проверьте формат и proxies.txt "
                    "(dump-аккаунтам нужен прокси по номеру строки)",
                    error=True,
                )
                code = 1
            else:
                log("[i] accounts.txt пуст — однопоточный режим без прокси-пула")
                code = run_single(tags)
            _live_export_once(quiet=False)
        finally:
            export_stop.set()

        log(f"[dbg] run_tags.main exit code={code}")
        return code
    except BaseException as exc:  # noqa: BLE001
        import traceback

        log(f"[!] run_tags.main crash: {type(exc).__name__}: {exc}", error=True)
        log(traceback.format_exc(), error=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
