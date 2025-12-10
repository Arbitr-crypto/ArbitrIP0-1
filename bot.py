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
        + cmp_text +
        f"\nОбъёмы (approx USD): buy={v_buy:.2f}, sell={v_sell:.2f}"
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
async def background_loop(app):
    """Фоновая задача для периодического сканирования"""
    while True:
        try:
            logger.info("Background scan start")
            await scanner_once(app)
            logger.info("Background scan finished")
        except Exception as e:
            logger.exception(f"Error in background scan: {e}")
        await asyncio.sleep(CHECK_INTERVAL)


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

    # Запускаем фоновую задачу
    asyncio.create_task(background_loop(app))

    logger.info("Bot running...")
    await app.run_polling()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.exception(f"Fatal error: {e}")

