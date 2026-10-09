"""Durable state and proxy-pool storage for IG Tag Parser.

The database is deliberately small and local. SQLite WAL gives the GUI a consistent
read view while workers commit page results frequently. All mutating operations are
transactional and safe to call from multiple worker processes.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable

from paths import DATA_DIR

DB_PATH = DATA_DIR / "parser.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS proxies (
    id INTEGER PRIMARY KEY,
    proxy_key TEXT NOT NULL UNIQUE,
    raw TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'available',
    assigned_account TEXT,
    failure_count INTEGER NOT NULL DEFAULT 0,
    cooldown_until REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_proxies_available
    ON proxies(status, cooldown_until, assigned_account);

CREATE TABLE IF NOT EXISTS accounts (
    username TEXT PRIMARY KEY,
    proxy_id INTEGER,
    status TEXT NOT NULL DEFAULT 'ready',
    last_error TEXT,
    updated_at REAL NOT NULL DEFAULT 0,
    FOREIGN KEY(proxy_id) REFERENCES proxies(id)
);

CREATE TABLE IF NOT EXISTS tags (
    query TEXT PRIMARY KEY,
    cursor_key TEXT,
    end_cursor TEXT,
    page_num INTEGER NOT NULL DEFAULT 0,
    total_items INTEGER NOT NULL DEFAULT 0,
    total_videos INTEGER NOT NULL DEFAULT 0,
    total_accounts INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    stop_reason TEXT,
    updated_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS seen_posts (
    tag TEXT NOT NULL,
    item_key TEXT NOT NULL,
    code TEXT,
    payload TEXT NOT NULL,
    PRIMARY KEY(tag, item_key),
    FOREIGN KEY(tag) REFERENCES tags(query) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_seen_posts_code ON seen_posts(tag, code);

CREATE TABLE IF NOT EXISTS profiles (
    username TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tag_profiles (
    tag TEXT NOT NULL,
    username TEXT NOT NULL,
    first_seen_page INTEGER NOT NULL,
    PRIMARY KEY(tag, username),
    FOREIGN KEY(tag) REFERENCES tags(query) ON DELETE CASCADE,
    FOREIGN KEY(username) REFERENCES profiles(username) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    created_at REAL NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    account TEXT,
    tag TEXT,
    message TEXT NOT NULL
);
"""


def _now() -> float:
    return time.time()


def _key(raw: str) -> str:
    return hashlib.sha256(raw.strip().encode("utf-8")).hexdigest()


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    # Avoid re-running journal_mode=WAL on every open — it locks under load.
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA cache_size=-65536")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    mode = con.execute("PRAGMA journal_mode").fetchone()[0]
    if str(mode).lower() != "wal":
        con.execute("PRAGMA journal_mode=WAL")
    return con


def connect_readonly(path: Path = DB_PATH) -> sqlite3.Connection | None:
    if not Path(path).exists():
        return None
    uri = f"file:{Path(path).resolve().as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con


class StateDB:
    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self._local = threading.local()

    def connection(self) -> sqlite3.Connection:
        con = getattr(self._local, "connection", None)
        if con is None:
            con = connect(self.path)
            self._local.connection = con
        return con

    def close(self) -> None:
        con = getattr(self._local, "connection", None)
        if con is not None:
            con.close()
            self._local.connection = None

    def sync_proxies(self, raws: Iterable[str]) -> None:
        con = self.connection()
        now = _now()
        con.execute("BEGIN IMMEDIATE")
        try:
            for raw in dict.fromkeys(r.strip() for r in raws if r and r.strip()):
                con.execute(
                    """INSERT INTO proxies(proxy_key,raw,updated_at)
                       VALUES(?,?,?) ON CONFLICT(proxy_key) DO UPDATE SET raw=excluded.raw""",
                    (_key(raw), raw, now),
                )
            con.commit()
        except Exception:
            con.rollback()
            raise

    def ensure_account(self, username: str, preferred_raw: str = "") -> None:
        con = self.connection()
        now = _now()
        con.execute(
            """INSERT INTO accounts(username,updated_at) VALUES(?,?)
               ON CONFLICT(username) DO UPDATE SET updated_at=excluded.updated_at""",
            (username, now),
        )
        if preferred_raw:
            con.execute(
                """INSERT INTO proxies(proxy_key,raw,updated_at) VALUES(?,?,?)
                   ON CONFLICT(proxy_key) DO UPDATE SET raw=excluded.raw""",
                (_key(preferred_raw), preferred_raw, now),
            )

    def assign_proxy(self, username: str, preferred_raw: str = "") -> str | None:
        """Return a persistent exclusive proxy assignment for an account."""
        con = self.connection()
        now = _now()
        con.execute("BEGIN IMMEDIATE")
        try:
            self.ensure_account(username, preferred_raw)
            row = con.execute(
                """SELECT p.raw FROM accounts a JOIN proxies p ON p.id=a.proxy_id
                   WHERE a.username=? AND p.status='assigned' AND p.cooldown_until<=?""",
                (username, now),
            ).fetchone()
            if row:
                con.commit()
                return row["raw"]

            # Release a stale/non-healthy assignment owned by this account only.
            con.execute(
                """UPDATE proxies SET assigned_account=NULL,status='available',updated_at=?
                   WHERE assigned_account=? AND status='assigned'""",
                (now, username),
            )
            con.execute("UPDATE accounts SET proxy_id=NULL WHERE username=?", (username,))

            candidate = None
            if preferred_raw:
                candidate = con.execute(
                    """SELECT id,raw FROM proxies
                       WHERE proxy_key=? AND assigned_account IS NULL
                         AND status NOT IN ('disabled','bad') AND cooldown_until<=?""",
                    (_key(preferred_raw), now),
                ).fetchone()
            if candidate is None:
                candidate = con.execute(
                    """SELECT id,raw FROM proxies
                       WHERE assigned_account IS NULL
                         AND status NOT IN ('disabled','bad') AND cooldown_until<=?
                       ORDER BY failure_count, id LIMIT 1""",
                    (now,),
                ).fetchone()
            if candidate is None:
                con.commit()
                return None
            con.execute(
                "UPDATE proxies SET assigned_account=?,status='assigned',updated_at=? WHERE id=?",
                (username, now, candidate["id"]),
            )
            con.execute(
                "UPDATE accounts SET proxy_id=?,status='ready',last_error=NULL,updated_at=? WHERE username=?",
                (candidate["id"], now, username),
            )
            con.commit()
            return candidate["raw"]
        except Exception:
            con.rollback()
            raise

    def replace_proxy(self, username: str, bad_raw: str, reason: str, cooldown: int = 300) -> str | None:
        """Quarantine a proxy and atomically assign a free replacement."""
        con = self.connection()
        now = _now()
        con.execute("BEGIN IMMEDIATE")
        try:
            bad = con.execute("SELECT id FROM proxies WHERE proxy_key=?", (_key(bad_raw),)).fetchone()
            if bad:
                con.execute(
                    """UPDATE proxies SET status='bad',assigned_account=NULL,
                       failure_count=failure_count+1,cooldown_until=?,last_error=?,updated_at=?
                       WHERE id=?""",
                    (now + cooldown, reason[:500], now, bad["id"]),
                )
            con.execute(
                """UPDATE accounts SET proxy_id=NULL,status='waiting_proxy',last_error=?,updated_at=?
                   WHERE username=?""",
                (reason[:500], now, username),
            )
            candidate = con.execute(
                """SELECT id,raw FROM proxies
                   WHERE assigned_account IS NULL AND status NOT IN ('disabled','bad')
                     AND cooldown_until<=? ORDER BY failure_count,id LIMIT 1""",
                (now,),
            ).fetchone()
            if candidate is None:
                con.commit()
                return None
            con.execute(
                "UPDATE proxies SET assigned_account=?,status='assigned',updated_at=? WHERE id=?",
                (username, now, candidate["id"]),
            )
            con.execute(
                "UPDATE accounts SET proxy_id=?,status='ready',last_error=NULL,updated_at=? WHERE username=?",
                (candidate["id"], now, username),
            )
            con.commit()
            return candidate["raw"]
        except Exception:
            con.rollback()
            raise

    def mark_account(self, username: str, status: str, error: str = "") -> None:
        self.connection().execute(
            "UPDATE accounts SET status=?,last_error=?,updated_at=? WHERE username=?",
            (status, error[:500], _now(), username),
        )

    def event(self, level: str, kind: str, message: str, account: str = "", tag: str = "") -> None:
        self.connection().execute(
            "INSERT INTO events(created_at,level,kind,account,tag,message) VALUES(?,?,?,?,?,?)",
            (_now(), level, kind, account, tag, message[:2000]),
        )

    def load_tag(self, query: str) -> dict:
        row = self.connection().execute("SELECT * FROM tags WHERE query=?", (query,)).fetchone()
        return dict(row) if row else {}

    def load_seen(self, query: str) -> tuple[set[str], set[str]]:
        rows = self.connection().execute(
            "SELECT item_key,code FROM seen_posts WHERE tag=?", (query,)
        ).fetchall()
        keys = {r["item_key"] for r in rows}
        codes = {r["code"] for r in rows if r["code"]}
        return keys, codes

    def save_page(self, query: str, page: dict, number: int, state: dict) -> None:
        """Fast checkpoint: tag cursor + new profiles only (no heavy post blobs)."""
        con = self.connection()
        now = _now()
        items = page.get("items") or []
        profile_rows = []
        tag_profile_rows = []
        for item in items:
            if not isinstance(item, dict):
                continue
            user = item.get("user") or {}
            username = user.get("username")
            if not username:
                continue
            profile_rows.append(
                (username, json.dumps(user, ensure_ascii=False), now)
            )
            tag_profile_rows.append((query, username, number))

        # DEFERRED: don't fight other workers for an immediate write lock.
        con.execute("BEGIN")
        try:
            con.execute(
                """INSERT INTO tags(query,updated_at) VALUES(?,?)
                   ON CONFLICT(query) DO NOTHING""",
                (query, now),
            )
            if profile_rows:
                # IGNORE existing usernames — rewriting the same profiles every
                # page was a major slowdown as the scrape grew.
                con.executemany(
                    """INSERT OR IGNORE INTO profiles(username,payload,updated_at)
                       VALUES(?,?,?)""",
                    profile_rows,
                )
            if tag_profile_rows:
                con.executemany(
                    "INSERT OR IGNORE INTO tag_profiles(tag,username,first_seen_page) VALUES(?,?,?)",
                    tag_profile_rows,
                )
            con.execute(
                """INSERT INTO tags(query,cursor_key,end_cursor,page_num,total_items,total_videos,
                   total_accounts,status,stop_reason,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(query) DO UPDATE SET
                   cursor_key=excluded.cursor_key,end_cursor=excluded.end_cursor,
                   page_num=excluded.page_num,total_items=excluded.total_items,
                   total_videos=excluded.total_videos,total_accounts=excluded.total_accounts,
                   status=excluded.status,stop_reason=excluded.stop_reason,updated_at=excluded.updated_at""",
                (
                    query,
                    state.get("cursor_key"),
                    state.get("end_cursor"),
                    state.get("page_num", number),
                    state.get("total_items", 0),
                    state.get("total_videos", 0),
                    state.get("total_accounts", 0),
                    state.get("status", "running"),
                    state.get("stop_reason"),
                    now,
                ),
            )
            con.commit()
        except Exception:
            con.rollback()
            raise

    def save_tag(self, query: str, state: dict) -> None:
        con = self.connection()
        con.execute(
            """INSERT INTO tags(query,cursor_key,end_cursor,page_num,total_items,total_videos,
               total_accounts,status,stop_reason,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(query) DO UPDATE SET
               cursor_key=excluded.cursor_key,end_cursor=excluded.end_cursor,
               page_num=excluded.page_num,total_items=excluded.total_items,
               total_videos=excluded.total_videos,total_accounts=excluded.total_accounts,
               status=excluded.status,stop_reason=excluded.stop_reason,updated_at=excluded.updated_at""",
            (
                query,
                state.get("cursor_key"),
                state.get("end_cursor"),
                state.get("page_num", 0),
                state.get("total_items", 0),
                state.get("total_videos", 0),
                state.get("total_accounts", 0),
                state.get("status", "pending"),
                state.get("stop_reason"),
                _now(),
            ),
        )


DB = StateDB()


def reset_database(path: Path = DB_PATH, *, clear_tag_files: bool = True) -> list[str]:
    """Delete the SQLite DB files and open a fresh empty schema.

    Also clears data/tags/* progress folders (state.json / jsonl) so a «сброс БД»
    действительно стартует с нуля — иначе probe поднимает старый JSON и пишет
    «со стр. N». Excel/exports/sessions не трогаем.
    """
    from paths import TAGS_DIR

    removed: list[str] = []
    try:
        DB.close()
    except Exception:
        pass

    candidates = [
        Path(path),
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
    ]
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            candidate.unlink()
            removed.append(candidate.name)
        except OSError:
            time.sleep(0.2)
            candidate.unlink()
            removed.append(candidate.name)

    if clear_tag_files and TAGS_DIR.exists():
        import shutil

        for child in list(TAGS_DIR.iterdir()):
            try:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                    removed.append(f"tags/{child.name}/")
                else:
                    child.unlink(missing_ok=True)
                    removed.append(f"tags/{child.name}")
            except OSError:
                pass

    con = connect(path)
    con.close()
    try:
        DB.close()
    except Exception:
        pass
    return removed

