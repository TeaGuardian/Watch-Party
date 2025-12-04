import sys
import os
import socketio
from aiohttp import web
import logging

# Добавляем корень проекта в путь, чтобы видеть core
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_service.utils import (
    db_execute,
    get_user_volume_settings_sync,
    save_volume_setting_sync,
    check_room_access_sync
)

# Настройка логов
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("VoiceService")

# Создаем Socket.IO сервер (Async)
sio = socketio.AsyncServer(async_mode='aiohttp', cors_allowed_origins='*')
app = web.Application()
sio.attach(app)

# Хранилище состояний в памяти (SID -> Metadata)
# Структура: { sid: { 'user_id': 1, 'room_uuid': '...', 'mic_on': True, 'sound_on': True } }
CLIENTS = {}


@sio.event
async def connect(sid, environ):
    logger.info(f"Client connected: {sid}")


@sio.event
async def disconnect(sid):
    if sid in CLIENTS:
        client = CLIENTS[sid]
        room = client['room_uuid']
        user_id = client['user_id']

        # Уведомляем комнату, что юзер ушел из войса
        await sio.emit('voice_user_left', {'sid': sid, 'user_id': user_id}, room=room)

        del CLIENTS[sid]
    logger.info(f"Client disconnected: {sid}")


@sio.event
async def join_voice(sid, data):
    """
    Вход в голосовой канал.
    data: { 'room_uuid': '...', 'user_id': 123 }
    TODO: В продакшене тут нужна валидация токена/сессии.
    """
    room_uuid = data.get('room_uuid')
    user_id = data.get('user_id')

    if not room_uuid or not user_id:
        return

    # 1. Проверяем в БД, есть ли такая комната и включен ли войс
    has_voice = await db_execute(check_room_access_sync, room_uuid)
    if not has_voice:
        await sio.emit('error', {'msg': 'Voice chat disabled for this room'}, to=sid)
        return

    # 2. Джойним сокет в комнату
    sio.enter_room(sid, room_uuid)

    # 3. Сохраняем состояние
    CLIENTS[sid] = {
        'user_id': user_id,
        'room_uuid': room_uuid,
        'mic_on': True,
        'sound_on': True
    }

    # 4. Уведомляем других
    await sio.emit('voice_user_joined', {
        'sid': sid,
        'user_id': user_id,
        'mic_on': True
    }, room=room_uuid, skip_sid=sid)

    # 5. Отправляем вошедшему список текущих участников (чтобы отрисовать иконки)
    # Собираем список из CLIENTS для этой комнаты
    # (В реальном хайлоаде лучше брать из Redis)
    current_users = []
    for client_sid, meta in CLIENTS.items():
        if meta['room_uuid'] == room_uuid:
            current_users.append({
                'sid': client_sid,
                'user_id': meta['user_id'],
                'mic_on': meta['mic_on']
            })

    await sio.emit('voice_state_update', current_users, to=sid)

    # 6. Отправляем персональные настройки громкости
    settings = await db_execute(get_user_volume_settings_sync, user_id)
    await sio.emit('volume_config', settings, to=sid)

    logger.info(f"User {user_id} joined voice in room {room_uuid}")


@sio.event
async def audio_packet(sid, data):
    """
    Реле аудио данных.
    data: Binary Blob (Int16 PCM)
    """
    if sid not in CLIENTS: return
    client = CLIENTS[sid]

    # Если микрофон выключен сервером (бан) или клиентом (рассинхрон) - игнорим
    if not client['mic_on']: return

    room = client['room_uuid']
    user_id = client['user_id']

    # Рассылаем всем в комнате, КРОМЕ себя
    # Оптимизация: можно фильтровать тех, у кого sound_on=False,
    # но socketio.emit не умеет фильтровать список получателей внутри комнаты эффективно без циклов.
    # Для MVP шлем всем (broadcast), клиент сам отбросит если deafened.

    await sio.emit('audio_stream', {
        'sid': sid,
        'user_id': user_id,
        'data': data
    }, room=room, skip_sid=sid)


@sio.event
async def set_state(sid, data):
    """
    Изменение статуса (Mic/Headphones)
    data: { 'mic_on': bool, 'sound_on': bool }
    """
    if sid not in CLIENTS: return

    client = CLIENTS[sid]
    room = client['room_uuid']

    # Обновляем состояние
    if 'mic_on' in data: client['mic_on'] = data['mic_on']
    if 'sound_on' in data: client['sound_on'] = data['sound_on']

    # Уведомляем всех об изменении (чтобы обновить иконки)
    await sio.emit('mute_update', {
        'sid': sid,
        'user_id': client['user_id'],
        'mic_on': client['mic_on'],
        'sound_on': client['sound_on']
    }, room=room)


@sio.event
async def save_volume(sid, data):
    """
    Сохранение громкости
    data: { 'target_id': 123, 'volume': 50 }
    """
    if sid not in CLIENTS: return
    owner_id = CLIENTS[sid]['user_id']
    target_id = data.get('target_id')
    volume = data.get('volume')

    if target_id is None or volume is None: return

    # Сохраняем в БД асинхронно
    await db_execute(save_volume_setting_sync, owner_id, target_id, volume)
    logger.info(f"User {owner_id} set volume for {target_id} to {volume}%")


if __name__ == '__main__':
    web.run_app(app, port=8001)