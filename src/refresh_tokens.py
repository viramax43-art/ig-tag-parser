#!/usr/bin/env python3
"""
Автообновление токенов fb_dtsg / lsd / jazoest в data/req.sh.

Токены живут несколько часов, куки — месяцами. Скрипт берёт куки из
req.sh, тянет главную страницу Instagram и подставляет свежие токены.
Это убирает ручной перезахват cURL каждые 3-4 часа.

  python src/refresh_tokens.py          — проверить, ничего не менять
  python src/refresh_tokens.py --write  — обновить data/req.sh
"""
import re
import shlex
import shutil
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

from curl_cffi import requests

from paths import REQ_FILE as CURL_FILE

HOME_URL = "https://www.instagram.com/"

VALUE_FLAGS = {"-H", "--header", "--data-raw", "--data", "-d", "--data-binary",
               "-X", "--request", "-b", "--cookie", "-e", "--referer",
               "-A", "--user-agent"}

# Instagram меняет разметку, поэтому пробуем несколько вариантов.
DTSG_PATTERNS = [
    r'"DTSGInitialData",\[\],\{"token":"([^"]+)"',
    r'"DTSGInitData",\[\],\{"token":"([^"]+)"',
    r'name="fb_dtsg"\s+value="([^"]+)"',
    r'\\"dtsg\\":\{\\"token\\":\\"([^"\\]+)',
]
LSD_PATTERNS = [
    r'"LSD",\[\],\{"token":"([^"]+)"',
    r'name="lsd"\s+value="([^"]+)"',
]


def parse_curl(raw: str) -> dict:
    raw = raw.replace("\\\n", " ")
    tokens = shlex.split(raw)
    if not tokens or tokens[0] != "curl":
        raise ValueError("файл должен начинаться со слова 'curl'")

    headers, body = {}, None
    i = 1
    while i < len(tokens):
        token = tokens[i]
        if token in VALUE_FLAGS:
            i += 1
            value = tokens[i]
            if token in ("-H", "--header"):
                key, _, val = value.partition(":")
                headers[key.strip().lower()] = val.strip()
            elif token in ("-b", "--cookie"):
                headers["cookie"] = value
            elif token in ("-A", "--user-agent"):
                headers["user-agent"] = value
            elif token not in ("-X", "--request", "-e", "--referer"):
                body = value
        i += 1
    if not body:
        raise ValueError("в команде нет тела запроса (--data-raw)")
    return {"headers": headers, "body": body}


def find_first(patterns, text):
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1), pattern
    return None, None


def jazoest_from(token: str) -> str:
    """jazoest = '2' + сумма кодов символов токена."""
    return "2" + str(sum(ord(ch) for ch in token))


def main() -> int:
    write = "--write" in sys.argv

    if not CURL_FILE.exists():
        print(f"[!] нет файла {CURL_FILE}", file=sys.stderr)
        return 1

    raw = CURL_FILE.read_text(encoding="utf-8")
    try:
        req = parse_curl(raw)
    except ValueError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1

    pairs = parse_qsl(req["body"], keep_blank_values=True)
    current = dict(pairs)
    old_dtsg = current.get("fb_dtsg", "")
    old_lsd = current.get("lsd", "")
    old_jazoest = current.get("jazoest", "")

    # Самопроверка формулы jazoest на данных, которые уже есть в req.sh.
    formula_ok = None
    if old_dtsg and old_jazoest:
        formula_ok = jazoest_from(old_dtsg) == old_jazoest
        print(f"[i] формула jazoest {'совпала' if formula_ok else 'НЕ совпала'} "
              f"с текущим значением в req.sh")

    cookie = req["headers"].get("cookie", "")
    if "sessionid" not in cookie:
        print("[!] в req.sh нет cookie с sessionid", file=sys.stderr)
        return 1

    print(f"[i] тяну {HOME_URL}")
    try:
        resp = requests.get(
            HOME_URL,
            headers={
                "cookie": cookie,
                "user-agent": req["headers"].get("user-agent", ""),
                "accept": "text/html,application/xhtml+xml",
                "accept-language": req["headers"].get("accept-language", "ru-RU,ru;q=0.9"),
            },
            impersonate="chrome",
            timeout=45,
        )
    except Exception as exc:
        print(f"[!] запрос не прошёл: {exc}", file=sys.stderr)
        return 2

    print(f"[i] HTTP {resp.status_code}, {len(resp.text)} символов")
    if resp.status_code != 200:
        print("[!] ожидали 200", file=sys.stderr)
        return 3

    html = resp.text
    new_dtsg, dtsg_pat = find_first(DTSG_PATTERNS, html)
    new_lsd, lsd_pat = find_first(LSD_PATTERNS, html)

    if not new_dtsg:
        print("[!] fb_dtsg в HTML не найден — разметка Instagram изменилась",
              file=sys.stderr)
        print("    нужен ручной перезахват cURL", file=sys.stderr)
        return 4

    print(f"[+] fb_dtsg найден шаблоном: {dtsg_pat}")
    print(f"    было:  {old_dtsg[:28]}...")
    print(f"    стало: {new_dtsg[:28]}...")
    print(f"    {'ИЗМЕНИЛСЯ' if new_dtsg != old_dtsg else 'тот же'}")

    if new_lsd:
        print(f"[+] lsd найден: {new_lsd[:20]}... "
              f"({'изменился' if new_lsd != old_lsd else 'тот же'})")
    else:
        print("[~] lsd в HTML не найден, оставляю прежний")

    new_jazoest = jazoest_from(new_dtsg)
    print(f"[i] jazoest пересчитан: {new_jazoest}")

    if not write:
        print()
        print("Ничего не записано. Чтобы применить:")
        print("  python src/refresh_tokens.py --write")
        return 0

    replacements = {"fb_dtsg": new_dtsg, "jazoest": new_jazoest}
    if new_lsd:
        replacements["lsd"] = new_lsd

    new_pairs = [(k, replacements.get(k, v)) for k, v in pairs]
    new_body = urlencode(new_pairs)

    # Бэкап на случай, если новые токены не подойдут.
    backup = CURL_FILE.with_suffix(".sh.bak")
    shutil.copy2(CURL_FILE, backup)

    # Тело в curl-команде лежит в кавычках после --data-raw.
    old_body = req["body"]
    if old_body not in raw:
        print("[!] не нашёл тело запроса в файле для замены", file=sys.stderr)
        return 5
    CURL_FILE.write_text(raw.replace(old_body, new_body, 1), encoding="utf-8")

    print()
    print(f"[+] {CURL_FILE} обновлён")
    print(f"[i] бэкап: {backup}")
    print()
    print("Проверьте, что сессия жива:")
    print("  python src/fetch_from_curl.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
