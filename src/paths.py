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


def resolve_proxies_file() -> Path:
    """proxies.txt рядом с exe или в data/ — что реально заполнено."""
    candidates = [BASE_DIR / "proxies.txt", DATA_DIR / "proxies.txt"]
    for path in candidates:
        if not path.exists():
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s and not s.startswith("#") and not s.startswith("//"):
                    return path
        except OSError:
            continue
    return PROXIES_FILE


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
            "# host:port:user:pass — по одной строке на аккаунт (dump-формат)\n"
            "# Также: login:password@ip:port или http://user:pass@host:port\n"
            "# Обязательно, если Instagram заблокирован (РФ и др.): "
            "весь трафик к IG идёт через эти прокси.\n",
            encoding="utf-8",
        )
    data_proxies = DATA_DIR / "proxies.txt"
    if not data_proxies.exists():
        data_proxies.write_text(
            "# альтернатива: можно держать прокси здесь вместо корневого proxies.txt\n"
            "# host:port:user:pass\n",
            encoding="utf-8",
        )


def invoke_cmd(script: str) -> list:
    """Команда запуска probe/refresh/export: python file.py или exe --probe."""
    if getattr(sys, "frozen", False):
        return [sys.executable, f"--{script}"]
    return [sys.executable, str(SRC_DIR / f"{script}.py")]
