# /app/sockets.py
import os
import sys
import time
from datetime import datetime, date
from functools import wraps

from flask import session, request
from flask_socketio import emit, join_room, leave_room
print("🔥 SOCKETS.PY IMPORTED SUCCESSFULLY 🔥", flush=True)
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import AppConfig
from app import socketio
from core.models import User, Room, Video, db, RoomAccess, RoomBan, DailyWatchStat

# --- ГЛОБАЛЬНОЕ СОСТОЯНИЕ ---

ROOM_STATE = {}
SYNC_METRICS = {}  # Метрики качества соединения для выбора лидера

ACTIVE_CONNECTIONS = {}
SID_MAP = {}
SID_TIMINGS = {}

# Для аналитики
WATCH_SESSIONS = {}
WATCH_BUFFER = {}


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def db_session(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if db.is_closed():
            db.connect()
        try:
            return f(*args, **kwargs)
        except Exception as e:
            print(f"❌ Socket DB Error in {f.__name__}: {e}")
            raise e
        finally:
            if not db.is_closed():
                db.close()

    return wrapper


def flush_buffer_to_db(user_id):
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
        WATCH_BUFFER[user_id] = 0
    except Exception as e:
        print(f"Error flushing stats: {e}")


def track_connection_add(user_id, room_uuid, sid):
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
    if sid in SID_TIMINGS: del SID_TIMINGS[sid]
    if sid in SYNC_METRICS: del SYNC_METRICS[sid]

    if sid not in SID_MAP: return
    user_id, room_uuid = SID_MAP.pop(sid)

    if user_id in ACTIVE_CONNECTIONS:
        if room_uuid in ACTIVE_CONNECTIONS[user_id]:
            ACTIVE_CONNECTIONS[user_id][room_uuid].discard(sid)
            if not ACTIVE_CONNECTIONS[user_id][room_uuid]:
                del ACTIVE_CONNECTIONS[user_id][room_uuid]
        if not ACTIVE_CONNECTIONS[user_id]:
            del ACTIVE_CONNECTIONS[user_id]


def get_current_user():
    user_id = session.get('user_id')
    if not user_id:
        return None
    return User.get_or_none(User.id == user_id)


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


def format_seconds(seconds):
    if seconds is None: return "00:00"
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02d}:{s:02d}"


def send_system_message(room_uuid, text):
    emit('new_message', {
        'username': 'System',
        'text': text,
        'is_system': True,
        'time': datetime.now().strftime('%H:%M')
    }, to=room_uuid)


# --- АЛГОРИТМ ВЫБОРА ЛИДЕРА (SYNC) ---

def elect_leader(room_uuid, room_obj):
    state = ROOM_STATE[room_uuid]
    now = time.time()

    try:
        # Получаем список SID в комнате через менеджер SocketIO
        room_participants = socketio.server.manager.rooms.get('/', {}).get(room_uuid, set())
    except:
        room_participants = set()

    if not room_participants:
        state['leader_sid'] = None
        return

    candidates = []
    owner_online = (now - state.get('owner_last_seen', 0)) < 10.0

    # Строгий режим: если владелец задал настройки
    strict_mode = not room_obj.allow_guest_control and (room_obj.owner_id is not None)

    if strict_mode:
        if not owner_online:
            # Если владельца нет, ставим паузу
            if not state['paused']:
                state['paused'] = True
                socketio.emit('sync_event', {'action': 'pause', 'timestamp': state['timestamp']}, to=room_uuid)
                send_system_message(room_uuid, "Владелец отключился. Пауза.")
            state['leader_sid'] = None
            return

        # Лидером может быть только владелец
        for sid in room_participants:
            info = SID_MAP.get(sid)
            if info and info[0] == room_obj.owner_id:
                metrics = SYNC_METRICS.get(sid, {'streak': 0})
                candidates.append({'sid': sid, 'score': metrics.get('streak', 0)})
    else:
        # Демократия: лидером становится тот, у кого лучший коннект (streak)
        for sid in room_participants:
            metrics = SYNC_METRICS.get(sid)
            if not metrics: continue
            if now - metrics['last_seen'] > 5.0: continue

            score = metrics['streak']
            user_id = SID_MAP.get(sid, (0, 0))[0]
            if user_id == room_obj.owner_id: score += 50  # Бонус владельцу

            candidates.append({'sid': sid, 'score': score})

    if not candidates:
        return

    candidates.sort(key=lambda x: x['score'], reverse=True)
    best = candidates[0]
    current = state.get('leader_sid')

    # Анти-дребезг смены лидера (меняем только если новый кандидат явно лучше)
    if current:
        curr_stats = next((c for c in candidates if c['sid'] == current), None)
        if curr_stats and curr_stats['score'] >= (best['score'] - 10):
            return

    if state['leader_sid'] != best['sid']:
        state['leader_sid'] = best['sid']
        # print(f"👑 New Leader in {room_uuid}: {best['sid']}")


# --- SOCKET EVENTS ---

@socketio.on('connect')
@db_session
def on_connect(*args, **kwargs):
    user = get_current_user()
    if user:
        join_room(f"user_{user.id}")
    # print(f"✅ Connect: {request.sid}")


@socketio.on('disconnect')
def on_disconnect():
    track_connection_remove(request.sid)


@socketio.on('join')
@db_session
def on_join(data):
    room_uuid = str(data.get('room_uuid'))  # Force string! Важно!
    user = get_current_user()

    if not user:
        emit('error', {'msg': 'Session lost. Please refresh.'})
        return
    if not room_uuid: return

    success, error_msg = track_connection_add(user.id, room_uuid, request.sid)
    if not success:
        emit('error_limit', {'msg': error_msg, 'redirect': True}, to=request.sid)
        return

    try:
        room = Room.get(Room.uuid == room_uuid)
        if RoomBan.select().where((RoomBan.room == room) & (RoomBan.user == user)).exists():
            emit('error', {'msg': 'Вы забанены'}, to=request.sid)
            return
        if room.is_private and room.owner_id != user.id and user.role != 'admin':
            if not RoomAccess.select().where((RoomAccess.room == room) & (RoomAccess.user == user)).exists():
                emit('error', {'msg': 'Доступ запрещен'}, to=request.sid)
                return
    except Exception as e:
        print(f"❌ Join Error: {e}")
        return

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
                if d['url']: video_data = d
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
    room_uuid = str(data.get('room_uuid'))
    user = get_current_user()
    if room_uuid and user:
        flush_buffer_to_db(user.id)
        if request.sid in WATCH_SESSIONS: del WATCH_SESSIONS[request.sid]
        leave_room(room_uuid)
        emit('user_left', {'user_id': user.id, 'sid': request.sid}, to=room_uuid)


@socketio.on('heartbeat')
@db_session
def on_heartbeat(data):
    user = get_current_user()
    room_uuid = str(data.get('room_uuid'))
    client_ts = float(data.get('timestamp', 0))
    client_state = data.get('state')
    buffered = float(data.get('buffered', 0))

    if not user or not room_uuid: return

    sid = request.sid
    now = time.time()
    state = get_room_state_default(room_uuid)

    try:
        room = Room.get(Room.uuid == room_uuid)
    except:
        return

    # 1. Метрики Владельца
    if user.id == room.owner_id:
        state['owner_last_seen'] = now

    # 2. Метрики Сокета (Streak - качество синхронизации)
    if sid not in SYNC_METRICS:
        SYNC_METRICS[sid] = {'streak': 0, 'last_seen': now}

    metric = SYNC_METRICS[sid]
    metric['last_seen'] = now

    # Проверка "дрифта" времени для оценки качества клиента
    if client_state == 'playing' and not state['paused']:
        server_est = state['timestamp'] + (now - state['last_update'])
        diff = abs(client_ts - server_est)
        if diff < 2.0:
            metric['streak'] = min(metric['streak'] + 1, 600)
        else:
            metric['streak'] = 0

    # Выборы лидера
    elect_leader(room_uuid, room)

    # Если мы лидер - обновляем глобальный стейт
    if state['leader_sid'] == sid:
        state['timestamp'] = client_ts
        state['last_update'] = now
        state['paused'] = (client_state == 'paused')

    # Аналитика просмотра (Watch Time)
    if client_state == 'playing':
        if sid not in WATCH_SESSIONS:
            WATCH_SESSIONS[sid] = {'start_ts': now, 'last_ts': now}
        else:
            s_data = WATCH_SESSIONS[sid]
            if (now - s_data['start_ts']) > 10:
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

    # Подготовка данных для отправки обратно (Server Truth)
    server_time_now = state['timestamp']
    if not state['paused'] and state['leader_sid']:
        server_time_now += (now - state['last_update'])

    # ВОТ ЭТОГО НЕ БЫЛО В ВАШЕМ ФАЙЛЕ:
    emit('status_update', {
        'user_id': user.id,
        'username': user.username,
        'sid': request.sid,
        'state': client_state,
        'timestamp': client_ts,
        'buffered': buffered,
        'avatar': user.to_dict()['avatar_url'],
        # Критически важные поля для JS:
        'server_timestamp': server_time_now,
        'server_paused': state['paused'],
        'is_leader': (state['leader_sid'] == sid)
    }, to=room_uuid)


@socketio.on('sync_action')
@db_session
def on_sync_action(data):
    user = get_current_user()
    room_uuid = str(data.get('room_uuid'))
    action = data.get('action')
    val = float(data.get('timestamp', 0))

    if not user or not room_uuid: return

    try:
        room = Room.get(Room.uuid == room_uuid)
        is_owner = (room.owner_id == user.id)
        if not is_owner and not room.allow_guest_control: return

        state = get_room_state_default(room_uuid)
        now = time.time()

        # Принудительное обновление стейта от инициатора действия
        state['timestamp'] = val
        state['last_update'] = now
        state['leader_sid'] = request.sid

        # Даем инициатору "бонус", чтобы лидерство не отскочило сразу
        if request.sid in SYNC_METRICS:
            SYNC_METRICS[request.sid]['streak'] += 20

        sys_msg = ""
        if action == 'play':
            state['paused'] = False
            sys_msg = f"{user.username} запустил видео"
            if state.get('video_id'):
                try:
                    Video.update(last_played_at=datetime.now()).where(Video.id == state['video_id']).execute()
                except:
                    pass
        elif action == 'pause':
            state['paused'] = True
            sys_msg = f"{user.username} поставил на паузу"
            if request.sid in WATCH_SESSIONS:
                flush_buffer_to_db(user.id)
                del WATCH_SESSIONS[request.sid]
        elif action == 'seek':
            sys_msg = f"{user.username} перемотал на {format_seconds(val)}"

        emit('sync_event', {
            'action': action,
            'timestamp': val,
            'actor': user.username
        }, to=room_uuid, include_self=False)

        if sys_msg:
            send_system_message(room_uuid, sys_msg)

    except Exception as e:
        print(f"Sync error: {e}")


@socketio.on('change_video')
@db_session
def on_change_video(data):
    # 1. Сразу печатаем, что событие пришло
    print(f"🔍 EVENT: change_video received. Data: {data}", flush=True)

    user = get_current_user()
    room_uuid = str(data.get('room_uuid'))

    # 2. Печатаем, кого мы нашли
    print(f"👤 USER: {user} (ID: {user.id if user else 'None'}), ROOM: {room_uuid}", flush=True)

    if not user or not room_uuid:
        # 3. Печатаем, почему мы выходим
        print("❌ ABORT: No user or no room_uuid!", flush=True)
        # Отправляем ошибку клиенту, чтобы увидеть её в консоли браузера
        emit('error', {'msg': 'Auth failed or Room missing'}, to=request.sid)
        return

    video_id = data.get('video_id')

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


@socketio.on('chat_message')
@db_session
def on_chat_message(data):
    user = get_current_user()
    room_uuid = str(data.get('room_uuid'))
    message = data.get('message', '').strip()
    if user and room_uuid and message:
        emit('new_message', {
            'user_id': user.id,
            'username': user.username,
            'text': message,
            'time': datetime.now().strftime('%H:%M'),
            'is_system': False
        }, to=room_uuid)


@socketio.on('knock_knock')
@db_session
def on_knock(data):
    room_uuid = str(data.get('room_uuid'))
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
    except:
        pass


@socketio.on('decide_knock')
@db_session
def on_decide_knock(data):
    owner = get_current_user()
    target_user_id = data.get('target_user_id')
    room_uuid = str(data.get('room_uuid'))
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
    except:
        pass