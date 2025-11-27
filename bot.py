import os
import asyncio
import sqlite3
from datetime import datetime, timezone
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes
)
import ccxt.async_support as ccxt

# ---------------------------------------------------
# Токены и ID
# ---------------------------------------------------
TELEGRAM_TOKEN = "8546366016:AAEWSe8vsdlBhyboZzOgcPb8h9cDSj09A80"
OWNER_CHAT_ID = 6590452577
OPERATOR_ID = 8193755967

# ---------------------------------------------------
# Настройки арбитража
# ---------------------------------------------------
SPREAD_THRESHOLD = 1.5
MIN_VOLUME_USD = 1500
MAX_COINS = 150
CHECK_INTERVAL = 60
USDT_PAIRS_ONLY = True
EXCHANGES = ["bitrue", "bitmart", "poloniex", "kucoin", "gateio"]

# ---------------------------------------------------
# База данных SQLite
# ---------------------------------------------------
DB_FILE = "arbi_data.db"
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS whitelist (
    tg_id INTEGER PRIMARY KEY,
    added_by INTEGER,
    added_at TEXT
)
""")
conn.commit()

# ---------------------------------------------------
# Проверка whitelist
# ---------------------------------------------------
def is_allowed(user_id: int) -> bool:
    cur.execute("SELECT tg_id FROM whitelist WHERE tg_id = ?", (user_id,))
    return cur.fetchone() is not None or user_id == OWNER_CHAT_ID


# ---------------------------------------------------
# Команда /start
# ---------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.id

    if not is_allowed(user):
        await update.message.reply_text("Доступ запрещён.")
        return

    buttons = [[InlineKeyboardButton("Запустить скан", callback_data="scan")]]
    reply_markup = InlineKeyboardMarkup(buttons)

    await update.message.reply_text("Главное меню:", reply_markup=reply_markup)


# ---------------------------------------------------
# Добавление пользователя
# ---------------------------------------------------
async def cmd_add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_CHAT_ID:
        return

    try:
        uid = int(context.args[0])
    except:
        await update.message.reply_text("Используй: /add_user ID")
        return

    cur.execute("INSERT OR IGNORE INTO whitelist VALUES (?, ?, ?)", (
        uid, update.effective_user.id, datetime.now().isoformat()
    ))
    conn.commit()

    await update.message.reply_text(f"Пользователь {uid} добавлен.")


# ---------------------------------------------------
# Удаление пользователя
# ---------------------------------------------------
async def cmd_remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_CHAT_ID:
        return

    try:
        uid = int(context.args[0])
    except:
        await update.message.reply_text("Используй: /remove_user ID")
        return

    cur.execute("DELETE FROM whitelist WHERE tg_id = ?", (uid,))
    conn.commit()

    await update.message.reply_text(f"Пользователь {uid} удалён.")


# ---------------------------------------------------
# Список пользователей
# ---------------------------------------------------
async def cmd_list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_CHAT_ID:
        return

    cur.execute("SELECT tg_id FROM whitelist")
    rows = cur.fetchall()

    if not rows:
        await update.message.reply_text("Whitelist пуст.")
        return

    text = "Разрешённые пользователи:\n" + "\n".join(str(r[0]) for r in rows)
    await update.message.reply_text(text)


# ---------------------------------------------------
# Callback кнопок
# ---------------------------------------------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "scan":
        await query.edit_message_text("Сканирование рынков запущено.")
        return


# ---------------------------------------------------
# Основной сканер
# ---------------------------------------------------
async def scanner_job(context: ContextTypes.DEFAULT_TYPE):
    print("Выполняю скан...")
    # Здесь будет логика
    return


# ---------------------------------------------------
# Основной запуск
# ---------------------------------------------------
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add_user", cmd_add_user))
    app.add_handler(CommandHandler("remove_user", cmd_remove_user))
    app.add_handler(CommandHandler("list_users", cmd_list_users))

    # Callback
    app.add_handler(CallbackQueryHandler(callback_handler))

    # JobQueue
    app.job_queue.run_repeating(scanner_job, interval=CHECK_INTERVAL, first=5)

    print("Bot started...")
    app.run_polling()


if __name__ == "__main__":
    main()
