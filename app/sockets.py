#/app/sockets.py
import os
import sys
import time
from datetime import datetime, date
from functools import wraps

from flask import session, request
from flask_socketio import emit, join_room, leave_room
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import AppConfig
from app import socketio
from core.models import User, Room, Video, db, RoomAccess, RoomBan, DailyWatchStat

ROOM_STATE = {}
ACTIVE_CONNECTIONS = {}
SID_MAP = {}
WATCH_SESSIONS = {}
WATCH_BUFFER = {}


def db_session(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        # Если соединение закрыто - открываем (берем из пула)
        if db.is_closed():
            db.connect()
        try:
            return f(*args, **kwargs)
        except Exception as e:
            print(f"Socket DB Error in {f.__name__}: {e}")
            raise e
        finally:
            # Всегда возвращаем соединение в пул после обработки события!
            if not db.is_closed():
                db.close()
    return wrapper


def flush_buffer_to_db(user_id):
    if user_id not in WATCH_BUFFER or WATCH_BUFFER[user_id] <= 0:
        return

    seconds_to_add = WATCH_BUFFER[user_id]
    today = date.today()

    try:
        # Пытаемся получить запись за сегодня
        stat, created = DailyWatchStat.get_or_create(
            user_id=user_id,
            date=today,
            defaults={'total_seconds': 0}
        )
        # Атомарное обновление (чтобы не было гонки потоков)
        query = DailyWatchStat.update(total_seconds=DailyWatchStat.total_seconds + seconds_to_add).where(
            DailyWatchStat.id == stat.id)
        query.execute()

        # Очищаем буфер
        print(f"📈 Analytics: Saved {seconds_to_add}s for user {user_id}")
        WATCH_BUFFER[user_id] = 0

    except Exception as e:
        print(f"Error flushing stats: {e}")


def track_connection_add(user_id, room_uuid, sid):
    """Пытается добавить соединение. Возвращает (Success, ErrorMsg)"""
    if user_id not in ACTIVE_CONNECTIONS:
        ACTIVE_CONNECTIONS[user_id] = {}

    user_rooms = ACTIVE_CONNECTIONS[user_id]

    # 1. Проверка на уникальные комнаты (если этой комнаты еще нет в списке)
    if room_uuid not in user_rooms:
        if len(user_rooms) >= AppConfig.MAX_OPENED_ROOMS:
            return False, "Limit of active rooms reached"
        # Создаем слот под новую комнату
        user_rooms[room_uuid] = set()

    # 2. Проверка на дубликаты (вкладок этой же комнаты)
    if len(user_rooms[room_uuid]) >= AppConfig.MAX_DUPLICATION:
        return False, "Limit of tabs for this room reached"

    # Всё ок - записываем
    user_rooms[room_uuid].add(sid)
    SID_MAP[sid] = (user_id, room_uuid)
    return True, None


def track_connection_remove(sid):
    """Удаляет соединение при разрыве"""
    if sid not in SID_MAP: return

    user_id, room_uuid = SID_MAP.pop(sid)

    if user_id in ACTIVE_CONNECTIONS:
        if room_uuid in ACTIVE_CONNECTIONS[user_id]:
            ACTIVE_CONNECTIONS[user_id][room_uuid].discard(sid)

            # Если вкладок не осталось - удаляем комнату из активных
            if not ACTIVE_CONNECTIONS[user_id][room_uuid]:
                del ACTIVE_CONNECTIONS[user_id][room_uuid]

        # Если комнат не осталось - удаляем юзера
        if not ACTIVE_CONNECTIONS[user_id]:
            del ACTIVE_CONNECTIONS[user_id]


# Хелпер для получения юзера из сессии
def get_current_user():
    user_id = session.get('user_id')
    if not user_id:
        return None
    return User.get_or_none(User.id == user_id)


@socketio.on('connect')
@db_session
def on_connect():
    user = get_current_user()
    if user:
        join_room(f"user_{user.id}")


@socketio.on('disconnect')
def on_disconnect():
    track_connection_remove(request.sid)


@socketio.on('join')
@db_session
def on_join(data):
    """Вход пользователя"""
    room_uuid = data.get('room_uuid')
    user = get_current_user()

    if not user or not room_uuid: return

    # --- ПРОВЕРКА БЕЗОПАСНОСТИ ---
    success, error_msg = track_connection_add(user.id, room_uuid, request.sid)
    if not success:
        # Отправляем ошибку и принудительно отключаем
        emit('error_limit', {'msg': error_msg, 'redirect': True}, to=request.sid)
        return
    # Нельзя просто так взять и сделать join в сокеты приватной комнаты
    try:
        room = Room.get(Room.uuid == room_uuid)
        if RoomBan.select().where((RoomBan.room == room) & (RoomBan.user == user)).exists():
            emit('error', {'msg': 'Вы забанены в этой комнате'}, to=request.sid)
            return  # Не делаем join_room
        if room.is_private and room.owner_id != user.id and user.role != 'admin':
            # Проверяем БД
            has_access = RoomAccess.select().where(
                (RoomAccess.room == room) & (RoomAccess.user == user)
            ).exists()

            if not has_access:
                emit('error', {'msg': 'Доступ запрещен'}, to=request.sid)
                return
    except:
        return
    # -----------------------------

    join_room(room_uuid)

    # 1. Уведомляем других
    emit('user_joined', {
        'username': user.username,
        'user_id': user.id,
        'sid': request.sid,
        'avatar': user.to_dict()['avatar_url']
    }, to=room_uuid)

    # 2. Отправляем вошедшему ТЕКУЩЕЕ состояние комнаты
    state = ROOM_STATE.get(room_uuid)
    if state:
        # Если видео выбрано, нужно получить его URL и Title
        video_data = None
        if state.get('video_id'):
            try:
                vid = Video.get_by_id(state['video_id'])
                d = vid.to_dict()
                if d['url']:
                    video_data = d
            except:
                pass

        # Отправляем персональное событие восстановления
        emit('restore_state', {
            'video': video_data,
            'timestamp': state.get('timestamp', 0),
            'paused': state.get('paused', True)
        })


@socketio.on('leave')
@db_session
def on_leave(data):
    room_uuid = data.get('room_uuid')
    user = get_current_user()

    if room_uuid and user:
        flush_buffer_to_db(user.id)
        if request.sid in WATCH_SESSIONS:
            del WATCH_SESSIONS[request.sid]

        leave_room(room_uuid)
        emit('user_left', {
            'user_id': user.id,
            'sid': request.sid
        }, to=room_uuid)


@socketio.on('sync_action')
@db_session
def on_sync_action(data):
    """Play/Pause/Seek"""
    user = get_current_user()
    room_uuid = data.get('room_uuid')
    if not user or not room_uuid: return

    try:
        room = Room.get(Room.uuid == room_uuid)
        is_owner = (room.owner_id == user.id)
        allow_guest = (room.is_private and room.allow_guest_control)

        if not is_owner and not allow_guest: return

        # Обновляем состояние в памяти
        if room_uuid not in ROOM_STATE: ROOM_STATE[room_uuid] = {}

        ROOM_STATE[room_uuid]['timestamp'] = data.get('timestamp')
        if data.get('action') == 'play':
            ROOM_STATE[room_uuid]['paused'] = False

            # [NEW] Обновляем время последнего просмотра у текущего видео
            current_video_id = ROOM_STATE[room_uuid].get('video_id')
            if current_video_id:
                try:
                    # Используем прямой SQL update для скорости, или через ORM
                    Video.update(last_played_at=datetime.now()).where(Video.id == current_video_id).execute()
                except:
                    pass

        elif data.get('action') == 'pause':
            ROOM_STATE[room_uuid]['paused'] = True
            if request.sid in WATCH_SESSIONS:
                if user: flush_buffer_to_db(user.id)
                del WATCH_SESSIONS[request.sid]

        # Рассылаем всем кроме себя
        emit('sync_event', {
            'action': data.get('action'),
            'timestamp': data.get('timestamp'),
            'actor': user.username
        }, to=room_uuid, include_self=False)


    except Exception as e:
        print(f"Sync error: {e}")


@socketio.on('heartbeat')
@db_session
def on_heartbeat(data):
    user = get_current_user()
    room_uuid = data.get('room_uuid')
    if not user or not room_uuid: return

    # Если хартбит шлет Владелец, мы верим ему больше всех и обновляем глобальное состояние
    # Это позволяет новым юзерам подключаться точно в момент, где сейчас владелец

    # --- ЛОГИКА ПОДСЧЕТА ВРЕМЕНИ ---
    client_state = data.get('state')
    sid = request.sid
    now = time.time()

    if client_state == 'playing':
        if sid not in WATCH_SESSIONS:
            # Начало новой сессии просмотра
            WATCH_SESSIONS[sid] = {
                'start_ts': now,  # Когда начали смотреть (для проверки 10 сек)
                'last_ts': now  # Время последнего хартбита
            }
        else:
            session_data = WATCH_SESSIONS[sid]
            # Проверяем, смотрим ли мы уже более 10 секунд непрерывно
            total_duration = now - session_data['start_ts']

            if total_duration > 10:
                # Считаем дельту с прошлого хартбита
                delta = now - session_data['last_ts']

                # Фильтр аномалий (если лаг сети и дельта огромная, например > 10 сек, игнорим)
                if 0 < delta < 10:
                    # Добавляем в буфер пользователя
                    if user.id not in WATCH_BUFFER: WATCH_BUFFER[user.id] = 0
                    WATCH_BUFFER[user.id] += int(delta)  # Округляем до секунд

                    # Если накопилось больше 60 секунд (1 минута) -> сбрасываем в БД
                    if WATCH_BUFFER[user.id] >= 60:
                        flush_buffer_to_db(user.id)

            # Обновляем метку времени
            session_data['last_ts'] = now

    else:
        # Если статус не playing (paused), удаляем сессию
        if sid in WATCH_SESSIONS:
            flush_buffer_to_db(user.id)  # Сохраняем остатки
            del WATCH_SESSIONS[sid]
    # -------------------------------------

    try:
        room = Room.get(Room.uuid == room_uuid)
        if room.owner_id == user.id:
            if room_uuid not in ROOM_STATE: ROOM_STATE[room_uuid] = {}
            ROOM_STATE[room_uuid]['timestamp'] = data.get('timestamp')
            ROOM_STATE[room_uuid]['paused'] = (data.get('state') == 'paused')
    except:
        pass

    # Рассылка статуса для UI
    emit('status_update', {
        'user_id': user.id,
        'username': user.username,
        'sid': request.sid,
        'timestamp': data.get('timestamp'),
        'state': data.get('state'),
        'avatar': user.to_dict()['avatar_url']
    }, to=room_uuid)


@socketio.on('chat_message')
@db_session
def on_chat_message(data):
    """Простой чат"""
    user = get_current_user()
    room_uuid = data.get('room_uuid')
    message = data.get('message', '').strip()

    if user and room_uuid and message:
        # Здесь можно сохранить в БД (если нужна история)
        # Message.create(...)

        emit('new_message', {
            'user_id': user.id,
            'username': user.username,
            'text': message,
            'time': 'now'  # JS подставит локальное время
        }, to=room_uuid)


@socketio.on('change_video')
@db_session
def on_change_video(data):
    """Смена видео"""
    user = get_current_user()
    room_uuid = data.get('room_uuid')
    video_id = data.get('video_id')

    if not user or not room_uuid: return

    try:
        room = Room.get(Room.uuid == room_uuid)
        if room.owner_id != user.id and not room.allow_guest_control:
            return

        video = Video.get_by_id(video_id)
        video.last_played_at = datetime.now()
        video.save()

        v_data = video.to_dict()

        if not v_data['url']:
            emit('error', {'msg': 'Видео не готово'}, to=request.sid)
            return

        # Обновляем состояние
        if room_uuid not in ROOM_STATE: ROOM_STATE[room_uuid] = {}
        ROOM_STATE[room_uuid]['video_id'] = video.id
        ROOM_STATE[room_uuid]['timestamp'] = 0
        ROOM_STATE[room_uuid]['paused'] = True

        emit('load_video', {
            'url': v_data['url'],
            'title': v_data['title'],
            'video_id': video.id
        }, to=room_uuid)

    except Exception as e:
        print(f"Change video error: {e}")


@socketio.on('knock_knock')
@db_session
def on_knock(data):
    """Гость просит впустить его"""
    room_uuid = data.get('room_uuid')
    user = get_current_user()

    if not user or not room_uuid: return

    try:
        room = Room.get(Room.uuid == room_uuid)
        # Отправляем уведомление Владельцу.
        # Владелец находится в своей комнате user_{owner_id}, если он онлайн.

        print(f"User {user.username} knocking to room {room.name}")

        emit('incoming_knock', {
            'user_id': user.id,
            'username': user.username,
            'room_uuid': room_uuid,
            'avatar': user.to_dict()['avatar_url']
        }, to=f"user_{room.owner_id}")

    except Exception as e:
        print(f"Knock error: {e}")


@socketio.on('decide_knock')
@db_session
def on_decide_knock(data):
    """Владелец принял решение"""
    owner = get_current_user()
    target_user_id = data.get('target_user_id')
    room_uuid = data.get('room_uuid')
    decision = data.get('decision')  # 'approve' or 'reject'

    if not owner or not target_user_id: return

    try:
        room = Room.get(Room.uuid == room_uuid)
        if room.owner_id != owner.id: return  # Только владелец решает

        if decision == 'approve':
            # 1. Пишем в БД
            target_user = User.get_by_id(target_user_id)
            RoomAccess.get_or_create(room=room, user=target_user)

            # 2. Уведомляем гостя
            emit('access_granted', {
                'room_uuid': room_uuid
            }, to=f"user_{target_user_id}")

        else:
            emit('access_denied', {
                'reason': 'Владелец отклонил запрос'
            }, to=f"user_{target_user_id}")

    except Exception as e:
        print(f"Decide error: {e}")