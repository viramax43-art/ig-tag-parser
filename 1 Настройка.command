#!/bin/bash
# Первичная настройка: создаёт окружение и ставит зависимости.
# Запускать один раз после распаковки, а также если папку переносили.

cd "$(dirname "$0")" || exit 1
clear
echo "=============================================="
echo "  НАСТРОЙКА ПАРСЕРА"
echo "=============================================="
echo "Папка: $(pwd)"
echo

if ! command -v python3 >/dev/null 2>&1; then
    echo "[!] Python 3 не установлен."
    echo "    Скачайте с https://www.python.org/downloads/"
    echo
    read -n 1 -s -r -p "Нажмите любую клавишу для выхода"
    exit 1
fi
echo "[+] Python: $(python3 --version)"

if [ -d venv ]; then
    echo "[i] Старое окружение найдено, пересоздаю"
    rm -rf venv
fi

echo "[i] Создаю окружение..."
python3 -m venv venv || { echo "[!] Не удалось создать venv"; read -n 1 -s -r; exit 1; }

echo "[i] Устанавливаю библиотеки..."
source venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt || { echo "[!] Ошибка установки"; read -n 1 -s -r; exit 1; }

echo
echo "[+] Готово."
echo
if [ -d data/tags ]; then
    echo "    Папок тегов: $(ls data/tags | wc -l | tr -d ' ')"
fi
if [ -f data/req.sh ] && [ "$(wc -c < data/req.sh | tr -d ' ')" -gt 1000 ]; then
    echo "    Сессия Instagram: есть"
else
    echo "    Сессия Instagram: НЕТ — откройте '2 Обновить сессию.command'"
fi
echo
read -n 1 -s -r -p "Нажмите любую клавишу для выхода"
