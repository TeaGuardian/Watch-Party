# /app/sockets.py
# /app/sockets.py
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
SYNC_METRICS = {}

ACTIVE_CONNECTIONS = {}
SID_TIMINGS = {}
SID_MAP = {}

# Для аналитики (оставляем как было)
WATCH_SESSIONS = {}
WATCH_BUFFER = {}


def db_session(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if db.is_closed():
            db.connect()
        try:
            return f(*args, **kwargs)
        except Exception as e:
            print(f"Socket DB Error in {f.__name__}: {e}")
            raise e
        finally:
            if not db.is_closed():
                db.close()

    return wrapper


def flush_buffer_to_db(user_id):
    """Сбрасывает накопленные секунды просмотра в БД"""
    if user_id not in WATCH_BUFFER or WATCH_BUFFER[user_id] <= 0:
        return

    seconds_to_add = WATCH_BUFFER[user_id]
    today = date.today()

    try:
        stat, created = DailyWatchStat.get_or_create(
            user_id=user_id,
            date=today,
            defaults={'total_seconds': 0}
        )
        query = DailyWatchStat.update(total_seconds=DailyWatchStat.total_seconds + seconds_to_add).where(
            DailyWatchStat.id == stat.id)
        query.execute()

        # print(f"📈 Analytics: Saved {seconds_to_add}s for user {user_id}")
        WATCH_BUFFER[user_id] = 0

    except Exception as e:
        print(f"Error flushing stats: {e}")


def track_connection_add(user_id, room_uuid, sid):
    """Пытается добавить соединение"""
    if user_id not in ACTIVE_CONNECTIONS:
        ACTIVE_CONNECTIONS[user_id] = {}

    user_rooms = ACTIVE_CONNECTIONS[user_id]

    if room_uuid not in user_rooms:
        if len(user_rooms) >= AppConfig.MAX_OPENED_ROOMS:
            return False, "Limit of active rooms reached"
        user_rooms[room_uuid] = set()

    if len(user_rooms[room_uuid]) >= AppConfig.MAX_DUPLICATION:
        return False, "Limit of tabs for this room reached"

    user_rooms[room_uuid].add(sid)
    SID_MAP[sid] = (user_id, room_uuid)
    return True, None


def track_connection_remove(sid):
    """Удаляет соединение при разрыве"""
    # Удаляем тайминг входа
    if sid in SID_TIMINGS:
        del SID_TIMINGS[sid]

    if sid not in SID_MAP: return

    user_id, room_uuid = SID_MAP.pop(sid)

    if user_id in ACTIVE_CONNECTIONS:
        if room_uuid in ACTIVE_CONNECTIONS[user_id]:
            ACTIVE_CONNECTIONS[user_id][room_uuid].discard(sid)
            if not ACTIVE_CONNECTIONS[user_id][room_uuid]:
                del ACTIVE_CONNECTIONS[user_id][room_uuid]

        if not ACTIVE_CONNECTIONS[user_id]:
            del ACTIVE_CONNECTIONS[user_id]


def get_room_state_default(room_uuid):
    if room_uuid not in ROOM_STATE:
        ROOM_STATE[room_uuid] = {
            'video_id': None,
            'timestamp': 0.0,
            'paused': True,
            'last_update': time.time(),
            'leader_sid': None,
            'owner_last_seen': 0.0
        }
    return ROOM_STATE[room_uuid]


def elect_leader(room_uuid, room_obj):
    """
    Алгоритм выбора источника синхронизации (Лидера).
    Вызывается при каждом хартбите.
    """
    state = ROOM_STATE[room_uuid]
    now = time.time()

    # 1. Получаем список всех SID в комнате
    try:
        # socketio.server.manager.rooms returns { namespace: { room: { sid, ... } } }
        # Но в новой версии python-socketio доступ может отличаться, используем безопасный метод:
        room_participants = socketio.server.manager.rooms.get('/', {}).get(room_uuid, set())
    except:
        room_participants = set()

    if not room_participants:
        state['leader_sid'] = None
        return

    # 2. Фильтрация кандидатов
    candidates = []

    owner_online = (now - state.get('owner_last_seen', 0)) < 10.0  # Владелец был тут менее 10 сек назад

    # Если "Управление гостями запрещено" -> Синхронимся ТОЛЬКО по владельцу
    # (или админу, если владелец вышел, но тут для простоты - по владельцу)
    strict_mode = not room_obj.allow_guest_control and not room_obj.owner_id is None  # owner_id check just in case

    if strict_mode:
        if not owner_online:
            # Владелец ушел более чем на 10 сек -> СТОП
            if not state['paused']:
                state['paused'] = True
                socketio.emit('sync_event', {'action': 'pause', 'timestamp': state['timestamp']}, to=room_uuid)
            state['leader_sid'] = None
            return

        # Кандидаты - только сессии владельца
        for sid in room_participants:
            info = SID_MAP.get(sid)
            if info and info[0] == room_obj.owner_id:  # info[0] is user_id
                metrics = SYNC_METRICS.get(sid, {'streak': 0})
                candidates.append({'sid': sid, 'score': metrics.get('streak', 0)})

    else:
        # Свободный режим: кандидаты все, кто стабилен
        for sid in room_participants:
            metrics = SYNC_METRICS.get(sid)
            if not metrics: continue

            # Пропускаем тех, кто давно не слал хартбит (более 5 сек - лаг или обрыв)
            if now - metrics['last_seen'] > 5.0:
                continue

            # Критерий: смотрит > 20 сек (10 хартбитов по 2 сек) стабильно
            score = metrics['streak']
            user_id = SID_MAP.get(sid, (0, 0))[0]
            is_owner = (user_id == room_obj.owner_id)

            # Владелец получает бонус к скору, чтобы при прочих равных он был главным
            if is_owner:
                score += 50

            candidates.append({'sid': sid, 'score': score})

    # 3. Выбор победителя
    if not candidates:
        if not state['paused']:
           state['paused'] = True
           socketio.emit('sync_event', {'action': 'pause', 'timestamp': state['timestamp']}, to=room_uuid)
        return  # Оставляем старого лидера или None

    # Сортируем по скору убыванию
    candidates.sort(key=lambda x: x['score'], reverse=True)
    best_candidate = candidates[0]

    # Гистерезис: меняем лидера, только если новый кандидат сильно лучше старого (или старый умер)
    current_leader = state.get('leader_sid')

    # Если текущий лидер все еще в списке кандидатов и его скор неплох - оставляем его
    # (чтобы не прыгало между двумя зрителями с хорошим инетом)
    if current_leader:
        current_leader_stats = next((c for c in candidates if c['sid'] == current_leader), None)
        if current_leader_stats and current_leader_stats['score'] >= (best_candidate['score'] - 10):
            return  # Оставляем старого

    # Назначаем нового
    if state['leader_sid'] != best_candidate['sid']:
        state['leader_sid'] = best_candidate['sid']


def get_current_user():
    user_id = session.get('user_id')
    if not user_id:
        return None
    return User.get_or_none(User.id == user_id)


@socketio.on('connect')
@db_session
def on_connect(*args, **kwargs):
    user = get_current_user()
    if user:
        join_room(f"user_{user.id}")


@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    if sid in SYNC_METRICS:
        del SYNC_METRICS[sid]
    track_connection_remove(sid)


@socketio.on('join')
@db_session
def on_join(data):
    """Вход пользователя"""
    room_uuid = data.get('room_uuid')
    user = get_current_user()

    if not user or not room_uuid: return

    success, error_msg = track_connection_add(user.id, room_uuid, request.sid)
    if not success:
        emit('error_limit', {'msg': error_msg, 'redirect': True}, to=request.sid)
        return

    try:
        room = Room.get(Room.uuid == room_uuid)
        if RoomBan.select().where((RoomBan.room == room) & (RoomBan.user == user)).exists():
            emit('error', {'msg': 'Вы забанены в этой комнате'}, to=request.sid)
            return
        if room.is_private and room.owner_id != user.id and user.role != 'admin':
            has_access = RoomAccess.select().where(
                (RoomAccess.room == room) & (RoomAccess.user == user)
            ).exists()
            if not has_access:
                emit('error', {'msg': 'Доступ запрещен'}, to=request.sid)
                return
    except:
        return

    # Запоминаем время входа для алгоритма доверия
    SID_TIMINGS[request.sid] = datetime.now()

    join_room(room_uuid)

    emit('user_joined', {
        'username': user.username,
        'user_id': user.id,
        'sid': request.sid,
        'avatar': user.to_dict()['avatar_url']
    }, to=room_uuid)

    state = ROOM_STATE.get(room_uuid)
    if state:
        video_data = None
        if state.get('video_id'):
            try:
                vid = Video.get_by_id(state['video_id'])
                d = vid.to_dict()
                if d['url']:
                    video_data = d
            except:
                pass

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
        # Аналитика: сохраняем при выходе
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
    """
    Ручные действия (Play/Pause/Seek).
    Они форсированно меняют стейт и делают отправителя временным лидером.
    """
    user = get_current_user()
    room_uuid = data.get('room_uuid')
    action = data.get('action')
    val = data.get('timestamp')

    if not user or not room_uuid: return

    try:
        room = Room.get(Room.uuid == room_uuid)

        # Проверка прав (как и раньше)
        is_owner = (room.owner_id == user.id)
        if not is_owner and not room.allow_guest_control:
            return

        state = get_room_state_default(room_uuid)
        now = time.time()

        # Принудительно обновляем стейт
        state['timestamp'] = float(val)
        state['last_update'] = now

        # При ручном действии этот юзер становится лидером (временный захват)
        # Это предотвращает "борьбу" с текущим лидером
        state['leader_sid'] = request.sid
        if request.sid in SYNC_METRICS:
            # Даем бонус стрика, чтобы он удержал лидерство какое-то время
            SYNC_METRICS[request.sid]['streak'] += 20

        if action == 'play':
            state['paused'] = False
            # Обновляем last_played_at в БД
            if state.get('video_id'):
                try:
                    Video.update(last_played_at=datetime.now()).where(Video.id == state['video_id']).execute()
                except:
                    pass
        elif action == 'pause':
            state['paused'] = True
            # Сброс аналитики
            if request.sid in WATCH_SESSIONS:
                flush_buffer_to_db(user.id)
                del WATCH_SESSIONS[request.sid]
        elif action == 'seek':
            # При сике паузу не меняем, просто время
            pass

        # Рассылаем всем
        emit('sync_event', {
            'action': action,
            'timestamp': val,
            'actor': user.username
        }, to=room_uuid, include_self=False)

    except Exception as e:
        print(f"Sync Action Error: {e}")

@socketio.on('heartbeat')
@db_session
def on_heartbeat(data):
    user = get_current_user()
    room_uuid = data.get('room_uuid')
    client_ts = float(data.get('timestamp', 0))
    client_state = data.get('state')

    if not user or not room_uuid: return

    sid = request.sid
    now = time.time()

    state = get_room_state_default(room_uuid)

    # 1. Обновляем метрики владельца (глобально для комнаты)
    try:
        room = Room.get(Room.uuid == room_uuid)
    except:
        return

    if user.id == room.owner_id:
        state['owner_last_seen'] = now

    # 2. Обновляем личные метрики сокета
    if sid not in SYNC_METRICS:
        SYNC_METRICS[sid] = {'streak': 0, 'last_seen': now, 'last_diff': 0}

    metric = SYNC_METRICS[sid]
    metric['last_seen'] = now

    # Анализ стабильности (только если видео играет)
    if client_state == 'playing' and not state['paused']:
        # Разница между временем клиента и временем сервера (интерполированным)
        # Время сервера двигается, поэтому сравниваем аккуратно

        # Ожидаемое время на сервере: last_known_ts + (now - last_update)
        # Но для оценки стабильности лучше сравнивать с ПРЕДЫДУЩИМ значением лидера, если он есть

        # Упростим: Стабильность = клиент не скачет по времени.
        # Если разница с серверным временем < 2 сек -> +1 к стрику
        server_estimated_time = state['timestamp']
        if not state['paused']:
            server_estimated_time += (now - state['last_update'])

        diff = abs(client_ts - server_estimated_time)

        if diff < 2.0:
            # Бонус за длительный просмотр (кап в 600 поинтов = 20 минут)
            metric['streak'] = min(metric['streak'] + 1, 600)
        else:
            # Сброс стрика при рассинхроне/сике
            metric['streak'] = 0

    else:
        # На паузе стрик не растет, но и не сбрасывается мгновенно (можно чуть уменьшать)
        pass

    # 3. Запускаем выборы лидера
    elect_leader(room_uuid, room)

    # 4. Если ЭТОТ клиент - Лидер, обновляем глобальное состояние комнаты
    if state['leader_sid'] == sid:
        # Лидер диктует правду
        state['timestamp'] = client_ts
        state['last_update'] = now

        # Лидер диктует паузу (но не буферизацию - при буферизации лидера время просто стопается)
        if client_state == 'paused':
            state['paused'] = True
        elif client_state == 'playing':
            state['paused'] = False

        # Если лидер буферится, мы не ставим глобальную паузу (чтобы другие не встали),
        # но и таймстемп не двигаем.

    # 5. Аналитика (Watch Time) - старый код
    if client_state == 'playing':
        if sid not in WATCH_SESSIONS:
            WATCH_SESSIONS[sid] = {'start_ts': now, 'last_ts': now}
        else:
            s_data = WATCH_SESSIONS[sid]
            if (now - s_data['start_ts']) > 10:  # >10 сек непрерывно
                delta = now - s_data['last_ts']
                if 0 < delta < 10:
                    if user.id not in WATCH_BUFFER: WATCH_BUFFER[user.id] = 0
                    WATCH_BUFFER[user.id] += int(delta)
                    if WATCH_BUFFER[user.id] >= 60: flush_buffer_to_db(user.id)
            s_data['last_ts'] = now
    else:
        if sid in WATCH_SESSIONS:
            flush_buffer_to_db(user.id)
            del WATCH_SESSIONS[sid]

    # 6. Отправляем ответ (Status Update) всем в комнате
    # Важно: отправляем Server Estimated Time, чтобы клиенты подстраивались под Лидера

    server_time_now = state['timestamp']
    if not state['paused'] and state['leader_sid']:
        # Интерполяция: сколько прошло времени с момента получения данных от лидера
        server_time_now += (now - state['last_update'])

    emit('status_update', {
        'user_id': user.id,
        'username': user.username,
        'sid': request.sid,
        'state': client_state,
        'timestamp': client_ts,
        'avatar': user.to_dict()['avatar_url'],
        'server_timestamp': server_time_now,
        'server_paused': state['paused'],
        'is_leader': (state['leader_sid'] == sid)
    }, to=room_uuid)


@socketio.on('chat_message')
@db_session
def on_chat_message(data):
    user = get_current_user()
    room_uuid = data.get('room_uuid')
    message = data.get('message', '').strip()

    if user and room_uuid and message:
        emit('new_message', {
            'user_id': user.id,
            'username': user.username,
            'text': message,
            'time': 'now'
        }, to=room_uuid)


@socketio.on('change_video')
@db_session
def on_change_video(data):
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
    room_uuid = data.get('room_uuid')
    user = get_current_user()

    if not user or not room_uuid: return

    try:
        room = Room.get(Room.uuid == room_uuid)
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
    owner = get_current_user()
    target_user_id = data.get('target_user_id')
    room_uuid = data.get('room_uuid')
    decision = data.get('decision')

    if not owner or not target_user_id: return

    try:
        room = Room.get(Room.uuid == room_uuid)
        if room.owner_id != owner.id: return

        if decision == 'approve':
            target_user = User.get_by_id(target_user_id)
            RoomAccess.get_or_create(room=room, user=target_user)
            emit('access_granted', {'room_uuid': room_uuid}, to=f"user_{target_user_id}")
        else:
            emit('access_denied', {'reason': 'Владелец отклонил запрос'}, to=f"user_{target_user_id}")

    except Exception as e:
        print(f"Decide error: {e}")