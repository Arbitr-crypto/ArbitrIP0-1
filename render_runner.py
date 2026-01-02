import os
import subprocess
import sys

def run_bot():
    """Запускает бота в дочернем процессе"""
    try:
        print("🚀 Запуск Arbitr Bot из runner...")
        # Заменяем текущий процесс процессом бота
        os.execvp(sys.executable, ['python', 'bot.py'])
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return False
    return True

if __name__ == "__main__":
    run_bot()
