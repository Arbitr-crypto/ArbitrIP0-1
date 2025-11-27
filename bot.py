# bot.py

import asyncio
import ccxt
from datetime import datetime, timezone
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler

# --------------------------
# Настройки
# --------------------------

TELEGRAM_TOKEN = "8546366016:AAEWSe8vsdlBhyboZzOgcPb8h9cDSj09A80"      # <- Вставьте сюда токен бота
OWNER_CHAT_ID = 6590452577                       # <- Вставьте сюда ваш ID владельца
OPERATOR_ID = 8193755967                          # <- Вставьте сюда ID оператора
EXCHANGES_IDS = ["kucoin", "bitrue", "bitmart", "gateio", "poloniex"]

SPREAD_THRESHOLD = 0.015   # минимальный спред 1.5%
MIN_VOLUME_USD = 1500      # минимальный объем
MAX_COINS = 150            # макс. количество монет для сканирования
CHECK_INTERVAL = 60        # интервал проверки в секундах

# --------------------------
# Whitelist
# --------------------------

whitelist = set()  # пустой сет, добавляем telegram_id через add_whitelist

def add_whitelist(tg_id):
    whitelist.add(tg_id)

def remove_whitelist(tg_id):
    whitelist.discard(tg_id)

def is_whitelisted(tg_id):
    return tg_id in whitelist

# --------------------------
# Инициализация бирж
# --------------------------

exchanges = {}
for ex_id in EXCHANGES_IDS:
    ex_id = ex_id.strip()
    if ex_id:
        ex_cls = getattr(ccxt, ex_id)
        exchanges[ex_id] = ex_cls({'enableRateLimit': True})

# --------------------------
# Логика сканера
# --------------------------

scanner_task = None
scanner_running = False

def is_valid_symbol(symbol):
    if not symbol.endswith("/USDT"):
        return False
    bad_keywords = ['3S','3L','UP','DOWN','BULL','BEAR','ETF','HALF','MOON','INVERSE']
    for b in bad_keywords:
        if b in symbol.upper():
            return False
    base = symbol.split("/")[0]
    return 2 <= len(base) <= 20

async def orderbook_volume_usd_async(exchange, symbol):
    try:
        ob = await asyncio.to_thread(exchange.fetch_order_book, symbol, 5)
        bid_vol = sum([p*a for p,a in ob.get('bids', [])[:3]])
        ask_vol = sum([p*a for p,a in ob.get('asks', [])[:3]])
        return max(bid_vol, ask_vol)
    except:
        return 0.0

async def scan_arbitrage(context: ContextTypes.DEFAULT_TYPE):
    chat_id = OWNER_CHAT_ID
    for ex_a_name, ex_a in exchanges.items():
        try:
            markets = await asyncio.to_thread(ex_a.load_markets)
            symbols = [s for s in markets.keys() if is_valid_symbol(s)][:MAX_COINS]
            for symbol in symbols:
                vol = await orderbook_volume_usd_async(ex_a, symbol)
                if vol < MIN_VOLUME_USD:
                    continue
                price_a = ex_a.fetch_ticker(symbol)['bid']
                for ex_b_name, ex_b in exchanges.items():
                    if ex_b_name == ex_a_name:
                        continue
                    price_b = ex_b.fetch_ticker(symbol)['ask']
                    spread = price_a / price_b - 1
                    if spread >= SPREAD_THRESHOLD:
                        msg = (f"🟢 Арбитраж найден!\n"
                               f"{symbol}\n"
                               f"BUY: {ex_b_name} @ {price_b}\n"
                               f"SELL: {ex_a_name} @ {price_a}\n"
                               f"Spread: {spread*100:.2f}%\n"
                               f"Объем: {vol:.2f} USD")
                        await context.bot.send_message(chat_id=chat_id, text=msg)
        except Exception as e:
            print(f"Ошибка при сканировании {ex_a_name}: {e}")

# --------------------------
# Telegram Bot
# --------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_whitelisted(update.effective_user.id):
        await update.message.reply_text("❌ Вы не в whitelist")
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Старт сканера", callback_data="start_scanner")],
        [InlineKeyboardButton("Стоп сканера", callback_data="stop_scanner")],
        [InlineKeyboardButton("Поддержка", url="https://t.me/Arbitr_IP")]
    ])
    await update.message.reply_text("Выберите действие:", reply_markup=keyboard)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global scanner_task, scanner_running
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "start_scanner":
        if not scanner_running:
            scanner_task = context.job_queue.run_repeating(scan_arbitrage, interval=CHECK_INTERVAL, first=0)
            scanner_running = True
            await query.message.reply_text("✅ Сканер запущен!")
        else:
            await query.message.reply_text("Сканер уже запущен")
    elif data == "stop_scanner":
        if scanner_running:
            scanner_task.schedule_removal()
            scanner_running = False
            await query.message.reply_text("⏹️ Сканер остановлен")
        else:
            await query.message.reply_text("Сканер не запущен")

# --------------------------
# Главная функция
# --------------------------

def main():
    # Добавляем владельца и оператора в whitelist
    add_whitelist(OWNER_CHAT_ID)
    add_whitelist(OPERATOR_ID)

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(button_callback))
    print("Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()
