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
import time
from collections import deque
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

from curl_cffi import requests

from paths import DATA_DIR, TAGS_DIR

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
RETRY_BACKOFF = 15
DELAY_MIN = float(os.getenv("REQUEST_DELAY_MIN", "5"))
DELAY_MAX = float(os.getenv("REQUEST_DELAY_MAX", "9"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "0"))
EMPTY_RETRIES = int(os.getenv("EMPTY_RETRIES", "3"))
EMPTY_PAUSE = int(os.getenv("EMPTY_PAUSE", "30"))
STALL_WINDOW = int(os.getenv("STALL_WINDOW", "0"))
STALL_MIN_ACCOUNTS = int(os.getenv("STALL_MIN_ACCOUNTS", "3"))
RESET = os.getenv("RESET") == "1"
TAG = os.getenv("TAG", "").strip()

# Коллбек из run_tags / GUI: True → прервать сбор между страницами.
_stop_checker = None


def set_stop_checker(fn) -> None:
    global _stop_checker
    _stop_checker = fn


def should_stop() -> bool:
    try:
        return bool(_stop_checker and _stop_checker())
    except Exception:
        return False


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


def set_tag_paths(query: str) -> Path:
    """Задаёт пути к файлам данных для конкретного тега."""
    global STATE_FILE, PROGRESS_FILE, CODES_FILE, ACCOUNTS_FILE
    tag_dir = TAGS_DIR / slugify(query)
    tag_dir.mkdir(parents=True, exist_ok=True)
    STATE_FILE = tag_dir / "state.json"
    PROGRESS_FILE = tag_dir / "progress.jsonl"
    CODES_FILE = tag_dir / "seen_codes.txt"
    ACCOUNTS_FILE = tag_dir / "accounts.jsonl"
    return tag_dir



# Facebook префиксует JSON строкой for (;;); против JSON-hijacking.
FB_PREFIXES = ("for (;;);", "for(;;);", ")]}\'")

# Коды, означающие протухшие fb_dtsg/lsd, а не блокировку аккаунта.
TOKEN_ERRORS = {1357004, 1357001}


def strip_fb_prefix(text: str) -> str:
    text = text.lstrip()
    for prefix in FB_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


class SessionDead(Exception):
    """Запрос больше не проходит: протухли токены или ограничен аккаунт."""


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
        raise SessionDead(f"код {error}: {summary[:150]}")

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


def _proxies():
    if not PROXY_URL:
        return None
    return {"http": PROXY_URL, "https": PROXY_URL}


def post(session, req, body):
    """POST с ретраями. Сетевой сбой не должен убивать многочасовой прогон.

    Запрос идёт без общей сессии: на переиспользуемом хэндле curl_cffi
    таймаут переставал применяться, и один запрос висел 17 минут вместо
    минуты. Каждый вызов получает свой хэндл со своим таймаутом.
    """
    last_exc = None
    for attempt in range(1, RETRIES + 1):
        if should_stop():
            raise StopRequested()
        try:
            return requests.post(req["url"], headers=req["headers"], data=body,
                                 proxies=_proxies(),
                                 impersonate="chrome", timeout=TIMEOUT)
        except StopRequested:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt == RETRIES:
                break
            pause = RETRY_BACKOFF * attempt
            print(f"    [~] сетевой сбой ({exc.__class__.__name__}), "
                  f"попытка {attempt}/{RETRIES}, пауза {pause} с", file=sys.stderr)
            sleep_interruptible(pause)
    raise last_exc


class Collector:
    """Копит статистику и пишет чекпойнты на диск."""

    def __init__(self, query, cursor_key):
        self.query = query
        self.cursor_key = cursor_key
        self.seen_codes = set()
        self.seen_users = set()
        self.total_items = 0
        self.total_videos = 0
        self.page_num = 0
        self.end_cursor = None
        self.finished = False

    def load(self) -> bool:
        if not STATE_FILE.exists():
            return False
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("[!] состояние повреждено, начинаю заново", file=sys.stderr)
            return False
        if state.get("query") != self.query:
            print(f"[!] в состоянии другой тег ({state.get('query')}), начинаю заново",
                  file=sys.stderr)
            return False

        self.cursor_key = state["cursor_key"]
        self.end_cursor = state["end_cursor"]
        self.page_num = state["page_num"]
        self.total_items = state["total_items"]
        self.total_videos = state["total_videos"]
        self.finished = bool(state.get("finished"))

        if CODES_FILE.exists():
            self.seen_codes = {line.strip() for line in
                               CODES_FILE.read_text(encoding="utf-8").splitlines()
                               if line.strip()}
        if ACCOUNTS_FILE.exists():
            for line in ACCOUNTS_FILE.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    self.seen_users.add(json.loads(line)["username"])
                except (json.JSONDecodeError, KeyError):
                    continue
        return True

    def save(self, finished=False, stop_reason=None):
        """finished=True означает, что тег добит и возобновлять его не нужно."""
        tmp = STATE_FILE.with_suffix(".tmp")
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
        tmp.replace(STATE_FILE)

    def absorb(self, page, number):
        """Учитывает страницу и сразу пишет чекпойнт."""
        new_codes, new_accounts = [], 0
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
                # Сохраняем всё, что Instagram отдаёт вместе с постом.
                # Бизнес-полей тут нет — они только в профиле.
                with ACCOUNTS_FILE.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "username": username,
                        "url": f"https://www.instagram.com/{username}/",
                        "pk": user.get("pk"),
                        "full_name": user.get("full_name"),
                        "is_private": user.get("is_private"),
                        "is_verified": user.get("is_verified"),
                        "first_seen_page": number,
                    }, ensure_ascii=False) + "\n")

        if new_codes:
            with CODES_FILE.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(new_codes) + "\n")

        with PROGRESS_FILE.open("a", encoding="utf-8") as fh:
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

        self.page_num = number
        self.end_cursor = page["end_cursor"]
        self.save(finished=self.finished)
        return len(new_codes), new_accounts


def detect_cursor_key(session, req, pairs, variables, first_codes, cursor):
    for name in CURSOR_CANDIDATES:
        probe_vars = dict(variables)
        probe_vars[name] = cursor
        print(f"[i] пробую параметр курсора: {name!r}")
        try:
            resp = post(session, req, build_body(pairs, probe_vars))
        except Exception as exc:
            print(f"    сетевая ошибка: {exc}", file=sys.stderr)
            continue
        if resp.status_code != 200:
            print(f"    HTTP {resp.status_code}")
            continue
        try:
            page = extract(parse_json(resp))
        except SessionDead as exc:
            print(f"    {exc}", file=sys.stderr)
            raise
        codes = {item.get("code") for item in page["items"] if item.get("code")}
        if codes and not codes & first_codes:
            print(f"[+] подошёл {name!r}")
            return name, page
        print(f"    вернулась та же страница ({len(codes)} постов)")
        sleep_interruptible(random.uniform(DELAY_MIN, DELAY_MAX))
    return None, None


def reset_files():
    for path in (STATE_FILE, PROGRESS_FILE, CODES_FILE, ACCOUNTS_FILE):
        if path.exists():
            path.unlink()


def main() -> int:
    global CURL_FILE, PROXY_URL, TAG
    old_out, old_err = sys.stdout, sys.stderr
    redirected = None
    try:
        # Всегда UTF-8 с replace — иначе cp1251 падает на BOM/кириллице.
        try:
            if getattr(sys, "frozen", False):
                redirected = open(os.devnull, "w", encoding="utf-8", errors="replace")
                sys.stdout = redirected
                sys.stderr = redirected
            elif hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        return _main_body()
    finally:
        sys.stdout, sys.stderr = old_out, old_err
        if redirected is not None:
            try:
                redirected.close()
            except Exception:
                pass


def _main_body() -> int:
    global CURL_FILE, PROXY_URL, TAG
    CURL_FILE = Path(os.getenv("CURL_FILE", str(DATA_DIR / "req.sh")))
    PROXY_URL = os.getenv("PROXY_URL", "").strip()
    TAG = _clean_text(os.getenv("TAG", ""))

    if not CURL_FILE.exists() or CURL_FILE.stat().st_size < MIN_CURL_SIZE:
        print(f"[!] нет валидного {CURL_FILE}", file=sys.stderr)
        return 1

    try:
        req = parse_curl(CURL_FILE.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1

    pairs = parse_qsl(req["body"], keep_blank_values=True)
    raw_vars = dict(pairs).get("variables")
    if not raw_vars:
        print("[!] в теле запроса нет поля variables", file=sys.stderr)
        return 1
    base_vars = json.loads(raw_vars)

    if TAG:
        query = TAG if TAG.startswith("#") else f"#{TAG}"
        base_vars["query"] = query
    else:
        query = _clean_text(base_vars.get("query") or "")
        if query:
            base_vars["query"] = query

    query = _clean_text(query or "")
    if not query:
        print("[!] пустой тег (TAG / variables.query)", file=sys.stderr)
        return 1

    tag_dir = set_tag_paths(query)

    if RESET:
        reset_files()
        print("[i] состояние сброшено")

    collector = Collector(query, None)
    resumed = collector.load()

    # Состояние без курсора возобновлять нечем: так выглядит тег, который
    # упал на первом же запросе. Начинаем его с нуля, иначе цикл ниже сразу
    # выйдет и тег будет ложно помечен добитым.
    if resumed and (not collector.end_cursor or not collector.cursor_key):
        print("[i] в состоянии нет курсора — начинаю тег заново", file=sys.stderr)
        resumed = False
        collector = Collector(query, None)

    print(f"[i] тег: {query}")
    print(f"[i] данные: {tag_dir}")
    print(f"[i] curl: {CURL_FILE}")
    if PROXY_URL:
        print(f"[i] proxy: {PROXY_URL.split('@')[-1] if '@' in PROXY_URL else PROXY_URL}")
    print(f"[i] задержка {DELAY_MIN}-{DELAY_MAX} с, таймаут {TIMEOUT} с, ретраев {RETRIES}")
    if resumed:
        print(f"[i] ПРОДОЛЖАЮ со страницы {collector.page_num + 1}: "
              f"постов {collector.total_items}, аккаунтов {len(collector.seen_users)}")
    print()

    session = requests.Session()
    stop_reason = "лимит страниц"
    session_dead = False
    recent = deque(maxlen=STALL_WINDOW or 1)

    try:
        if not resumed:
            resp = post(session, req, build_body(pairs, base_vars))
            if resp.status_code != 200:
                print(f"[!] HTTP {resp.status_code} на первом запросе — "
                      f"скорее всего протухли куки", file=sys.stderr)
                return 3
            page = extract(parse_json(resp))
            if not page["items"]:
                # Тега не существует. Помечаем добитым, иначе он вечно
                # висит в очереди на повтор и роняет прогон по списку.
                print("[!] тег пуст — не существует или без постов",
                      file=sys.stderr)
                collector.finished = True
                collector.save(finished=True, stop_reason="тег пуст")
                return 0

            first_codes = {i.get("code") for i in page["items"] if i.get("code")}
            new_items, new_accounts = collector.absorb(page, 1)
            print(f"[+] страница 1: {new_items} постов, аккаунтов {new_accounts}")

            if not page["has_next_page"] or not page["end_cursor"]:
                # Выдача поместилась в одну страницу — это и есть весь тег.
                print("[!] выдача из одной страницы — тег исчерпан")
                collector.finished = True
                collector.save(finished=True,
                               stop_reason="выдача из одной страницы")
                return 0

            sleep_interruptible(random.uniform(DELAY_MIN, DELAY_MAX))
            cursor_key, page2 = detect_cursor_key(
                session, req, pairs, base_vars, first_codes, page["end_cursor"])
            if not cursor_key:
                print("[!] не удалось подобрать имя параметра курсора", file=sys.stderr)
                return 5
            collector.cursor_key = cursor_key
            new_items, new_accounts = collector.absorb(page2, 2)
            print(f"[+] страница 2: {new_items} новых, всего {collector.total_items}, "
                  f"аккаунтов {len(collector.seen_users)}")
            has_next = page2["has_next_page"]
        else:
            has_next = True

        if not collector.end_cursor:
            print("[!] курсора нет, продолжать нечем", file=sys.stderr)
            stop_reason = "нет курсора для продолжения"
            has_next = False

        while has_next and collector.end_cursor:
            if should_stop():
                raise StopRequested()
            if MAX_PAGES and collector.page_num >= MAX_PAGES:
                break
            sleep_interruptible(random.uniform(DELAY_MIN, DELAY_MAX))
            number = collector.page_num + 1

            variables = dict(base_vars)
            variables[collector.cursor_key] = collector.end_cursor

            # Пустая страница у Instagram бывает разовым сбоем, а не концом
            # выдачи, поэтому останавливаемся только после нескольких попыток.
            page, http_error = None, None
            for empty_try in range(1, EMPTY_RETRIES + 1):
                if should_stop():
                    raise StopRequested()
                resp = post(session, req, build_body(pairs, variables))
                if resp.status_code != 200:
                    http_error = resp.status_code
                    break
                candidate = extract(parse_json(resp))
                if candidate["items"]:
                    page = candidate
                    break
                print(f"    [~] пустая страница {number}, попытка "
                      f"{empty_try}/{EMPTY_RETRIES}, пауза {EMPTY_PAUSE} с",
                      file=sys.stderr)
                sleep_interruptible(EMPTY_PAUSE)

            if http_error:
                stop_reason = f"HTTP {http_error} на странице {number}"
                break
            if page is None:
                stop_reason = (f"пустая страница {number} после "
                               f"{EMPTY_RETRIES} попыток")
                break

            new_items, new_accounts = collector.absorb(page, number)
            has_next = page["has_next_page"]
            print(f"[+] страница {number}: {new_items} новых, "
                  f"всего {collector.total_items}, видео {collector.total_videos}, "
                  f"аккаунтов {len(collector.seen_users)}")

            if STALL_WINDOW:
                recent.append(new_accounts)
                if (len(recent) == STALL_WINDOW
                        and sum(recent) / STALL_WINDOW < STALL_MIN_ACCOUNTS):
                    stop_reason = (f"затухание: за последние {STALL_WINDOW} страниц "
                                   f"в среднем меньше {STALL_MIN_ACCOUNTS} новых "
                                   f"аккаунтов на страницу")
                    break
        else:
            stop_reason = "has_next_page = false, выдача закончилась"

    except StopRequested:
        stop_reason = "остановлено вручную"
    except KeyboardInterrupt:
        stop_reason = "остановлено вручную"
    except SessionDead as exc:
        stop_reason = f"СЕССИЯ НЕДЕЙСТВИТЕЛЬНА: {exc}"
        session_dead = True
    except Exception as exc:
        stop_reason = f"сетевая ошибка: {exc}"

    # Тег считается добитым, если выдача исчерпана или сработала отсечка.
    # Сетевой обрыв и Ctrl+C добитостью не считаются — их надо возобновлять.
    finished = (("затухание" in stop_reason
                 or "has_next_page" in stop_reason
                 or "после" in stop_reason)
                and collector.page_num > 0)
    collector.finished = finished
    collector.save(finished=finished, stop_reason=stop_reason)

    print()
    print("=" * 60)
    print("ИТОГ ЗАМЕРА")
    print("=" * 60)
    print(f"  тег:                  {query}")
    print(f"  параметр курсора:     {collector.cursor_key}")
    print(f"  страниц пройдено:     {collector.page_num}")
    print(f"  постов собрано:       {collector.total_items}")
    print(f"  из них с видео:       {collector.total_videos}")
    print(f"  уникальных аккаунтов: {len(collector.seen_users)}")
    print(f"  остановка:            {stop_reason}")
    print(f"  тег добит:            {'да' if finished else 'нет, нужен повтор'}")
    print("=" * 60)
    if session_dead:
        print("  ОСТАНОВИТЕ ПРОГОН. Проверьте аккаунт в браузере и")
        print("  возьмите свежий cURL — дальнейшие запросы бесполезны.")
        return 6
    if stop_reason == "остановлено вручную":
        return 130
    # Без TAG скрипт продолжит тег из req.sh, а не текущий.
    prefix = f"TAG={slugify(query)} " if TAG else ""
    print(f"  продолжить:    {prefix}python src/probe.py")
    print(f"  начать заново: RESET=1 {prefix}python src/probe.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
