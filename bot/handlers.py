#/bot/handlers.py
import logging
from aiogram import Router, types, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import BotConfig
from core.models import User
from core.validators import validate_password_strength

router = Router()
logger = logging.getLogger("BotHandlers")


# --- FSM STATES ---
class ResetPasswordState(StatesGroup):
    waiting_for_password = State()


# --- HELP ---
@router.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "🤖 <b>Доступные команды:</b>\n\n"
        "/start [код] - Привязать аккаунт\n"
        "/reset_password - Сбросить пароль от сайта\n"
        "/unlink - Отвязать Telegram от аккаунта"
    )


@router.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject):
    """
    Обработка команды /start <code>
    Только привязка аккаунта.
    """
    code = command.args
    tg_id = message.from_user.id
    tg_username = message.from_user.username or "NoUsername"

    # 1. Если просто /start без кода
    if not code:
        await message.answer(
            "👋 Привет!\n"
            "Этот бот служит только для привязки аккаунта к сервису совместного просмотра.\n"
            "Перейдите по ссылке в вашем профиле на сайте.\n\n"
            "/help - список команд"
        )
        return

    try:
        # 2. Ищем пользователя по коду верификации
        user = User.get_or_none(User.verification_code == code)

        if not user:
            await message.answer("⛔ Код неверный или устарел. Попробуйте сгенерировать новый на сайте.")
            return

        # Проверка: не привязан ли уже кто-то к этому юзеру (хотя код одноразовый, но для надежности)
        if user.tg_id:
            await message.answer("✅ Этот аккаунт на сайте уже привязан к Telegram.")
            return

        # Проверка: не пытаемся ли мы привязать ТГ, который уже есть у другого юзера
        if User.select().where(User.tg_id == tg_id).exists():
            await message.answer("⛔ Этот Telegram аккаунт уже используется другим пользователем.")
            return

        # 3. Привязываем Telegram
        user.tg_id = tg_id
        user.tg_username = tg_username
        user.verification_code = None  # Сбрасываем код (он одноразовый)

        # --- ЛОГИКА СУПЕР-АДМИНА ---
        # Если ID пользователя прописан в конфиге (hardcoded admin),
        # мы сразу даем ему права админа и доступ. Это нужно, чтобы первый админ мог зайти в панель.
        if tg_id in BotConfig.ADMIN_IDS:
            user.role = 'admin'
            user.status = 'approved'
            # Админ подтверждает сам себя фактом владения "золотым" Telegram ID
            user.approved_by = user
            user.save()

            await message.answer(
                f"✅ <b>Аккаунт успешно привязан!</b>\n"
                f"Система опознала вас как Администратора. Вам выдан полный доступ."
            )
            logger.info(f"SuperAdmin linked & promoted: {user.username} (TG: {tg_id})")

        else:
            # Обычный пользователь
            # Если юзер был 'new', переводим в 'tg_verified'.
            # Если он уже был 'approved' (мало ли), статус не трогаем.
            if user.status == 'new':
                user.status = 'tg_verified'

            user.save()

            await message.answer(
                "✅ <b>Telegram успешно привязан!</b>\n"
                "Теперь ваш аккаунт ожидает подтверждения администратором на сайте.\n"
                "Как только доступ будет открыт, вы сможете пользоваться сервисом."
            )
            logger.info(f"User linked TG: {user.username} (TG: {tg_id})")

    except Exception as e:
        logger.error(f"Error in cmd_start: {e}")
        await message.answer("Произошла внутренняя ошибка сервиса.")


# --- PASSWORD RESET ---
@router.message(Command("reset_password"))
async def cmd_reset_password(message: types.Message, state: FSMContext):
    tg_id = message.from_user.id

    # Ищем юзера по TG ID
    user = User.get_or_none(User.tg_id == tg_id)

    if not user:
        await message.answer("⛔ Ваш Telegram не привязан ни к одному аккаунту.")
        return

    await message.answer(
        "⚠️ <b>Внимание!</b>\n"
        "Смена пароля сбросит ваш статус доступа. Вам придется снова ждать одобрения администратора.\n\n"
        "Введите новый пароль:"
    )
    # Устанавливаем состояние ожидания
    await state.set_state(ResetPasswordState.waiting_for_password)


@router.message(ResetPasswordState.waiting_for_password)
async def process_new_password(message: types.Message, state: FSMContext):
    new_password = message.text.strip()
    tg_id = message.from_user.id

    # Валидация
    is_valid, err_msg = validate_password_strength(new_password)
    if not is_valid:
        await message.answer(f"❌ {err_msg}\nПопробуйте еще раз:")
        return  # Не сбрасываем состояние, ждем ввод снова

    try:
        user = User.get(User.tg_id == tg_id)
        user.set_password(new_password)

        # СБРОС СТАТУСА (если он не админ)
        if user.role != 'admin':
            user.status = 'tg_verified'  # Откатываемся на шаг "проверен ТГ, но не админом"
            user.approved_by = None

        user.save()

        await message.answer(
            "✅ <b>Пароль успешно изменен!</b>\n"
            "Вы можете войти на сайт с новым паролем.\n"
            "Ваш статус изменен на 'Ожидает одобрения'."
        )
    except Exception as e:
        logger.error(f"Reset pwd error: {e}")
        await message.answer("Ошибка при сохранении.")

    # Сбрасываем состояние
    await state.clear()


# --- UNLINK (Отвязка) ---
@router.message(Command("unlink"))
async def cmd_unlink(message: types.Message):
    tg_id = message.from_user.id
    user = User.get_or_none(User.tg_id == tg_id)

    if not user:
        await message.answer("⛔ Аккаунт не найден.")
        return

    # Отвязываем
    user.tg_id = None
    user.tg_username = None

    # СБРОС СТАТУСА
    if user.role != 'admin':
        user.status = 'new'  # Теперь он вообще как новенький
        user.approved_by = None

    user.save()

    await message.answer(
        "✅ <b>Telegram отвязан.</b>\n"
        "Вы больше не сможете использовать функции бота для этого аккаунта, пока не привяжете его снова."
    )