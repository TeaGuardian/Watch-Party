import asyncio
import functools
from core.models import UserVolumeSettings, User, Room

# Запускаем блокирующие DB операции в отдельном потоке
async def db_execute(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))

# --- Синхронные функции (будут вызываться через db_execute) ---

def get_user_volume_settings_sync(owner_id):
    """Получить все настройки громкости для пользователя"""
    settings = UserVolumeSettings.select().where(UserVolumeSettings.owner_id == owner_id)
    return [{'target_id': s.target_id, 'volume': s.volume} for s in settings]

def save_volume_setting_sync(owner_id, target_id, volume):
    """Сохранить или обновить настройку"""
    # upsert (insert or update)
    obj, created = UserVolumeSettings.get_or_create(
        owner_id=owner_id,
        target_id=target_id,
        defaults={'volume': volume}
    )
    if not created:
        obj.volume = volume
        obj.save()
    return True

def check_room_access_sync(room_uuid):
    """Проверить, существует ли комната и включен ли там войс"""
    try:
        room = Room.get(Room.uuid == room_uuid)
        return room.has_voice_chat
    except Room.DoesNotExist:
        return False