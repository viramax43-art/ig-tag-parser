"""Pool synchronisation helpers.

The legacy text files remain the editable input format. This module makes proxy
assignment persistent and exclusive in SQLite, so it is safe for workers to call
it concurrently.
"""
from __future__ import annotations

from pathlib import Path

from accounts import load_proxy_lines
from paths import resolve_proxies_file
from state_db import DB


def sync_proxy_pool(path: Path | None = None) -> list[str]:
    raws = load_proxy_lines(path or resolve_proxies_file())
    DB.sync_proxies(raws)
    return raws


def assign_account_proxy(username: str, preferred_raw: str = "") -> str | None:
    return DB.assign_proxy(username, preferred_raw)


def replace_account_proxy(username: str, bad_raw: str, reason: str) -> str | None:
    return DB.replace_proxy(username, bad_raw, reason)
