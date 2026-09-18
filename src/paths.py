#!/usr/bin/env python3
"""Корень проекта: рядом с .py в разработке, рядом с .exe в сборке."""
from __future__ import annotations

import sys
from pathlib import Path


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def get_src_dir() -> Path:
    if getattr(sys, "frozen", False):
        # В onedir-сборке исходники лежат в _internal; для import хватает sys.path.
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return get_base_dir()
    return Path(__file__).resolve().parent


BASE_DIR = get_base_dir()
SRC_DIR = get_src_dir()
DATA_DIR = BASE_DIR / "data"
TAGS_FILE = DATA_DIR / "tags.txt"
ACCOUNTS_FILE = DATA_DIR / "accounts.txt"
PROXIES_FILE = BASE_DIR / "proxies.txt"
REQ_FILE = DATA_DIR / "req.sh"
SESSIONS_DIR = DATA_DIR / "sessions"
TAGS_DIR = DATA_DIR / "tags"
EXPORTS_DIR = DATA_DIR / "exports"


def ensure_layout() -> None:
    """Создаёт data/ и пустые конфиги при первом запуске exe."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TAGS_DIR.mkdir(parents=True, exist_ok=True)
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    if not TAGS_FILE.exists():
        TAGS_FILE.write_text(
            "// один тег на строку\n"
            "маникюр\n"
            "мастерманикюра\n",
            encoding="utf-8",
        )
    if not ACCOUNTS_FILE.exists():
        ACCOUNTS_FILE.write_text(
            "# username;password;2FA_SECRET;ip:port:proxyuser:proxypass\n"
            "# или dump-строка + прокси из proxies.txt по номеру строки\n",
            encoding="utf-8",
        )
    if not PROXIES_FILE.exists():
        PROXIES_FILE.write_text(
            "# host:port:user:pass — по одной строке на аккаунт (для dump-формата)\n",
            encoding="utf-8",
        )


def invoke_cmd(script: str) -> list:
    """Команда запуска probe/refresh/export: python file.py или exe --probe."""
    if getattr(sys, "frozen", False):
        return [sys.executable, f"--{script}"]
    return [sys.executable, str(SRC_DIR / f"{script}.py")]
