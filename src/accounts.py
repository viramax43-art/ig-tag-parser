#!/usr/bin/env python3
"""
Загрузка рабочих аккаунтов из data/accounts.txt.

Поддерживаются два формата строки:

1) План (4 поля через ';'):
   username;password;2fa_secret;ip:port:proxyuser:proxypass

2) Дамп сессии (как в магазинах аккаунтов):
   username:password:2FA_SECRET|device;sessionid=...;ds_user_id=...;Authorization=Bearer...

   Прокси для формата (2) берётся из proxies.txt по номеру строки
   (1-я строка accounts ↔ 1-я строка proxies).

Пустые строки и строки, начинающиеся с # или //, игнорируются.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote

from paths import ACCOUNTS_FILE as DEFAULT_ACCOUNTS
from paths import PROXIES_FILE as DEFAULT_PROXIES


@dataclass
class Account:
    username: str
    password: str
    totp_secret: str
    proxy_raw: str  # host:port:user:pass
    line_no: int = 0
    cookies: Dict[str, str] = field(default_factory=dict)
    authorization: str = ""

    @property
    def proxy_host(self) -> str:
        return self.proxy_raw.split(":", 1)[0]

    @property
    def proxy_url(self) -> str:
        return proxy_to_url(self.proxy_raw)

    @property
    def label(self) -> str:
        return f"{self.username}@{self.proxy_host}"


def proxy_to_url(raw: str) -> str:
    """host:port:user:pass -> http://user:pass@host:port"""
    parts = raw.split(":")
    if len(parts) == 2:
        host, port = parts
        return f"http://{host}:{port}"
    if len(parts) >= 4:
        host, port, user = parts[0], parts[1], parts[2]
        password = ":".join(parts[3:])
        user_q = quote(user, safe="")
        pass_q = quote(password, safe="")
        return f"http://{user_q}:{pass_q}@{host}:{port}"
    raise ValueError(f"неверный формат прокси: {raw!r}")


def load_proxy_lines(path: Path = None) -> List[str]:
    path = path or DEFAULT_PROXIES
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        # http://user:pass@host:port → нормализуем в host:port:user:pass
        if line.startswith("http://") or line.startswith("https://"):
            out.append(_http_proxy_to_raw(line))
        else:
            proxy_to_url(line)  # validate
            out.append(line)
    return out


def _http_proxy_to_raw(url: str) -> str:
    """http://user:pass@host:port -> host:port:user:pass"""
    m = re.match(
        r"^https?://([^:@]+):([^@]+)@([^:/]+):(\d+)/?$",
        url.strip(),
    )
    if not m:
        raise ValueError(f"не разобрать proxy URL: {url!r}")
    user, password, host, port = m.groups()
    return f"{host}:{port}:{user}:{password}"


def _parse_cookie_tail(tail: str) -> tuple[Dict[str, str], str]:
    """Разбирает хвост вида key=value;key=value;Authorization=Bearer ..."""
    cookies: Dict[str, str] = {}
    authorization = ""
    for part in tail.split(";"):
        part = part.strip().lstrip("|").strip()
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        value = value.strip()
        if key.lower() == "authorization":
            authorization = value
            continue
        if key:
            cookies[key] = value
    return cookies, authorization


def _extract_totp(third: str) -> str:
    """Из 'SECRET' или 'SECRET|device info' достаёт base32-секрет."""
    head = third.split("|", 1)[0].strip().replace(" ", "").upper()
    if not head:
        raise ValueError("пустой 2FA secret")
    return head


def parse_planned(line: str, line_no: int) -> Optional[Account]:
    """username;password;2fa;ip:port:user:pass"""
    parts = [p.strip() for p in line.split(";")]
    if len(parts) != 4:
        return None
    username, password, totp_secret, proxy_raw = parts
    if not all((username, password, totp_secret, proxy_raw)):
        return None
    # Прокси обязан содержать ':' (host:port...), иначе это дамп.
    if proxy_raw.count(":") < 1 or "=" in proxy_raw:
        return None
    try:
        proxy_to_url(proxy_raw)
    except ValueError:
        return None
    return Account(
        username=username,
        password=password,
        totp_secret=totp_secret.replace(" ", "").upper(),
        proxy_raw=proxy_raw,
        line_no=line_no,
    )


def parse_dump(line: str, line_no: int, proxy_raw: str) -> Account:
    """
    username:password:2FA|device;cookies...
    """
    if not proxy_raw:
        raise ValueError(
            f"строка {line_no}: нет прокси — добавьте строку в proxies.txt "
            f"или укажите proxy в формате user;pass;2fa;host:port:u:p"
        )

    # credentials живут до первого ';', дальше cookies
    if ";" in line:
        head, tail = line.split(";", 1)
    else:
        head, tail = line, ""

    cred_parts = head.split(":", 2)
    if len(cred_parts) < 3:
        raise ValueError(
            f"строка {line_no}: ожидалось username:password:2fa[|device]"
        )
    username, password, third = (p.strip() for p in cred_parts)
    totp_secret = _extract_totp(third)
    cookies, authorization = _parse_cookie_tail(tail)

    proxy_to_url(proxy_raw)
    return Account(
        username=username,
        password=password,
        totp_secret=totp_secret,
        proxy_raw=proxy_raw,
        line_no=line_no,
        cookies=cookies,
        authorization=authorization,
    )


def parse_line(line: str, line_no: int = 0,
               proxy_raw: str = "") -> Optional[Account]:
    line = line.strip()
    if not line or line.startswith("#") or line.startswith("//"):
        return None

    planned = parse_planned(line, line_no)
    if planned is not None:
        return planned

    # Дамп: username:password:...
    if line.count(":") >= 2:
        return parse_dump(line, line_no, proxy_raw)

    raise ValueError(
        f"строка {line_no}: неизвестный формат "
        f"(нужно user;pass;2fa;proxy или user:pass:2fa|…;cookies)"
    )


def load_accounts(path: Path = None,
                  proxies_path: Path = None) -> List[Account]:
    path = path or DEFAULT_ACCOUNTS
    if not path.exists():
        return []

    proxies = load_proxy_lines(proxies_path)
    accounts: List[Account] = []
    proxy_idx = 0

    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
            continue

        # Для дампа — следующий свободный прокси по порядку.
        proxy_raw = ""
        planned = parse_planned(stripped, line_no)
        if planned is None and proxy_idx < len(proxies):
            proxy_raw = proxies[proxy_idx]

        try:
            account = parse_line(stripped, line_no, proxy_raw=proxy_raw)
        except ValueError as exc:
            print(f"[!] accounts.txt: {exc}")
            continue
        if account is None:
            continue

        if planned is None:
            proxy_idx += 1

        accounts.append(account)
    return accounts
