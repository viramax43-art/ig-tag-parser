#!/usr/bin/env python3
"""
Автологин Instagram через instagrapi: пароль + TOTP 2FA + прокси.

Сессия (cookies) сохраняется в data/sessions/<username>.json.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

import pyotp
from instagrapi import Client
from instagrapi.exceptions import (
    BadPassword,
    ChallengeRequired,
    TwoFactorRequired,
)

from accounts import Account

from paths import SESSIONS_DIR


def session_path(username: str) -> Path:
    return SESSIONS_DIR / f"{username}.json"


def _cookie_dict(client: Client) -> Dict[str, str]:
    """Достаёт cookies из клиента в плоский dict для web-запросов."""
    cookies: Dict[str, str] = {}
    # instagrapi хранит сессию в private.session / settings
    try:
        jar = client.private.cookies
        for cookie in jar:
            cookies[cookie.name] = cookie.value
    except Exception:
        pass

    settings = client.get_settings() or {}
    for item in settings.get("cookies", []) or []:
        name = item.get("name")
        value = item.get("value")
        if name and value is not None:
            cookies[name] = value

    # Гарантируем ключевые поля из authorization_data, если есть.
    auth = settings.get("authorization_data") or {}
    if auth.get("sessionid") and "sessionid" not in cookies:
        cookies["sessionid"] = auth["sessionid"]
    if auth.get("ds_user_id") and "ds_user_id" not in cookies:
        cookies["ds_user_id"] = str(auth["ds_user_id"])

    return cookies


def save_cookies(account: Account, cookies: Dict[str, str],
                 settings: Optional[dict] = None) -> Path:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = session_path(account.username)
    payload = {
        "username": account.username,
        "proxy": account.proxy_raw,
        "cookies": cookies,
        "authorization": account.authorization or "",
        "settings": settings or {},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def save_session(account: Account, client: Client) -> Path:
    return save_cookies(account, _cookie_dict(client), client.get_settings())


def load_session_file(username: str) -> Optional[dict]:
    path = session_path(username)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def cookies_header(cookies: Dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)


def _make_client(proxy_url: str) -> Client:
    client = Client()
    client.delay_range = [1, 3]
    client.set_proxy(proxy_url)
    return client


def _totp_code(secret: str) -> str:
    return pyotp.TOTP(secret).now()


def login(account: Account, force: bool = False) -> Dict[str, str]:
    """
    Логин (или восстановление сессии). Возвращает dict cookies.
    force=True — игнорировать кэш/дамп и логиниться заново.
    """
    if not force:
        # 1) cookies прямо из accounts.txt (дамп сессии)
        if account.cookies.get("sessionid"):
            save_cookies(account, dict(account.cookies))
            return dict(account.cookies)

        # 2) кэш на диске
        cached = load_session_file(account.username)
        if cached and cached.get("cookies", {}).get("sessionid"):
            try:
                client = _make_client(account.proxy_url)
                if cached.get("settings"):
                    client.set_settings(cached["settings"])
                client.set_proxy(account.proxy_url)
                client.get_timeline_feed()
                cookies = _cookie_dict(client)
                if cookies.get("sessionid"):
                    save_session(account, client)
                    return cookies
            except Exception:
                pass
            cookies = cached.get("cookies") or {}
            if cookies.get("sessionid"):
                return cookies

    client = _make_client(account.proxy_url)
    code = _totp_code(account.totp_secret)
    try:
        try:
            client.login(
                account.username,
                account.password,
                verification_code=code,
            )
        except TypeError:
            client.login(account.username, account.password)
    except TwoFactorRequired:
        try:
            client.two_factor_login(code)
        except AttributeError:
            client.login(
                account.username,
                account.password,
                verification_code=_totp_code(account.totp_secret),
            )
    except BadPassword as exc:
        raise RuntimeError(f"неверный пароль для {account.username}: {exc}") from exc
    except ChallengeRequired as exc:
        raise RuntimeError(
            f"challenge для {account.username}: нужен вход в браузере"
        ) from exc

    cookies = _cookie_dict(client)

    if not cookies.get("sessionid"):
        raise RuntimeError(f"логин {account.username}: нет sessionid в cookies")

    save_session(account, client)
    return cookies


def relogin(account: Account) -> Dict[str, str]:
    path = session_path(account.username)
    if path.exists():
        path.unlink()
    # При перелогине дамп из файла уже мог протухнуть — идём через пароль+2FA.
    account.cookies = {}
    return login(account, force=True)
