import logging
import asyncio
import nest_asyncio

from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Вставь сюда токен
TELEGRAM_TOKEN = "8014312970:AAGf2vGFfr-H3sF-DhvV3sisd6Q3wSKCp-s"

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

logger = logging.getLogger(__name__)

# Применяем nest_asyncio для совместимости с asyncio
nest_asyncio.apply()

# Пример команды /start
async def start(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот запущен и работает!")

async def main():
    # Создаем приложение Telegram
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Добавляем обработчик команды /start
    app.add_handler(CommandHandler("start", start))

    # Запускаем бота
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
