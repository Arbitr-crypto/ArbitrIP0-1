# Используем официальный образ Python 3.10
FROM python:3.10-slim

# Устанавливаем рабочую директорию внутри контейнера
WORKDIR /app

# Копируем файл зависимостей и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь исходный код проекта
COPY . .

# Команда для запуска бота
CMD ["python", "bot.py"]
