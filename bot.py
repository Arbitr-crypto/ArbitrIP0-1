# bot.py
# Арбитражный сканер + Telegram-бот (python-telegram-bot v20.x)
# Запуск: python bot.py
# Рекомендация: положи TELEGRAM_TOKEN в переменную окружения TELEGRAM_TOKEN
# или отредактируй значение TELEGRAM_TOKEN ниже.

import os
import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Dict, Set

import ccxt
import pandas as pd
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ================== CONFIG ==================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN") or "8546366016:AAEWSe8vsdlBhyboZzOgcPb8h9cDSj09A80"
TARGET_CHAT_ID = int(os.environ.get("TARGET_CHAT_ID") or 6590452577)

EXCHANGE_IDS = ["kucoin", "bitrue", "bitmart", "gateio", "poloniex"]

MAX_COINS = 150
SPREAD_THRESHOLD = 0.015   # процентный порог (1.5% -> 0.015)
MIN_VOLUME_USD = 1500.0
CHECK_INTERVAL = 60       # seconds
SYMBOL_QUOTE = "/USDT"
MARKETS_CACHE_TTL = 600   # seconds (10 мин)
# ============================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("arbi-bot")

# ---------- Database ----------
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


# ---------- Init exchanges ----------
exchanges: Dict[str, ccxt.Exchange] = {}
for ex_id in EXCHANGE_IDS:
    try:
        ex_cls = getattr(ccxt, ex_id)
        exchanges[ex_id] = ex_cls({"enableRateLimit": True})
        logger.info("%s client created", ex_id)
    except Exception as e:
        logger.warning("Cannot init %s: %s", ex_id, e)


# ---------- Helpers ----------
def is_valid_symbol(sym: str) -> bool:
    if not sym.endswith(SYMBOL_QUOTE):
        return False
    bad = ['3S', '3L', 'UP', 'DOWN', 'BULL', 'BEAR', 'ETF', 'INVERSE']
    up = sym.upper()
    for b in bad:
        if b in up:
            return False
    base = sym.split("/")[0]
    return 2 <= len(base) <= 20


def orderbook_volume_usd_sync(exchange: ccxt.Exchange, symbol: str) -> float:
    try:
        ob = exchange.fetch_order_book(symbol, limit=5)
        bid_vol = sum([p * a for p, a in ob.get('bids', [])[:3]])
        ask_vol = sum([p * a for p, a in ob.get('asks', [])[:3]])
        return max(bid_vol, ask_vol)
    except Exception:
        return 0.0


async def orderbook_volume_usd(exchange: ccxt.Exchange, symbol: str) -> float:
    return await asyncio.to_thread(orderbook_volume_usd_sync, exchange, symbol)


async def safe_fetch_order_book(exchange: ccxt.Exchange, symbol: str, limit=5):
    try:
        return await asyncio.to_thread(exchange.fetch_order_book, symbol, limit)
    except Exception as e:
        logger.debug("fetch_order_book %s@%s failed: %s", symbol, getattr(exchange, 'id', '?'), e)
        return None


async def check_deposit_withdraw(exchange: ccxt.Exchange, currency_code: str) -> (bool, bool, str):
    """
    Проверяет deposit/withdraw для currency_code на exchange.
    Возвращает (can_deposit, can_withdraw, note).
    Делает sync вызовы через asyncio.to_thread.
    """
    # 1) quick check via exchange.has if available
    try:
        has = getattr(exchange, 'has', {}) or {}
        deposit_ok = bool(has.get('deposit', None) is not False)
        withdraw_ok = bool(has.get('withdraw', None) is not False)
    except Exception:
        deposit_ok = True
        withdraw_ok = True

    # 2) try fetch_currencies for more detail
    try:
        currencies = await asyncio.to_thread(getattr(exchange, 'fetch_currencies', lambda: {}) )
        if currencies and isinstance(currencies, dict):
            cur_info = currencies.get(currency_code) or currencies.get(currency_code.upper()) or currencies.get(currency_code.lower())
            if isinstance(cur_info, dict):
                # many exchanges provide 'deposit'/'withdraw' or 'active' flags
                if 'deposit' in cur_info:
                    deposit_ok = bool(cur_info.get('deposit'))
                if 'withdraw' in cur_info:
                    withdraw_ok = bool(cur_info.get('withdraw'))
                # some provide minWithdraw or active flag
                if 'active' in cur_info:
                    active = cur_info.get('active')
                    if active is False:
                        deposit_ok = False
                        withdraw_ok = False
    except Exception as e:
        # fetch_currencies may not be implemented or may fail; ignore
        logger.debug("fetch_currencies failed on %s: %s", getattr(exchange, 'id', '?'), e)

    note = ""
    if not deposit_ok:
        note += "deposit_disabled "
    if not withdraw_ok:
        note += "withdraw_disabled"

    return deposit_ok, withdraw_ok, note.strip()


# ---------- Markets caching ----------
_markets_cache = {}
_markets_cache_time = 0


async def load_markets_cached():
    global _markets_cache, _markets_cache_time
    if time.time() - _markets_cache_time < MARKETS_CACHE_TTL and _markets_cache:
        return _markets_cache
    data = {}
    for name, ex in exchanges.items():
        try:
            markets = await asyncio.to_thread(ex.load_markets)
            usdt_pairs = [s for s in markets.keys() if is_valid_symbol(s)]
            data[name] = set(usdt_pairs)
            logger.info("%s: %d /USDT pairs", name, len(usdt_pairs))
        except Exception as e:
            logger.warning("load_markets %s failed: %s", name, e)
            data[name] = set()
    _markets_cache = data
    _markets_cache_time = time.time()
    return data


# ---------- Scanner ----------
scanner_running = False


async def scanner_once(app):
    markets_map = await load_markets_cached()

    # build common symbols map
    symbol_map = {}
    for ex_name, pairs in markets_map.items():
        for s in pairs:
            symbol_map.setdefault(s, []).append(ex_name)

    common_symbols = [s for s, exs in symbol_map.items() if len(exs) >= 2]
    common_symbols = sorted(common_symbols)[:MAX_COINS]
    logger.info("Selected %d common symbols", len(common_symbols))

    for symbol in common_symbols:
        ex_list = symbol_map[symbol]
        # check all buy/sell permutations
        for buy_ex in ex_list:
            for sell_ex in ex_list:
                if buy_ex == sell_ex:
                    continue
                buy_client = exchanges.get(buy_ex)
                sell_client = exchanges.get(sell_ex)
                if buy_client is None or sell_client is None:
                    continue

                # fetch orderbooks in parallel (to threads)
                ob_buy_task = asyncio.create_task(safe_fetch_order_book(buy_client, symbol))
                ob_sell_task = asyncio.create_task(safe_fetch_order_book(sell_client, symbol))
                ob_buy = await ob_buy_task
                ob_sell = await ob_sell_task

                if not ob_buy or not ob_sell:
                    continue
                if not ob_buy.get('asks') or not ob_sell.get('bids'):
                    continue

                ask_price, ask_amount = ob_buy['asks'][0]
                bid_price, bid_amount = ob_sell['bids'][0]
                if ask_price <= 0:
                    continue

                spread_rel = (bid_price - ask_price) / ask_price
                # spread filter (we use percent threshold as decimal)
                if spread_rel < SPREAD_THRESHOLD:
                    continue

                # approx volume check
                approx_vol = max(
                    await orderbook_volume_usd(buy_client, symbol),
                    await orderbook_volume_usd(sell_client, symbol)
                )
                if approx_vol < MIN_VOLUME_USD:
                    continue

                # --- we have candidate signal: check deposit/withdraw for asset ---
                # symbol format "BASE/USDT"
                base = symbol.split("/")[0]
                try:
                    dep_buy, wdr_buy, note_buy = await check_deposit_withdraw(buy_client, base)
                except Exception as e:
                    dep_buy, wdr_buy, note_buy = True, True, "chk_err"

                try:
                    dep_sell, wdr_sell, note_sell = await check_deposit_withdraw(sell_client, base)
                except Exception as e:
                    dep_sell, wdr_sell, note_sell = True, True, "chk_err"

                # Prepare message: we always show signal, and warn if deposit/withdraw disabled
                now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                text = (
                    f"🔥 <b>Арбитраж</b> {symbol}\n"
                    f"Купить: <b>{buy_ex}</b> → {ask_price:.8f}\n"
                    f"Продать: <b>{sell_ex}</b> → {bid_price:.8f}\n"
                    f"СПРЕД: <b>{spread_rel*100:.4f}%</b>\n"
                    f"Объём (approx USD): {approx_vol:.2f}\n"
                    f"Время: {now}\n\n"
                    f"Вывод на {buy_ex} (withdraw {base}): {'✔' if wdr_buy else '✖'} {note_buy}\n"
                    f"Ввод на {sell_ex} (deposit {base}): {'✔' if dep_sell else '✖'} {note_sell}"
                )

                # If withdraw on buy_ex is disabled or deposit on sell_ex is disabled, add warning
                warn = ""
                if not wdr_buy:
                    warn += f"\n⚠ ВНИМАНИЕ: вывод ({base}) на {buy_ex} отключён — выполнение невозможнo."
                if not dep_sell:
                    warn += f"\n⚠ ВНИМАНИЕ: ввод ({base}) на {sell_ex} отключён — выполнение невозможнo."

                keyboard = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Проверить спред", callback_data=f"check|{symbol}|{buy_ex}|{sell_ex}")]]
                )

                # send message to target chat
                try:
                    await app.bot.send_message(chat_id=TARGET_CHAT_ID, text=text + warn, reply_markup=keyboard, parse_mode="HTML")
                except Exception as e:
                    logger.exception("Failed to send telegram message: %s", e)

                save_signal(symbol, buy_ex, sell_ex, spread_rel)
                # small delay to avoid bursts
                await asyncio.sleep(0.35)


# ---------- Bot handlers ----------
async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Старт сканера", callback_data="start_scanner"), InlineKeyboardButton("Стоп сканера", callback_data="stop_scanner")],
        [InlineKeyboardButton("Поддержка", url="https://t.me/Arbitr_IP")]
    ])
    await update.message.reply_text("Меню управления сканером:", reply_markup=keyboard)


async def cmd_start_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global scanner_running
    scanner_running = True
    await update.message.reply_text("✅ Сканер запущен (фоновая работа).")


async def cmd_stop_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global scanner_running
    scanner_running = False
    await update.message.reply_text("⛔ Сканер остановлен.")


async def cmd_scan_once(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Запускаю ручную итерацию сканера (может занять время)...")
    await scanner_once(context.application)
    await update.message.reply_text("Готово.")


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global scanner_running
    query = update.callback_query
    await query.answer()
    if query.data == "start_scanner":
        scanner_running = True
        await query.edit_message_text("✅ Сканер запущен (через кнопку).")
    elif query.data == "stop_scanner":
        scanner_running = False
        await query.edit_message_text("⛔ Сканер остановлен (через кнопку).")


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
        await query.message.reply_text("Клиенты бирж недоступны.")
        return

    ob_buy = await safe_fetch_order_book(buy_client, symbol)
    ob_sell = await safe_fetch_order_book(sell_client, symbol)
    if not ob_buy or not ob_sell:
        await query.message.reply_text("Не удалось получить стаканы.")
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

    v_buy = await orderbook_volume_usd(buy_client, symbol)
    v_sell = await orderbook_volume_usd(sell_client, symbol)
    text = (f"🔄 Актуальный спред для {symbol}\nКупить: {buy_ex} → {ask_price:.8f}\nПродать: {sell_ex} → {bid_price:.8f}\n"
            + cmp_text + f"\nОбъёмы (approx USD): buy={v_buy:.2f}, sell={v_sell:.2f}")
    await query.message.reply_text(text)


# ---------- Background task ----------
async def background_loop(app):
    global scanner_running
    while True:
        try:
            if scanner_running:
                logger.info("Background scan start")
                await scanner_once(app)
                logger.info("Background scan finished")
            else:
                logger.debug("Scanner paused (scanner_running=False)")
        except Exception as e:
            logger.exception("Error in background scan: %s", e)
        await asyncio.sleep(CHECK_INTERVAL)


# ---------- Main ----------
async def main():
    if not TELEGRAM_TOKEN or "ВСТАВЬ" in TELEGRAM_TOKEN:
        print("Пожалуйста, заполните TELEGRAM_TOKEN в начале файла или в переменной окружения TELEGRAM_TOKEN")
        return

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # commands & callbacks
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("start", cmd_start_bot))
    app.add_handler(CommandHandler("stop", cmd_stop_bot))
    app.add_handler(CommandHandler("scan", cmd_scan_once))
    app.add_handler(CallbackQueryHandler(callback_handler, pattern="^(start_scanner|stop_scanner)$"))
    app.add_handler(CallbackQueryHandler(check_callback, pattern=r"^check\|"))

    # start background loop as task
    app.create_task(background_loop(app))

    logger.info("Bot running...")
    await app.run_polling()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Terminated by user")
