#!/usr/bin/env python3
"""
Разбирает data/sample_response.json и печатает безопасный отчёт о структуре:
где массив постов, где курсор пагинации, где автор.
Секретов не выводит — отчёт можно пересылать.
"""
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
IN_FILE = BASE_DIR / "data" / "sample_response.json"

CURSOR_KEYS = {"end_cursor", "next_max_id", "max_id", "next_cursor",
               "has_next_page", "more_available", "next_page", "page_info"}
MEDIA_MARKERS = {"product_type", "media_type", "is_video", "code",
                 "shortcode", "video_versions", "play_count"}
USER_MARKERS = {"username", "full_name", "profile_pic_url"}

MAX_LIST_SAMPLE = 2


def walk(node, path, out):
    """Рекурсивно обходит JSON, складывает находки в out."""
    if isinstance(node, dict):
        keys = set(node.keys())
        for key in sorted(keys & CURSOR_KEYS):
            value = node[key]
            if isinstance(value, (str, bool, int, type(None))):
                shown = repr(value)[:60]
            else:
                shown = f"<{type(value).__name__}>"
            out["cursors"].append((f"{path}.{key}", shown))
        if len(keys & MEDIA_MARKERS) >= 2:
            out["media"].append((path, sorted(keys)))
        if keys & USER_MARKERS:
            out["users"].append((path, sorted(keys & USER_MARKERS)))
        for key, value in node.items():
            walk(value, f"{path}.{key}", out)
    elif isinstance(node, list):
        if len(node) >= 3 and node and isinstance(node[0], dict):
            out["lists"].append((path, len(node)))
        for idx, value in enumerate(node[:MAX_LIST_SAMPLE]):
            walk(value, f"{path}[{idx}]", out)


def main() -> int:
    if not IN_FILE.exists():
        print(f"[!] нет файла {IN_FILE}", file=sys.stderr)
        return 1

    try:
        data = json.loads(IN_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[!] битый JSON: {exc}", file=sys.stderr)
        return 1

    out = {"lists": [], "cursors": [], "media": [], "users": []}
    walk(data, "$", out)

    print("=" * 60)
    print("МАССИВЫ (кандидаты на ленту, по убыванию длины)")
    print("=" * 60)
    for path, size in sorted(out["lists"], key=lambda x: -x[1])[:10]:
        print(f"  {size:>4} эл.  {path}")
    if not out["lists"]:
        print("  не найдены")

    print()
    print("=" * 60)
    print("КУРСОРЫ ПАГИНАЦИИ")
    print("=" * 60)
    for path, value in out["cursors"][:20]:
        print(f"  {path}")
        print(f"        = {value}")
    if not out["cursors"]:
        print("  не найдены")

    print()
    print("=" * 60)
    print("ОБЪЕКТЫ ПОСТОВ")
    print("=" * 60)
    seen = set()
    for path, keys in out["media"]:
        sig = tuple(keys)
        if sig in seen:
            continue
        seen.add(sig)
        print(f"  путь: {path}")
        print(f"  поля: {', '.join(keys)}")
        print()
        if len(seen) >= 2:
            break
    if not out["media"]:
        print("  не найдены")
        print()

    print("=" * 60)
    print("ОБЪЕКТЫ АВТОРОВ")
    print("=" * 60)
    seen_users = set()
    for path, keys in out["users"]:
        generic = path.replace("[0]", "[i]").replace("[1]", "[i]")
        if generic in seen_users:
            continue
        seen_users.add(generic)
        print(f"  {generic}  ->  {', '.join(keys)}")
        if len(seen_users) >= 5:
            break
    if not out["users"]:
        print("  не найдены")

    return 0


if __name__ == "__main__":
    sys.exit(main())
