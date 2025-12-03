# /run_bot.py
import asyncio
import logging
import sys
import os

# Добавляем текущую директорию в путь, чтобы видеть пакеты app, bot, core
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from bot import start_bot_async
from core.models import create_tables

# Настройка логирования
logging.basicConfig(level=logging.INFO)

if __name__ == "__main__":
    print("--- Starting Telegram Bot Service ---")

    # Инициализируем БД (на случай, если бот стартует первым)
    create_tables()

    # Запускаем асинхронный цикл
    try:
        asyncio.run(start_bot_async())
    except KeyboardInterrupt:
        print("Bot stopped")
    except Exception as e:
        print(f"Bot crashed: {e}")