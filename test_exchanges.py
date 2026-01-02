import os
import ccxt
from dotenv import load_dotenv

load_dotenv()

def test_exchange(name, config):
    try:
        print(f"\n🔍 Тестируем {name}...")
        exchange = ccxt.__dict__[name](config)
        # Простая проверка - получаем тикер BTC/USDT
        ticker = exchange.fetch_ticker('BTC/USDT')
        print(f"✅ {name} работает! BTC цена: ${ticker['last']}")
        return True
    except Exception as e:
        print(f"❌ {name} ошибка: {type(e).__name__}: {str(e)[:200]}")
        return False

# Тестируем Bybit
test_exchange('bybit', {
    'apiKey': os.getenv('BYBIT_API_KEY', ''),
    'secret': os.getenv('BYBIT_SECRET', ''),
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'}
})

# Тестируем HTX
test_exchange('huobi', {
    'apiKey': os.getenv('HTX_API_KEY', ''),
    'secret': os.getenv('HTX_SECRET', ''),
    'enableRateLimit': True,
})
