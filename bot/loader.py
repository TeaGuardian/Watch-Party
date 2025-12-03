#/bot/loader.py
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import BotConfig

# Инициализация бота с HTML парсингом по умолчанию
bot = Bot(
    token=BotConfig.TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

# Хранилище состояний (в памяти)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)