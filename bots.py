import os
import asyncio
import sqlite3
from datetime import datetime, timezone
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

# -------------------------
# ВАШИ ТОКЕНЫ / ID
# -------------------------
TELEGRAM_TOKEN = "8546366016:AAEWSe8vsdlBhyboZzOgcPb8h9cDSj09A80"       # <-- Вставьте сюда токен вашего бота
OWNER_CHAT_ID = 6590452577                    # <-- Ваш Telegram ID
OPERATOR_ID = 8193755967                      # <-- ID оператора (можно добавить второго человека)

# -------------------------
# Настройки арбитража
# -------------------------
SPREAD_THRESHOLD = 0.015    # Минимальный спред 1.5%
MIN_VOLUME_USD = 1500       # Минимальный объем по USDT
MAX_COINS = 150             # Максимальное количество монет
CHECK_INTERVAL = 60         # Интервал проверки в секундах

# -------------------------
# SQLite база (whitelist и сигналы)
# -------------------------
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

cur.execute("""
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT,
    buy_ex TEXT,
    sell_ex TEXT,
    initial_spread REAL,
    initial_time TEXT
)
""")
conn.commit()

# -------------------------
# Функции для whitelist
# -------------------------
def is_whitelisted(tg_id: int) -> bool:
    cur.execute("SELECT 1 FROM whitelist WHERE tg_id=?", (tg_id,))
    return cur.fetchone() is not None

def add_whitelist(tg_id: int, added_by: int):
    cur.execute(
        "INSERT OR REPLACE INTO whitelist (tg_id, added_by, added_at) VALUES (?, ?, ?)",
        (tg_id, added_by, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()

def remove_whitelist(tg_id: int):
    cur.execute("DELETE FROM whitelist WHERE tg_id=?", (tg_id,))
    conn.commit()

def list_whitelist():
    cur.execute("SELECT tg_id, added_by, added_at FROM whitelist")
    return cur.fetchall()

# -------------------------
# Флаги сканера
# -------------------------
scanner_running = False

# -------------------------
# Команды /start, /stop, /add_user, /remove_user, /list_users
# -------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global scanner_running
    scanner_running = True
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Старт сканера", callback_data="start_scanner")],
        [InlineKeyboardButton("Стоп сканера", callback_data="stop_scanner")],
        [InlineKeyboardButton("Поддержка", url="https://t.me/Arbitr_IP")]
    ])
    await update.message.reply_text(
        "Добро пожаловать! Используй кнопки ниже для управления сканером.",
        reply_markup=keyboard
    )

async def cmd_add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user.id
    if caller not in (OWNER_CHAT_ID, OPERATOR_ID):
        await update.message.reply_text("🚫 Только владелец или оператор могут управлять whitelist.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /add_user <tg_id>")
        return
    tg_id = int(context.args[0])
    add_whitelist(tg_id, caller)
    await update.message.reply_text(f"✅ Пользователь {tg_id} добавлен в whitelist.")

async def cmd_remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user.id
    if caller not in (OWNER_CHAT_ID, OPERATOR_ID):
        await update.message.reply_text("🚫 Только владелец или оператор могут управлять whitelist.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /remove_user <tg_id>")
        return
    tg_id = int(context.args[0])
    remove_whitelist(tg_id)
    await update.message.reply_text(f"✅ Пользователь {tg_id} удалён из whitelist.")

async def cmd_list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caller = update.effective_user.id
    if caller not in (OWNER_CHAT_ID, OPERATOR_ID):
        await update.message.reply_text("🚫 Только владелец или оператор могут просматривать whitelist.")
        return
    rows = list_whitelist()
    if not rows:
        await update.message.reply_text("Whitelist пуст.")
        return
    txt = "Whitelist:\n" + "\n".join([f"{r[0]} (added_by={r[1]}) at {r[2]}" for r in rows])
    await update.message.reply_text(txt)

# -------------------------
# Callback кнопок
# -------------------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global scanner_running
    query = update.callback_query
    await query.answer()
    if query.data == "start_scanner":
        scanner_running = True
        await query.message.reply_text("✅ Сканер запущен!")
    elif query.data == "stop_scanner":
        scanner_running = False
        await query.message.reply_text("⛔ Сканер остановлен!")

# -------------------------
# Простейший "сканер" (заглушка, имитирует арбитраж)
# -------------------------
async def scanner_job(context: ContextTypes.DEFAULT_TYPE):
    if not scanner_running:
        return
    cur.execute("SELECT tg_id FROM whitelist")
    users = cur.fetchall()
    for (tg_id,) in users:
        await context.bot.send_message(
            chat_id=tg_id,
            text=f"🔥 Сигнал арбитража!\nСимвол: BTC/USDT\nСПРЕД: 1.6%\nОбъём: 2000 USDT"
        )

# -------------------------
# Основной запуск
# -------------------------
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    # команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add_user", cmd_add_user))
    app.add_handler(CommandHandler("remove_user", cmd_remove_user))
    app.add_handler(CommandHandler("list_users", cmd_list_users))
    # кнопки
    app.add_handler(CallbackQueryHandler(callback_handler))
    # периодическая задача (каждую минуту)
    app.job_queue.run_repeating(scanner_job, interval=60, first=5)
    # запуск бота
    print("Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()
