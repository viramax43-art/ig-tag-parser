#!/bin/bash
# Показывает, как взять сессию Instagram, и сохраняет её из буфера обмена.

cd "$(dirname "$0")" || exit 1
clear
echo "=============================================="
echo "  ОБНОВЛЕНИЕ СЕССИИ INSTAGRAM"
echo "=============================================="
echo
echo "Нужно один раз в несколько месяцев или если сбор"
echo "пишет, что сессия недействительна."
echo
echo "ПОРЯДОК ДЕЙСТВИЙ В CHROME:"
echo
echo "  1. Войдите в Instagram под рабочим аккаунтом"
echo "  2. Откройте ссылку (скопируйте в адресную строку):"
echo "     https://www.instagram.com/explore/search/keyword/?q=%23manicure"
echo "  3. Нажмите F12 — откроется панель разработчика"
echo "  4. Вкладка Network, кнопка Clear (круг с чертой)"
echo "  5. В поле Filter впишите: graphql"
echo "  6. Прокрутите ленту вниз, пока не подгрузятся новые фото"
echo "  7. В списке найдите строку graphql с самым большим Size"
echo "  8. Правый клик на ней -> Copy -> Copy as cURL (bash)"
echo
echo "  ВАЖНО: после этого ничего больше не копируйте."
echo
read -n 1 -s -r -p "Сделали? Нажмите любую клавишу"
echo
echo

pbpaste > data/req.sh
SIZE=$(wc -c < data/req.sh | tr -d ' ')
echo "[i] Сохранено: $SIZE байт"
echo

if [ "$SIZE" -lt 1000 ]; then
    echo "[!] Слишком мало. В буфере обмена была не та команда."
    echo "    Повторите с шага 7, ничего не копируя после."
    rm -f data/req.sh
    echo
    read -n 1 -s -r -p "Нажмите любую клавишу для выхода"
    exit 1
fi

if ! head -c 4 data/req.sh | grep -q curl; then
    echo "[!] Это не команда curl. Повторите с шага 7."
    rm -f data/req.sh
    echo
    read -n 1 -s -r -p "Нажмите любую клавишу для выхода"
    exit 1
fi

echo "[+] Сессия сохранена."
echo
if [ -d venv ]; then
    source venv/bin/activate
    echo "[i] Проверяю связь с Instagram..."
    python src/fetch_from_curl.py
fi
echo
read -n 1 -s -r -p "Нажмите любую клавишу для выхода"
