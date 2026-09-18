#!/bin/bash
# Собирает все найденные аккаунты в файл Excel.

cd "$(dirname "$0")" || exit 1
clear
echo "=============================================="
echo "  ВЫГРУЗКА В EXCEL"
echo "=============================================="
echo

if [ ! -d venv ]; then
    echo "[!] Сначала откройте '1 Настройка.command'"
    echo
    read -n 1 -s -r -p "Нажмите любую клавишу для выхода"
    exit 1
fi

source venv/bin/activate
python src/export_xlsx.py
CODE=$?

if [ $CODE -eq 0 ]; then
    echo
    echo "[i] Открываю папку с файлом..."
    open data/exports 2>/dev/null
fi
echo
read -n 1 -s -r -p "Нажмите любую клавишу для выхода"
