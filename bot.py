# bot.py
import os
import sqlite3
from datetime import datetime, timezone
import logging
import asyncio
import nest_asyncio
import ccxt
import pandas as pd

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ========== CONFIG ==========
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8546366016:AAEWSe8vsdlBhyboZzOgcPb8h9cDSj09A80")
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "6590452577"))

EXCHANGE_IDS = ["kucoin", "bitrue", "bitmart", "gateio", "poloniex"]

MAX_COINS = 150
SPREAD_THRESHOLD = 0.005
MIN_VOLUME_USD = 1500
CHECK_INTERVAL = 60
SYMBOL_QUOTE = "/USDT"
# ===========================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("arbi-bot")

# ------------ DB ------------
DB_FILE = "arbi_signals.db"
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cur = conn.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT,
    buy_ex TEXT,
    sell_ex TEXT,
    spread REAL,
    created_at TEXT
)
""")
conn.commit()

def save_signal(symbol, buy_ex, sell_ex, spread):
    cur.execute(
        "INSERT INTO signals (symbol, buy_ex, sell_ex, spread, created_at) VALUES (?, ?, ?, ?, ?)",
        (symbol, buy_ex, sell_ex, float(spread), datetime.now(timezone.utc).isoformat())
    )
    conn.commit()

def last_signal(symbol, buy_ex, sell_ex):
    cur.execute(
        "SELECT spread, created_at FROM signals WHERE symbol=? AND buy_ex=? AND sell_ex=? ORDER BY id DESC LIMIT 1",
        (symbol, buy_ex, sell_ex)
    )
    return cur.fetchone()

# ------------ exchanges init (public clients) ------------
exchanges = {}
for ex_id in EXCHANGE_IDS:
    try:
        ex_cls = getattr(ccxt, ex_id)
        # для публичных вызовов (без ключей)
        exchanges[ex_id] = ex_cls({"enableRateLimit": True})
        logger.info(f"{ex_id} client created")
    except Exception as e:
        logger.warning(f"Cannot init {ex_id}: {e}")

# ------------ helper funcs ------------
def is_valid_symbol(sym: str) -> bool:
    if not sym.endswith(SYMBOL_QUOTE):
        return False
    bad = ['3S','3L','UP','DOWN','BULL','BEAR','ETF','INVERSE']
    up = sym.upper()
    for b in bad:
        if b in up:
            return False
    base = sym.split("/")[0]
    return 2 <= len(base) <= 20

def orderbook_volume_usd(exchange, symbol):
    try:
        ob = exchange.fetch_order_book(symbol, limit=5)
        bid_vol = sum([p*a for p,a in ob.get('bids', [])[:3]])
        ask_vol = sum([p*a for p,a in ob.get('asks', [])[:3]])
        return max(bid_vol, ask_vol)
    except Exception:
        return 0.0

async def send_telegram_text(app, text, reply_markup=None):
    try:
        await app.bot.send_message(chat_id=TARGET_CHAT_ID, text=text, reply_markup=reply_markup, parse_mode="HTML")
    except Exception as e:
        logger.exception("Failed to send telegram message: %s", e)

# ------------ scanner ------------
async def scanner_once(app):
    exchange_pairs = {}
    for name, ex in exchanges.items():
        try:
            markets = ex.load_markets()
            usdt_pairs = [s for s in markets.keys() if is_valid_symbol(s)]
            exchange_pairs[name] = set(usdt_pairs)
            logger.info(f"{name}: {len(usdt_pairs)} /USDT pairs")
        except Exception as e:
            logger.warning(f"load_markets {name} failed: {e}")
            exchange_pairs[name] = set()

    symbol_map = {}
    for ex_name, pairs in exchange_pairs.items():
        for s in pairs:
            symbol_map.setdefault(s, []).append(ex_name)
    common_symbols = [s for s, exs in symbol_map.items() if len(exs) >= 2]
    common_symbols = sorted(common_symbols)[:MAX_COINS]
    logger.info(f"Selected {len(common_symbols)} common symbols")

    for symbol in common_symbols:
        ex_list = symbol_map[symbol]
        for buy_ex in ex_list:
            for sell_ex in ex_list:
                if buy_ex == sell_ex:
                    continue
                buy_client = exchanges.get(buy_ex)
                sell_client = exchanges.get(sell_ex)
                if buy_client is None or sell_client is None:
                    continue
                try:
                    ask_book = buy_client.fetch_order_book(symbol, limit=5)
                    bid_book = sell_client.fetch_order_book(symbol, limit=5)
                except Exception:
                    continue
                if not ask_book.get('asks') or not bid_book.get('bids'):
                    continue
                ask_price, ask_amount = ask_book['asks'][0]
                bid_price, bid_amount = bid_book['bids'][0]
                if ask_price <= 0:
                    continue
                spread_rel = (bid_price - ask_price) / ask_price
                approx_vol = max(orderbook_volume_usd(buy_client, symbol), orderbook_volume_usd(sell_client, symbol))
                if approx_vol < MIN_VOLUME_USD or spread_rel < SPREAD_THRESHOLD:
                    continue

                # try to detect deposit/withdraw availability (best-effort)
                withdraw_ok = "unknown"
                deposit_ok = "unknown"
                try:
                    # Many exchanges expose currencies/status via fetch_currencies or market info
                    if hasattr(buy_client, "fetch_currencies"):
                        curinfo = buy_client.fetch_currencies()
                        base = symbol.split("/")[0]
                        info = curinfo.get(base, {})
                        # different exchanges use different fields — attempt common ones
                        if isinstance(info, dict):
                            withdraw_ok = not info.get("withdraw_disabled", False) if "withdraw_disabled" in info else withdraw_ok
                            deposit_ok = not info.get("deposit_disabled", False) if "deposit_disabled" in info else deposit_ok
                except Exception:
                    pass

                now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                text = (f"🔥 <b>Арбитраж</b> {symbol}\n"
                        f"Купить: <b>{buy_ex}</b> → {ask_price:.8f} (withdraw:{withdraw_ok})\n"
                        f"Продать: <b>{sell_ex}</b> → {bid_price:.8f} (deposit:{deposit_ok})\n"
                        f"СПРЕД: <b>{spread_rel*100:.4f}%</b>\n"
                        f"Объём (approx USD): {approx_vol:.2f}\n"
                        f"Время: {now}")
                keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Проверить спред", callback_data=f"check|{symbol}|{buy_ex}|{sell_ex}")]])
                logger.info("Signal: %s -> %s (spread=%.6f)", buy_ex, sell_ex, spread_rel)
                await send_telegram_text(app, text, reply_markup=keyboard)
                save_signal(symbol, buy_ex, sell_ex, spread_rel)
                await asyncio.sleep(0.5)

# ------------ callback ------------
async def check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    try:
        _, symbol, buy_ex, sell_ex = data.split("|")
    except Exception:
        await query.message.reply_text("Неверные данные для проверки.")
        return

    buy_client = exchanges.get(buy_ex)
    sell_client = exchanges.get(sell_ex)
    if not buy_client or not sell_client:
        await query.message.reply_text("Клиент биржи недоступен.")
        return

    try:
        ob_buy = buy_client.fetch_order_book(symbol, limit=5)
        ob_sell = sell_client.fetch_order_book(symbol, limit=5)
    except Exception as e:
        await query.message.reply_text(f"Ошибка получения стаканов: {e}")
        return

    ask_price = ob_buy['asks'][0][0] if ob_buy.get('asks') else None
    bid_price = ob_sell['bids'][0][0] if ob_sell.get('bids') else None
    if ask_price is None or bid_price is None:
        await query.message.reply_text("Не удалось получить лучшие цены.")
        return

    current_spread = (bid_price - ask_price) / ask_price
    last = last_signal(symbol, buy_ex, sell_ex)
    if last:
        prev_spread, prev_time = last
        diff = (current_spread - prev_spread)
        cmp_text = f"Текущий спред: {current_spread*100:.4f}%\nРанее: {prev_spread*100:.4f}% ({prev_time})\nИзменение: {diff*100:+.4f}%"
    else:
        cmp_text = f"Текущий спред: {current_spread*100:.4f}%\n(нет предыдущего сигнала)"
    v_buy = orderbook_volume_usd(buy_client, symbol)
    v_sell = orderbook_volume_usd(sell_client, symbol)
    text = (f"🔄 Актуальный спред для {symbol}\nКупить: {buy_ex} → {ask_price:.8f}\nПродать: {sell_ex} → {bid_price:.8f}\n"
            + cmp_text + f"\nОбъёмы (approx USD): buy={v_buy:.2f}, sell={v_sell:.2f}")
    await query.message.reply_text(text)

# ------------ commands & control ------------
scanner_running = False

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Арбитражный бот запущен. Используй /scanner_start и /scanner_stop для управления фоновым сканером.")

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Запускаю одну итерацию сканера (может занять время)...")
    await scanner_once(context.application)
    await update.message.reply_text("Готово.")

async def cmd_scanner_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global scanner_running
    if update.effective_user.id != TARGET_CHAT_ID:
        await update.message.reply_text("Только владелец может запускать сканер.")
        return
    if scanner_running:
        await update.message.reply_text("Сканер уже запущен.")
        return
    scanner_running = True
    await update.message.reply_text("Сканер запущен (фоновые итерации).")

async def cmd_scanner_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global scanner_running
    if update.effective_user.id != TARGET_CHAT_ID:
        await update.message.reply_text("Только владелец может останавливать сканер.")
        return
    if not scanner_running:
        await update.message.reply_text("Сканер уже остановлен.")
        return
    scanner_running = False
    await update.message.reply_text("Сканер остановлен.")

# job wrapper
async def scanner_job(context: ContextTypes.DEFAULT_TYPE):
    if not scanner_running:
        return
    await scanner_once(context.application)

# ------------ main ------------
async def main():
    if TELEGRAM_TOKEN is None or TELEGRAM_TOKEN.strip() == "":
        logger.error("TELEGRAM_TOKEN is not set.")
        return

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("scanner_start", cmd_scanner_start))
    app.add_handler(CommandHandler("scanner_stop", cmd_scanner_stop))
    app.add_handler(CallbackQueryHandler(check_callback, pattern=r"^check\|"))

    # schedule job via JobQueue
    app.job_queue.run_repeating(scanner_job, interval=CHECK_INTERVAL, first=5)

    # background note: tasks created before app.run will not be auto-awaited
    logger.info("Bot starting...")
    await app.run_polling()

if __name__ == "__main__":
    # safe run: if eventloop already running, apply nest_asyncio and schedule task
    try:
        asyncio.run(main())
    except RuntimeError as e:
        logger.warning("asyncio.run failed (%s). Applying nest_asyncio and creating task.", e)
        try:
            nest_asyncio.apply()
            loop = asyncio.get_event_loop()
            loop.create_task(main())
            # do not call loop.run_forever() here because many hosts already run loop
        except Exception as ex:
            logger.exception("Fallback start failed: %s", ex)

