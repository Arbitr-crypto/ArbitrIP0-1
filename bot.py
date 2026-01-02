import asyncio
import ccxt
import logging
import os
import sys
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, JobQueue
import aiohttp
from flask import Flask
import threading
from typing import Dict, List, Optional, Tuple
import time
import random
from functools import wraps

# ==================== НАСТРОЙКИ ====================
MIN_NET_PROFIT_PERCENT = 0.5        # Минимальная ЧИСТАЯ прибыль 0.5% (после комиссий)
MIN_VOLUME_USDT = 5000              # Минимальный объем 5000 USDT
SCAN_LIMIT = 100                    # Сканировать 100 USDT пар
SCAN_INTERVAL = 60                  # Интервал 60 секунд
MAX_CONCURRENT_REQUESTS = 20        # Максимум одновременных запросов к биржам
MAX_API_RETRIES = 3                 # Максимум повторных попыток при ошибках API
INITIAL_RETRY_DELAY = 1             # Начальная задержка между повторными попытками (секунды)
REQUEST_TIMEOUT = 15                # Таймаут запросов к биржам (секунды)
# ==================================================

# ==================== ОСНОВНОЙ КОД ====================

# 1. Сначала настраиваем логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger('arbi-bot')

# 2. Создаем веб-сервер Flask (logger уже определен)
web_app = Flask(__name__)

@web_app.route('/')
def home():
    return "✅ Arbitr Bot v5.0 is running"

@web_app.route('/health')
def health():
    return "OK", 200

def run_web_server():
    """Запускает Flask-сервер в отдельном потоке"""
    port = int(os.getenv("PORT", 10000))
    web_app.run(host='0.0.0.0', port=port)

# Автоматически запускаем веб-сервер при импорте (для Render)
if __name__ != '__main__':
    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()
    logger.info(f"🌐 Фоновый веб-сервер запущен для порта {os.getenv('PORT', 10000)}")

# 3. Загрузка переменных окружения
load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    logger.error("ОШИБКА: BOT_TOKEN не найден.")
    sys.exit(1)

admin_ids_str = os.getenv('ADMIN_IDS', '').strip()
ADMIN_IDS = [int(id_str.strip()) for id_str in admin_ids_str.split(',')] if admin_ids_str else []
logger.info(f"Бот запущен. Админы: {ADMIN_IDS}")

# ==================== ЭТАП 2: ДОБАВЛЕНИЕ ВСЕХ БИРЖ ====================

EXCHANGES = {}
FEES = {}
EXCHANGE_STATUS = {}

def init_exchanges():
    """Инициализирует все биржи и загружает комиссии"""
    exchange_configs = [
        ('bitrue', ccxt.bitrue, {'enableRateLimit': True, 'timeout': REQUEST_TIMEOUT * 1000}),
        ('htx', ccxt.huobi, {'enableRateLimit': True, 'timeout': REQUEST_TIMEOUT * 1000}),
        ('bybit', ccxt.bybit, {'enableRateLimit': True, 'timeout': REQUEST_TIMEOUT * 1000}),
        ('bitmart', ccxt.bitmart, {'enableRateLimit': True, 'timeout': REQUEST_TIMEOUT * 1000}),
        ('kucoin', ccxt.kucoin, {'enableRateLimit': True, 'timeout': REQUEST_TIMEOUT * 1000}),
        ('gateio', ccxt.gateio, {'enableRateLimit': True, 'timeout': REQUEST_TIMEOUT * 1000}),
        ('poloniex', ccxt.poloniex, {'enableRateLimit': True, 'timeout': REQUEST_TIMEOUT * 1000}),
    ]
    
    # KCEX не поддерживается в ccxt - исключаем
    
    for exchange_id, exchange_class, config in exchange_configs:
        try:
            exchange = exchange_class(config)
            EXCHANGES[exchange_id] = exchange
            EXCHANGE_STATUS[exchange_id] = {
                'online': False,
                'last_check': None,
                'errors': 0,
                'last_error': None
            }
            
            # Загружаем комиссии
            try:
                if hasattr(exchange, 'fetch_trading_fees'):
                    fees = exchange.fetch_trading_fees()
                    FEES[exchange_id] = {
                        'taker': fees.get('taker', 0.001),
                        'maker': fees.get('maker', 0.001)
                    }
                else:
                    # Стандартные комиссии для бирж
                    default_fees = {
                        'bitrue': {'taker': 0.0005, 'maker': 0.0005},
                        'htx': {'taker': 0.002, 'maker': 0.002},
                        'bybit': {'taker': 0.001, 'maker': 0.001},
                        'bitmart': {'taker': 0.0025, 'maker': 0.0025},
                        'kucoin': {'taker': 0.001, 'maker': 0.001},
                        'gateio': {'taker': 0.002, 'maker': 0.002},
                        'poloniex': {'taker': 0.002, 'maker': 0.001},
                    }
                    FEES[exchange_id] = default_fees.get(exchange_id, {'taker': 0.001, 'maker': 0.001})
                
                logger.info(f"✅ {exchange_id.upper()}: комиссия taker={FEES[exchange_id]['taker']*100:.2f}%")
                EXCHANGE_STATUS[exchange_id]['online'] = True
                
            except Exception as e:
                logger.warning(f"Не удалось загрузить комиссии для {exchange_id}: {e}")
                FEES[exchange_id] = {'taker': 0.001, 'maker': 0.001}
                EXCHANGE_STATUS[exchange_id]['online'] = True
                
        except Exception as e:
            logger.error(f"Ошибка инициализации {exchange_id}: {e}")
            EXCHANGE_STATUS[exchange_id] = {
                'online': False,
                'last_check': datetime.now(),
                'errors': 1,
                'last_error': str(e)
            }

# Инициализируем биржи при запуске
init_exchanges()

# Глобальные переменные
active_users = set()
arbitrage_cache = {}
session = None

# ==================== ЭТАП 6: УЛУЧШЕННАЯ ОБРАБОТКА ОШИБОК ====================

def retry_on_failure(max_retries=MAX_API_RETRIES, delay=INITIAL_RETRY_DELAY):
    """Декоратор для повторных попыток при сбоях API"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except (ccxt.NetworkError, ccxt.ExchangeError, aiohttp.ClientError, 
                       asyncio.TimeoutError, ConnectionError) as e:
                    last_exception = e
                    exchange_name = kwargs.get('exchange_name', 'unknown')
                    symbol = kwargs.get('symbol', 'unknown')
                    
                    if attempt < max_retries - 1:
                        wait_time = delay * (2 ** attempt) + random.uniform(0, 0.5)
                        logger.warning(f"Попытка {attempt+1}/{max_retries} не удалась для {exchange_name} {symbol}: {e}. Повтор через {wait_time:.1f}с")
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(f"Все {max_retries} попыток не удались для {exchange_name} {symbol}: {e}")
                        EXCHANGE_STATUS[exchange_name]['errors'] += 1
                        EXCHANGE_STATUS[exchange_name]['last_error'] = str(e)
                        EXCHANGE_STATUS[exchange_name]['last_check'] = datetime.now()
            
            # Если все попытки не удались
            if last_exception:
                if isinstance(last_exception, (ccxt.NetworkError, aiohttp.ClientError, ConnectionError)):
                    EXCHANGE_STATUS[kwargs.get('exchange_name', 'unknown')]['online'] = False
                raise last_exception
            
            return None
        return wrapper
    return decorator

class AsyncExchangeFetcher:
    """Асинхронный загрузчик данных с бирж с обработкой ошибок"""
    
    def __init__(self):
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        self.session = None
        
    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT))
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
        
    async def fetch_ticker_batch(self, symbol: str) -> List[Dict]:
        """Асинхронно получает тикеры со всех бирж для одной пары"""
        tasks = []
        for exchange_name, exchange in EXCHANGES.items():
            # Пропускаем биржи, которые не онлайн
            if not EXCHANGE_STATUS[exchange_name].get('online', True):
                continue
                
            task = self._fetch_single_ticker(exchange, exchange_name, symbol)
            tasks.append(task)
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Фильтруем результаты
        valid_results = []
        for result in results:
            if isinstance(result, Exception):
                continue
            if result and isinstance(result, dict):
                valid_results.append(result)
        
        return valid_results
    
    @retry_on_failure()
    async def _fetch_single_ticker(self, exchange, exchange_name: str, symbol: str) -> Optional[Dict]:
        """Получает тикер с одной биржи с обработкой ошибок и повторными попытками"""
        async with self.semaphore:
            try:
                ticker = exchange.fetch_ticker(symbol)
                if ticker and ticker.get('bid') and ticker.get('ask'):
                    # Обновляем статус биржи
                    EXCHANGE_STATUS[exchange_name]['online'] = True
                    EXCHANGE_STATUS[exchange_name]['last_check'] = datetime.now()
                    
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
                else:
                    return None
                    
            except ccxt.BadSymbol:
                # Пара не найдена на бирже - это не ошибка
                return None
            except Exception as e:
                logger.debug(f"Ошибка {exchange_name} {symbol}: {e}")
                raise  # Пробрасываем для декоратора retry_on_failure

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
    for exchange_name, exchange in EXCHANGES.items():
        if not EXCHANGE_STATUS[exchange_name].get('online', True):
            continue
            
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
            logger.error(f"Ошибка загрузки рынков {exchange_name}: {e}")
            EXCHANGE_STATUS[exchange_name]['online'] = False
            EXCHANGE_STATUS[exchange_name]['last_error'] = str(e)
    
    # Добавляем приоритетные пары в начало
    result = []
    for pair in priority_pairs:
        if pair in symbols:
            result.append(pair)
            symbols.remove(pair)
    
    # Добавляем остальные
    result.extend(sorted(list(symbols)))
    return result[:SCAN_LIMIT]  # Ограничиваем лимитом

# ==================== ЭТАП 1: УЧЕТ КОМИССИЙ ====================

def calculate_real_profit(buy_price: float, sell_price: float, 
                         buy_exchange: str, sell_exchange: str) -> Tuple[float, float, float]:
    """
    Рассчитывает реальную прибыль с учетом комиссий
    
    Returns:
        (чистая_прибыль_в_процентах, чистая_прибыль_абсолютная, валовая_прибыль_в_процентах)
    """
    # Получаем комиссии
    buy_fee = FEES.get(buy_exchange, {'taker': 0.001})['taker']
    sell_fee = FEES.get(sell_exchange, {'taker': 0.001})['taker']
    
    # Валовая прибыль
    gross_profit_percent = ((sell_price - buy_price) / buy_price) * 100
    
    # Учитываем комиссии
    # При покупке: платим цену + комиссию
    effective_buy_price = buy_price * (1 + buy_fee)
    
    # При продаже: получаем цену - комиссию
    effective_sell_price = sell_price * (1 - sell_fee)
    
    # Чистая прибыль
    net_profit = effective_sell_price - effective_buy_price
    net_profit_percent = (net_profit / effective_buy_price) * 100 if effective_buy_price > 0 else 0
    
    return net_profit_percent, net_profit, gross_profit_percent

def calculate_profit_quality(opportunity: Dict) -> float:
    """Рассчитывает качество арбитражной возможности (0-100) с учетом комиссий"""
    quality = 0
    
    # 1. Чистая прибыльность (50% веса)
    profit_score = min(opportunity['net_profit_percentage'] * 4, 50)  # 0.5% = 2 балла, максимум 50
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
    reliable_exchanges = {'kucoin', 'gateio', 'bybit', 'htx'}
    if opportunity['buy_exchange'] in reliable_exchanges and opportunity['sell_exchange'] in reliable_exchanges:
        quality += 20
    elif opportunity['buy_exchange'] in reliable_exchanges or opportunity['sell_exchange'] in reliable_exchanges:
        quality += 10
    
    return quality

async def check_arbitrage_for_pair(symbol: str, fetcher: AsyncExchangeFetcher) -> Optional[Dict]:
    """Проверяет арбитражную возможность для одной пары с учетом комиссий"""
    
    # Получаем данные со всех бирж
    tickers = await fetcher.fetch_ticker_batch(symbol)
    
    if tickers and len(tickers) >= 2:
        logger.debug(f"[DEBUG] {symbol}: данные с {len(tickers)} бирж")
    
    if len(tickers) < 2:
        return None
    
    best_opportunity = None
    max_net_profit = 0
    
    # Проверяем все возможные комбинации бирж
    for buy_ticker in tickers:
        for sell_ticker in tickers:
            if buy_ticker['exchange'] == sell_ticker['exchange']:
                continue
            
            buy_price = buy_ticker['ask']
            sell_price = sell_ticker['bid']
            
            if sell_price <= buy_price:
                continue
            
            # Рассчитываем чистую прибыль с учетом комиссий
            net_profit_percent, net_profit, gross_profit = calculate_real_profit(
                buy_price, sell_price, 
                buy_ticker['exchange'], 
                sell_ticker['exchange']
            )
            
            # Проверяем минимальную чистую прибыль
            if net_profit_percent < MIN_NET_PROFIT_PERCENT:
                continue
            
            # Проверяем объемы
            if buy_ticker['quoteVolume'] < MIN_VOLUME_USDT or sell_ticker['quoteVolume'] < MIN_VOLUME_USDT:
                continue
            
            # Проверяем что цены не слишком низкие (мусорные токены)
            if buy_price < 0.000001 or sell_price < 0.000001:
                continue
            
            # Проверяем нереалистичную прибыль
            if net_profit_percent > 15.0:
                continue
            
            # Получаем комиссии для отображения
            buy_fee = FEES.get(buy_ticker['exchange'], {'taker': 0.001})['taker']
            sell_fee = FEES.get(sell_ticker['exchange'], {'taker': 0.001})['taker']
            
            opportunity = {
                'symbol': symbol,
                'buy_exchange': buy_ticker['exchange'],
                'buy_price': buy_price,
                'buy_fee_percent': buy_fee * 100,
                'sell_exchange': sell_ticker['exchange'],
                'sell_price': sell_price,
                'sell_fee_percent': sell_fee * 100,
                'buy_volume': buy_ticker['quoteVolume'],
                'sell_volume': sell_ticker['quoteVolume'],
                'gross_profit_percentage': gross_profit,
                'net_profit': net_profit,
                'net_profit_percentage': net_profit_percent,
                'quality': 0,
                'timestamp': datetime.now().isoformat()
            }
            
            # Рассчитываем качество
            opportunity['quality'] = calculate_profit_quality(opportunity)
            
            # Выбираем лучшую возможность для этой пары
            if net_profit_percent > max_net_profit:
                max_net_profit = net_profit_percent
                best_opportunity = opportunity
    
    return best_opportunity

async def check_arbitrage_opportunities(context: ContextTypes.DEFAULT_TYPE):
    """Основная функция сканирования арбитражных возможностей"""
    logger.info(f"🔍 Начинаю сканирование {SCAN_LIMIT} пар с учетом комиссий...")
    
    # Проверяем статус бирж
    online_exchanges = [ex for ex, status in EXCHANGE_STATUS.items() if status.get('online', False)]
    if len(online_exchanges) < 2:
        logger.warning(f"Мало бирж онлайн: {len(online_exchanges)}. Минимум требуется 2.")
        return
    
    # Получаем пары для сканирования
    symbols = get_usdt_symbols()
    logger.info(f"Получено USDT пар для сканирования: {len(symbols)}")
    
    # Создаем асинхронный загрузчик с контекстным менеджером
    async with AsyncExchangeFetcher() as fetcher:
        opportunities = []
        
        # Сканируем пары пакетами по 10 для лучшей производительности
        batch_size = 10
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i + batch_size]
            
            # Создаем задачи для пакета
            tasks = [check_arbitrage_for_pair(symbol, fetcher) for symbol in batch]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Обрабатываем результаты пакета
            for result in batch_results:
                if isinstance(result, Exception):
                    logger.error(f"Ошибка при сканировании партии: {result}")
                    continue
                    
                if result:
                    opportunities.append(result)
                    logger.info(f"Найдено: {result['symbol']} - {result['net_profit_percentage']:.2f}% чистой прибыли (качество: {result['quality']:.1f})")
            
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
    """Форматирует сообщение с арбитражными возможностями (с комиссиями)"""
    if not opportunities:
        return "🤷‍♂️ Арбитражных возможностей не найдено. Попробуйте позже."
    
    message = f"🏆 <b>ТОП АРБИТРАЖЕЙ (чистая прибыль >{MIN_NET_PROFIT_PERCENT}%):</b>\n\n"
    
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
            f"   📥 Купить: {opp['buy_exchange'].upper()} - ${opp['buy_price']:.8f} (комиссия: {opp['buy_fee_percent']:.2f}%)\n"
            f"   📤 Продать: {opp['sell_exchange'].upper()} - ${opp['sell_price']:.8f} (комиссия: {opp['sell_fee_percent']:.2f}%)\n"
            f"   📊 Объёмы: ${opp['buy_volume']:,.0f} / ${opp['sell_volume']:,.0f}\n"
            f"   💰 <b>Чистая прибыль: {opp['net_profit_percentage']:.2f}%</b>\n"
            f"   🎯 Качество: {opp['quality']:.1f}/100\n"
            f"   ⏱ {datetime.fromisoformat(opp['timestamp']).strftime('%H:%M:%S')}\n\n"
        )
    
    message += (
        f"<i>Настройки: чистая прибыль >{MIN_NET_PROFIT_PERCENT}%, "
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
    
    # Формируем информацию о биржах
    online_count = sum(1 for status in EXCHANGE_STATUS.values() if status.get('online', False))
    
    welcome_text = (
        f"🤖 <b>Arbitr Bot v5.0</b>\n\n"
        f"⚡ <b>Умный арбитражный сканер с учетом комиссий</b>\n\n"
        f"<b>Текущие настройки:</b>\n"
        f"• Чистая прибыль: >{MIN_NET_PROFIT_PERCENT}% (после комиссий)\n"
        f"• Объём: >{MIN_VOLUME_USDT} USDT\n"
        f"• Сканируемых пар: {SCAN_LIMIT}\n"
        f"• Интервал: {SCAN_INTERVAL} сек\n"
        f"• Бирж: {online_count}/{len(EXCHANGES)} онлайн\n\n"
        f"<b>Доступные команды:</b>\n"
        f"/scan - Ручное сканирование\n"
        f"/status - Статус и статистика\n"
        f"/help - Помощь и инструкции\n"
        f"/stop - Отключить уведомления\n"
        f"/settings - Текущие настройки\n"
        f"/fees - Показать комиссии бирж\n"
        f"/exchanges - Статус всех бирж\n\n"
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
    
    await update.message.reply_text(f"🔍 Сканирую {SCAN_LIMIT} пар с учетом комиссий...")
    
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
                "• Учет комиссий снижает реальную прибыль\n"
                "• Мало ликвидных пар\n"
                "• Все биржи синхронизированы\n\n"
                "Попробуйте позже или измените настройки."
            )
    except Exception as e:
        logger.error(f"Ошибка сканирования: {e}")
        await update.message.reply_text(f"❌ Ошибка: {str(e)[:200]}")

async def bot_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /status"""
    online_count = sum(1 for status in EXCHANGE_STATUS.values() if status.get('online', False))
    
    status_text = (
        f"📊 <b>СТАТУС БОТА v5.0</b>\n\n"
        f"<b>Пользователи:</b>\n"
        f"• Активных: {len(active_users)}\n"
        f"• Админов: {len(ADMIN_IDS)}\n\n"
        f"<b>Сканирование:</b>\n"
        f"• Бирж онлайн: {online_count}/{len(EXCHANGES)}\n"
        f"• Последнее сканирование: {arbitrage_cache.get('last_scan', 'ещё не было')}\n"
        f"• Найдено возможностей: {len(arbitrage_cache.get('opportunities', []))}\n"
        f"• В кэше: {len(arbitrage_cache.get('opportunities', []))} сигналов\n\n"
        f"<b>Производительность:</b>\n"
        f"• Макс. повторных попыток: {MAX_API_RETRIES}\n"
        f"• Таймаут запросов: {REQUEST_TIMEOUT}с\n"
        f"• Макс. конкурентных запросов: {MAX_CONCURRENT_REQUESTS}\n\n"
        f"Используйте /exchanges для детального статуса бирж"
    )
    
    await update.message.reply_text(status_text, parse_mode='HTML')

async def show_exchanges_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает детальный статус всех бирж"""
    status_text = "<b>📈 СТАТУС ВСЕХ БИРЖ</b>\n\n"
    
    for exchange_id, status in sorted(EXCHANGE_STATUS.items()):
        online_emoji = "✅" if status.get('online', False) else "❌"
        last_check = status.get('last_check', 'никогда')
        if isinstance(last_check, datetime):
            last_check = last_check.strftime('%H:%M:%S')
        
        errors = status.get('errors', 0)
        last_error = status.get('last_error', 'нет')
        
        status_text += (
            f"<b>{exchange_id.upper()}:</b> {online_emoji}\n"
            f"• Последняя проверка: {last_check}\n"
            f"• Ошибок: {errors}\n"
        )
        
        if last_error != 'нет' and errors > 0:
            status_text += f"• Последняя ошибка: {last_error[:50]}...\n"
        
        # Показываем комиссии
        if exchange_id in FEES:
            fee = FEES[exchange_id]
            status_text += f"• Комиссии: taker={fee['taker']*100:.2f}%, maker={fee['maker']*100:.2f}%\n"
        
        status_text += "\n"
    
    await update.message.reply_text(status_text, parse_mode='HTML')

async def show_fees(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает комиссии бирж"""
    fees_text = "<b>💰 КОМИССИИ БИРЖ (для арбитража)</b>\n\n"
    fees_text += "<i>При расчете прибыли учитываются taker-комиссии</i>\n\n"
    
    for exchange_id in sorted(FEES.keys()):
        fee = FEES[exchange_id]
        online_emoji = "✅" if EXCHANGE_STATUS.get(exchange_id, {}).get('online', False) else "❌"
        
        fees_text += f"<b>{exchange_id.upper()}:</b> {online_emoji}\n"
        fees_text += f"• Taker: {fee['taker']*100:.2f}% (покупка/продажа по рынку)\n"
        fees_text += f"• Maker: {fee['maker']*100:.2f}% (лимитные ордера)\n\n"
    
    fees_text += "📝 <i>Бот учитывает комиссии при расчете чистой прибыли</i>"
    
    await update.message.reply_text(fees_text, parse_mode='HTML')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help"""
    help_text = (
        f"📚 <b>ПОМОЩЬ ПО ARBITR BOT v5.0</b>\n\n"
        f"<b>Как это работает:</b>\n"
        f"Бот сканирует {len(EXCHANGES)} биржи каждые {SCAN_INTERVAL} секунд.\n"
        f"Ищет разницу в ценах на одинаковые торговые пары.\n"
        f"<b>Учитывает комиссии бирж</b> при расчете прибыли.\n"
        f"Показывает только возможности с чистой прибылью (> {MIN_NET_PROFIT_PERCENT}%).\n\n"
        f"<b>Основные команды:</b>\n"
        f"/start - Запустить бота и получать уведомления\n"
        f"/scan - Ручное сканирование (немедленно)\n"
        f"/status - Статус бота и бирж\n"
        f"/exchanges - Детальный статус всех бирж\n"
        f"/stop - Отключить автоматические уведомления\n"
        f"/help - Эта справка\n"
        f"/fees - Показать комиссии бирж\n\n"
        f"<b>Что такое 'качество' арбитража?</b>\n"
        f"• 🔥 70+ - Отличная возможность\n"
        f"• ⭐ 50-70 - Хорошая возможность\n"
        f"• 📊 <50 - Средняя возможность\n\n"
        f"<b>Отслеживаемые биржи:</b>\n"
    )
    
    for name in sorted(EXCHANGES.keys()):
        online_emoji = "✅" if EXCHANGE_STATUS.get(name, {}).get('online', False) else "❌"
        help_text += f"• {name.upper()} {online_emoji}\n"
    
    help_text += f"\n⚙️ <b>Текущие настройки:</b>\n"
    help_text += f"• Минимальная чистая прибыль: {MIN_NET_PROFIT_PERCENT}%\n"
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
    online_count = sum(1 for status in EXCHANGE_STATUS.values() if status.get('online', False))
    
    settings_text = (
        f"⚙️ <b>ТЕКУЩИЕ НАСТРОЙКИ v5.0</b>\n\n"
        f"<b>Основные параметры:</b>\n"
        f"MIN_NET_PROFIT_PERCENT = {MIN_NET_PROFIT_PERCENT}% (чистая прибыль после комиссий)\n"
        f"MIN_VOLUME_USDT = {MIN_VOLUME_USDT:,} USDT\n"
        f"SCAN_LIMIT = {SCAN_LIMIT} пар\n"
        f"SCAN_INTERVAL = {SCAN_INTERVAL} сек\n\n"
        f"<b>Производительность и обработка ошибок:</b>\n"
        f"MAX_CONCURRENT_REQUESTS = {MAX_CONCURRENT_REQUESTS}\n"
        f"MAX_API_RETRIES = {MAX_API_RETRIES}\n"
        f"INITIAL_RETRY_DELAY = {INITIAL_RETRY_DELAY}с\n"
        f"REQUEST_TIMEOUT = {REQUEST_TIMEOUT}с\n"
        f"Бирж подключено: {len(EXCHANGES)}\n"
        f"Бирж онлайн: {online_count}\n\n"
        f"<b>Подключенные биржи:</b>\n"
    )
    
    for name in sorted(EXCHANGES.keys()):
        online_emoji = "✅" if EXCHANGE_STATUS.get(name, {}).get('online', False) else "❌"
        settings_text += f"• {name} {online_emoji}\n"
    
    settings_text += (
        f"\n📝 <i>Для изменения параметров отредактируйте файл bot.py</i>\n"
        f"📍 <i>Строки 8-16 в начале файла</i>"
    )
    
    await update.message.reply_text(settings_text, parse_mode='HTML')

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок с улучшенной диагностикой"""
    error = context.error
    
    # Логируем разные типы ошибок по-разному
    if isinstance(error, (ccxt.NetworkError, aiohttp.ClientError, ConnectionError)):
        logger.error(f"Сетевая ошибка: {error}")
    elif isinstance(error, ccxt.ExchangeError):
        logger.error(f"Ошибка биржи: {error}")
    elif isinstance(error, asyncio.TimeoutError):
        logger.error(f"Таймаут операции: {error}")
    else:
        logger.error(f"Неожиданная ошибка: {error}", exc_info=True)
    
    # Отправляем пользователю понятное сообщение
    if update and update.effective_message:
        try:
            error_type = "сетевая ошибка" if isinstance(error, (ccxt.NetworkError, aiohttp.ClientError, ConnectionError)) else \
                        "ошибка биржи" if isinstance(error, ccxt.ExchangeError) else \
                        "таймаут" if isinstance(error, asyncio.TimeoutError) else "ошибка"
            
            await update.effective_message.reply_text(
                f"⚠️ Произошла {error_type}. Бот продолжает работу.\n"
                f"Детали: {str(error)[:100]}..."
            )
        except:
            pass

async def post_init(application):
    """Функция, выполняемая после инициализации"""
    online_count = sum(1 for status in EXCHANGE_STATUS.values() if status.get('online', False))
    logger.info(f"✅ Бот запущен! Бирж: {len(EXCHANGES)} (онлайн: {online_count})")
    
    # Логируем комиссии
    logger.info("📊 Комиссии бирж:")
    for ex in sorted(FEES.keys()):
        fee = FEES[ex]
        online_emoji = "✅" if EXCHANGE_STATUS.get(ex, {}).get('online', False) else "❌"
        logger.info(f"   {ex.upper()} {online_emoji}: taker={fee['taker']*100:.2f}%, maker={fee['maker']*100:.2f}%")

def main():
    """Основная функция запуска бота"""
    logger.info(f"🚀 Запуск Arbitr Bot v5.0 (Этапы 1,2,6)...")
    
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
        application.add_handler(CommandHandler("exchanges", show_exchanges_status))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("fees", show_fees))
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
        logger.error(f"💥 Критическая ошибка: {e}", exc_info=True)
        raise

if __name__ == '__main__':
    main()
