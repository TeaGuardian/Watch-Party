#/bot/__init__.py
import asyncio
import logging
from .loader import bot, dp
from .handlers import router


async def start_bot_async():
    """Асинхронная функция запуска поллинга"""
    # Регистрируем роутеры
    dp.include_router(router)

    # Удаляем вебхуки, если были, и стартуем
    await bot.delete_webhook(drop_pending_updates=True)
    print("🤖 Bot started polling...")
    try:
        await dp.start_polling(bot)
    except Exception as e:
        print(f"Bot stopped with error: {e}")


def start_bot_in_thread():
    """Обертка для запуска в отдельном потоке (для main.py)"""
    # Создаем новый event loop для потока
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(start_bot_async())