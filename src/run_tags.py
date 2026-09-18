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
from queue import Empty, Queue

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

sys.path.insert(0, str(SRC_DIR))

PROBE = SRC_DIR / "probe.py"
REFRESH = SRC_DIR / "refresh_tokens.py"

STALL_WINDOW = os.getenv("STALL_WINDOW", "20")
STALL_MIN_ACCOUNTS = os.getenv("STALL_MIN_ACCOUNTS", "3")
PAUSE_BETWEEN = int(os.getenv("PAUSE_BETWEEN", "60"))
RETRY_UNFINISHED = os.getenv("RETRY_UNFINISHED") == "1"
MAX_CONSECUTIVE_FAILURES = int(os.getenv("MAX_CONSECUTIVE_FAILURES", "3"))
AUTO_REFRESH = os.getenv("AUTO_REFRESH", "1") == "1"
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "0"))  # 0 = по числу аккаунтов

print_lock = threading.Lock()
_active_procs: list = []
_procs_lock = threading.Lock()
_stop_flag = threading.Event()
_log_file = None


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
    """Запуск дочернего процесса. Вывод в файл рядом, без PIPE."""
    if _stop_flag.is_set():
        return 130

    from paths import DATA_DIR
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / f"_worker_{os.getpid()}_{threading.get_ident()}.log"

    kwargs = {
        "stdout": open(out_path, "w", encoding="utf-8", errors="replace"),
        "stderr": subprocess.STDOUT,
        "close_fds": False,
    }
    if sys.platform == "win32":
        # DETACHED/NEW console иногда мёртво висит для windowed exe —
        # используем обычный CREATE_NO_WINDOW только если родитель не frozen.
        if not getattr(sys, "frozen", False):
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    out_f = kwargs["stdout"]
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
    try:
        deadline = (time.time() + timeout) if timeout else (time.time() + 3600)
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
            if out_path.exists():
                text = out_path.read_text(encoding="utf-8", errors="replace")
                for line in text.splitlines():
                    log(line)
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
    path = TAGS_DIR / slugify(tag) / "state.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


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


_probe_lock = threading.Lock()


def run_one(tag: str, curl_file: Path = None, proxy_url: str = "") -> int:
    if _stop_flag.is_set():
        return 130

    # В exe и из GUI probe крутится в этом же процессе: иначе windowed
    # Popen того же .exe зависает, а кнопка «Стоп» не доходит до воркера.
    inprocess = getattr(sys, "frozen", False) or os.getenv("IG_INPROCESS_PROBE") == "1"
    if inprocess:
        old = {
            "TAG": os.environ.get("TAG"),
            "CURL_FILE": os.environ.get("CURL_FILE"),
            "PROXY_URL": os.environ.get("PROXY_URL"),
            "STALL_WINDOW": os.environ.get("STALL_WINDOW"),
            "STALL_MIN_ACCOUNTS": os.environ.get("STALL_MIN_ACCOUNTS"),
        }
        os.environ["TAG"] = tag.lstrip("#").replace("\ufeff", "").strip()
        os.environ["STALL_WINDOW"] = STALL_WINDOW
        os.environ["STALL_MIN_ACCOUNTS"] = STALL_MIN_ACCOUNTS
        if curl_file is not None:
            os.environ["CURL_FILE"] = str(curl_file)
        else:
            os.environ.pop("CURL_FILE", None)
        if proxy_url:
            os.environ["PROXY_URL"] = proxy_url
        else:
            os.environ.pop("PROXY_URL", None)
        try:
            with _probe_lock:
                import probe as probe_mod
                probe_mod.set_stop_checker(lambda: _stop_flag.is_set())
                try:
                    code = int(probe_mod.main())
                finally:
                    probe_mod.set_stop_checker(None)
            if _stop_flag.is_set():
                return 130
            log(f"[i] probe finished code={code} tag=#{tag}")
            return code
        except Exception as exc:
            if _stop_flag.is_set():
                return 130
            log(f"[!] probe: {exc}", error=True)
            return 1
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    env = {
        **os.environ,
        "TAG": tag.lstrip("#").replace("\ufeff", "").strip(),
        "STALL_WINDOW": STALL_WINDOW,
        "STALL_MIN_ACCOUNTS": STALL_MIN_ACCOUNTS,
    }
    if curl_file is not None:
        env["CURL_FILE"] = str(curl_file)
    if proxy_url:
        env["PROXY_URL"] = proxy_url
    else:
        env.pop("PROXY_URL", None)
    return _run_streaming(invoke_cmd("probe"), env=env)


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
    """Логин + сборка per-user req.sh. Возвращает Path curl-файла."""
    if _stop_flag.is_set():
        raise RuntimeError("остановлено")
    from login import login, relogin
    from session_req import build_session_req

    cookies = relogin(account) if force else login(account, force=False)
    if _stop_flag.is_set():
        raise RuntimeError("остановлено")
    return build_session_req(account, cookies, template_path=TEMPLATE_REQ)


def worker_loop(account, tag_queue: Queue, stats: dict, alive: dict) -> None:
    prefix = f"[{account.label}]"

    try:
        curl_file = prepare_worker_session(account, force=False)
        log(f"{prefix} сессия готова -> {curl_file}")
    except Exception as exc:
        log(f"{prefix} логин не удался: {exc}", error=True)
        alive[account.username] = False
        return

    alive[account.username] = True
    proxy_url = account.proxy_url

    while not _stop_flag.is_set():
        try:
            tag = tag_queue.get_nowait()
        except Empty:
            break

        state = tag_state(tag)
        if state.get("finished") and not RETRY_UNFINISHED:
            log(f"{prefix} #{tag} — уже добит, пропуск")
            with print_lock:
                stats["skipped"] += 1
            tag_queue.task_done()
            continue

        log(f"{prefix} #{tag} — старт"
            + (f" (со стр. {state.get('page_num', 0) + 1})"
               if state and not state.get("finished") else ""))

        code = run_one(tag, curl_file=curl_file, proxy_url=proxy_url)
        if _stop_flag.is_set() or code == 130:
            tag_queue.put(tag)
            tag_queue.task_done()
            log(f"{prefix} остановлен")
            return

        if code == 6:
            log(f"{prefix} сессия мертва, перелогин и повтор #{tag}")
            try:
                curl_file = prepare_worker_session(account, force=True)
                code = run_one(tag, curl_file=curl_file, proxy_url=proxy_url)
            except Exception as exc:
                log(f"{prefix} перелогин не удался: {exc}", error=True)
                alive[account.username] = False
                tag_queue.put(tag)
                tag_queue.task_done()
                return

            if code == 6:
                log(f"{prefix} аккаунт мёртв после перелогина — выхожу",
                    error=True)
                alive[account.username] = False
                tag_queue.put(tag)
                tag_queue.task_done()
                return

        if code == 1:
            log(f"{prefix} нет валидного curl/шаблона — выхожу", error=True)
            alive[account.username] = False
            tag_queue.put(tag)
            tag_queue.task_done()
            return

        after = tag_state(tag)
        if after.get("finished"):
            with print_lock:
                stats["done"] += 1
            log(f"{prefix} #{tag} готово: "
                f"{after.get('total_accounts', 0)} аккаунтов, "
                f"{after.get('page_num', 0)} страниц")
        else:
            with print_lock:
                stats["failed"] += 1
            log(f"{prefix} #{tag} прервано (код {code})", error=True)

        tag_queue.task_done()

        if not tag_queue.empty() and not _stop_flag.is_set():
            log(f"{prefix} пауза {PAUSE_BETWEEN} с")
            for _ in range(PAUSE_BETWEEN):
                if _stop_flag.is_set():
                    break
                time.sleep(1)

    log(f"{prefix} очередь пуста, воркер завершён")


def run_pool(tags: list, accounts: list) -> int:
    if not TEMPLATE_REQ.exists() or TEMPLATE_REQ.stat().st_size < 1000:
        log("[!] для мультиаккаунта нужен шаблон data/req.sh "
            "(скопируйте graphql cURL один раз)", error=True)
        return 1

    workers_n = len(accounts)
    if MAX_WORKERS > 0:
        workers_n = min(workers_n, MAX_WORKERS)

    log(f"[i] режим: мультиаккаунт, потоков {workers_n}")
    log(f"[i] аккаунтов в файле: {len(accounts)}")
    log(f"[i] отсечка: {STALL_WINDOW} страниц / {STALL_MIN_ACCOUNTS} аккаунтов")
    for acc in accounts[:workers_n]:
        log(f"    - {acc.label}")
    log("")

    # В очередь только теги, которые ещё не добиты (или RETRY_UNFINISHED).
    pending = []
    skipped_ahead = 0
    for tag in tags:
        state = tag_state(tag)
        if state.get("finished") and not RETRY_UNFINISHED:
            skipped_ahead += 1
            continue
        pending.append(tag)

    if not pending:
        log("[i] все теги уже добиты")
        return 0

    tag_queue: Queue = Queue()
    for tag in pending:
        tag_queue.put(tag)

    stats = {"done": 0, "failed": 0, "skipped": skipped_ahead}
    alive = {acc.username: True for acc in accounts[:workers_n]}
    started = time.time()

    pool = ThreadPoolExecutor(max_workers=workers_n)
    try:
        futures = [
            pool.submit(worker_loop, acc, tag_queue, stats, alive)
            for acc in accounts[:workers_n]
        ]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as exc:
                log(f"[!] воркер упал: {exc}", error=True)
    finally:
        # wait=False: иначе зависаем на non-daemon потоках curl_cffi.
        pool.shutdown(wait=False, cancel_futures=False)

    if not any(alive.values()) and not tag_queue.empty():
        log("[!] все аккаунты мертвы, в очереди остались теги", error=True)
        return 6

    # Если воркеры умерли, а теги остались — они уже возвращены в queue,
    # но никто их не заберёт. Посчитаем как failed.
    left = 0
    while not tag_queue.empty():
        try:
            tag_queue.get_nowait()
            left += 1
            tag_queue.task_done()
        except Empty:
            break
    if left:
        stats["failed"] += left
        log(f"[!] не разобрано тегов: {left}", error=True)

    elapsed = (time.time() - started) / 60
    log("")
    log("=" * 60)
    log("ПРОГОН ЗАВЕРШЁН (мультиаккаунт)")
    log("=" * 60)
    log(f"  добито:   {stats['done']}")
    log(f"  пропущено:{stats['skipped']}")
    log(f"  прервано: {stats['failed']}")
    log(f"  времени:  {elapsed:.0f} мин")
    log("=" * 60)
    if stats["failed"]:
        log("  добить: RETRY_UNFINISHED=1 python src/run_tags.py")
    return 0 if not left or any(alive.values()) else 6


def main() -> int:
    from accounts import load_accounts
    from paths import ensure_layout

    ensure_layout()
    reset_stop()

    list_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_LIST
    # В frozen-режиме argv[1] может быть --run_tags — список тегов тогда argv[2].
    if list_path.name.startswith("--"):
        list_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_LIST

    tags = read_tags(list_path)
    if not tags:
        return 1

    log(f"[i] список: {list_path}")
    log(f"[i] тегов в списке: {len(tags)}")

    accounts = load_accounts(ACCOUNTS_FILE)
    if accounts:
        code = run_pool(tags, accounts)
    else:
        code = run_single(tags)

    # curl_cffi/instagrapi оставляют non-daemon потоки — windowed exe
    # иначе не завершается после успешного сбора.
    if getattr(sys, "frozen", False):
        import os as _os
        _os._exit(code)
    return code


if __name__ == "__main__":
    sys.exit(main())
