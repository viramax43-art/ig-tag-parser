#!/usr/bin/env python3
"""
Замер глубины пагинации Instagram по хештегу, с возобновлением после сбоя.

Состояние (курсор, счётчики) сохраняется после каждой страницы.
Повторный запуск продолжает с последнего курсора.
Сброс: RESET=1 python src/probe.py

Файлы:
  data/probe_state.json      — курсор и счётчики (для резюма)
  data/probe_progress.jsonl  — по строке на страницу
  data/seen_codes.txt        — коды виденных постов
  data/accounts.jsonl        — уникальные аккаунты
"""
import json
import os
import random
import shlex
import sys
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

from curl_cffi import requests

from paths import DATA_DIR, TAGS_DIR
from state_db import DB

CURL_FILE = Path(os.getenv("CURL_FILE", str(DATA_DIR / "req.sh")))
PROXY_URL = os.getenv("PROXY_URL", "").strip()

# Пути задаются в set_tag_paths() — свои файлы на каждый тег,
# иначе замеры разных тегов затирают друг друга.
STATE_FILE = PROGRESS_FILE = CODES_FILE = ACCOUNTS_FILE = None

MIN_CURL_SIZE = 1000
ROOT_KEY = "xdt_fbsearch__top_serp_graphql"
CURSOR_CANDIDATES = ["after", "cursor", "end_cursor", "serp_cursor", "search_cursor"]

DROP_HEADERS = {"accept-encoding", "content-length", "host"}
VALUE_FLAGS = {"-H", "--header", "--data-raw", "--data", "-d", "--data-binary",
               "-X", "--request", "-b", "--cookie", "-e", "--referer",
               "-A", "--user-agent"}

TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "45"))
RETRIES = int(os.getenv("REQUEST_RETRIES", "5"))
RETRY_BACKOFF = int(os.getenv("RETRY_BACKOFF", "5"))
DELAY_MIN = float(os.getenv("REQUEST_DELAY_MIN", "1"))
DELAY_MAX = float(os.getenv("REQUEST_DELAY_MAX", "2"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "0"))
EMPTY_RETRIES = int(os.getenv("EMPTY_RETRIES", "3"))
EMPTY_PAUSE = int(os.getenv("EMPTY_PAUSE", "10"))
CHECKPOINT_EVERY = int(os.getenv("CHECKPOINT_EVERY", "5"))
STALL_WINDOW = int(os.getenv("STALL_WINDOW", "20"))
STALL_MIN_ACCOUNTS = int(os.getenv("STALL_MIN_ACCOUNTS", "3"))
RATE_LIMIT_BACKOFF = float(os.getenv("RATE_LIMIT_BACKOFF", "30"))
RATE_LIMIT_MAX_HITS = int(os.getenv("RATE_LIMIT_MAX_HITS", "3"))
RESET = os.getenv("RESET") == "1"
TAG = os.getenv("TAG", "").strip()
REPORT_EVERY_ACCOUNTS = int(os.getenv("REPORT_EVERY_ACCOUNTS", "10"))

# Коллбек из run_tags / GUI: True → прервать сбор между страницами.
_stop_checker = None
# Логгер — thread-local, иначе параллельные воркеры затирают префикс тега.
_tls = threading.local()
_STDOUT_REDIRECTED = False
# Удачный параметр курсора (обычно "after") — не перебирать каждый тег заново.
_cursor_lock = threading.Lock()
_known_cursor_key: str | None = None


def reload_runtime_config() -> None:
    """Перечитать лимиты из env (важно для in-process: import был без STALL_*)."""
    global TIMEOUT, RETRIES, RETRY_BACKOFF, DELAY_MIN, DELAY_MAX, MAX_PAGES
    global EMPTY_RETRIES, EMPTY_PAUSE, CHECKPOINT_EVERY
    global STALL_WINDOW, STALL_MIN_ACCOUNTS, REPORT_EVERY_ACCOUNTS, RESET
    global RATE_LIMIT_BACKOFF, RATE_LIMIT_MAX_HITS

    TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "45"))
    RETRIES = int(os.getenv("REQUEST_RETRIES", "5"))
    RETRY_BACKOFF = int(os.getenv("RETRY_BACKOFF", "5"))
    DELAY_MIN = float(os.getenv("REQUEST_DELAY_MIN", "1"))
    DELAY_MAX = float(os.getenv("REQUEST_DELAY_MAX", "2"))
    MAX_PAGES = int(os.getenv("MAX_PAGES", "0"))
    EMPTY_RETRIES = int(os.getenv("EMPTY_RETRIES", "3"))
    EMPTY_PAUSE = int(os.getenv("EMPTY_PAUSE", "10"))
    CHECKPOINT_EVERY = int(os.getenv("CHECKPOINT_EVERY", "5"))
    STALL_WINDOW = int(os.getenv("STALL_WINDOW", "20"))
    STALL_MIN_ACCOUNTS = int(os.getenv("STALL_MIN_ACCOUNTS", "3"))
    RATE_LIMIT_BACKOFF = float(os.getenv("RATE_LIMIT_BACKOFF", "30"))
    RATE_LIMIT_MAX_HITS = int(os.getenv("RATE_LIMIT_MAX_HITS", "3"))
    REPORT_EVERY_ACCOUNTS = int(os.getenv("REPORT_EVERY_ACCOUNTS", "10"))
    RESET = os.getenv("RESET") == "1"


def set_stop_checker(fn) -> None:
    global _stop_checker
    _stop_checker = fn


def set_log_fn(fn) -> None:
    """Подключить внешний логгер для текущего потока."""
    _tls.log_fn = fn


def should_stop() -> bool:
    try:
        return bool(_stop_checker and _stop_checker())
    except Exception:
        return False


def plog(msg: str, *, error: bool = False) -> None:
    """Единый вывод probe: в GUI-коллбек или в stdout/stderr."""
    text = str(msg)
    fn = getattr(_tls, "log_fn", None)
    if fn is not None:
        try:
            fn(text, error=error)
            return
        except TypeError:
            try:
                fn(text)
                return
            except Exception:
                pass
        except Exception:
            pass
    stream = sys.stderr if error else sys.stdout
    try:
        print(text, file=stream, flush=True)
    except Exception:
        try:
            enc = getattr(stream, "encoding", None) or "utf-8"
            safe = text.encode(enc, errors="replace").decode(enc, errors="replace")
            print(safe, file=stream, flush=True)
        except Exception:
            pass


def _ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


class StopRequested(Exception):
    """Пользователь нажал «Остановить»."""


def sleep_interruptible(seconds: float) -> None:
    """Сон кусками; при запросе остановки бросает StopRequested."""
    end = time.time() + max(0.0, float(seconds))
    while True:
        if should_stop():
            raise StopRequested()
        left = end - time.time()
        if left <= 0:
            return
        time.sleep(min(0.25, left))


def _clean_text(value: str) -> str:
    """Убирает BOM и прочий мусор из тегов/путей (часто из UTF-8 с BOM)."""
    if not value:
        return ""
    return (
        str(value)
        .replace("\ufeff", "")
        .replace("\ufffe", "")
        .strip()
    )


def slugify(query: str) -> str:
    """Имя папки из тега: #Coffee Lover -> coffee_lover"""
    query = _clean_text(query)
    cleaned = [c if c.isalnum() else "_" for c in query.lstrip("#").lower()]
    slug = "".join(cleaned).strip("_")
    return slug or "unnamed"


def tag_paths(query: str) -> dict:
    """Пути к файлам данных для тега (без глобального состояния — для параллели)."""
    tag_dir = TAGS_DIR / slugify(query)
    tag_dir.mkdir(parents=True, exist_ok=True)
    return {
        "dir": tag_dir,
        "state": tag_dir / "state.json",
        "progress": tag_dir / "progress.jsonl",
        "codes": tag_dir / "seen_codes.txt",
        "accounts": tag_dir / "accounts.jsonl",
    }


def set_tag_paths(query: str) -> Path:
    """Legacy: выставляет модульные пути (CLI / одиночный запуск)."""
    global STATE_FILE, PROGRESS_FILE, CODES_FILE, ACCOUNTS_FILE
    paths = tag_paths(query)
    STATE_FILE = paths["state"]
    PROGRESS_FILE = paths["progress"]
    CODES_FILE = paths["codes"]
    ACCOUNTS_FILE = paths["accounts"]
    return paths["dir"]



# Facebook префиксует JSON строкой for (;;); против JSON-hijacking.
FB_PREFIXES = ("for (;;);", "for(;;);", ")]}\'")

# Коды, означающие протухшие fb_dtsg/lsd, а не блокировку аккаунта.
TOKEN_ERRORS = {1357004, 1357001}
# Rate limit — сессия жива, нужен backoff, а не перелогин.
RATE_LIMIT_CODES = {1675004}


def strip_fb_prefix(text: str) -> str:
    text = text.lstrip()
    for prefix in FB_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


class SessionDead(Exception):
    """Запрос больше не проходит: протухли токены или ограничен аккаунт."""


class RateLimited(Exception):
    """Instagram временно ограничил частоту запросов — сессия жива."""


def _is_rate_limit_error(err: dict) -> bool:
    code = err.get("code")
    msg = (err.get("message") or "").lower()
    return code in RATE_LIMIT_CODES or "rate limit" in msg


def parse_json(resp):
    """Разбирает ответ, снимая префикс FB и распознавая ошибки Instagram."""
    try:
        payload = json.loads(strip_fb_prefix(resp.text))
    except ValueError:
        head = resp.text[:200].replace("\n", " ")
        raise SessionDead(f"ответ не разбирается как JSON: {head}") from None

    error = payload.get("error")
    if error:
        summary = payload.get("errorSummary") or ""
        if error in TOKEN_ERRORS:
            raise SessionDead(
                f"код {error}: протухли токены fb_dtsg/lsd в data/req.sh. "
                "Аккаунт, скорее всего, в порядке — возьмите свежий cURL "
                "из DevTools и повторите.")
        if error in RATE_LIMIT_CODES:
            raise RateLimited(f"код {error}: {summary[:150] or 'Rate limit exceeded'}")
        raise SessionDead(f"код {error}: {summary[:150]}")

    errors = payload.get("errors") or []
    if isinstance(errors, list) and errors:
        for err in errors:
            if isinstance(err, dict) and _is_rate_limit_error(err):
                raise RateLimited(err.get("message") or "Rate limit exceeded")
        first = errors[0] if isinstance(errors[0], dict) else {}
        code = first.get("code")
        if code in TOKEN_ERRORS:
            raise SessionDead(
                f"код {code}: протухли токены fb_dtsg/lsd. "
                "Возьмите свежий cURL из DevTools.")
        if "data" not in payload:
            msg = first.get("message") or str(payload)[:150]
            raise SessionDead(f"в ответе нет поля data: {msg}")

    if "data" not in payload:
        raise SessionDead(f"в ответе нет поля data: {str(payload)[:150]}")
    return payload


def parse_curl(raw: str) -> dict:
    raw = raw.replace("\\\n", " ")
    tokens = shlex.split(raw)
    if not tokens or tokens[0] != "curl":
        raise ValueError("файл должен начинаться со слова 'curl'")

    url, headers, body = None, {}, None
    i = 1
    while i < len(tokens):
        token = tokens[i]
        if token in VALUE_FLAGS:
            i += 1
            if i >= len(tokens):
                raise ValueError(f"у флага {token} нет значения")
            value = tokens[i]
            if token in ("-H", "--header"):
                key, _, val = value.partition(":")
                headers[key.strip().lower()] = val.strip()
            elif token in ("-b", "--cookie"):
                headers["cookie"] = value
            elif token in ("-e", "--referer"):
                headers["referer"] = value
            elif token in ("-A", "--user-agent"):
                headers["user-agent"] = value
            elif token in ("-X", "--request"):
                pass
            else:
                body = value
        elif token.startswith("-"):
            pass
        elif url is None:
            url = token
        i += 1

    if not url:
        raise ValueError("URL в команде не найден")
    if not body:
        raise ValueError("тело запроса (--data-raw) не найдено")

    for header in DROP_HEADERS:
        headers.pop(header, None)
    return {"url": url, "headers": headers, "body": body}


def build_body(pairs: list, variables: dict) -> str:
    out = []
    for key, value in pairs:
        if key == "variables":
            out.append((key, json.dumps(variables, separators=(",", ":"))))
        else:
            out.append((key, value))
    return urlencode(out)


def extract(payload: dict) -> dict:
    root = (payload.get("data") or {}).get(ROOT_KEY) or {}
    page_info = root.get("page_info") or {}
    items = []
    for edge in root.get("edges") or []:
        node = (edge or {}).get("node") or {}
        for item in node.get("items") or []:
            if isinstance(item, dict):
                items.append(item)
    return {
        "items": items,
        "end_cursor": page_info.get("end_cursor"),
        "has_next_page": bool(page_info.get("has_next_page")),
    }


def _proxies(proxy_url: str | None = None):
    url = (proxy_url if proxy_url is not None else PROXY_URL) or ""
    url = url.strip()
    if not url:
        return None
    return {"http": url, "https": url}


def post(session, req, body, proxy_url: str | None = None):
    """POST с ретраями. Сетевой сбой не должен убивать многочасовой прогон.

    Запрос идёт без общей сессии: на переиспользуемом хэндле curl_cffi
    таймаут переставал применяться, и один запрос висел 17 минут вместо
    минуты. Каждый вызов получает свой хэндл со своим таймаутом.

    Во время ожидания ответа периодически проверяем should_stop(), иначе
    кнопка «Стоп» в GUI молчит до конца TIMEOUT (десятки секунд × ретраи).
    """
    last_exc = None
    proxies = _proxies(proxy_url)
    for attempt in range(1, RETRIES + 1):
        if should_stop():
            raise StopRequested()
        t0 = time.perf_counter()
        box: dict = {"resp": None, "exc": None}

        def _do_post() -> None:
            try:
                box["resp"] = requests.post(
                    req["url"],
                    headers=req["headers"],
                    data=body,
                    proxies=proxies,
                    impersonate="chrome",
                    timeout=TIMEOUT,
                )
            except Exception as exc:  # noqa: BLE001 — в основной поток
                box["exc"] = exc

        worker = threading.Thread(target=_do_post, name="ig-http", daemon=True)
        worker.start()
        while worker.is_alive():
            if should_stop():
                raise StopRequested()
            worker.join(0.35)

        if should_stop():
            raise StopRequested()
        if box["exc"] is not None:
            last_exc = box["exc"]
            elapsed = _ms(t0)
            plog(
                f"    [http-fail] {last_exc.__class__.__name__}: {last_exc} "
                f"after={elapsed:.0f}ms attempt={attempt}/{RETRIES}",
                error=True,
            )
            if attempt == RETRIES:
                break
            pause = RETRY_BACKOFF * attempt
            plog(f"    [retry-wait] {pause}s перед повтором", error=True)
            sleep_interruptible(pause)
            continue

        resp = box["resp"]
        elapsed = _ms(t0)
        body_len = len(resp.content or b"")
        plog(
            f"    [http] status={resp.status_code} time={elapsed:.0f}ms "
            f"bytes={body_len} attempt={attempt}/{RETRIES}"
            + (f" proxy={proxies['http'].split('@')[-1]}" if proxies else " proxy=direct")
        )
        if elapsed >= 8000:
            plog(
                f"    [slow] HTTP {elapsed:.0f}ms — сеть/прокси/IG отвечают медленно",
                error=True,
            )
        return resp
    raise last_exc


class Collector:
    """Копит статистику и пишет чекпойнты на диск."""

    def __init__(self, query, cursor_key):
        self.query = query
        self.cursor_key = cursor_key
        paths = tag_paths(query)
        self.tag_dir = paths["dir"]
        self.state_file = paths["state"]
        self.progress_file = paths["progress"]
        self.codes_file = paths["codes"]
        self.accounts_file = paths["accounts"]
        self.seen_codes = set()
        self.seen_users = set()
        self.total_items = 0
        self.total_videos = 0
        self.page_num = 0
        self.end_cursor = None
        self.finished = False
        self._pending_profiles: list = []
        self._report_every = max(0, int(os.getenv("REPORT_EVERY_ACCOUNTS", str(REPORT_EVERY_ACCOUNTS))))
        self._last_milestone = 0
        self._checkpoint_every = max(1, int(os.getenv("CHECKPOINT_EVERY", str(CHECKPOINT_EVERY))))
        # Timing aggregates for slowdown diagnosis.
        self.stats = {
            "pages": 0,
            "wait_ms": 0.0,
            "http_ms": 0.0,
            "parse_ms": 0.0,
            "absorb_ms": 0.0,
            "file_ms": 0.0,
            "sqlite_ms": 0.0,
            "empty_retries": 0,
            "ckpts": 0,
        }
        self._run_started = time.perf_counter()

    def _sync_milestone_baseline(self) -> None:
        total = len(self.seen_users)
        if self._report_every > 0:
            self._last_milestone = (total // self._report_every) * self._report_every
        else:
            self._last_milestone = total

    def _report_milestones(self) -> None:
        if self._report_every <= 0:
            return
        total = len(self.seen_users)
        while self._last_milestone + self._report_every <= total:
            self._last_milestone += self._report_every
            plog(f"[*] собрано {self._last_milestone} аккаунтов")

    def load(self) -> bool:
        # SQLite is authoritative when available; legacy JSON is retained as a
        # fallback for old installations and is migrated by the next page commit.
        try:
            durable = DB.load_tag(self.query)
            if durable:
                self.cursor_key = durable.get("cursor_key")
                self.end_cursor = durable.get("end_cursor")
                self.page_num = int(durable.get("page_num") or 0)
                self.total_items = int(durable.get("total_items") or 0)
                self.total_videos = int(durable.get("total_videos") or 0)
                self.finished = durable.get("status") == "finished"
                _, self.seen_codes = DB.load_seen(self.query)
                con = DB.connection()
                self.seen_users = {
                    r[0]
                    for r in con.execute(
                        "SELECT username FROM tag_profiles WHERE tag=?",
                        (self.query,),
                    )
                }
                self._sync_milestone_baseline()
                return True
        except Exception as exc:
            plog(f"[!] SQLite state read failed, fallback to JSON: {exc}",
                 error=True)
        if not self.state_file.exists():
            return False
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        if state.get("query") != self.query:
            return False
        self.cursor_key = state.get("cursor_key")
        self.end_cursor = state.get("end_cursor")
        self.page_num = int(state.get("page_num", 0) or 0)
        self.total_items = int(state.get("total_items", 0) or 0)
        self.total_videos = int(state.get("total_videos", 0) or 0)
        self.finished = bool(state.get("finished"))
        if self.codes_file.exists():
            self.seen_codes = {
                line.strip()
                for line in self.codes_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            }
        if self.accounts_file.exists():
            for line in self.accounts_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    self.seen_users.add(json.loads(line)["username"])
                except (json.JSONDecodeError, KeyError):
                    continue
        self._sync_milestone_baseline()
        return True

    def save(self, finished=False, stop_reason=None):
        """finished=True означает, что тег добит и возобновлять его не нужно."""
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "query": self.query,
            "cursor_key": self.cursor_key,
            "end_cursor": self.end_cursor,
            "page_num": self.page_num,
            "total_items": self.total_items,
            "total_videos": self.total_videos,
            "total_accounts": len(self.seen_users),
            "finished": finished,
            "stop_reason": stop_reason,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_file)

    def absorb(self, page, number, force_checkpoint: bool = False):
        """Учитывает страницу; тяжёлый чекпойнт — раз в N страниц.

        Returns (new_codes_count, new_accounts, timing_dict).
        """
        t_all = time.perf_counter()
        new_codes, new_accounts = [], 0
        account_lines = []
        for item in page["items"]:
            code = item.get("code")
            if code:
                if code in self.seen_codes:
                    continue
                self.seen_codes.add(code)
                new_codes.append(code)
            self.total_items += 1
            if item.get("video_versions"):
                self.total_videos += 1
            user = item.get("user") or {}
            username = user.get("username")
            if username and username not in self.seen_users:
                self.seen_users.add(username)
                new_accounts += 1
                account_lines.append(json.dumps({
                    "username": username,
                    "url": f"https://www.instagram.com/{username}/",
                    "pk": user.get("pk"),
                    "full_name": user.get("full_name"),
                    "is_private": user.get("is_private"),
                    "is_verified": user.get("is_verified"),
                    "first_seen_page": number,
                }, ensure_ascii=False))

        t_file = time.perf_counter()
        if account_lines:
            with self.accounts_file.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(account_lines) + "\n")

        if new_codes:
            with self.codes_file.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(new_codes) + "\n")

        if number == 1 or number % 5 == 0 or not page.get("has_next_page"):
            with self.progress_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "page": number,
                    "items_on_page": len(page["items"]),
                    "new_items": len(new_codes),
                    "total_items": self.total_items,
                    "total_videos": self.total_videos,
                    "total_accounts": len(self.seen_users),
                    "new_accounts": new_accounts,
                    "has_next_page": page["has_next_page"],
                }, ensure_ascii=False) + "\n")
        file_ms = _ms(t_file)

        self.page_num = number
        self.end_cursor = page["end_cursor"]

        do_ckpt = (
            force_checkpoint
            or number == 1
            or number % self._checkpoint_every == 0
            or not page.get("has_next_page")
        )
        sqlite_ms = 0.0
        if do_ckpt:
            t_sql = time.perf_counter()
            if self._pending_profiles:
                by_page: dict[int, list] = {}
                for item, pnum in self._pending_profiles:
                    by_page.setdefault(pnum, []).append(item)
                for pnum, items in by_page.items():
                    DB.save_page(
                        self.query,
                        {"items": items, "end_cursor": self.end_cursor, "has_next_page": True},
                        pnum,
                        {
                            "cursor_key": self.cursor_key,
                            "end_cursor": self.end_cursor,
                            "page_num": self.page_num,
                            "total_items": self.total_items,
                            "total_videos": self.total_videos,
                            "total_accounts": len(self.seen_users),
                            "status": "finished" if self.finished else "running",
                        },
                    )
                self._pending_profiles.clear()
            DB.save_page(self.query, page, number, {
                "cursor_key": self.cursor_key,
                "end_cursor": self.end_cursor,
                "page_num": self.page_num,
                "total_items": self.total_items,
                "total_videos": self.total_videos,
                "total_accounts": len(self.seen_users),
                "status": "finished" if self.finished else "running",
            })
            self.save(finished=self.finished)
            sqlite_ms = _ms(t_sql)
            self.stats["ckpts"] += 1
        else:
            for item in page.get("items") or []:
                if isinstance(item, dict):
                    self._pending_profiles.append((item, number))

        self._report_milestones()
        absorb_ms = _ms(t_all)
        self.stats["absorb_ms"] += absorb_ms
        self.stats["file_ms"] += file_ms
        self.stats["sqlite_ms"] += sqlite_ms
        timing = {
            "absorb_ms": absorb_ms,
            "file_ms": file_ms,
            "sqlite_ms": sqlite_ms,
            "ckpt": do_ckpt,
            "items_on_page": len(page.get("items") or []),
            "pending": len(self._pending_profiles),
        }
        return len(new_codes), new_accounts, timing

    def note_page_timing(
        self,
        *,
        number: int,
        wait_ms: float,
        http_ms: float,
        parse_ms: float,
        timing: dict,
        new_items: int,
        new_accounts: int,
        empty_tries: int = 0,
    ) -> None:
        cycle_ms = wait_ms + http_ms + parse_ms + timing["absorb_ms"]
        self.stats["pages"] += 1
        self.stats["wait_ms"] += wait_ms
        self.stats["http_ms"] += http_ms
        self.stats["parse_ms"] += parse_ms
        if empty_tries > 1:
            self.stats["empty_retries"] += empty_tries - 1

        elapsed_min = max((time.perf_counter() - self._run_started) / 60.0, 1e-6)
        rate = len(self.seen_users) / elapsed_min
        n = max(self.stats["pages"], 1)
        avg_cycle = (
            self.stats["wait_ms"]
            + self.stats["http_ms"]
            + self.stats["parse_ms"]
            + self.stats["absorb_ms"]
        ) / n

        bottleneck = max(
            ("wait", wait_ms),
            ("http", http_ms),
            ("parse", parse_ms),
            ("absorb", timing["absorb_ms"]),
            key=lambda x: x[1],
        )

        plog(
            f"[+] стр.{number}: +{new_items} постов / +{new_accounts} акк. "
            f"(всего {self.total_items} постов, {len(self.seen_users)} акк., "
            f"на стр. {timing['items_on_page']})"
        )
        plog(
            f"    [timing] wait={wait_ms:.0f}ms http={http_ms:.0f}ms "
            f"parse={parse_ms:.0f}ms absorb={timing['absorb_ms']:.0f}ms "
            f"(file={timing['file_ms']:.0f} sqlite={timing['sqlite_ms']:.0f} "
            f"ckpt={'yes' if timing['ckpt'] else 'no'} pending={timing['pending']}) "
            f"cycle={cycle_ms:.0f}ms | bottleneck={bottleneck[0]} "
            f"| rate={rate:.1f} акк/мин avg_cycle={avg_cycle:.0f}ms"
        )
        if empty_tries > 1:
            plog(f"    [empty] пустых попыток до успеха: {empty_tries - 1}", error=True)
        if cycle_ms >= 10000:
            plog(
                f"    [slow] цикл стр.{number} = {cycle_ms/1000:.1f}с "
                f"(узкое место: {bottleneck[0]}={bottleneck[1]:.0f}ms)",
                error=True,
            )
        if number % 10 == 0:
            self.log_timing_summary()

    def log_timing_summary(self) -> None:
        n = max(self.stats["pages"], 1)
        elapsed = time.perf_counter() - self._run_started
        plog(
            f"[stats] pages={self.stats['pages']} "
            f"avg_wait={self.stats['wait_ms']/n:.0f}ms "
            f"avg_http={self.stats['http_ms']/n:.0f}ms "
            f"avg_parse={self.stats['parse_ms']/n:.0f}ms "
            f"avg_absorb={self.stats['absorb_ms']/n:.0f}ms "
            f"avg_file={self.stats['file_ms']/n:.0f}ms "
            f"avg_sqlite={self.stats['sqlite_ms']/n:.0f}ms "
            f"ckpts={self.stats['ckpts']} empty_retries={self.stats['empty_retries']} "
            f"elapsed={elapsed:.0f}s "
            f"rate={len(self.seen_users)/max(elapsed/60,1e-6):.1f} акк/мин"
        )

    def flush_checkpoint(self, stop_reason: str | None = None) -> None:
        """Сбросить буфер профилей + курсор в SQLite/state.json."""
        if self._pending_profiles:
            by_page: dict[int, list] = {}
            for item, pnum in self._pending_profiles:
                by_page.setdefault(pnum, []).append(item)
            for pnum, items in by_page.items():
                DB.save_page(
                    self.query,
                    {"items": items, "end_cursor": self.end_cursor, "has_next_page": True},
                    pnum,
                    {
                        "cursor_key": self.cursor_key,
                        "end_cursor": self.end_cursor,
                        "page_num": self.page_num,
                        "total_items": self.total_items,
                        "total_videos": self.total_videos,
                        "total_accounts": len(self.seen_users),
                        "status": "finished" if self.finished else "running",
                    },
                )
            self._pending_profiles.clear()
        else:
            DB.save_tag(self.query, {
                "cursor_key": self.cursor_key,
                "end_cursor": self.end_cursor,
                "page_num": self.page_num,
                "total_items": self.total_items,
                "total_videos": self.total_videos,
                "total_accounts": len(self.seen_users),
                "status": "finished" if self.finished else "running",
                "stop_reason": stop_reason,
            })
        self.save(finished=self.finished, stop_reason=stop_reason)


def detect_cursor_key(session, req, pairs, variables, first_codes, cursor, proxy_url: str | None = None):
    global _known_cursor_key
    with _cursor_lock:
        cached = _known_cursor_key
    # Env/file — чтобы дочерние процессы не гадали каждый раз заново.
    env_key = (os.getenv("IG_CURSOR_KEY") or "").strip()
    file_key = ""
    try:
        key_path = DATA_DIR / "cursor_key.txt"
        if key_path.exists():
            file_key = key_path.read_text(encoding="utf-8").strip().splitlines()[0]
    except Exception:
        pass

    candidates = []
    for name in (cached, env_key, file_key, *CURSOR_CANDIDATES):
        if name and name not in candidates:
            candidates.append(name)

    for name in candidates:
        probe_vars = dict(variables)
        probe_vars[name] = cursor
        plog(f"[i] пробую параметр курсора: {name!r}")
        try:
            resp = post(session, req, build_body(pairs, probe_vars), proxy_url=proxy_url)
        except Exception as exc:
            plog(f"    сетевая ошибка: {exc}", error=True)
            continue
        if resp.status_code != 200:
            plog(f"    HTTP {resp.status_code}")
            continue
        try:
            page = extract(parse_json(resp))
        except RateLimited:
            raise
        except SessionDead as exc:
            plog(f"    {exc}", error=True)
            raise
        codes = {item.get("code") for item in page["items"] if item.get("code")}
        if codes and not codes & first_codes:
            plog(f"[+] подошёл {name!r}")
            with _cursor_lock:
                _known_cursor_key = name
            try:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                (DATA_DIR / "cursor_key.txt").write_text(name + "\n", encoding="utf-8")
            except Exception:
                pass
            return name, page
        plog(f"    вернулась та же страница ({len(codes)} постов)")
        sleep_interruptible(random.uniform(DELAY_MIN, DELAY_MAX))
    return None, None


def reset_files(collector: "Collector | None" = None):
    paths = (
        (collector.state_file, collector.progress_file, collector.codes_file, collector.accounts_file)
        if collector is not None
        else (STATE_FILE, PROGRESS_FILE, CODES_FILE, ACCOUNTS_FILE)
    )
    for path in paths:
        if path is not None and path.exists():
            path.unlink()


def main(
    tag: str | None = None,
    curl_file: str | Path | None = None,
    proxy_url: str | None = None,
) -> int:
    """Запуск probe. Аргументы предпочтительнее env — так безопасен параллельный in-process."""
    global _STDOUT_REDIRECTED
    old_out, old_err = sys.stdout, sys.stderr
    redirected = None
    try:
        try:
            # В GUI лог идёт через set_log_fn — stdout глушить не нужно.
            if getattr(sys, "frozen", False) and getattr(_tls, "log_fn", None) is None:
                if not _STDOUT_REDIRECTED:
                    redirected = open(os.devnull, "w", encoding="utf-8", errors="replace")
                    sys.stdout = redirected
                    sys.stderr = redirected
                    _STDOUT_REDIRECTED = True
                    redirected = None
            elif hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        return _main_body(tag=tag, curl_file=curl_file, proxy_url=proxy_url)
    finally:
        if redirected is not None:
            sys.stdout, sys.stderr = old_out, old_err
            try:
                redirected.close()
            except Exception:
                pass


def _timed_parse(resp):
    t0 = time.perf_counter()
    page = extract(parse_json(resp))
    return page, _ms(t0)


def _main_body(
    tag: str | None = None,
    curl_file: str | Path | None = None,
    proxy_url: str | None = None,
) -> int:
    # In-process GUI path imports probe before env is set — re-read knobs now.
    reload_runtime_config()

    curl_path = Path(curl_file) if curl_file else Path(
        os.getenv("CURL_FILE", str(DATA_DIR / "req.sh"))
    )
    proxy = (proxy_url if proxy_url is not None else os.getenv("PROXY_URL", "")).strip()
    tag_value = _clean_text(tag if tag is not None else os.getenv("TAG", ""))

    if not curl_path.exists() or curl_path.stat().st_size < MIN_CURL_SIZE:
        plog(f"[!] нет валидного {curl_path}", error=True)
        return 1

    try:
        req = parse_curl(curl_path.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        plog(f"[!] {exc}", error=True)
        return 1

    pairs = parse_qsl(req["body"], keep_blank_values=True)
    raw_vars = dict(pairs).get("variables")
    if not raw_vars:
        plog("[!] в теле запроса нет поля variables", error=True)
        return 1
    base_vars = json.loads(raw_vars)

    if tag_value:
        query = tag_value if tag_value.startswith("#") else f"#{tag_value}"
        base_vars["query"] = query
    else:
        query = _clean_text(base_vars.get("query") or "")
        if query:
            base_vars["query"] = query

    query = _clean_text(query or "")
    if not query:
        plog("[!] пустой тег (TAG / variables.query)", error=True)
        return 1

    collector = Collector(query, None)
    set_tag_paths(query)

    if RESET:
        reset_files(collector)
        plog("[i] состояние сброшено")

    resumed = collector.load()

    if resumed and (not collector.end_cursor or not collector.cursor_key):
        plog("[i] в состоянии нет курсора — начинаю тег заново", error=True)
        resumed = False
        collector = Collector(query, None)

    plog(f"[i] тег: {query}")
    plog(f"[i] данные: {collector.tag_dir}")
    plog(f"[i] curl: {curl_path}")
    if proxy:
        plog(f"[i] proxy: {proxy.split('@')[-1] if '@' in proxy else proxy}")
    else:
        plog("[i] proxy: direct (без прокси)")
    plog(
        f"[i] лимиты: delay={DELAY_MIN}-{DELAY_MAX}s timeout={TIMEOUT}s "
        f"retries={RETRIES} empty_pause={EMPTY_PAUSE}s "
        f"checkpoint_every={CHECKPOINT_EVERY}"
    )
    if STALL_WINDOW:
        plog(
            f"[i] отсечка: {STALL_WINDOW} страниц / "
            f"{STALL_MIN_ACCOUNTS} новых аккаунтов (сумма за окно)"
        )
    if resumed:
        plog(
            f"[i] ПРОДОЛЖАЮ со страницы {collector.page_num + 1}: "
            f"постов {collector.total_items}, аккаунтов {len(collector.seen_users)}"
        )
    plog("")

    session = requests.Session()
    stop_reason = "лимит страниц"
    session_dead = False
    recent = deque(maxlen=STALL_WINDOW or 1)
    rate_hits = 0

    try:
        if not resumed:
            t_http = time.perf_counter()
            resp = post(session, req, build_body(pairs, base_vars), proxy_url=proxy)
            http_ms = _ms(t_http)
            if resp.status_code != 200:
                plog(
                    f"[!] HTTP {resp.status_code} на первом запросе — "
                    f"скорее всего протухли куки",
                    error=True,
                )
                return 3
            page, parse_ms = _timed_parse(resp)
            if not page["items"]:
                plog("[!] тег пуст — не существует или без постов", error=True)
                collector.finished = True
                collector.save(finished=True, stop_reason="тег пуст")
                return 0

            first_codes = {i.get("code") for i in page["items"] if i.get("code")}
            new_items, new_accounts, timing = collector.absorb(page, 1)
            collector.note_page_timing(
                number=1, wait_ms=0.0, http_ms=http_ms, parse_ms=parse_ms,
                timing=timing, new_items=new_items, new_accounts=new_accounts,
            )
            if STALL_WINDOW:
                recent.append(new_accounts)

            if not page["has_next_page"] or not page["end_cursor"]:
                plog("[!] выдача из одной страницы — тег исчерпан")
                collector.finished = True
                collector.save(finished=True, stop_reason="выдача из одной страницы")
                return 0

            delay = random.uniform(DELAY_MIN, DELAY_MAX)
            plog(f"    [wait] {delay:.2f}s перед детект курсора")
            sleep_interruptible(delay)
            cursor_key, page2 = detect_cursor_key(
                session, req, pairs, base_vars, first_codes, page["end_cursor"],
                proxy_url=proxy)
            if not cursor_key:
                plog("[!] не удалось подобрать имя параметра курсора", error=True)
                return 5
            collector.cursor_key = cursor_key
            new_items, new_accounts, timing = collector.absorb(page2, 2)
            collector.note_page_timing(
                number=2, wait_ms=delay * 1000, http_ms=0.0, parse_ms=0.0,
                timing=timing, new_items=new_items, new_accounts=new_accounts,
            )
            if STALL_WINDOW:
                recent.append(new_accounts)
            has_next = page2["has_next_page"]
        else:
            has_next = True

        if not collector.end_cursor:
            plog("[!] курсора нет, продолжать нечем", error=True)
            stop_reason = "нет курсора для продолжения"
            has_next = False

        while has_next and collector.end_cursor:
            if should_stop():
                raise StopRequested()
            if MAX_PAGES and collector.page_num >= MAX_PAGES:
                break

            delay = random.uniform(DELAY_MIN, DELAY_MAX)
            number = collector.page_num + 1
            plog(f"    [wait] {delay:.2f}s перед стр.{number}")
            t_wait = time.perf_counter()
            sleep_interruptible(delay)
            wait_ms = _ms(t_wait)

            variables = dict(base_vars)
            variables[collector.cursor_key] = collector.end_cursor

            page, http_error = None, None
            http_ms = 0.0
            parse_ms = 0.0
            empty_tries = 0
            rate_limited_stop = False
            for empty_try in range(1, EMPTY_RETRIES + 1):
                if should_stop():
                    raise StopRequested()
                empty_tries = empty_try
                t_http = time.perf_counter()
                resp = post(session, req, build_body(pairs, variables), proxy_url=proxy)
                http_ms += _ms(t_http)
                if resp.status_code != 200:
                    http_error = resp.status_code
                    break
                try:
                    candidate, p_ms = _timed_parse(resp)
                except RateLimited as exc:
                    rate_hits += 1
                    plog(
                        f"[!] rate limit ({rate_hits}/{RATE_LIMIT_MAX_HITS}): {exc}",
                        error=True,
                    )
                    if rate_hits >= RATE_LIMIT_MAX_HITS:
                        stop_reason = (
                            f"rate limit: Instagram ограничил запросы "
                            f"{RATE_LIMIT_MAX_HITS} раз подряд — "
                            f"пауза и повтор тега позже"
                        )
                        rate_limited_stop = True
                        break
                    t_rl = time.perf_counter()
                    sleep_interruptible(RATE_LIMIT_BACKOFF)
                    wait_ms += _ms(t_rl)
                    continue
                parse_ms += p_ms
                if candidate["items"]:
                    page = candidate
                    break
                plog(
                    f"    [empty] стр.{number} пустая, попытка "
                    f"{empty_try}/{EMPTY_RETRIES}, пауза {EMPTY_PAUSE}s",
                    error=True,
                )
                t_empty = time.perf_counter()
                sleep_interruptible(EMPTY_PAUSE)
                wait_ms += _ms(t_empty)

            if rate_limited_stop:
                plog(f"[!] {stop_reason}", error=True)
                break
            if http_error:
                stop_reason = f"HTTP {http_error} на странице {number}"
                plog(f"[!] {stop_reason}", error=True)
                break
            if page is None:
                stop_reason = (
                    f"пустая страница {number} после {EMPTY_RETRIES} попыток"
                )
                plog(f"[!] {stop_reason}", error=True)
                break

            new_items, new_accounts, timing = collector.absorb(page, number)
            rate_hits = 0
            has_next = page["has_next_page"]
            collector.note_page_timing(
                number=number, wait_ms=wait_ms, http_ms=http_ms, parse_ms=parse_ms,
                timing=timing, new_items=new_items, new_accounts=new_accounts,
                empty_tries=empty_tries,
            )

            if STALL_WINDOW:
                recent.append(new_accounts)
                # UI «20 страниц / 3 аккаунтов» = сумма за окно, не среднее.
                if (len(recent) == STALL_WINDOW
                        and sum(recent) < STALL_MIN_ACCOUNTS):
                    stop_reason = (
                        f"затухание: за последние {STALL_WINDOW} страниц "
                        f"меньше {STALL_MIN_ACCOUNTS} новых аккаунтов "
                        f"(сумма={sum(recent)})"
                    )
                    break
        else:
            stop_reason = "has_next_page = false, выдача закончилась"

    except StopRequested:
        stop_reason = "остановлено вручную"
    except KeyboardInterrupt:
        stop_reason = "остановлено вручную"
    except RateLimited as exc:
        # Сессия жива — не code 6 / не перелогин.
        stop_reason = f"rate limit: {exc}"
        plog(f"[!] {stop_reason}", error=True)
    except SessionDead as exc:
        stop_reason = f"СЕССИЯ НЕДЕЙСТВИТЕЛЬНА: {exc}"
        session_dead = True
    except Exception as exc:
        stop_reason = f"сетевая ошибка: {exc}"
        plog(f"[!] {stop_reason}", error=True)

    finished = (
        ("затухание" in stop_reason
         or "has_next_page" in stop_reason
         or stop_reason in {"выдача из одной страницы", "тег пуст"})
        and collector.page_num > 0
    )
    collector.finished = finished
    collector.flush_checkpoint(stop_reason=stop_reason)
    try:
        DB.save_tag(query, {
            "cursor_key": collector.cursor_key,
            "end_cursor": collector.end_cursor,
            "page_num": collector.page_num,
            "total_items": collector.total_items,
            "total_videos": collector.total_videos,
            "total_accounts": len(collector.seen_users),
            "status": (
                "finished" if finished
                else ("cancelled" if stop_reason == "остановлено вручную" else "failed")
            ),
            "stop_reason": stop_reason,
        })
    except Exception as exc:
        plog(f"[!] SQLite final checkpoint failed: {exc}", error=True)

    collector.log_timing_summary()
    plog("")
    plog("=" * 60)
    plog("ИТОГ ЗАМЕРА")
    plog("=" * 60)
    plog(f"  тег:                  {query}")
    plog(f"  параметр курсора:     {collector.cursor_key}")
    plog(f"  страниц пройдено:     {collector.page_num}")
    plog(f"  постов собрано:       {collector.total_items}")
    plog(f"  из них с видео:       {collector.total_videos}")
    plog(f"  уникальных аккаунтов: {len(collector.seen_users)}")
    plog(f"  остановка:            {stop_reason}")
    plog(f"  тег добит:            {'да' if finished else 'нет, нужен повтор'}")
    plog("=" * 60)
    if session_dead:
        plog("  ОСТАНОВИТЕ ПРОГОН. Проверьте аккаунт в браузере и")
        plog("  возьмите свежий cURL — дальнейшие запросы бесполезны.")
        return 6
    if "rate limit" in (stop_reason or "").lower():
        # Отдельный код: воркер должен сменить аккаунт из пула, а не
        # считать тег «успешно прерванным» тем же аккаунтом.
        plog("  RATE LIMIT — смените аккаунт / сделайте паузу.")
        return 7
    if stop_reason == "остановлено вручную":
        return 130
    prefix = f"TAG={slugify(query)} " if tag_value else ""
    plog(f"  продолжить:    {prefix}python src/probe.py")
    plog(f"  начать заново: RESET=1 {prefix}python src/probe.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
