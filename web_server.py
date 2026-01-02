"""
Простой веб-сервер для Render
"""
import os
import sys
import subprocess
import threading
from flask import Flask

app = Flask(__name__)

# Функция для запуска бота в отдельном потоке
def run_bot():
    """Запускает бота в отдельном процессе"""
    try:
        print("🚀 Запуск Arbitr Bot...")
        subprocess.run([sys.executable, "bot.py"], check=True)
    except Exception as e:
        print(f"❌ Ошибка при запуске бота: {e}")
        sys.exit(1)

@app.route('/')
def home():
    return "✅ Arbitr Bot is running!"

@app.route('/health')
def health():
    return "OK", 200

if __name__ == "__main__":
    # Запускаем бота в отдельном потоке
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    # Запускаем веб-сервер
    port = int(os.getenv("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
