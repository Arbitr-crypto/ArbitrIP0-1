import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone

import ccxt
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ================== CONFIG ==================
# ВАЖНО: Для безопасности используйте переменные окружения!
# Не коммитьте токен в Git. Используйте .env файл или переменные окружения Railway
# Токен должен быть передан через переменную окружения TELEGRAM_TOKEN
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", None)

# Chat id, куда будут отправляться сигналы
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "6590452577"))

# Биржи (ccxt id)
EXCHANGE_IDS = ["kucoin", "bitrue", "bitmart", "gateio", "poloniex"]

# Параметры сканера
MAX_COINS = 150            # сколько пар максимум проверяем
SPREAD_THRESHOLD = 0.005   # 0.5% (в относительных)
MIN_VOLUME_USD = 1500      # минимальный approximate объем в USD
CHECK_INTERVAL = 60        # секунд между итерациями (при loop)
SYMBOL_QUOTE = "/USDT"     # только пары /USDT
# ============================================

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("arbi-bot")

# ---------- БД для хранения последних сигналов ----------
DB_FILE = "arbi_signals.db"


def init_database():
    """Инициализация базы данных SQLite"""
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            buy_ex TEXT,
            sell_ex TEXT,
            spread REAL,
            created_at TEXT
        )
        """
    )
    conn.commit()
    return conn


# Глобальное подключение к БД
db_conn = init_database()
db_cur = db_conn.cursor()


def save_signal(symbol, buy_ex, sell_ex, spread):
    """Сохраняет сигнал арбитража в базу данных"""
    try:
        db_cur.execute(
            "INSERT INTO signals (symbol, buy_ex, sell_ex, spread, created_at) VALUES (?, ?, ?, ?, ?)",
            (symbol, buy_ex, sell_ex, float(spread), datetime.now(timezone.utc).isoformat()),
        )
        db_conn.commit()
    except Exception as e:
        logger.error(f"Ошибка сохранения сигнала в БД: {e}")


def last_signal(symbol, buy_ex, sell_ex):
    """Получает последний сигнал для данной комбинации символа и бирж"""
    try:
        db_cur.execute(
            "SELECT spread, created_at FROM signals WHERE symbol=? AND buy_ex=? AND sell_ex=? ORDER BY id DESC LIMIT 1",
            (symbol, buy_ex, sell_ex),
        )
        return db_cur.fetchone()  # (spread, created_at) or None
    except Exception as e:
        logger.error(f"Ошибка получения последнего сигнала: {e}")
        return None


# ---------- Инициализация ccxt клиентов (публично) ----------
exchanges = {}


def init_exchanges():
    """Инициализирует клиенты для всех бирж"""
    global exchanges
    exchanges = {}
    for ex_id in EXCHANGE_IDS:
        try:
            ex_cls = getattr(ccxt, ex_id)
            exchanges[ex_id] = ex_cls(
                {
                    "enableRateLimit": True,
                    "timeout": 30000,  # 30 секунд таймаут
                }
            )
            logger.info(f"✓ {ex_id} client created")
        except Exception as e:
            logger.warning(f"✗ Cannot init {ex_id}: {e}")


# Инициализируем биржи при загрузке модуля
init_exchanges()


# ---------- Вспомогательные функции ----------
def is_valid_symbol(sym: str) -> bool:
    """
    Проверяет, является ли символ валидным для арбитража
    Фильтрует деривативы, левереджи и другие нежелательные пары
    """
    if not sym.endswith(SYMBOL_QUOTE):
        return False

    # Исключаем деривативы и левереджи
    bad = ["3S", "3L", "UP", "DOWN", "BULL", "BEAR", "ETF", "INVERSE"]
    up = sym.upper()
    for b in bad:
        if b in up:
            return False

    # Проверяем длину базового символа
    base = sym.split("/")[0]
    if len(base) < 2 or len(base) > 20:
        return False

    return True


def orderbook_volume_usd(exchange, symbol):
    """
    Вычисляет приблизительный объем в USD из стакана заявок
    Использует первые 3 уровня bid/ask
    """
    try:
        ob = exchange.fetch_order_book(symbol, limit=5)
        bid_vol = sum([p * a for p, a in ob.get("bids", [])[:3]])
        ask_vol = sum([p * a for p, a in ob.get("asks", [])[:3]])
        return max(bid_vol, ask_vol)
    except Exception as e:
        logger.debug(f"Ошибка получения объема для {symbol}: {e}")
        return 0.0


async def send_telegram_text(app, text, reply_markup=None):
    """Отправляет сообщение в Telegram"""
    try:
        await app.bot.send_message(
            chat_id=TARGET_CHAT_ID,
            text=text,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.exception(f"Failed to send telegram message: {e}")


# ---------- Сканер: собираем общие пары и анализируем ----------
async def scanner_once(app):
    """
    Выполняет одну итерацию сканирования арбитражных возможностей
    1. Загружает рынки с каждой биржи
    2. Находит общие пары (минимум на 2 биржах)
    3. Проверяет спреды между всеми комбинациями бирж
    4. Отправляет сигналы при обнаружении арбитража
    """
    logger.info("=== Начало сканирования ===")

    # 1) Загружаем markets с каждой биржи
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

    # 2) Формируем список общих пар, которые есть минимум на 2 биржах
    symbol_map = {}
    for ex_name, pairs in exchange_pairs.items():
        for s in pairs:
            symbol_map.setdefault(s, []).append(ex_name)

    common_symbols = [s for s, exs in symbol_map.items() if len(exs) >= 2]
    common_symbols = sorted(common_symbols)[:MAX_COINS]
    logger.info(f"Selected {len(common_symbols)} common symbols")

    signals_found = 0

    # 3) Для каждой пары проверяем все комбинации buy/sell
    for symbol in common_symbols:
        ex_list = symbol_map[symbol]

        # Перебор: покупка на A / продажа на B
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
                except Exception as e:
                    logger.debug(f"Ошибка получения стакана {symbol} на {buy_ex}/{sell_ex}: {e}")
                    continue

                if not ask_book.get("asks") or not bid_book.get("bids"):
                    continue

                ask_price, ask_amount = ask_book["asks"][0]
                bid_price, bid_amount = bid_book["bids"][0]

                if ask_price <= 0 or bid_price <= 0:
                    continue

                spread_rel = (bid_price - ask_price) / ask_price

                # Проверяем объем
                approx_vol = max(
                    orderbook_volume_usd(buy_client, symbol),
                    orderbook_volume_usd(sell_client, symbol),
                )
                if approx_vol < MIN_VOLUME_USD:
                    continue

                # Проверяем спред
                if spread_rel < SPREAD_THRESHOLD:
                    continue

                # Сигнал найден!
                signals_found += 1
                now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                text = (
                    f"🔥 <b>Арбитраж</b> {symbol}\n"
                    f"Купить: <b>{buy_ex}</b> → {ask_price:.8f}\n"
                    f"Продать: <b>{sell_ex}</b> → {bid_price:.8f}\n"
                    f"СПРЕД: <b>{spread_rel*100:.4f}%</b>\n"
                    f"Объём (approx USD): {approx_vol:.2f}\n"
                    f"Время: {now}"
                )

                # Inline кнопка для проверки спреда
                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "Проверить спред",
                                callback_data=f"check|{symbol}|{buy_ex}|{sell_ex}",
                            )
                        ]
                    ]
                )

                logger.info(f"Signal #{signals_found}: {symbol} {buy_ex}→{sell_ex} (spread={spread_rel*100:.4f}%)")
                await send_telegram_text(app, text, reply_markup=keyboard)
                save_signal(symbol, buy_ex, sell_ex, spread_rel)

                # Небольшая задержка чтобы не перегружать API
                await asyncio.sleep(0.5)

    logger.info(f"=== Сканирование завершено. Найдено сигналов: {signals_found} ===")


# ---------- Callback кнопки "Проверить спред" ----------
async def check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обработчик callback для кнопки "Проверить спред"
    Показывает актуальный спред и сравнивает с предыдущим сигналом
    """
    query = update.callback_query
    await query.answer()

    data = query.data  # format: check|SYMBOL|BUY_EX|SELL_EX
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

    ask_price = ob_buy["asks"][0][0] if ob_buy.get("asks") else None
    bid_price = ob_sell["bids"][0][0] if ob_sell.get("bids") else None

    if ask_price is None or bid_price is None:
        await query.message.reply_text("Не удалось получить лучшие цены.")
        return

    current_spread = (bid_price - ask_price) / ask_price
    last = last_signal(symbol, buy_ex, sell_ex)

    if last:
        prev_spread, prev_time = last
        diff = current_spread - prev_spread
        cmp_text = (
            f"Текущий спред: {current_spread*100:.4f}%\n"
            f"Ранее: {prev_spread*100:.4f}% ({prev_time})\n"
            f"Изменение: {diff*100:+.4f}%"
        )
    else:
        cmp_text = f"Текущий спред: {current_spread*100:.4f}%\n(нет предыдущего сигнала)"

    v_buy = orderbook_volume_usd(buy_client, symbol)
    v_sell = orderbook_volume_usd(sell_client, symbol)

    text = (
        f"🔄 Актуальный спред для {symbol}\n"
        f"Купить: {buy_ex} → {ask_price:.8f}\n"
        f"Продать: {sell_ex} → {bid_price:.8f}\n"
        + cmp_text
        + f"\nОбъёмы (approx USD): buy={v_buy:.2f}, sell={v_sell:.2f}"
    )

    await query.message.reply_text(text)


# ---------- Команда /scan (вручную запустить одну итерацию) ----------
async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для ручного запуска сканирования"""
    await update.message.reply_text("Запускаю одну итерацию сканера (это может занять время)...")
    try:
        await scanner_once(context.application)
        await update.message.reply_text("Готово.")
    except Exception as e:
        logger.exception("Ошибка при выполнении команды /scan")
        await update.message.reply_text(f"Ошибка при сканировании: {e}")


# ---------- Фоновый цикл (бесконечное сканирование) ----------
async def background_job(context: ContextTypes.DEFAULT_TYPE):
    """Задача для JobQueue: периодический запуск сканера"""
    app = context.application
    try:
        logger.info("JobQueue scan start")
        await scanner_once(app)
        logger.info("JobQueue scan finished")
    except Exception as e:
        logger.exception(f"Error in JobQueue scan: {e}")


# ---------- /start команда ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start - приветствие"""
    await update.message.reply_text(
        "🤖 Арбитражный бот запущен!\n\n"
        "Команды:\n"
        "/start - показать это сообщение\n"
        "/scan - запустить сканирование вручную\n\n"
        "Бот автоматически сканирует биржи каждые 60 секунд."
    )


# ---------- /status команда ----------
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для проверки статуса бота"""
    active_exchanges = len([ex for ex in exchanges.values() if ex is not None])
    status_text = (
        f"📊 Статус бота:\n\n"
        f"Активных бирж: {active_exchanges}/{len(EXCHANGE_IDS)}\n"
        f"Интервал сканирования: {CHECK_INTERVAL} сек\n"
        f"Минимальный спред: {SPREAD_THRESHOLD*100:.2f}%\n"
        f"Минимальный объем: ${MIN_VOLUME_USD}\n"
        f"Максимум монет: {MAX_COINS}"
    )
    await update.message.reply_text(status_text)


# ---------- main ----------
async def main():
    """Главная функция запуска бота"""
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN.strip() == "":
        logger.error("TELEGRAM_TOKEN не установлен! Установите переменную окружения TELEGRAM_TOKEN")
        return

    logger.info("Инициализация бота...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Регистрируем обработчики команд
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(check_callback, pattern=r"^check\|"))

    # Запускаем периодический сканер через JobQueue (без ручного управления event loop)
    app.job_queue.run_repeating(
        background_job,
        interval=CHECK_INTERVAL,
        first=5,
        name="background_scanner",
    )

    logger.info("Bot running...")
    await app.run_polling()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
