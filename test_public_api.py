# test_public_api.py - диагностика публичного доступа к биржам
import ccxt
import asyncio

async def test_public_access():
    print("🔧 Тестирование публичного доступа к биржам\n")
    
    # Конфигурация бирж (только публичный доступ)
    exchanges = {
        'kucoin': ccxt.kucoin({'enableRateLimit': True}),
        'bitrue': ccxt.bitrue({'enableRateLimit': True}),
        'bitmart': ccxt.bitmart({'enableRateLimit': True}),
        'gateio': ccxt.gateio({'enableRateLimit': True}),
        'poloniex': ccxt.poloniex({'enableRateLimit': True}),
    }
    
    results = {}
    
    for name, exchange in exchanges.items():
        try:
            print(f"Тестируем {name}...")
            
            # 1. Пробуем получить тикер BTC/USDT
            ticker = exchange.fetch_ticker('BTC/USDT')
            
            # 2. Пробуем получить список торговых пар
            markets = exchange.load_markets()
            
            results[name] = {
                'status': '✅ РАБОТАЕТ',
                'btc_price': ticker['last'] if ticker['last'] else 'Нет данных',
                'pairs_count': len(markets),
                'error': None
            }
            
            print(f"  {results[name]['status']}")
            print(f"  BTC цена: ${results[name]['btc_price']}")
            print(f"  Пар доступно: {results[name]['pairs_count']}")
            
        except Exception as e:
            results[name] = {
                'status': '❌ ОШИБКА',
                'btc_price': 'Нет данных',
                'pairs_count': 0,
                'error': str(e)[:100]
            }
            print(f"  {results[name]['status']}: {results[name]['error']}")
        
        print()
    
    # Итоговая таблица
    print("\n📋 ИТОГОВЫЙ ОТЧЁТ:")
    print("-" * 60)
    print(f"{'Биржа':<10} {'Статус':<12} {'BTC цена':<15} {'Пар':<10} {'Ошибка'}")
    print("-" * 60)
    
    for name, data in results.items():
        print(f"{name:<10} {data['status']:<12} ${str(data['btc_price']):<14} {data['pairs_count']:<10} {data['error'] or ''}")
    
    print("-" * 60)
    
    # Рекомендации
    working = sum(1 for data in results.values() if data['status'] == '✅ РАБОТАЕТ')
    print(f"\n✅ Работает бирж: {working}/{len(exchanges)}")
    
    if working < len(exchanges):
        print("\n⚠️ Рекомендации по ошибкам:")
        for name, data in results.items():
            if data['status'] == '❌ ОШИБКА':
                if "cloudflare" in data['error'].lower() or "403" in data['error']:
                    print(f"  • {name}: Возможна блокировка Cloudflare. Попробуйте позже.")
                elif "429" in data['error']:
                    print(f"  • {name}: Слишком много запросов. Увеличьте 'rateLimit' в настройках.")
                else:
                    print(f"  • {name}: {data['error']}")

if __name__ == '__main__':
    asyncio.run(test_public_access())
