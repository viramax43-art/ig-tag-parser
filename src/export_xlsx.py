#!/usr/bin/env python3
"""
Выгрузка собранных аккаунтов в XLSX.

  python src/export_xlsx.py                 — все теги
  python src/export_xlsx.py маникюр гельлак — только указанные
  SKIP_PRIVATE=1 python src/export_xlsx.py  — без закрытых профилей
  ONLY_RU=1 python src/export_xlsx.py       — только кириллические аккаунты

Лист "Аккаунты"  — уникальные аккаунты с ссылками
Лист "По тегам"  — сколько собрано с каждого тега
"""
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

# Поиск Instagram по ключевому слову подмешивает глобальный контент: по
# русскому тегу приходят бразильские, иранские и азиатские мастера. Отличаем
# их по алфавиту имени профиля, иначе клиент получает нецелевую базу.
CYRILLIC = re.compile(r"[а-яёА-ЯЁ]")
NON_LATIN = re.compile(
    r"[\u0600-\u06FF\u0590-\u05FF\u3040-\u30FF\u4E00-\u9FFF"
    r"\uAC00-\uD7AF\u0E00-\u0E7F\u0900-\u097F]")
# Транслит и города — ловят русскоязычных, пишущих имя латиницей.
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


def load_tag(tag_dir: Path):
    """Читает аккаунты одного тега. Возвращает (список записей, состояние)."""
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


def main() -> int:
    if not TAGS_DIR.exists():
        print(f"[!] нет папки {TAGS_DIR}", file=sys.stderr)
        return 1

    wanted = {a.lstrip("#").lower() for a in sys.argv[1:]}
    accounts = {}   # username -> запись
    tag_rows = []
    skipped_private = 0
    skipped_foreign = 0

    for tag_dir in sorted(TAGS_DIR.iterdir()):
        if not tag_dir.is_dir():
            continue
        if wanted and tag_dir.name.lower() not in wanted:
            continue

        records, state = load_tag(tag_dir)
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
                # Аккаунт встретился ещё в одном теге — копим список тегов.
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

    if not accounts:
        print("[!] аккаунтов не найдено", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    out_path = OUT_DIR / f"accounts_{stamp}.xlsx"

    # constant_memory: пишем строки потоком, не держим книгу в памяти —
    # на сотнях тысяч строк иначе съедается вся RAM.
    book = xlsxwriter.Workbook(str(out_path), {"constant_memory": True})
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

    for row, rec in enumerate(sorted(accounts.values(),
                                     key=lambda r: -len(r["tags"])), start=1):
        sheet.write_string(row, 0, rec["username"])
        sheet.write_url(row, 1, rec["url"], link, rec["url"])
        sheet.write_string(row, 2, rec["full_name"])
        sheet.write_string(row, 3, rec["region"])
        sheet.write_string(row, 4, flag(rec["is_private"]))
        sheet.write_string(row, 5, flag(rec["is_verified"]))
        sheet.write_string(row, 6, ", ".join(rec["tags"]))
        sheet.write_number(row, 7, len(rec["tags"]))

    stats = book.add_worksheet("По тегам")
    for col, name in enumerate(["Тег", "Аккаунтов", "Страниц",
                                "Постов", "Добит"]):
        stats.write(0, col, name, header)
    stats.set_column(0, 0, 34)
    stats.set_column(1, 4, 12)
    stats.freeze_panes(1, 0)
    for row, item in enumerate(sorted(tag_rows,
                                      key=lambda r: -r["accounts"]), start=1):
        stats.write_string(row, 0, item["tag"])
        stats.write_number(row, 1, item["accounts"])
        stats.write_number(row, 2, item["pages"])
        stats.write_number(row, 3, item["posts"])
        stats.write_string(row, 4, item["finished"])

    book.close()

    print("=" * 60)
    print(f"  файл:                 {out_path}")
    print(f"  уникальных аккаунтов: {len(accounts)}")
    print(f"  тегов:                {len(tag_rows)}")
    by_region = {}
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
