import asyncio
import ccxt
import logging
import os
import sys
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, JobQueue
from concurrent.futures import ThreadPoolExecutor

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
    logger.error("Токен не найден!")
    sys.exit(1)
# BOT_TOKEN = os.getenv('BOT_TOKEN')  # <-- Эту старую строку можно закомментировать
ADMIN_IDS = ['6590452577']  # Укажите ID в списке
# ADMIN_IDS = list(map(int, os.getenv('ADMIN_IDS', '').split(','))) if os.getenv('ADMIN_IDS') else []
# Инициализация бирж
exchanges = {
    'kucoin': ccxt.kucoin({
        'apiKey': os.getenv('KUCOIN_API_KEY'),
        'secret': os.getenv('KUCOIN_SECRET'),
        'password': os.getenv('KUCOIN_PASSWORD'),
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    }),
    'bitrue': ccxt.bitrue({
        'apiKey': os.getenv('BITRUE_API_KEY'),
        'secret': os.getenv('BITRUE_SECRET'),
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    }),
    'bitmart': ccxt.bitmart({
        'apiKey': os.getenv('BITMART_API_KEY'),
        'secret': os.getenv('BITMART_SECRET'),
        'uid': os.getenv('BITMART_UID'),
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    }),
    'gateio': ccxt.gateio({
        'apiKey': os.getenv('GATEIO_API_KEY'),
        'secret': os.getenv('GATEIO_SECRET'),
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    }),
    'poloniex': ccxt.poloniex({
        'apiKey': os.getenv('POLONIEX_API_KEY'),
        'secret': os.getenv('POLONIEX_SECRET'),
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    }),
}

# Логируем успешную инициализацию бирж
for name, exchange in exchanges.items():
    logger.info(f'✓ {name} клиент создан')

# Глобальные переменные
active_users = set()
arbitrage_cache = {}
executor = ThreadPoolExecutor(max_workers=10)

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
    """Асинхронное получение тикера"""
    exchange = exchanges[exchange_name]
    try:
        ticker = exchange.fetch_ticker(symbol)
        return {
            'symbol': symbol,
            'bid': ticker['bid'] if ticker['bid'] else 0,
            'ask': ticker['ask'] if ticker['ask'] else 0,
            'last': ticker['last'] if ticker['last'] else 0,
            'exchange': exchange_name
        }
    except Exception as e:
        logger.debug(f"Ошибка получения {symbol} с {exchange_name}: {e}")
        return None

async def check_arbitrage_for_pair(symbol):
    """Проверка арбитражных возможностей для конкретной пары"""
    tasks = []
    for exchange_name in exchanges.keys():
        tasks.append(fetch_ticker(exchange_name, symbol))
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    valid_prices = []
    
    for result in results:
        if isinstance(result, dict) and result and result['bid'] > 0 and result['ask'] > 0:
            valid_prices.append(result)
    
    if len(valid_prices) < 2:
        return None
    
    # Находим лучшие цены покупки и продажи
    best_bid = max(valid_prices, key=lambda x: x['bid'])
    best_ask = min(valid_prices, key=lambda x: x['ask'])
    
    if best_bid['exchange'] == best_ask['exchange']:
        return None
    
    spread = best_bid['bid'] - best_ask['ask']
    if spread <= 0:
        return None
    
    profit_percentage = (spread / best_ask['ask']) * 100
    
    if profit_percentage < 0.5:  # Минимальный порог прибыли 0.5%
        return None
    
    return {
        'symbol': symbol,
        'buy_exchange': best_ask['exchange'],
        'buy_price': best_ask['ask'],
        'sell_exchange': best_bid['exchange'],
        'sell_price': best_bid['bid'],
        'profit': spread,
        'profit_percentage': profit_percentage,
        'timestamp': datetime.now().isoformat()
    }

async def check_arbitrage_opportunities(context: ContextTypes.DEFAULT_TYPE):
    """Проверка арбитражных возможностей по всем парам"""
    logger.info("Начинаю сканирование арбитражных возможностей...")
    
    symbols = get_all_symbols()
    logger.info(f"Всего пар для сканирования: {len(symbols)}")
    
    opportunities = []
    
    # Ограничиваем количество пар для сканирования
    symbols_to_scan = symbols[:50]  # Сканируем первые 50 пар для скорости
    
    for symbol in symbols_to_scan:
        try:
            opportunity = await check_arbitrage_for_pair(symbol)
            if opportunity:
                opportunities.append(opportunity)
                logger.info(f"Найдена возможность: {opportunity}")
        except Exception as e:
            logger.error(f"Ошибка при проверке пары {symbol}: {e}")
    
    if opportunities:
        # Сортируем по прибыльности
        opportunities.sort(key=lambda x: x['profit_percentage'], reverse=True)
        
        # Кэшируем лучшие возможности
        arbitrage_cache['last_scan'] = datetime.now().isoformat()
        arbitrage_cache['opportunities'] = opportunities[:10]  # Сохраняем топ-10
        
        # Отправляем уведомления активным пользователям
        for user_id in active_users:
            try:
                message = format_opportunities_message(opportunities[:5])
                await context.bot.send_message(
                    chat_id=user_id,
                    text=message,
                    parse_mode='HTML'
                )
            except Exception as e:
                logger.error(f"Ошибка отправки сообщения пользователю {user_id}: {e}")
    
    logger.info(f"Сканирование завершено. Найдено возможностей: {len(opportunities)}")

def format_opportunities_message(opportunities):
    """Форматирование сообщения с арбитражными возможностями"""
    if not opportunities:
        return "На данный момент арбитражных возможностей не найдено."
    
    message = "🏆 <b>ТОП Арбитражных возможностей:</b>\n\n"
    
    for i, opp in enumerate(opportunities[:5], 1):
        message += (
            f"{i}. <b>{opp['symbol']}</b>\n"
            f"   📥 Купить на: {opp['buy_exchange'].upper()} - ${opp['buy_price']:.8f}\n"
            f"   📤 Продать на: {opp['sell_exchange'].upper()} - ${opp['sell_price']:.8f}\n"
            f"   💰 Прибыль: ${opp['profit']:.8f} (<b>{opp['profit_percentage']:.2f}%</b>)\n"
            f"   ⏰ Время: {datetime.fromisoformat(opp['timestamp']).strftime('%H:%M:%S')}\n\n"
        )
    
    return message

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user_id = update.effective_user.id
    
    if ADMIN_IDS and user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ У вас нет доступа к этому боту.")
        return
    
    active_users.add(user_id)
    
    welcome_text = (
        "🤖 <b>Добро пожаловать в Arbitr Bot!</b>\n\n"
        "Я сканирую криптобиржи в поисках арбитражных возможностей.\n\n"
        "<b>Доступные команды:</b>\n"
        "/scan - Ручное сканирование\n"
        "/status - Статус бота\n"
        "/help - Справка\n"
        "/stop - Остановить уведомления\n\n"
        "Автоматические уведомления включены."
    )
    
    await update.message.reply_text(welcome_text, parse_mode='HTML')
    
    # Запускаем периодическую проверку
    if 'job' not in context.chat_data:
        context.job_queue.run_repeating(
            check_arbitrage_opportunities,
            interval=60.0,  # Проверка каждые 60 секунд
            first=10.0,
            chat_id=update.effective_chat.id
        )
        context.chat_data['job'] = True

async def manual_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /scan"""
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
    """Обработчик команды /status"""
    status_text = (
        "📊 <b>Статус бота:</b>\n\n"
        f"• Активных пользователей: {len(active_users)}\n"
        f"• Отслеживаемых бирж: {len(exchanges)}\n"
        f"• Последнее сканирование: {arbitrage_cache.get('last_scan', 'еще не было')}\n"
        f"• Найдено возможностей: {len(arbitrage_cache.get('opportunities', []))}\n\n"
        "<b>Статус бирж:</b>\n"
    )
    
    for name, exchange in exchanges.items():
        try:
            # Быстрая проверка доступности биржи
            exchange.fetch_ticker('BTC/USDT')
            status_text += f"• {name.upper()}: ✅ Онлайн\n"
        except:
            status_text += f"• {name.upper()}: ❌ Оффлайн\n"
    
    await update.message.reply_text(status_text, parse_mode='HTML')

async def stop_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /stop"""
    user_id = update.effective_user.id
    if user_id in active_users:
        active_users.remove(user_id)
        await update.message.reply_text("🔕 Уведомления отключены. Используйте /start для возобновления.")
    else:
        await update.message.reply_text("Уведомления уже отключены.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help"""
    help_text = (
        "📚 <b>Справка по Arbitr Bot</b>\n\n"
        "<b>Команды:</b>\n"
        "/start - Запустить бота и начать получать уведомления\n"
        "/scan - Ручное сканирование арбитражных возможностей\n"
        "/status - Показать статус бота и бирж\n"
        "/stop - Остановить автоматические уведомления\n"
        "/help - Показать эту справку\n\n"
        "<b>Как это работает:</b>\n"
        "Бот автоматически сканирует цены на различных криптобиржах "
        "и находит разницы в ценах (арбитраж). При обнаружении прибыльной "
        "возможности (более 0.5%) отправляется уведомление.\n\n"
        "<b>Отслеживаемые биржи:</b>\n"
        "• KuCoin\n• Bitrue\n• Bitmart\n• Gate.io\n• Poloniex\n\n"
        "⏰ Автоматическое сканирование происходит каждые 60 секунд."
    )
    await update.message.reply_text(help_text, parse_mode='HTML')

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок"""
    logger.error(f"Ошибка при обработке обновления: {context.error}")
    
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Произошла ошибка. Попробуйте позже или свяжитесь с администратором."
            )
        except:
            pass

async def post_init(application):
    """Функция, выполняемая после инициализации бота"""
    logger.info("Бот успешно инициализирован и готов к работе!")

def main():
    """Основная функция запуска бота"""
    logger.info("Инициализация бота...")
    
    if not BOT_TOKEN:
        logger.error("Токен бота не найден! Убедитесь, что BOT_TOKEN установлен в переменных окружения.")
        # В Railway нужно установить переменную окружения
        print("Установите переменную окружения BOT_TOKEN на Railway!")
        print("Перейдите в Settings -> Variables и добавьте BOT_TOKEN")
        sys.exit(1)
    
    try:
        # Создаем приложение
        application = ApplicationBuilder() \
            .token(BOT_TOKEN) \
            .post_init(post_init) \
            .build()
        
        # Добавляем обработчики команд
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("scan", manual_scan))
        application.add_handler(CommandHandler("status", bot_status))
        application.add_handler(CommandHandler("stop", stop_notifications))
        application.add_handler(CommandHandler("help", help_command))
        
        # Добавляем обработчик ошибок
        application.add_error_handler(error_handler)
        
        logger.info("Бот запущен и ожидает команды...")
        
        # Запускаем бота (это блокирующий вызов)
        application.run_polling(allowed_updates=Update.ALL_TYPES)
        
    except Exception as e:
        logger.error(f"Критическая ошибка при запуске бота: {e}")
        raise

if __name__ == '__main__':
    # Простой запуск без asyncio.run()
    main()
