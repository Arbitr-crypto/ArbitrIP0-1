import os
import logging
import asyncio
import nest_asyncio

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Применяем nest_asyncio для работы на Railway / Jupyter-подобных средах
nest_asyncio.apply()

# Настройки логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Ваш токен Telegram
TELEGRAM_TOKEN = os.getenv("8546366016:AAEWSe8vsdlBhyboZzOgcPb8h9cDSj09A80")

# Период проверки бирж (сек)
CHECK_INTERVAL = 10

# Флаг работы сканера
scanner_running = False

# --------------------------
# Биржевые клиенты (пример)
# --------------------------
class ExchangeClient:
    def __init__(self, name):
        self.name = name
        logger.info(f"Клиент {self.name} создан")

clients = [
    ExchangeClient("Kucoin"),
    ExchangeClient("Bitrue"),
    ExchangeClient("Bitmart"),
    ExchangeClient("Gateio"),
    ExchangeClient("Poloniex")
]

# --------------------------
# Основной сканер
# --------------------------
async def scanner_task(app: "Application"):
    global scanner_running
    while scanner_running:
        for client in clients:
            logger.info(f"Сканирую биржу {client.name}...")
        await asyncio.sleep(CHECK_INTERVAL)

# --------------------------
# Команды бота
# --------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global scanner_running
    if scanner_running:
        await update.message.reply_text("Сканер уже запущен")
    else:
        scanner_running = True
        asyncio.create_task(scanner_task(context.application))
        await update.message.reply_text("Сканер запущен!")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global scanner_running
    if scanner_running:
        scanner_running = False
        await update.message.reply_text("Сканер остановлен")
    else:
        await update.message.reply_text("Сканер уже остановлен")

async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for client in clients:
        await update.message.reply_text(f"Проверяю биржу {client.name}...")

# --------------------------
# Основная функция запуска
# --------------------------
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Добавляем команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("scan", scan))

    # Запуск бота
    logger.info("Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()
