#!/usr/bin/env python3
"""
Повторяет запрос из curl-команды, скопированной в Chrome DevTools.
Читает data/req.sh -> отправляет через curl_cffi -> кладёт ответ в data/sample_response.json
"""
import json
import shlex
import sys
from pathlib import Path

from curl_cffi import requests

BASE_DIR = Path(__file__).resolve().parent.parent
CURL_FILE = BASE_DIR / "data" / "req.sh"
OUT_FILE = BASE_DIR / "data" / "sample_response.json"

MIN_CURL_SIZE = 1000

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


DROP_HEADERS = {"accept-encoding", "content-length", "host"}
VALUE_FLAGS = {"-H", "--header", "--data-raw", "--data", "-d", "--data-binary",
               "-X", "--request", "-b", "--cookie", "-e", "--referer",
               "-A", "--user-agent"}


def parse_curl(raw: str) -> dict:
    """Разбирает curl-команду в словарь url/headers/body/method."""
    raw = raw.replace("\\\n", " ")
    try:
        tokens = shlex.split(raw)
    except ValueError as exc:
        raise ValueError(f"не разобрать команду: {exc}") from exc

    if not tokens or tokens[0] != "curl":
        raise ValueError("файл должен начинаться со слова 'curl'")

    url, headers, body, method = None, {}, None, None
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
                method = value.upper()
            else:
                body = value
        elif token.startswith("-"):
            pass
        elif url is None:
            url = token
        i += 1

    if not url:
        raise ValueError("URL в команде не найден")

    for header in DROP_HEADERS:
        headers.pop(header, None)

    return {
        "url": url,
        "headers": headers,
        "body": body,
        "method": method or ("POST" if body else "GET"),
    }


def main() -> int:
    if not CURL_FILE.exists():
        print(f"[!] нет файла {CURL_FILE}", file=sys.stderr)
        print("    сделайте: pbpaste > data/req.sh", file=sys.stderr)
        return 1

    raw = CURL_FILE.read_text(encoding="utf-8")
    if len(raw) < MIN_CURL_SIZE:
        print(f"[!] в data/req.sh всего {len(raw)} байт, ожидалось больше {MIN_CURL_SIZE}",
              file=sys.stderr)
        print("    в буфер обмена попала не curl-команда. Скопируйте заново", file=sys.stderr)
        return 1

    try:
        req = parse_curl(raw)
    except ValueError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1

    print(f"[i] {req['method']} {req['url'][:90]}")
    print(f"[i] заголовков: {len(req['headers'])}, тело: {len(req['body'] or '')} байт")

    try:
        resp = requests.request(
            req["method"],
            req["url"],
            headers=req["headers"],
            data=req["body"],
            impersonate="chrome",
            timeout=30,
        )
    except Exception as exc:
        print(f"[!] запрос не выполнен: {exc}", file=sys.stderr)
        return 2

    print(f"[i] HTTP {resp.status_code}, получено {len(resp.content)} байт")

    if resp.status_code != 200:
        print("[!] ожидали 200. Тело ответа (первые 400 символов):", file=sys.stderr)
        print(resp.text[:400], file=sys.stderr)
        return 3

    try:
        data = json.loads(strip_fb_prefix(resp.text))
    except ValueError:
        OUT_FILE.write_text(resp.text, encoding="utf-8")
        print(f"[!] ответ не разбирается как JSON, сохранён в {OUT_FILE}",
              file=sys.stderr)
        return 4

    error = data.get("error")
    if error:
        print(f"[!] Instagram вернул ошибку {error}", file=sys.stderr)
        if error in TOKEN_ERRORS:
            print("    Протухли токены fb_dtsg/lsd в data/req.sh.", file=sys.stderr)
            print("    Аккаунт, скорее всего, в порядке — нужен свежий cURL.",
                  file=sys.stderr)
        else:
            summary = (data.get("errorSummary") or "")[:150]
            print(f"    {summary}", file=sys.stderr)
        return 5

    OUT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[+] сохранено в {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
