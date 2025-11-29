import os
import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Dict, Tuple
import ccxt
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TARGET_CHAT_ID = int(os.environ.get("TARGET_CHAT_ID") or 0)

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN не задан в .env")
if TARGET_CHAT_ID == 0:
    raise RuntimeError("TARGET_CHAT_ID не задан в .env")

EXCHANGE_IDS = ["kucoin", "bitrue", "bitmart", "gateio", "poloniex"]
MAX_COINS = 150
SPREAD_THRESHOLD = 0.015
MIN_VOLUME_USD = 1500.0
CHECK_INTERVAL = 60
SYMBOL_QUOTE = "/USDT"
MARKETS_CACHE_TTL = 600

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("arbi-bot")

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


def save_signal(symbol: str, buy_ex: str, sell_ex: str, spread: float):
    cur.execute(
        "INSERT INTO signals(symbol, buy_ex, sell_ex, spread, created_at) VALUES(?,?,?,?,?)",
        (symbol, buy_ex, sell_ex, float(spread), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def last_signal(symbol: str, buy_ex: str, sell_ex: str):
    cur.execute(
        "SELECT spread, created_at FROM signals WHERE symbol=? AND buy_ex=? AND sell_ex=? ORDER BY id DESC LIMIT 1",
        (symbol, buy_ex, sell_ex),
    )
    return cur.fetchone()


exchanges: Dict[str, ccxt.Exchange] = {}


async def initialize_exchanges():
    for ex_id in EXCHANGE_IDS:
        try:
            cls = getattr(ccxt, ex_id)
            exchanges[ex_id] = cls({"enableRateLimit": True})
            logger.info("Exchange %s initialized", ex_id)
        except Exception as e:
            logger.error("Exchange init %s failed: %s", ex_id, e)


def is_valid_symbol(sym: str) -> bool:
    if not sym.endswith(SYMBOL_QUOTE):
        return False
    bad = ["3S", "3L", "UP", "DOWN", "BULL", "BEAR", "ETF", "INVERSE"]
    u = sym.upper()
    if any(b in u for b in bad):
        return False
    base = sym.split("/")[0]
    return 2 <= len(base) <= 20


async def safe_fetch_order_book(exchange, symbol: str, limit=5):
    try:
        return await asyncio.to_thread(exchange.fetch_order_book, symbol, limit)
    except Exception:
        return None


def orderbook_volume_usd_sync(exchange: ccxt.Exchange, symbol: str) -> float:
    try:
        ob = exchange.fetch_order_book(symbol, limit=5)
        bids = ob.get("bids") or []
        asks = ob.get("asks") or []
        bid_vol = sum([p * a for p, a in bids[:3]])
        ask_vol = sum([p * a for p, a in asks[:3]])
        return max(bid_vol, ask_vol)
    except Exception:
        return 0.0


async def orderbook_volume_usd(exchange, symbol):
    return await asyncio.to_thread(orderbook_volume_usd_sync, exchange, symbol)


async def check_deposit_withdraw(exchange, currency: str) -> Tuple[bool, bool, str]:
    deposit_ok, withdraw_ok = True, True
    note = ""

    try:
        has = getattr(exchange, "has", {}) or {}
        if has.get("deposit") is False:
            deposit_ok = False
        if has.get("withdraw") is False:
            withdraw_ok = False
    except Exception:
        pass

    try:
        if hasattr(exchange, "fetch_currencies"):
            currencies = await asyncio.to_thread(exchange.fetch_currencies)
            if isinstance(currencies, dict):
                info = (
                    currencies.get(currency)
                    or currencies.get(currency.upper())
                    or currencies.get(currency.lower())
                )
                if isinstance(info, dict):
                    if "deposit" in info:
                        deposit_ok = bool(info["deposit"])
                    if "withdraw" in info:
                        withdraw_ok = bool(info["withdraw"])
                    if info.get("active") is False:
                        deposit_ok = False
                        withdraw_ok = False
    except Exception:
        pass

    if not deposit_ok:
        note += "deposit_disabled "
    if not withdraw_ok:
        note += "withdraw_disabled"

    return deposit_ok, withdraw_ok, note.strip()


_markets_cache = {}
_markets_cache_time = 0


async def load_markets_cached():
    global _markets_cache, _markets_cache_time

    now = time.time()
    if now - _markets_cache_time < MARKETS_CACHE_TTL and _markets_cache:
        return _markets_cache

    result = {}
    for name, ex in exchanges.items():
        try:
            markets = await asyncio.to_thread(ex.load_markets)
            valid = [s for s in markets.keys() if is_valid_symbol(s)]
            result[name] = set(valid)
        except Exception as e:
            logger.error("load_markets %s failed: %s", name, e)
            result[name] = set()

    _markets_cache = result
    _markets_cache_time = now
    return result


scanner_running = False


async def scanner_once(app):
    markets = await load_markets_cached()

    symbol_map = {}
    for ex, pairs in markets.items():
        for s in pairs:
            symbol_map.setdefault(s, []).append(ex)

    common = [s for s, lst in symbol_map.items() if len(lst) >= 2]
    common = sorted(common)[:MAX_COINS]

    logger.info("Checking %d symbols...", len(common))

    for symbol in common:
        ex_list = symbol_map[symbol]

        for buy_ex in ex_list:
            for sell_ex in ex_list:
                if buy_ex == sell_ex:
                    continue

                ex_buy = exchanges[buy_ex]
                ex_sell = exchanges[sell_ex]

                ob_buy = await safe_fetch_order_book(ex_buy, symbol)
                ob_sell = await safe_fetch_order_book(ex_sell, symbol)
                if not ob_buy or not ob_sell:
                    continue

                if not ob_buy.get("asks") or not ob_sell.get("bids"):
                    continue

                ask, ask_amt = ob_buy["asks"][0]
                bid, bid_amt = ob_sell["bids"][0]
                if ask <= 0:
                    continue

                spread = (bid - ask) / ask
                if spread < SPREAD_THRESHOLD:
                    continue

                vol = max(
                    await orderbook_volume_usd(ex_buy, symbol),
                    await orderbook_volume_usd(ex_sell, symbol),
                )
                if vol < MIN_VOLUME_USD:
                    continue

                base = symbol.split("/")[0]

                dep_buy, w_buy, note_buy = await check_deposit_withdraw(ex_buy, base)
                dep_sell, w_sell, note_sell = await check_deposit_withdraw(ex_sell, base)

                now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

                text = (
                    f"🔥 <b>Арбитраж</b> {symbol}\n"
                    f"Купить: <b>{buy_ex}</b> → {ask:.8f}\n"
                    f"Продать: <b>{sell_ex}</b> → {bid:.8f}\n"
                    f"СПРЕД: <b>{spread * 100:.4f}%</b>\n"
                    f"Объём (USD): {vol:.2f}\n"
                    f"Время: {now}\n\n"
                    f"Вывод {base} с {buy_ex}: {'✔' if w_buy else '✖'} {note_buy}\n"
                    f"Ввод {base} на {sell_ex}: {'✔' if dep_sell else '✖'} {note_sell}"
                )

                keyboard = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Подробнее", callback_data=f"details_{symbol}")]]
                )

                await app.bot.send_message(
                    chat_id=TARGET_CHAT_ID,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )


async def run_scanner(app):
    global scanner_running
    if scanner_running:
        return
    scanner_running = True

    while True:
        try:
            await scanner_once(app)
        except Exception as e:
            logger.exception("scanner_once error: %s", e)
        await asyncio.sleep(CHECK_INTERVAL)


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("Дополнительная информация скоро будет доступна.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот запущен.")


async def main():
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_callback))

    await initialize_exchanges()

    scanner_task = asyncio.create_task(run_scanner(application))

    try:
        await application.run_polling()
    finally:
        scanner_task.cancel()
        try:
            await application.shutdown()
        except:
            pass


if __name__ == "__main__":
    asyncio.run(main())
