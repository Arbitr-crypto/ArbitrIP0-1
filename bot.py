import asyncio
import ccxt
import logging
import os
import sys
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, JobQueue

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger('arbi-bot')

# Загрузка переменных окружения
load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    logger.error("КРИТИЧЕСКАЯ ОШИБКА: BOT_TOKEN не найден.")
    sys.exit(1)

admin_ids_str = os.getenv('ADMIN_IDS', '').strip()
if admin_ids_str:
    ADMIN_IDS = [int(id_str.strip()) for id_str in admin_ids_str.split(',')]
else:
    ADMIN_IDS = []
    logger.warning("ADMIN_IDS не задан. Доступ открыт для всех.")

logger.info(f"Токен получен. ID администраторов: {ADMIN_IDS}")

# Инициализация ТОЛЬКО рабочих бирж (BitMart убран)
exchanges = {
    'kucoin': ccxt.kucoin({
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    }),
    'bitrue': ccxt.bitrue({
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    }),
    'gateio': ccxt.gateio({
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    }),
    'poloniex': ccxt.poloniex({
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    }),
}

for name in exchanges.keys():
    logger.info(f'✓ {name} клиент создан')

# Глобальные переменные
active_users = set()
arbitrage_cache = {}

# НАСТРОЙКИ ФИЛЬТРОВ (меняйте здесь!)
MIN_PROFIT_PERCENT = 1.0  # ↓ Уменьшил с 2.0% до 1.0%
MIN_VOLUME_USDT = 1000    # ↓ Уменьшил с 10000 до 1000 USDT
SCAN_LIMIT = 100          # ↑ Увеличил с 50 до 100 пар

def get_usdt_symbols():
    """Получение только USDT пар с бирж"""
    symbols = set()
    for exchange in exchanges.values():
        try:
            markets = exchange.load_markets()
            # Берем только пары с /USDT
            for symbol in markets.keys():
                if symbol.endswith('/USDT'):
                    symbols.add(symbol)
        except Exception as e:
            logger.error(f"Ошибка загрузки рынков с {exchange.name}: {e}")
    return list(symbols)

async def fetch_ticker(exchange_name, symbol):
    """Асинхронное получение тикера"""
    exchange = exchanges[exchange_name]
    try:
        ticker = exchange.fetch_ticker(symbol)
        return {
            'symbol': symbol,
            'bid': ticker['bid'] if ticker['bid'] else 0,
            'ask': ticker['ask'] if ticker['ask'] else 0,
            'quoteVolume': ticker.get('quoteVolume', 0),
            'exchange': exchange_name
        }
    except Exception as e:
        logger.debug(f"Ошибка получения {symbol} с {exchange_name}: {e}")
        return None

async def check_arbitrage_for_pair(symbol):
    """Проверка арбитражных возможностей"""
    # Исключаем левереджные токены
    leveraged_keywords = ['3S', '3L', '5S', '5L', '10S', '10L', 'BEAR', 'BULL', 'UP', 'DOWN']
    if any(keyword in symbol.upper() for keyword in leveraged_keywords):
        return None
    
    # Получаем цены со всех бирж
    tasks = [fetch_ticker(name, symbol) for name in exchanges.keys()]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Фильтруем валидные
    valid_prices = []
    for result in results:
        if isinstance(result, dict) and result and result['bid'] > 0 and result['ask'] > 0:
            valid_prices.append(result)
    
    if len(valid_prices) < 2:
        return None
    
    # Находим лучшие цены
    best_bid = max(valid_prices, key=lambda x: x['bid'])
    best_ask = min(valid_prices, key=lambda x: x['ask'])
    
    if best_bid['exchange'] == best_ask['exchange']:
        return None
    
    # Фильтр по объёму
    if best_bid['quoteVolume'] < MIN_VOLUME_USDT or best_ask['quoteVolume'] < MIN_VOLUME_USDT:
        return None
    
    # Расчёт прибыли
    buy_price = best_ask['ask']
    sell_price = best_bid['bid']
    spread = sell_price - buy_price
    
    if spread <= 0:
        return None
    
    profit_percentage = (spread / buy_price) * 100
    
    # Основной фильтр по прибыли
    if profit_percentage < MIN_PROFIT_PERCENT:
        return None
    
    # Фильтр нереалистичной прибыли
    if profit_percentage > 10.0:
        return None
    
    return {
        'symbol': symbol,
        'buy_exchange': best_ask['exchange'],
        'buy_price': buy_price,
        'sell_exchange': best_bid['exchange'],
        'sell_price': sell_price,
        'buy_volume': best_ask['quoteVolume'],
        'sell_volume': best_bid['quoteVolume'],
        'profit': spread,
        'profit_percentage': profit_percentage,
        'timestamp': datetime.now().isoformat()
    }

async def check_arbitrage_opportunities(context: ContextTypes.DEFAULT_TYPE):
    """Проверка арбитражных возможностей"""
    logger.info("Начинаю сканирование...")
    
    # Получаем ТОЛЬКО USDT пары
    symbols = get_usdt_symbols()
    logger.info(f"USDT пар для сканирования: {len(symbols)}")
    
    opportunities = []
    symbols_to_scan = symbols[:SCAN_LIMIT]  # Ограничиваем
    
    for symbol in symbols_to_scan:
        try:
            opportunity = await check_arbitrage_for_pair(symbol)
            if opportunity:
                opportunities.append(opportunity)
                logger.info(f"Найдено: {opportunity['symbol']} - {opportunity['profit_percentage']:.2f}%")
        except Exception as e:
            logger.debug(f"Ошибка при проверке {symbol}: {e}")
    
    if opportunities:
        opportunities.sort(key=lambda x: x['profit_percentage'], reverse=True)
        arbitrage_cache['last_scan'] = datetime.now().isoformat()
        arbitrage_cache['opportunities'] = opportunities[:10]
        
        for user_id in active_users:
            try:
                message = format_opportunities_message(opportunities[:5])
                await context.bot.send_message(chat_id=user_id, text=message, parse_mode='HTML')
            except Exception as e:
                logger.error(f"Ошибка отправки {user_id}: {e}")
    
    logger.info(f"Сканирование завершено. Найдено: {len(opportunities)}")

def format_opportunities_message(opportunities):
    """Форматирование сообщения"""
    if not opportunities:
        return "Арбитражных возможностей не найдено."
    
    message = "🏆 <b>ТОП АРБИТРАЖЕЙ:</b>\n\n"
    
    for i, opp in enumerate(opportunities[:5], 1):
        message += (
            f"{i}. <b>{opp['symbol']}</b>\n"
            f"   📥 Купить на: {opp['buy_exchange'].upper()} - ${opp['buy_price']:.8f}\n"
            f"   📤 Продать на: {opp['sell_exchange'].upper()} - ${opp['sell_price']:.8f}\n"
            f"   💰 Прибыль: <b>{opp['profit_percentage']:.2f}%</b>\n"
            f"   📊 Объём: ${opp['buy_volume']:.0f} / ${opp['sell_volume']:.0f}\n"
            f"   ⏰ {datetime.fromisoformat(opp['timestamp']).strftime('%H:%M:%S')}\n\n"
        )
    
    message += f"<i>Настройки: прибыль >{MIN_PROFIT_PERCENT}%, объём >{MIN_VOLUME_USDT} USDT</i>"
    return message

# Команды бота
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if ADMIN_IDS and user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Нет доступа.")
        return
    
    active_users.add(user_id)
    welcome_text = (
        "🤖 <b>Arbitr Bot v2</b>\n\n"
        "Сканирую 4 биржи на арбитраж.\n"
        f"• Минимальная прибыль: {MIN_PROFIT_PERCENT}%\n"
        f"• Минимальный объём: {MIN_VOLUME_USDT} USDT\n"
        f"• Сканирую: {SCAN_LIMIT} USDT пар\n\n"
        "<b>Команды:</b>\n"
        "/scan - Ручное сканирование\n"
        "/status - Статус\n"
        "/settings - Настройки\n"
        "/stop - Остановить уведомления"
    )
    
    await update.message.reply_text(welcome_text, parse_mode='HTML')
    
    if 'job' not in context.chat_data:
        context.job_queue.run_repeating(
            check_arbitrage_opportunities,
            interval=120.0,  # Увеличил до 120 секунд
            first=5.0,
            chat_id=update.effective_chat.id
        )
        context.chat_data['job'] = True

async def manual_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if ADMIN_IDS and user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Нет доступа.")
        return
    
    await update.message.reply_text("🔍 Сканирую...")
    
    try:
        await check_arbitrage_opportunities(context)
        
        if 'opportunities' in arbitrage_cache:
            message = format_opportunities_message(arbitrage_cache['opportunities'][:5])
            await update.message.reply_text(message, parse_mode='HTML')
        else:
            await update.message.reply_text("Арбитражных возможностей не найдено.")
    except Exception as e:
        logger.error(f"Ошибка сканирования: {e}")
        await update.message.reply_text(f"Ошибка: {str(e)[:100]}")

async def bot_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_text = (
        "📊 <b>Статус:</b>\n\n"
        f"• Пользователей: {len(active_users)}\n"
        f"• Бирж: {len(exchanges)}\n"
        f"• Последнее сканирование: {arbitrage_cache.get('last_scan', 'нет')}\n"
        f"• Возможностей в кэше: {len(arbitrage_cache.get('opportunities', []))}\n\n"
        f"<b>Настройки:</b>\n"
        f"• Прибыль: >{MIN_PROFIT_PERCENT}%\n"
        f"• Объём: >{MIN_VOLUME_USDT} USDT\n"
        f"• Пар за сканирование: {SCAN_LIMIT}\n"
        f"• Интервал: 120 сек\n\n"
        "<b>Биржи:</b>\n"
    )
    
    for name in exchanges.keys():
        status_text += f"• {name.upper()}\n"
    
    await update.message.reply_text(status_text, parse_mode='HTML')

async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings_text = (
        "⚙️ <b>Текущие настройки:</b>\n\n"
        f"• MIN_PROFIT_PERCENT = {MIN_PROFIT_PERCENT}%\n"
        f"• MIN_VOLUME_USDT = {MIN_VOLUME_USDT}\n"
        f"• SCAN_LIMIT = {SCAN_LIMIT} пар\n"
        f"• Биржи: {', '.join(exchanges.keys())}\n\n"
        "<i>Чтобы изменить, отредактируйте переменные в коде.</i>"
    )
    await update.message.reply_text(settings_text, parse_mode='HTML')

async def stop_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in active_users:
        active_users.remove(user_id)
        await update.message.reply_text("🔕 Уведомления отключены. /start для возобновления.")
    else:
        await update.message.reply_text("Уведомления уже отключены.")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Ошибка: {context.error}")

async def post_init(application):
    logger.info("Бот инициализирован!")

def main():
    logger.info("Запуск бота...")
    
    if not BOT_TOKEN:
        logger.error("Нет токена!")
        sys.exit(1)
    
    try:
        application = ApplicationBuilder() \
            .token(BOT_TOKEN) \
            .post_init(post_init) \
            .build()
        
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("scan", manual_scan))
        application.add_handler(CommandHandler("status", bot_status))
        application.add_handler(CommandHandler("settings", show_settings))
        application.add_handler(CommandHandler("stop", stop_notifications))
        
        application.add_error_handler(error_handler)
        
        logger.info("Бот запущен...")
        application.run_polling(allowed_updates=Update.ALL_TYPES, 
                                close_loop=False)  # Важно для Render
        
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        raise

if __name__ == '__main__':
    main()
