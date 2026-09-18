#!/bin/bash
# Основной сбор аккаунтов по списку тегов из data/tags.txt.
# Можно закрывать и запускать снова — продолжит с места остановки.

cd "$(dirname "$0")" || exit 1
clear
echo "=============================================="
echo "  СБОР АККАУНТОВ"
echo "=============================================="
echo

if [ ! -d venv ]; then
    echo "[!] Окружение не создано."
    echo "    Сначала откройте '1 Настройка.command'"
    echo
    read -n 1 -s -r -p "Нажмите любую клавишу для выхода"
    exit 1
fi

if [ ! -f data/req.sh ] || [ "$(wc -c < data/req.sh | tr -d ' ')" -lt 1000 ]; then
    echo "[!] Нет сессии Instagram."
    echo "    Откройте '2 Обновить сессию.command'"
    echo
    read -n 1 -s -r -p "Нажмите любую клавишу для выхода"
    exit 1
fi

source venv/bin/activate

echo "Список тегов: data/tags.txt ($(grep -c . data/tags.txt | tr -d ' ') шт.)"
echo
echo "Сбор идёт долго — часы. Не закрывайте окно и не"
echo "усыпляйте ноутбук. Прогресс сохраняется постоянно:"
echo "если прервётся, запустите этот файл снова."
echo
read -n 1 -s -r -p "Нажмите любую клавишу для старта"
echo
echo

MAX_CONSECUTIVE_FAILURES=10 \
    python src/run_tags.py data/tags.txt

echo
echo "=============================================="
echo "Чтобы получить файл Excel, откройте"
echo "'4 Выгрузить в Excel.command'"
echo "=============================================="
echo
read -n 1 -s -r -p "Нажмите любую клавишу для выхода"
