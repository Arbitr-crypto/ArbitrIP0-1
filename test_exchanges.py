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

# Конфигурация
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    logger.error("КРИТИЧЕСКАЯ ОШИБКА: Переменная окружения 'BOT_TOKEN' не найдена.")
    sys.exit(1)

# Получаем строку с ID администраторов
admin_ids_str = os.getenv('ADMIN_IDS', '').strip()
if ADMIN_IDS and admin_ids_str:
    ADMIN_IDS = [int(id_str.strip()) for id_str in admin_ids_str.split(',')]
else:
    ADMIN_IDS = []
    logger.warning("Переменная 'ADMIN_IDS' не задана. Доступ к боту будет открыт.")

logger.info(f"Токен получен. ID администраторов: {ADMIN_IDS}")

# Инициализация бирж ТОЛЬКО с публичным доступом
exchanges = {
    'kucoin': ccxt.kucoin({
        'apiKey': '',  # Публичный доступ
        'secret': '',
        'password': '',
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    }),
    'bitrue': ccxt.bitrue({
        'apiKey': '',  # Публичный доступ
        'secret': '',
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    }),
    'bitmart': ccxt.bitmart({
        'apiKey': '',  # Публичный доступ
        'secret': '',
        'uid': '',
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    }),
    'gateio': ccxt.gateio({
        'apiKey': '',  # Публичный доступ
        'secret': '',
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    }),
    'poloniex': ccxt.poloniex({
        'apiKey': '',  # Публичный доступ
        'secret': '',
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    }),
}

# Логируем успешную инициализацию бирж
for name in exchanges.keys():
    logger.info(f'✓ {name} клиент создан (публичный доступ)')

# Глобальные переменные
active_users = set()
arbitrage_cache = {}

def get_all_symbols():
    """Получение всех доступных торговых пар с бирж"""
    symbols = set()
    for exchange in exchanges.values():
        try:
            markets = exchange.load_markets()
            symbols.update(markets.keys())
        except Exception as e:
            logger.error(f"Ошибка загрузки рынков с {exchange.name}: {e}")
    return list(symbols)

async def fetch_ticker(exchange_name, symbol):
    """Асинхронное получение тикера с объемом"""
    exchange = exchanges[exchange_name]
    try:
        ticker = exchange.fetch_ticker(symbol)
        return {
            'symbol': symbol,
            'bid': ticker['bid'] if ticker['bid'] else 0,
            'ask': ticker['ask'] if ticker['ask'] else 0,
            'last': ticker['last'] if ticker['last'] else 0,
            'quoteVolume': ticker['quoteVolume'] if ticker.get('quoteVolume') else 0,
            'exchange': exchange_name
        }
    except Exception as e:
        logger.debug(f"Ошибка получения {symbol} с {exchange_name}: {e}")
        return None

async def check_arbitrage_for_pair(symbol):
    """Проверка арбитражных возможностей для конкретной пары"""
    
    # ФИЛЬТР: Только пары с USDT
    if not symbol.endswith('/USDT'):
        return None

    # ФИЛЬТР: Исключаем левереджные токены
    leveraged_keywords = ['3S', '3L', '5S', '5L', '10S', '10L', 'BEAR', 'BULL', 'UP', 'DOWN']
    if any(keyword in symbol.upper() for keyword in leveraged_keywords):
        return None
    
    # Получаем цены со всех бирж
    tasks = [fetch_ticker(name, symbol) for name in exchanges.keys()]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Фильтруем валидные результаты
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
    
    # ФИЛЬТР ПО ОБЪЕМУ
    min_volume = 10000
    if best_bid['quoteVolume'] < min_volume or best_ask['quoteVolume'] < min_volume:
        return None
    
    # РАСЧЁТ ПРИБЫЛИ
    buy_price = best_ask['ask']
    sell_price = best_bid['bid']
    spread = sell_price - buy_price
    
    if spread <= 0:
        return None
    
    profit_percentage = (spread / buy_price) * 100
    
    # ФИЛЬТР: Минимальная прибыль 2.0%
    if profit_percentage < 2.0:
        return None
    
    # ФИЛЬТР: Максимальная реалистичная прибыль
    if profit_percentage > 15.0:
        return None
    
    # ФИЛЬТР: Корректные цены
    if buy_price < 0.0005 or sell_price < 0.0005:
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
    """Проверка арбитражных возможностей по всем парам"""
    logger.info("Начинаю сканирование...")
    
    symbols = get_all_symbols()
    logger.info(f"Всего пар для сканирования: {len(symbols)}")
    
    opportunities = []
    symbols_to_scan = symbols[:50]  # Первые 50 пар для скорости
    
    for symbol in symbols_to_scan:
        try:
            opportunity = await check_arbitrage_for_pair(symbol)
            if opportunity:
                opportunities.append(opportunity)
                logger.info(f"Найдена возможность: {opportunity['symbol']} - {opportunity['profit_percentage']:.2f}%")
        except Exception as e:
            logger.debug(f"Ошибка при проверке пары {symbol}: {e}")
    
    if opportunities:
        opportunities.sort(key=lambda x: x['profit_percentage'], reverse=True)
        arbitrage_cache['last_scan'] = datetime.now().isoformat()
        arbitrage_cache['opportunities'] = opportunities[:10]
        
        for user_id in active_users:
            try:
                message = format_opportunities_message(opportunities[:5])
                await context.bot.send_message(chat_id=user_id, text=message, parse_mode='HTML')
            except Exception as e:
                logger.error(f"Ошибка отправки пользователю {user_id}: {e}")
    
    logger.info(f"Сканирование завершено. Найдено: {len(opportunities)}")

def format_opportunities_message(opportunities):
    """Форматирование сообщения с арбитражными возможностями"""
    if not opportunities:
        return "На данный момент арбитражных возможностей не найдено."
    
    message = "🏆 <b>ТОП АРБИТРАЖНЫХ ВОЗМОЖНОСТЕЙ:</b>\n\n"
    
    for i, opp in enumerate(opportunities[:5], 1):
        message += (
            f"{i}. <b>{opp['symbol']}</b>\n"
            f"   📥 Купить на: {opp['buy_exchange'].upper()} - ${opp['buy_price']:.8f}\n"
            f"   📤 Продать на: {opp['sell_exchange'].upper()} - ${opp['sell_price']:.8f}\n"
            f"   💰 Прибыль: <b>{opp['profit_percentage']:.2f}%</b>\n"
            f"   📊 Объём (24ч): Купить: ${opp['buy_volume']:.0f}, Продать: ${opp['sell_volume']:.0f}\n"
            f"   ⏰ Время: {datetime.fromisoformat(opp['timestamp']).strftime('%H:%M:%S')}\n\n"
        )
    
    return message

# Команды бота (без изменений)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if ADMIN_IDS and user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ У вас нет доступа к этому боту.")
        return
    
    active_users.add(user_id)
    welcome_text = (
        "🤖 <b>Добро пожаловать в Arbitr Bot!</b>\n\n"
        "Я сканирую 5 бирж в поисках арбитражных возможностей.\n"
        "Использую только публичные данные API.\n\n"
        "<b>Команды:</b>\n"
        "/scan - Ручное сканирование\n"
        "/status - Статус бота\n"
        "/help - Справка\n"
        "/stop - Остановить уведомления\n\n"
        "Автосканирование каждые 60 секунд."
    )
    
    await update.message.reply_text(welcome_text, parse_mode='HTML')
    
    if 'job' not in context.chat_data:
        context.job_queue.run_repeating(
            check_arbitrage_opportunities,
            interval=60.0,
            first=10.0,
            chat_id=update.effective_chat.id
        )
        context.chat_data['job'] = True

async def manual_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if ADMIN_IDS and user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ У вас нет доступа к этой команде.")
        return
    
    await update.message.reply_text("🔍 Начинаю ручное сканирование...")
    
    try:
        await check_arbitrage_opportunities(context)
        
        if 'opportunities' in arbitrage_cache:
            message = format_opportunities_message(arbitrage_cache['opportunities'][:5])
            await update.message.reply_text(message, parse_mode='HTML')
        else:
            await update.message.reply_text("Арбитражных возможностей не найдено.")
    except Exception as e:
        logger.error(f"Ошибка при ручном сканировании: {e}")
        await update.message.reply_text(f"Ошибка при сканировании: {str(e)}")

async def bot_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_text = (
        "📊 <b>Статус бота:</b>\n\n"
        f"• Активных пользователей: {len(active_users)}\n"
        f"• Отслеживаемых бирж: {len(exchanges)}\n"
        f"• Последнее сканирование: {arbitrage_cache.get('last_scan', 'еще не было')}\n"
        f"• Найдено возможностей: {len(arbitrage_cache.get('opportunities', []))}\n\n"
        "<b>Биржи (публичный доступ):</b>\n"
    )
    
    for name in exchanges.keys():
        status_text += f"• {name.upper()}: ✅ Онлайн\n"
    
    await update.message.reply_text(status_text, parse_mode='HTML')

async def stop_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in active_users:
        active_users.remove(user_id)
        await update.message.reply_text("🔕 Уведомления отключены. Используйте /start для возобновления.")
    else:
        await update.message.reply_text("Уведомления уже отключены.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📚 <b>Справка по Arbitr Bot</b>\n\n"
        "<b>Команды:</b>\n"
        "/start - Запустить бота и начать получать уведомления\n"
        "/scan - Ручное сканирование\n"
        "/status - Показать статус бота и бирж\n"
        "/stop - Остановить автоматические уведомления\n"
        "/help - Показать эту справку\n\n"
        "<b>Отслеживаемые биржи (публичный доступ):</b>\n"
        "• KuCoin\n• Bitrue\n• Bitmart\n• Gate.io\n• Poloniex\n\n"
        "⏰ Автоматическое сканирование каждые 60 секунд.\n"
        "💰 Минимальная прибыль для показа: 2.0%\n"
        "📊 Минимальный объём: 10,000 USDT"
    )
    await update.message.reply_text(help_text, parse_mode='HTML')

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Ошибка при обработке обновления: {context.error}")
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Произошла ошибка. Попробуйте позже.")
        except:
            pass

async def post_init(application):
    logger.info("Бот успешно инициализирован и готов к работе!")

def main():
    logger.info("Инициализация бота...")
    
    if not BOT_TOKEN:
        logger.error("Токен бота не найден!")
        sys.exit(1)
    
    try:
        application = ApplicationBuilder() \
            .token(BOT_TOKEN) \
            .post_init(post_init) \
            .build()
        
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("scan", manual_scan))
        application.add_handler(CommandHandler("status", bot_status))
        application.add_handler(CommandHandler("stop", stop_notifications))
        application.add_handler(CommandHandler("help", help_command))
        
        application.add_error_handler(error_handler)
        
        logger.info("Бот запущен и ожидает команды...")
        application.run_polling(allowed_updates=Update.ALL_TYPES)
        
    except Exception as e:
        logger.error(f"Критическая ошибка при запуске бота: {e}")
        raise

if __name__ == '__main__':
    main()
