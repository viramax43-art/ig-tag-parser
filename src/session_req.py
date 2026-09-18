#!/usr/bin/env python3
"""
Сборка per-user req.sh из шаблона data/req.sh + cookies аккаунта + свежие токены.

Шаблон даёт URL, GraphQL-тело и заголовки браузера.
Cookies и fb_dtsg/lsd/jazoest подставляются под конкретный аккаунт.
"""
from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import parse_qsl, urlencode

from curl_cffi import requests

from accounts import Account
from login import cookies_header

from paths import REQ_FILE as TEMPLATE
from paths import SESSIONS_DIR

VALUE_FLAGS = {"-H", "--header", "--data-raw", "--data", "-d", "--data-binary",
               "-X", "--request", "-b", "--cookie", "-e", "--referer",
               "-A", "--user-agent"}

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


def find_first(patterns, text):
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def jazoest_from(token: str) -> str:
    return "2" + str(sum(ord(ch) for ch in token))


def parse_curl(raw: str) -> dict:
    raw = raw.replace("\\\n", " ")
    tokens = shlex.split(raw)
    if not tokens or tokens[0] != "curl":
        raise ValueError("шаблон должен начинаться со слова 'curl'")

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
            elif token in ("-A", "--user-agent"):
                headers["user-agent"] = value
            elif token in ("-X", "--request", "-e", "--referer"):
                pass
            else:
                body = value
        elif token.startswith("-"):
            pass
        elif url is None:
            url = token
        i += 1

    if not url:
        raise ValueError("URL в шаблоне не найден")
    if not body:
        raise ValueError("тело (--data-raw) в шаблоне не найдено")
    return {"url": url, "headers": headers, "body": body, "raw": raw}


def refresh_web_tokens(cookie: str, proxy_url: str, user_agent: str = "",
                       accept_language: str = "ru-RU,ru;q=0.9") -> Dict[str, str]:
    """Тянет главную Instagram через proxy и возвращает fb_dtsg/lsd/jazoest."""
    headers = {
        "cookie": cookie,
        "user-agent": user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "accept": "text/html,application/xhtml+xml",
        "accept-language": accept_language,
    }
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    resp = requests.get(
        "https://www.instagram.com/",
        headers=headers,
        proxies=proxies,
        impersonate="chrome",
        timeout=45,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"главная Instagram HTTP {resp.status_code}")

    html = resp.text
    dtsg = find_first(DTSG_PATTERNS, html)
    lsd = find_first(LSD_PATTERNS, html)
    if not dtsg:
        raise RuntimeError("fb_dtsg в HTML не найден — сессия, скорее всего, мертва")
    tokens = {"fb_dtsg": dtsg, "jazoest": jazoest_from(dtsg)}
    if lsd:
        tokens["lsd"] = lsd
    return tokens


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def build_req_sh(template_raw: str, url: str, headers: dict, body: str) -> str:
    """Собирает curl-команду, совместимую с parse_curl в probe.py."""
    lines = [f"curl {_shell_quote(url)} \\"]
    for key, value in headers.items():
        if key.lower() == "cookie":
            continue
        lines.append(f"  -H {_shell_quote(f'{key}: {value}')} \\")
    cookie = headers.get("cookie") or headers.get("Cookie") or ""
    if cookie:
        lines.append(f"  -b {_shell_quote(cookie)} \\")
    lines.append(f"  --data-raw {_shell_quote(body)}")
    return "\n".join(lines) + "\n"


def user_req_path(username: str) -> Path:
    folder = SESSIONS_DIR / username
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "req.sh"


def build_session_req(account: Account, cookies: Dict[str, str],
                      template_path: Optional[Path] = None) -> Path:
    """
    Пишет data/sessions/<user>/req.sh с cookies аккаунта и свежими токенами.
    Возвращает путь к файлу.
    """
    template_path = template_path or TEMPLATE
    if not template_path.exists():
        raise FileNotFoundError(
            f"нет шаблона {template_path} — нужен хотя бы один валидный cURL "
            f"(скопируйте graphql-запрос в data/req.sh)"
        )

    parsed = parse_curl(template_path.read_text(encoding="utf-8"))
    headers = dict(parsed["headers"])
    cookie = cookies_header(cookies)
    if "sessionid" not in cookie:
        raise RuntimeError(f"{account.username}: в cookies нет sessionid")
    headers["cookie"] = cookie

    tokens = refresh_web_tokens(
        cookie=cookie,
        proxy_url=account.proxy_url,
        user_agent=headers.get("user-agent", ""),
        accept_language=headers.get("accept-language", "ru-RU,ru;q=0.9"),
    )

    pairs = parse_qsl(parsed["body"], keep_blank_values=True)
    new_pairs = [(k, tokens.get(k, v)) for k, v in pairs]
    new_body = urlencode(new_pairs)

    out = user_req_path(account.username)
    out.write_text(
        build_req_sh(parsed["raw"], parsed["url"], headers, new_body),
        encoding="utf-8",
    )
    return out


def main() -> int:
    """Ручная проверка: python src/session_req.py <username>."""
    from login import load_session_file

    if len(sys.argv) < 2:
        print("usage: python src/session_req.py <username>", file=sys.stderr)
        return 1
    username = sys.argv[1]
    cached = load_session_file(username)
    if not cached or not cached.get("cookies"):
        print(f"[!] нет data/sessions/{username}.json", file=sys.stderr)
        return 1
    proxy_raw = cached.get("proxy") or ""
    if not proxy_raw:
        print("[!] в сессии нет proxy", file=sys.stderr)
        return 1
    account = Account(
        username=username,
        password="",
        totp_secret="",
        proxy_raw=proxy_raw,
    )
    path = build_session_req(account, cached["cookies"])
    print(f"[+] записано {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
