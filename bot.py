import os
import asyncio
import sqlite3
from datetime import datetime, timezone
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
import ccxt.async_support as ccxt

# -------------------------
# Токены и ID
# -------------------------
TELEGRAM_TOKEN = "8546366016:AAEWSe8vsdlBhyboZzOgcPb8h9cDSj09A80"
OWNER_CHAT_ID = 6590452577
OPERATOR_ID = 8193755967

# -------------------------
# Настройки арбитража
# -------------------------
SPREAD_THRESHOLD = 1.5          # %
MIN_VOLUME_USD = 1500           # Минимальный объем USDT
MAX_COINS = 150
CHECK_INTERVAL = 60             # секунд
USDT_PAIRS_ONLY = True

EXCHANGES = ["bitrue", "bitmart", "poloniex", "kucoin", "gateio"]

# -------------------------
# База данных SQLite
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
    spread REAL,
    volume REAL,
    detected_at TEXT
)
""")
conn.commit()

# -------------------------
# Whitelist функции
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
# Сканер
# -------------------------
scanner_running = False

async def fetch_orderbook(exch, symbol):
    try:
        ob = await exch.fetch_order_book(symbol)
        return ob
    except Exception as e:
        return None

async def check_arbitrage():
    # Создаем экземпляры бирж
    exchange_objects = {name: getattr(ccxt, name)() for name in EXCHANGES}
    symbols_checked = set()
    signals = []

    # Получаем все пары USDT и ликвидные
    for ex_name, ex_obj in exchange_objects.items():
        try:
            markets = await ex_obj.load_markets()
            for symbol, data in markets.items():
                if USDT_PAIRS_ONLY and "USDT" not in symbol:
                    continue
                if data.get("active") != True:
                    continue
                if data.get("info", {}).get("quoteVolume", 0) < MIN_VOLUME_USD:
                    continue
                symbols_checked.add(symbol)
        except:
            continue

    # Ограничиваем кол-во монет
    symbols_checked = list(symbols_checked)[:MAX_COINS]

    # Проверяем спреды
    for symbol in symbols_checked:
        best_bid = 0
        best_bid_ex = ""
        best_ask = float('inf')
        best_ask_ex = ""

        for ex_name, ex_obj in exchange_objects.items():
            ob = await fetch_orderbook(ex_obj, symbol)
            if not ob: 
                continue
            if ob['bids']:
                top_bid = ob['bids'][0][0]
                if top_bid > best_bid:
                    best_bid = top_bid
                    best_bid_ex = ex_name
            if ob['asks']:
                top_ask = ob['asks'][0][0]
                if top_ask < best_ask:
                    best_ask = top_ask
                    best_ask_ex = ex_name

        if best_bid_ex != best_ask_ex and best_bid > best_ask:
            spread_percent = ((best_bid - best_ask)/best_ask)*100
            volume = min(
                [ob['bids'][0][1] if ob and ob['bids'] else 0 for ob in [await fetch_orderbook(exchange_objects[ex], symbol) for ex in EXCHANGES]]
            )
            if spread_percent >= SPREAD_THRESHOLD and volume*best_ask >= MIN_VOLUME_USD:
                signals.append({
                    "symbol": symbol,
                    "buy_ex": best_ask_ex,
                    "sell_ex": best_bid_ex,
                    "spread": round(spread_percent,2),
                    "volume": round(volume,2)
                })

    # Закрываем биржи
    for ex_obj in exchange_objects.values():
        await ex_obj.close()

    return signals

async def scanner_job(context: ContextTypes.DEFAULT_TYPE):
    if not scanner_running:
        return
    rows = list_whitelist()
    if not rows:
        return
    signals = await check_arbitrage()
    for signal in signals:
        for (tg_id,) in rows:
            await context.bot.send_message(
                chat_id=tg_id,
                text=f"🔥 Арбитражный сигнал!\nМонета: {signal['symbol']}\nКупить: {signal['buy_ex']}\nПродать: {signal['sell_ex']}\nСпред: {signal['spread']}%\nОбъём: {signal['volume']} USDT"
            )

# -------------------------
# Telegram команды и кнопки
# -------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global scanner_running
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
# Основной запуск
# -------------------------
app = Application.builder().token(BOT_TOKEN).build()

# включаем job_queue
job_queue = app.job_queue
job_queue.run_repeating(scanner_job, interval=CHECK_INTERVAL, first=5)

if __name__ == "__main__":
    main()
