#!/bin/bash
# активируем виртуальное окружение
python3 -m venv /opt/venv
source /opt/venv/bin/activate

# устанавливаем зависимости
pip install --upgrade pip
pip install -r requirements.txt

# запускаем бота
python bot.py
