#!/usr/bin/env python3
"""
Считает пересечение аккаунтов между собранными тегами.

Отвечает на главный вопрос планирования: сколько тегов нужно,
чтобы набрать целевое число уникальных аккаунтов.

Запуск:
  python src/overlap.py                — все собранные теги
  python src/overlap.py coffee espresso — только указанные
"""
import json
import sys
from itertools import combinations
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
TAGS_DIR = BASE_DIR / "data" / "tags"


def load_accounts(tag_dir: Path) -> set:
    path = tag_dir / "accounts.jsonl"
    if not path.exists():
        return set()
    users = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            users.add(json.loads(line)["username"])
        except (json.JSONDecodeError, KeyError):
            continue
    return users


def main() -> int:
    if not TAGS_DIR.exists():
        print(f"[!] нет папки {TAGS_DIR}", file=sys.stderr)
        return 1

    wanted = set(sys.argv[1:])
    tags = {}
    for tag_dir in sorted(TAGS_DIR.iterdir()):
        if not tag_dir.is_dir():
            continue
        if wanted and tag_dir.name not in wanted:
            continue
        users = load_accounts(tag_dir)
        if users:
            tags[tag_dir.name] = users

    if not tags:
        print("[!] собранных тегов не найдено", file=sys.stderr)
        return 1

    print("=" * 60)
    print("СОБРАНО ПО ТЕГАМ")
    print("=" * 60)
    for name, users in tags.items():
        print(f"  {name:<25} {len(users):>7} аккаунтов")

    if len(tags) < 2:
        print()
        print("[i] для расчёта пересечения нужно минимум два тега")
        return 0

    print()
    print("=" * 60)
    print("ПЕРЕСЕЧЕНИЕ ПАР")
    print("=" * 60)
    for left, right in combinations(tags, 2):
        shared = tags[left] & tags[right]
        smaller = min(len(tags[left]), len(tags[right])) or 1
        print(f"  {left} x {right}")
        print(f"    общих: {len(shared)}  ({len(shared) / smaller * 100:.1f}% "
              f"от меньшего тега)")

    # Предельный вклад: каждый следующий тег пересекается со ВСЕМ
    # накопленным объединением, а не с одним предыдущим. Падение этой
    # величины и есть насыщение — по нему видно, во что упрётся сбор.
    print()
    print("=" * 60)
    print("ПРЕДЕЛЬНЫЙ ВКЛАД (в порядке добавления)")
    print("=" * 60)
    accumulated = set()
    retentions = []
    sizes = []
    for name, users in tags.items():
        added = len(users - accumulated)
        accumulated |= users
        sizes.append(len(users))
        share = added / len(users) * 100 if users else 0
        retentions.append(share)
        print(f"  {name:<25} собрано {len(users):>6}  новых {added:>6}  "
              f"удержание {share:>3.0f}%")

    # Насыщение меряется УДЕРЖАНИЕМ, а не абсолютным вкладом: маленький
    # тег даёт мало новых просто потому, что он маленький.
    if len(retentions) >= 3:
        later = sum(retentions[1:]) / len(retentions[1:])
        print()
        print(f"  среднее удержание после первого тега: {later:.0f}%")
        if later < 40:
            print("  насыщение сильное — тегов понадобится заметно больше оценки")
        elif later < 70:
            print("  насыщение умеренное — закладывайте запас к оценке")
        else:
            print("  насыщение слабое — теги собирают разную аудиторию")

        print()
        print(f"  размер тегов: от {min(sizes)} до {max(sizes)} аккаунтов")
        if max(sizes) > min(sizes) * 3:
            print("  РАЗБРОС БОЛЬШОЙ: оценка числа тегов ниже верна только для")
            print("  тегов размером с крупнейший. Мелкие теги дают в разы меньше.")

    union = set().union(*tags.values())
    total = sum(len(u) for u in tags.values())
    dedup_loss = (1 - len(union) / total) * 100 if total else 0
    avg_new = len(union) / len(tags)

    print()
    print("=" * 60)
    print("СВОДКА")
    print("=" * 60)
    print(f"  тегов собрано:            {len(tags)}")
    print(f"  сумма по тегам:           {total}")
    print(f"  уникальных после дедупа:  {len(union)}")
    print(f"  потери на пересечении:    {dedup_loss:.1f}%")
    print(f"  новых аккаунтов на тег:   {avg_new:.0f}")
    print()
    for target in (50_000, 100_000, 150_000):
        need = target / avg_new if avg_new else 0
        print(f"  на {target:>7,} аккаунтов нужно ~{need:.0f} тегов")
    print("=" * 60)
    print("  ВАЖНО: расчёт делит объединение на число тегов и не учитывает")
    print("  насыщение. При десятках тегов это НИЖНЯЯ граница — реальное")
    print("  число тегов будет больше. Смотрите на предельный вклад выше.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
