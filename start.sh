#!/bin/bash
# ===============================
# stats.sh - запуск Telegram бота
# ===============================

# Включаем строгий режим, чтобы ловить ошибки
set -e

# Активируем виртуальное окружение, если оно есть
if [ -d "./venv" ]; then
    echo "Активируем виртуальное окружение..."
    source ./venv/bin/activate
else
    echo "Виртуальное окружение не найдено. Создаем новое..."
    python3 -m venv venv
    source ./venv/bin/activate
    echo "Устанавливаем зависимости..."
    pip install --upgrade pip
    pip install -r requirements.txt
fi

# Запуск бота
echo "Запуск бота..."
python3 bot.py
