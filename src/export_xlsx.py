#!/usr/bin/env python3
"""
Выгрузка собранных аккаунтов в XLSX.

  python src/export_xlsx.py                 — все теги
  python src/export_xlsx.py маникюр гельлак — только указанные
  SKIP_PRIVATE=1 python src/export_xlsx.py  — без закрытых профилей
  ONLY_RU=1 python src/export_xlsx.py       — только кириллические аккаунты

Источник по умолчанию — SQLite (data/parser.sqlite3), JSONL — запасной.

Лист "Аккаунты"  — уникальные аккаунты с ссылками
Лист "По тегам"  — сколько собрано с каждого тега
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import xlsxwriter

from paths import EXPORTS_DIR as OUT_DIR
from paths import TAGS_DIR

SKIP_PRIVATE = os.getenv("SKIP_PRIVATE") == "1"
ONLY_RU = os.getenv("ONLY_RU") == "1"
LIVE_NAME = "accounts_live.xlsx"


def archive_live_excel() -> Path | None:
    """Rename accounts_live.xlsx to a timestamped snapshot so it is preserved."""
    live = OUT_DIR / LIVE_NAME
    if not live.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = OUT_DIR / f"accounts_saved_{stamp}.xlsx"
    n = 1
    while dest.exists():
        dest = OUT_DIR / f"accounts_saved_{stamp}_{n}.xlsx"
        n += 1
    live.replace(dest)
    return dest


# Поиск Instagram по ключевому слову подмешивает глобальный контент: по
# русскому тегу приходят бразильские, иранские и азиатские мастера. Отличаем
# их по алфавиту имени профиля, иначе клиент получает нецелевую базу.
CYRILLIC = re.compile(r"[а-яёА-ЯЁ]")
NON_LATIN = re.compile(
    r"[\u0600-\u06FF\u0590-\u05FF\u3040-\u30FF\u4E00-\u9FFF"
    r"\uAC00-\uD7AF\u0E00-\u0E7F\u0900-\u097F]")
RU_LOGIN = re.compile(
    r"(nogti|manikur|manikyur|pedikur|shellac|msk|moscow|spb|piter|krasnodar|"
    r"kazan|ekb|nsk|samara|ufa|rostov|sochi|tyumen|perm|omsk|chelyab|"
    r"voronezh|volgograd|barnaul|irkutsk|krasnoyarsk)", re.I)


def detect_region(username: str, full_name: str) -> str:
    """Грубая метка происхождения аккаунта: РУ / не РУ / не определено."""
    if CYRILLIC.search(full_name):
        return "РУ"
    if NON_LATIN.search(full_name):
        return "не РУ"
    if RU_LOGIN.search(username):
        return "РУ"
    return "не определено"


def slugify(query: str) -> str:
    cleaned = [c if c.isalnum() else "_" for c in query.lstrip("#").lower()]
    return "".join(cleaned).strip("_") or "unnamed"


def load_tag_jsonl(tag_dir: Path):
    """Читает аккаунты одного тега из legacy JSONL."""
    accounts_file = tag_dir / "accounts.jsonl"
    if not accounts_file.exists():
        return [], {}

    records = []
    for line in accounts_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    state = {}
    state_file = tag_dir / "state.json"
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return records, state


def load_from_sqlite(
    wanted: set[str] | None = None,
    *,
    only_ru: bool | None = None,
    skip_private: bool | None = None,
):
    """Build accounts + tag_rows from durable SQLite state (read-only).

    only_ru / skip_private default to module env flags; pass explicitly to
    override (live export must dump the full DB, never a filtered subset).
    """
    apply_only_ru = ONLY_RU if only_ru is None else only_ru
    apply_skip_private = SKIP_PRIVATE if skip_private is None else skip_private
    try:
        from state_db import DB_PATH, connect_readonly
    except Exception:
        return None
    if not Path(DB_PATH).exists():
        return None

    con = None
    try:
        con = connect_readonly(DB_PATH)
        if con is None:
            return None
        tag_rows_db = con.execute("SELECT * FROM tags").fetchall()
        if not tag_rows_db and not con.execute(
            "SELECT 1 FROM profiles LIMIT 1"
        ).fetchone():
            return None

        # LEFT JOIN: keep every profile even if a tag link is missing.
        profiles = con.execute(
            """
            SELECT p.username, p.payload,
                   GROUP_CONCAT(tp.tag, char(31)) AS tags
            FROM profiles p
            LEFT JOIN tag_profiles tp ON tp.username = p.username
            GROUP BY p.username
            """
        ).fetchall()
    except Exception:
        return None
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass

    accounts: dict = {}
    tag_rows = []
    skipped_private = 0
    skipped_foreign = 0

    for row in tag_rows_db:
        query = row["query"] or ""
        slug = slugify(query)
        if wanted and slug.lower() not in wanted and query.lstrip("#").lower() not in wanted:
            continue
        tag_rows.append({
            "tag": slug,
            "accounts": int(row["total_accounts"] or 0),
            "pages": int(row["page_num"] or 0),
            "posts": int(row["total_items"] or 0),
            "finished": "да" if row["status"] == "finished" else "нет",
        })

    wanted_slugs = wanted or set()
    for row in profiles:
        username = row["username"]
        if not username:
            continue
        try:
            payload = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        raw_tags = [t for t in (row["tags"] or "").split("\x1f") if t]
        tag_slugs = []
        for t in raw_tags:
            slug = slugify(t)
            if wanted_slugs and slug.lower() not in wanted_slugs and t.lstrip("#").lower() not in wanted_slugs:
                continue
            tag_slugs.append(slug)
        if wanted_slugs and not tag_slugs:
            continue
        if not tag_slugs:
            tag_slugs = [slugify(t) for t in raw_tags] or ["unknown"]

        if apply_skip_private and payload.get("is_private"):
            skipped_private += 1
            continue
        full_name = payload.get("full_name") or ""
        region = detect_region(username, full_name)
        if apply_only_ru and region != "РУ":
            skipped_foreign += 1
            continue
        accounts[username] = {
            "username": username,
            "url": f"https://www.instagram.com/{username}/",
            "full_name": full_name,
            "is_private": payload.get("is_private"),
            "is_verified": payload.get("is_verified"),
            "region": region,
            "tags": sorted(set(tag_slugs)),
        }

    return accounts, tag_rows, skipped_private, skipped_foreign


def load_from_jsonl(wanted: set[str] | None = None):
    accounts: dict = {}
    tag_rows = []
    skipped_private = 0
    skipped_foreign = 0

    if not TAGS_DIR.exists():
        return accounts, tag_rows, skipped_private, skipped_foreign

    for tag_dir in sorted(TAGS_DIR.iterdir()):
        if not tag_dir.is_dir():
            continue
        if wanted and tag_dir.name.lower() not in wanted:
            continue

        records, state = load_tag_jsonl(tag_dir)
        if not records:
            continue

        for rec in records:
            username = rec.get("username")
            if not username:
                continue
            if SKIP_PRIVATE and rec.get("is_private"):
                skipped_private += 1
                continue
            region = detect_region(username, rec.get("full_name") or "")
            if ONLY_RU and region != "РУ":
                skipped_foreign += 1
                continue
            existing = accounts.get(username)
            if existing:
                if tag_dir.name not in existing["tags"]:
                    existing["tags"].append(tag_dir.name)
                continue
            accounts[username] = {
                "username": username,
                "url": rec.get("url") or f"https://www.instagram.com/{username}/",
                "full_name": rec.get("full_name") or "",
                "is_private": rec.get("is_private"),
                "is_verified": rec.get("is_verified"),
                "region": region,
                "tags": [tag_dir.name],
            }

        tag_rows.append({
            "tag": tag_dir.name,
            "accounts": len(records),
            "pages": state.get("page_num", 0),
            "posts": state.get("total_items", 0),
            "finished": "да" if state.get("finished") else "нет",
        })

    return accounts, tag_rows, skipped_private, skipped_foreign


def write_workbook(out_path: Path, accounts: dict, tag_rows: list) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    book = xlsxwriter.Workbook(str(tmp), {"constant_memory": True})
    header = book.add_format({"bold": True, "bg_color": "#DDDDDD", "border": 1})
    link = book.add_format({"font_color": "blue", "underline": 1})

    sheet = book.add_worksheet("Аккаунты")
    columns = ["Логин", "Ссылка", "Имя профиля", "Регион", "Закрытый",
               "Верифицирован", "Теги", "Тегов"]
    for col, name in enumerate(columns):
        sheet.write(0, col, name, header)
    sheet.set_column(0, 0, 24)
    sheet.set_column(1, 1, 42)
    sheet.set_column(2, 2, 30)
    sheet.set_column(3, 3, 15)
    sheet.set_column(4, 5, 14)
    sheet.set_column(6, 6, 46)
    sheet.set_column(7, 7, 8)
    sheet.freeze_panes(1, 0)

    def flag(value):
        if value is None:
            return ""
        return "да" if value else "нет"

    for row, rec in enumerate(
        sorted(accounts.values(), key=lambda r: -len(r["tags"])), start=1
    ):
        sheet.write_string(row, 0, rec["username"])
        sheet.write_url(row, 1, rec["url"], link, rec["url"])
        sheet.write_string(row, 2, rec["full_name"])
        sheet.write_string(row, 3, rec["region"])
        sheet.write_string(row, 4, flag(rec["is_private"]))
        sheet.write_string(row, 5, flag(rec["is_verified"]))
        sheet.write_string(row, 6, ", ".join(rec["tags"]))
        sheet.write_number(row, 7, len(rec["tags"]))

    stats = book.add_worksheet("По тегам")
    for col, name in enumerate(["Тег", "Аккаунтов", "Страниц", "Постов", "Добит"]):
        stats.write(0, col, name, header)
    stats.set_column(0, 0, 34)
    stats.set_column(1, 4, 12)
    stats.freeze_panes(1, 0)
    for row, item in enumerate(
        sorted(tag_rows, key=lambda r: -r["accounts"]), start=1
    ):
        stats.write_string(row, 0, item["tag"])
        stats.write_number(row, 1, item["accounts"])
        stats.write_number(row, 2, item["pages"])
        stats.write_number(row, 3, item["posts"])
        stats.write_string(row, 4, item["finished"])

    book.close()
    tmp.replace(out_path)


def collect_accounts(wanted: set[str] | None = None):
    loaded = load_from_sqlite(wanted)
    if loaded is not None and (loaded[0] or loaded[1]):
        return loaded
    return load_from_jsonl(wanted)


def export_accounts(
    out_path: Path | None = None,
    wanted: set[str] | None = None,
    *,
    quiet: bool = False,
) -> tuple[int, Path | None, int]:
    """Write Excel from SQLite (fallback JSONL). Returns (code, path, count)."""
    # Re-read filters each call — live export inherits env from parent.
    global SKIP_PRIVATE, ONLY_RU
    SKIP_PRIVATE = os.getenv("SKIP_PRIVATE") == "1"
    ONLY_RU = os.getenv("ONLY_RU") == "1"

    accounts, tag_rows, skipped_private, skipped_foreign = collect_accounts(wanted)
    if not accounts:
        if not quiet:
            print("[!] аккаунтов не найдено", file=sys.stderr)
        return 1, None, 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if out_path is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        out_path = OUT_DIR / f"accounts_{stamp}.xlsx"

    try:
        write_workbook(out_path, accounts, tag_rows)
    except PermissionError:
        # Excel often locks the live file while open — skip this tick.
        if not quiet:
            print(f"[!] файл занят (закройте в Excel): {out_path}", file=sys.stderr)
        return 2, out_path, len(accounts)
    except OSError as exc:
        if not quiet:
            print(f"[!] запись Excel: {exc}", file=sys.stderr)
        return 1, None, len(accounts)

    if not quiet:
        print("=" * 60)
        print(f"  файл:                 {out_path}")
        print(f"  уникальных аккаунтов: {len(accounts)}")
        print(f"  тегов:                {len(tag_rows)}")
        by_region: dict[str, int] = {}
        for rec in accounts.values():
            by_region[rec["region"]] = by_region.get(rec["region"], 0) + 1
        for name in ("РУ", "не определено", "не РУ"):
            if name in by_region:
                print(f"  {name:<21} {by_region[name]}")
        if SKIP_PRIVATE:
            print(f"  пропущено закрытых:   {skipped_private}")
        if ONLY_RU:
            print(f"  отсеяно нецелевых:    {skipped_foreign}")
        print("=" * 60)
    return 0, out_path, len(accounts)


def export_live(*, quiet: bool = True) -> tuple[int, Path | None, int]:
    """Overwrite the stable live workbook from the full SQLite DB.

    Always dumps every collected profile (no ONLY_RU / SKIP_PRIVATE). Those
    filters apply only to stamped exports via the Excel button — otherwise a
    resume would rewrite accounts_live.xlsx with a smaller RU-only subset and
    look like the file was reset.

    If the DB is empty, do not touch an existing live file (keeps archived /
    previous scrapes intact after a DB reset).
    """
    loaded = load_from_sqlite(None, only_ru=False, skip_private=False)
    if loaded is None or not loaded[0]:
        return 1, None, 0
    accounts, tag_rows, _, _ = loaded
    out_path = OUT_DIR / LIVE_NAME
    try:
        write_workbook(out_path, accounts, tag_rows)
    except PermissionError:
        return 2, out_path, len(accounts)
    except OSError:
        return 1, None, len(accounts)
    return 0, out_path, len(accounts)


def main() -> int:
    wanted = {a.lstrip("#").lower() for a in sys.argv[1:]} or None
    code, _, _ = export_accounts(wanted=wanted, quiet=False)
    return code


if __name__ == "__main__":
    sys.exit(main())
