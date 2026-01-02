import asyncio
import ccxt
import logging
import os
import sys
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, JobQueue
import aiohttp
from typing import Dict, List, Optional

# ==================== НАСТРОЙКИ ====================
MIN_PROFIT_PERCENT = 2.0        # Минимальная прибыль 2%
MIN_VOLUME_USDT = 5000          # Минимальный объем 5000 USDT
SCAN_LIMIT = 100                # Сканировать 100 USDT пар
SCAN_INTERVAL = 60              # Интервал 60 секунд
MAX_CONCURRENT_REQUESTS = 20    # Максимум одновременных запросов к биржам
# ==================================================

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
    logger.error("ОШИБКА: BOT_TOKEN не найден.")
    sys.exit(1)

admin_ids_str = os.getenv('ADMIN_IDS', '').strip()
ADMIN_IDS = [int(id_str.strip()) for id_str in admin_ids_str.split(',')] if admin_ids_str else []
logger.info(f"Бот запущен. Админы: {ADMIN_IDS}")

# Инициализация бирж (публичный доступ)
EXCHANGES = {
    'kucoin': ccxt.kucoin({'enableRateLimit': True, 'timeout': 10000}),
    'bitrue': ccxt.bitrue({'enableRateLimit': True, 'timeout': 10000}),
    'gateio': ccxt.gateio({'enableRateLimit': True, 'timeout': 10000}),
    'poloniex': ccxt.poloniex({'enableRateLimit': True, 'timeout': 10000}),
    # 'bitmart': ccxt.bitmart({'enableRateLimit': True, 'timeout': 10000}),  # Пока отключен
}

# Глобальные переменные
active_users = set()
arbitrage_cache = {}
session = None

class AsyncExchangeFetcher:
    """Асинхронный загрузчик данных с бирж"""
    
    def __init__(self):
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        
    async def fetch_ticker_batch(self, symbol: str) -> List[Dict]:
        """Асинхронно получает тикеры со всех бирж для одной пары"""
        tasks = []
        for exchange_name, exchange in EXCHANGES.items():
            task = self._fetch_single_ticker(exchange, exchange_name, symbol)
            tasks.append(task)
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in results if r and isinstance(r, dict)]
    
    async def _fetch_single_ticker(self, exchange, exchange_name: str, symbol: str) -> Optional[Dict]:
        """Получает тикер с одной биржи с обработкой ошибок"""
        async with self.semaphore:
            try:
                ticker = exchange.fetch_ticker(symbol)
                if ticker and ticker.get('bid') and ticker.get('ask'):
                    return {
                        'symbol': symbol,
                        'bid': float(ticker['bid']),
                        'ask': float(ticker['ask']),
                        'bidVolume': ticker.get('bidVolume', 0),
                        'askVolume': ticker.get('askVolume', 0),
                        'quoteVolume': ticker.get('quoteVolume', 0),
                        'exchange': exchange_name,
                        'timestamp': ticker.get('timestamp', datetime.now().timestamp())
                    }
            except Exception as e:
                logger.debug(f"Ошибка {exchange_name} {symbol}: {e}")
            return None

def get_usdt_symbols() -> List[str]:
    """Получает список всех USDT пар с исключением мусора"""
    symbols = set()
    
    # Ключевые популярные пары (сканируем в первую очередь)
    priority_pairs = [
        'BTC/USDT', 'ETH/USDT', 'BNB/USDT', 'SOL/USDT', 'XRP/USDT',
        'ADA/USDT', 'AVAX/USDT', 'DOT/USDT', 'DOGE/USDT', 'MATIC/USDT',
        'LINK/USDT', 'ATOM/USDT', 'UNI/USDT', 'LTC/USDT', 'ETC/USDT'
    ]
    
    # Получаем все пары с бирж
    for exchange in EXCHANGES.values():
        try:
            markets = exchange.load_markets()
            for symbol in markets.keys():
                if symbol.endswith('/USDT'):
                    # Фильтруем левередж и мусор
                    if any(x in symbol.upper() for x in ['3S', '3L', '5S', '5L', '10S', '10L', 'UP', 'DOWN', 'BEAR', 'BULL']):
                        continue
                    if symbol.count('/') != 1:  # Только один слэш
                        continue
                    
                    # Проверяем что это не слишком экзотическая пара
                    base_currency = symbol.split('/')[0]
                    if len(base_currency) > 10:  # Слишком длинное название
                        continue
                        
                    symbols.add(symbol)
        except Exception as e:
            logger.error(f"Ошибка загрузки рынков: {e}")
    
    # Добавляем приоритетные пары в начало
    result = []
    for pair in priority_pairs:
        if pair in symbols:
            result.append(pair)
            symbols.remove(pair)
    
    # Добавляем остальные
    result.extend(sorted(list(symbols)))
    return result[:SCAN_LIMIT]  # Ограничиваем лимитом

def calculate_profit_quality(opportunity: Dict) -> float:
    """Рассчитывает качество арбитражной возможности (0-100)"""
    quality = 0
    
    # 1. Прибыльность (50% веса)
    profit_score = min(opportunity['profit_percentage'] * 2, 50)  # 1% = 2 балла, максимум 50
    quality += profit_score
    
    # 2. Объемы (30% веса)
    avg_volume = (opportunity['buy_volume'] + opportunity['sell_volume']) / 2
    if avg_volume > 1000000:  # > 1M USDT
        quality += 30
    elif avg_volume > 100000:  # > 100K USDT
        quality += 20
    elif avg_volume > 10000:   # > 10K USDT
        quality += 10
    elif avg_volume > 5000:    # > 5K USDT
        quality += 5
    
    # 3. Надежность бирж (20% веса)
    reliable_exchanges = {'kucoin', 'gateio', 'poloniex'}
    if opportunity['buy_exchange'] in reliable_exchanges and opportunity['sell_exchange'] in reliable_exchanges:
        quality += 20
    elif opportunity['buy_exchange'] in reliable_exchanges or opportunity['sell_exchange'] in reliable_exchanges:
        quality += 10
    
    return quality

async def check_arbitrage_for_pair(symbol: str, fetcher: AsyncExchangeFetcher) -> Optional[Dict]:
    """Проверяет арбитражную возможность для одной пары"""
    
    # Получаем данные со всех бирж
    tickers = await fetcher.fetch_ticker_batch(symbol)
    if len(tickers) < 2:
        return None
    
    # Находим лучшие цены
    best_bid = max(tickers, key=lambda x: x['bid'])
    best_ask = min(tickers, key=lambda x: x['ask'])
    
    if best_bid['exchange'] == best_ask['exchange']:
        return None
    
    # Проверяем объемы
    if best_bid['quoteVolume'] < MIN_VOLUME_USDT or best_ask['quoteVolume'] < MIN_VOLUME_USDT:
        return None
    
    # Рассчитываем прибыль
    buy_price = best_ask['ask']
    sell_price = best_bid['bid']
    
    if sell_price <= buy_price:
        return None
    
    spread = sell_price - buy_price
    profit_percentage = (spread / buy_price) * 100
    
    # Основной фильтр по минимальной прибыли
    if profit_percentage < MIN_PROFIT_PERCENT:
        return None
    
    # Фильтр нереалистичной прибыли (больше 15% - скорее всего ошибка)
    if profit_percentage > 15.0:
        return None
    
    # Проверяем что цены не слишком низкие (мусорные токены)
    if buy_price < 0.000001 or sell_price < 0.000001:
        return None
    
    opportunity = {
        'symbol': symbol,
        'buy_exchange': best_ask['exchange'],
        'buy_price': buy_price,
        'sell_exchange': best_bid['exchange'],
        'sell_price': sell_price,
        'buy_volume': best_ask['quoteVolume'],
        'sell_volume': best_bid['quoteVolume'],
        'profit': spread,
        'profit_percentage': profit_percentage,
        'quality': 0,
        'timestamp': datetime.now().isoformat()
    }
    
    # Рассчитываем качество
    opportunity['quality'] = calculate_profit_quality(opportunity)
    
    return opportunity

async def check_arbitrage_opportunities(context: ContextTypes.DEFAULT_TYPE):
    """Основная функция сканирования арбитражных возможностей"""
    logger.info(f"🔍 Начинаю сканирование {SCAN_LIMIT} пар...")
    
    # Получаем пары для сканирования
    symbols = get_usdt_symbols()
    logger.info(f"Получено USDT пар для сканирования: {len(symbols)}")
    
    # Создаем асинхронный загрузчик
    fetcher = AsyncExchangeFetcher()
    opportunities = []
    
    # Сканируем пары пакетами по 10 для лучшей производительности
    batch_size = 10
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        
        # Создаем задачи для пакета
        tasks = [check_arbitrage_for_pair(symbol, fetcher) for symbol in batch]
        batch_results = await asyncio.gather(*tasks)
        
        # Добавляем найденные возможности
        for result in batch_results:
            if result:
                opportunities.append(result)
                logger.info(f"Найдено: {result['symbol']} - {result['profit_percentage']:.2f}% (качество: {result['quality']:.1f})")
        
        # Небольшая пауза между пакетами чтобы не перегружать API
        if i + batch_size < len(symbols):
            await asyncio.sleep(0.5)
    
    # Сортируем по качеству (а не только по прибыли)
    if opportunities:
        opportunities.sort(key=lambda x: x['quality'], reverse=True)
        arbitrage_cache['last_scan'] = datetime.now().isoformat()
        arbitrage_cache['opportunities'] = opportunities[:15]  # Сохраняем топ-15
        
        # Отправляем уведомления
        for user_id in active_users:
            try:
                message = format_opportunities_message(opportunities[:8])  # Показываем топ-8
                await context.bot.send_message(
                    chat_id=user_id,
                    text=message,
                    parse_mode='HTML'
                )
            except Exception as e:
                logger.error(f"Ошибка отправки {user_id}: {e}")
    
    logger.info(f"✅ Сканирование завершено. Найдено возможностей: {len(opportunities)}")

def format_opportunities_message(opportunities: List[Dict]) -> str:
    """Форматирует сообщение с арбитражными возможностями"""
    if not opportunities:
        return "🤷‍♂️ Арбитражных возможностей не найдено. Попробуйте позже."
    
    message = f"🏆 <b>ТОП АРБИТРАЖЕЙ (прибыль >{MIN_PROFIT_PERCENT}%):</b>\n\n"
    
    for i, opp in enumerate(opportunities[:8], 1):  # Показываем топ-8
        # Определяем эмодзи для качества
        if opp['quality'] > 70:
            quality_emoji = "🔥"
        elif opp['quality'] > 50:
            quality_emoji = "⭐"
        else:
            quality_emoji = "📊"
        
        message += (
            f"{i}. {quality_emoji} <b>{opp['symbol']}</b>\n"
            f"   📥 Купить: {opp['buy_exchange'].upper()} - ${opp['buy_price']:.8f}\n"
            f"   📤 Продать: {opp['sell_exchange'].upper()} - ${opp['sell_price']:.8f}\n"
            f"   💰 Прибыль: <b>{opp['profit_percentage']:.2f}%</b>\n"
            f"   📊 Объёмы: ${opp['buy_volume']:,.0f} / ${opp['sell_volume']:,.0f}\n"
            f"   🎯 Качество: {opp['quality']:.1f}/100\n"
            f"   ⏱ {datetime.fromisoformat(opp['timestamp']).strftime('%H:%M:%S')}\n\n"
        )
    
    message += (
        f"<i>Настройки: прибыль >{MIN_PROFIT_PERCENT}%, "
        f"объём >{MIN_VOLUME_USDT} USDT, "
        f"сканируется {SCAN_LIMIT} пар</i>\n\n"
        f"⚡ <b>Следующее сканирование через {SCAN_INTERVAL} сек</b>"
    )
    
    return message

# ==================== КОМАНДЫ ТЕЛЕГРАМ ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user_id = update.effective_user.id
    
    if ADMIN_IDS and user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ У вас нет доступа к этому боту.")
        return
    
    active_users.add(user_id)
    
    welcome_text = (
        f"🤖 <b>Arbitr Bot v3.0</b>\n\n"
        f"⚡ <b>Умный арбитражный сканер</b>\n\n"
        f"<b>Текущие настройки:</b>\n"
        f"• Прибыль: >{MIN_PROFIT_PERCENT}%\n"
        f"• Объём: >{MIN_VOLUME_USDT} USDT\n"
        f"• Сканируемых пар: {SCAN_LIMIT}\n"
        f"• Интервал: {SCAN_INTERVAL} сек\n"
        f"• Бирж: {len(EXCHANGES)}\n\n"
        f"<b>Доступные команды:</b>\n"
        f"/scan - Ручное сканирование\n"
        f"/status - Статус и статистика\n"
        f"/help - Помощь и инструкции\n"
        f"/stop - Отключить уведомления\n"
        f"/settings - Текущие настройки\n\n"
        f"<i>Автоматическое сканирование каждые {SCAN_INTERVAL} секунд</i>"
    )
    
    await update.message.reply_text(welcome_text, parse_mode='HTML')
    
    # Запускаем периодическое сканирование
    if 'job' not in context.chat_data:
        context.job_queue.run_repeating(
            check_arbitrage_opportunities,
            interval=SCAN_INTERVAL,
            first=10.0,
            chat_id=update.effective_chat.id,
            name=f"scan_job_{user_id}"
        )
        context.chat_data['job'] = True

async def manual_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /scan"""
    user_id = update.effective_user.id
    
    if ADMIN_IDS and user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ У вас нет доступа к этой команде.")
        return
    
    await update.message.reply_text(f"🔍 Сканирую {SCAN_LIMIT} пар...")
    
    try:
        await check_arbitrage_opportunities(context)
        
        if 'opportunities' in arbitrage_cache and arbitrage_cache['opportunities']:
            message = format_opportunities_message(arbitrage_cache['opportunities'][:8])
            await update.message.reply_text(message, parse_mode='HTML')
        else:
            await update.message.reply_text(
                "🤷‍♂️ Арбитражных возможностей не найдено.\n\n"
                "Возможные причины:\n"
                "• Слишком высокий порог прибыли\n"
                "• Мало ликвидных пар\n"
                "• Все биржи синхронизированы\n\n"
                "Попробуйте позже или измените настройки."
            )
    except Exception as e:
        logger.error(f"Ошибка сканирования: {e}")
        await update.message.reply_text(f"❌ Ошибка: {str(e)[:200]}")

async def bot_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /status"""
    working_exchanges = []
    for name, exchange in EXCHANGES.items():
        try:
            exchange.fetch_ticker('BTC/USDT')
            working_exchanges.append(name)
        except:
            pass
    
    status_text = (
        f"📊 <b>СТАТУС БОТА</b>\n\n"
        f"<b>Пользователи:</b>\n"
        f"• Активных: {len(active_users)}\n"
        f"• Админов: {len(ADMIN_IDS)}\n\n"
        f"<b>Сканирование:</b>\n"
        f"• Бирж онлайн: {len(working_exchanges)}/{len(EXCHANGES)}\n"
        f"• Последнее сканирование: {arbitrage_cache.get('last_scan', 'ещё не было')}\n"
        f"• Найдено возможностей: {len(arbitrage_cache.get('opportunities', []))}\n"
        f"• В кэше: {len(arbitrage_cache.get('opportunities', []))} сигналов\n\n"
        f"<b>Работающие биржи:</b>\n"
    )
    
    for name in working_exchanges:
        status_text += f"• {name.upper()}: ✅\n"
    
    for name in EXCHANGES:
        if name not in working_exchanges:
            status_text += f"• {name.upper()}: ❌\n"
    
    await update.message.reply_text(status_text, parse_mode='HTML')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help (РАБОЧАЯ ВЕРСИЯ)"""
    help_text = (
        f"📚 <b>ПОМОЩЬ ПО ARBITR BOT</b>\n\n"
        f"<b>Как это работает:</b>\n"
        f"Бот сканирует {len(EXCHANGES)} биржи каждые {SCAN_INTERVAL} секунд.\n"
        f"Ищет разницу в ценах на одинаковые торговые пары.\n"
        f"Показывает только прибыльные возможности (> {MIN_PROFIT_PERCENT}%).\n\n"
        f"<b>Основные команды:</b>\n"
        f"/start - Запустить бота и получать уведомления\n"
        f"/scan - Ручное сканирование (немедленно)\n"
        f"/status - Статус бота и бирж\n"
        f"/stop - Отключить автоматические уведомления\n"
        f"/help - Эта справка\n\n"
        f"<b>Что такое 'качество' арбитража?</b>\n"
        f"• 🔥 70+ - Отличная возможность\n"
        f"• ⭐ 50-70 - Хорошая возможность\n"
        f"• 📊 <50 - Средняя возможность\n\n"
        f"<b>Отслеживаемые биржи:</b>\n"
    )
    
    for name in EXCHANGES.keys():
        help_text += f"• {name.upper()}\n"
    
    help_text += f"\n⚙️ <b>Текущие настройки:</b>\n"
    help_text += f"• Минимальная прибыль: {MIN_PROFIT_PERCENT}%\n"
    help_text += f"• Минимальный объём: {MIN_VOLUME_USDT} USDT\n"
    help_text += f"• Сканируемых пар: {SCAN_LIMIT}\n"
    help_text += f"• Интервал: {SCAN_INTERVAL} сек\n\n"
    help_text += f"<i>Для изменения настроек отредактируйте файл bot.py</i>"
    
    await update.message.reply_text(help_text, parse_mode='HTML')

async def stop_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /stop"""
    user_id = update.effective_user.id
    if user_id in active_users:
        active_users.remove(user_id)
        await update.message.reply_text(
            "🔕 Автоматические уведомления отключены.\n\n"
            "Используйте /start чтобы возобновить."
        )
    else:
        await update.message.reply_text("Уведомления уже отключены.")

async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /settings"""
    settings_text = (
        f"⚙️ <b>ТЕКУЩИЕ НАСТРОЙКИ</b>\n\n"
        f"<b>Основные параметры:</b>\n"
        f"MIN_PROFIT_PERCENT = {MIN_PROFIT_PERCENT}%\n"
        f"MIN_VOLUME_USDT = {MIN_VOLUME_USDT:,} USDT\n"
        f"SCAN_LIMIT = {SCAN_LIMIT} пар\n"
        f"SCAN_INTERVAL = {SCAN_INTERVAL} сек\n\n"
        f"<b>Производительность:</b>\n"
        f"MAX_CONCURRENT_REQUESTS = {MAX_CONCURRENT_REQUESTS}\n"
        f"Бирж подключено: {len(EXCHANGES)}\n\n"
        f"<b>Подключенные биржи:</b>\n"
    )
    
    for name in EXCHANGES.keys():
        settings_text += f"• {name}\n"
    
    settings_text += (
        f"\n📝 <i>Для изменения параметров отредактируйте файл bot.py</i>\n"
        f"📍 <i>Строки 8-14 в начале файла</i>"
    )
    
    await update.message.reply_text(settings_text, parse_mode='HTML')

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок"""
    logger.error(f"Ошибка: {context.error}")
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Произошла ошибка. Попробуйте позже или свяжитесь с разработчиком."
            )
        except:
            pass

async def post_init(application):
    """Функция, выполняемая после инициализации"""
    logger.info(f"✅ Бот запущен! Бирж: {len(EXCHANGES)}")

def main():
    """Основная функция запуска бота"""
    logger.info(f"🚀 Запуск Arbitr Bot v3.0...")
    
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN не найден!")
        sys.exit(1)
    
    try:
        application = ApplicationBuilder() \
            .token(BOT_TOKEN) \
            .post_init(post_init) \
            .build()
        
        # Регистрируем обработчики команд
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("scan", manual_scan))
        application.add_handler(CommandHandler("status", bot_status))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("stop", stop_notifications))
        application.add_handler(CommandHandler("settings", show_settings))
        
        application.add_error_handler(error_handler)
        
        logger.info("🤖 Бот запущен и ожидает команды...")
        
        # Запускаем бота с опцией close_loop=False для Render
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            close_loop=False,
            drop_pending_updates=True
        )
        
    except Exception as e:
        logger.error(f"💥 Критическая ошибка: {e}")
        raise

if __name__ == '__main__':
    main()
