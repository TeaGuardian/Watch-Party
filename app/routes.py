# /app/routes.py
import hashlib
import json
import re
import secrets
import string
import time
import shutil
import random
from datetime import datetime, timedelta  # [NEW] Нужно для графиков

from captcha.image import ImageCaptcha
from flask import Blueprint, request, jsonify, session, redirect, send_file, send_from_directory
from core.models import User, db, RoomBan
import sys
import os
from werkzeug.utils import secure_filename
from peewee import fn
from core.storage import storage
from . import socketio
from .decorators import login_required, admin_required

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.models import Room, Video, NewsPost, User, RoomAccess, db, DailyWatchStat
from core.cache import get_cache, set_cache, delete_cache
from core.validators import validate_password_strength
from config import BotConfig, AppConfig, CeleryConfig
from tasks.media import process_video_task, delete_storage_folder_task, delete_account_files_task

api = Blueprint('api', __name__, url_prefix='/api')

ALLOWED_EXTENSIONS = {'mp4', 'mkv', 'avi', 'mov'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def calculate_file_hash(file_stream):
    sha256_hash = hashlib.sha256()
    pos = file_stream.tell()
    for byte_block in iter(lambda: file_stream.read(4096), b""):
        sha256_hash.update(byte_block)
    file_stream.seek(pos)
    return sha256_hash.hexdigest()


@api.route('/internal/progress', methods=['POST'])
def internal_progress_hook():
    """
    Этот роут вызывает Celery, чтобы уведомить Flask о прогрессе.
    Flask затем пушит это в сокеты.
    """
    # Простая защита: проверяем, что запрос пришел из локальной сети или с правильным ключом
    # (для простоты можно проверить, что sender - это доверенный сервис,
    # но в рамках docker сети это обычно безопасно)

    data = request.json
    if not data:
        return jsonify({'error': 'No data'}), 400

    video_id = data.get('video_id')
    percent = data.get('percent')
    room_uuid = data.get('room_uuid')

    if video_id is not None and percent is not None and room_uuid:
        # Отправляем в сокеты (Flask делает это локально, клиенты это увидят)
        socketio.emit('processing_progress', {
            'video_id': video_id,
            'percent': percent
        }, to=str(room_uuid))

        return jsonify({'status': 'ok'})

    return jsonify({'error': 'Invalid data'}), 400


@api.route('/internal/refresh', methods=['POST'])
def internal_refresh_hook():
    """Роут для обновления плейлиста по завершению"""
    data = request.json
    room_uuid = data.get('room_uuid')
    if room_uuid:
        socketio.emit('playlist_refresh', {}, to=str(room_uuid))
        return jsonify({'status': 'ok'})
    return jsonify({'error': 'No room_uuid'}), 400


# --- AUTH ---

@api.route('/captcha', methods=['GET'])
def get_captcha():
    # 1. Генерируем случайный текст (4 символа, цифры и буквы)
    code = ''.join(random.choices('WTRFYKVNMXZAQH' + string.digits, k=4))

    # 2. Сохраняем в сессию (чтобы потом проверить)
    session['captcha_code'] = code

    # 3. Рисуем картинку
    image = ImageCaptcha(width=280, height=90)
    data = image.generate(code)

    # 4. Отдаем как файл
    return send_file(data, mimetype='image/png')


@api.route('/register', methods=['POST'])
def register():
    data = request.json or {}
    username = data.get('username')
    password = data.get('password')
    cookie_consent = data.get('cookie_consent', False)

    captcha_input = data.get('captcha', '').upper()
    real_captcha = session.get('captcha_code', '')

    if not captcha_input or captcha_input != real_captcha:
        return jsonify({'error': 'Неверная капча'}), 400

    if not username or not password:
        return jsonify({'error': 'Login and password are required'}), 400

    if not cookie_consent:
        return jsonify({'error': 'You must accept cookies to register'}), 400

    # --- ПРОВЕРКА ПАРОЛЯ ---
    is_strong, msg = validate_password_strength(password)
    if not is_strong:
        return jsonify({'error': msg}), 400
    # -----------------------

    if User.get_or_none(User.username == username):
        return jsonify({'error': 'Username already taken'}), 400

    try:
        user = User(username=username, cookie_consent=True)
        user.set_password(password)
        user.save()
        session['user_id'] = user.id
        return jsonify({'success': True, 'message': 'Registered successfully'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@api.route('/login', methods=['POST'])
def login():
    data = request.json or {}
    username = data.get('username')
    password = data.get('password')

    user = User.get_or_none(User.username == username)

    captcha_input = data.get('captcha', '').upper()
    real_captcha = session.get('captcha_code', '')

    if not captcha_input or captcha_input != real_captcha:
        return jsonify({'error': 'Неверная капча'}), 400

    if not user or not user.check_password(password):
        return jsonify({'error': 'Invalid credentials'}), 401

    if user.status == 'banned':
        return jsonify({'error': 'Account is banned'}), 403

    session['user_id'] = user.id
    session.permanent = True

    return jsonify({
        'success': True,
        'user': {
            'username': user.username,
            'role': user.role,
            'status': user.status
        }
    })


@api.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True})


# --- PROFILE & TG ---

@api.route('/me', methods=['GET'])
@login_required
def get_me(current_user: User):
    """Получение состояния текущего пользователя"""
    return jsonify({
        'id': current_user.id,
        'username': current_user.username,
        'role': current_user.role,
        'status': current_user.status,
        'tg_connected': bool(current_user.tg_id),
        'tg_username': current_user.tg_username,
        'avatar_url': storage.get_url(current_user.avatar_path),
        'approved_by': current_user.approved_by.username if current_user.approved_by else None
    })


@api.route('/me/stats', methods=['GET'])
@login_required
def get_my_stats(current_user):
    """
    Статистика просмотра пользователя.
    Кэшируется на 1 час.
    """
    cache_key = f"user_stats_{current_user.id}"

    # 1. Проверяем кэш
    cached_data = get_cache(cache_key)
    if cached_data:
        return jsonify(json.loads(cached_data))

    # 2. Если кэша нет - считаем из БД
    try:
        # Общее время (сумма по всем дням)
        # fn.COALESCE вернет 0, если записей нет (иначе вернет None)
        total_seconds = DailyWatchStat.select(fn.COALESCE(fn.SUM(DailyWatchStat.total_seconds), 0)) \
            .where(DailyWatchStat.user == current_user) \
            .scalar()

        # История за последние 10 записей (дней)
        # Сортируем от новых к старым
        history_query = DailyWatchStat.select() \
            .where(DailyWatchStat.user == current_user) \
            .order_by(DailyWatchStat.date.desc()) \
            .limit(10)

        history_list = []
        for h in history_query:
            history_list.append({
                'date': h.date.strftime("%Y-%m-%d"),
                'seconds': h.total_seconds,
                # Для удобства фронта можно сразу передать минуты
                'minutes': round(h.total_seconds / 60, 1)
            })

        response_data = {
            'success': True,
            'total_seconds': total_seconds,
            'total_hours': round(total_seconds / 3600, 1),
            'history': history_list
        }

        # 3. Сохраняем в Redis на 1 час (3600 сек)
        set_cache(cache_key, json.dumps(response_data), ttl=3600)

        return jsonify(response_data)

    except Exception as e:
        print(f"Stats error: {e}")
        return jsonify({'error': 'Failed to calculate stats'}), 500


@api.route('/tg_link', methods=['GET'])
@login_required
def get_tg_link(current_user: User):
    """Генерация ссылки на бота"""
    if current_user.tg_id:
        return jsonify({'error': 'Telegram already connected'}), 400

    # Генерируем новый код, если старого нет
    if not current_user.verification_code:
        # Генерируем 16-значный случайный код
        code = secrets.token_hex(8)
        current_user.verification_code = code
        current_user.save()
    else:
        code = current_user.verification_code

    # Формируем ссылку (Deep Linking)
    link = f"{BotConfig.LINK}?start={code}"

    return jsonify({
        'success': True,
        'link': link,
        'code': code  # На случай, если нужно отобразить код вручную
    })


@api.route('/tg_link', methods=['DELETE'])
@login_required
def unlink_telegram(current_user):
    """Отвязка Telegram со стороны сайта"""
    if not current_user.tg_id:
        return jsonify({'error': 'Telegram not connected'}), 400

    # Очищаем данные
    current_user.tg_id = None
    current_user.tg_username = None

    # Сбрасываем статус, так как теряем доверие (если не админ)
    if current_user.role != 'admin':
        current_user.status = 'new'
        current_user.approved_by = None

    current_user.save()

    return jsonify({
        'success': True,
        'message': 'Telegram unlinked. Your status is reset to New.'
    })


@api.route('/account', methods=['DELETE'])
@login_required
def delete_account(current_user):
    """Удаление аккаунта с очисткой общих файлов"""
    if current_user.status == 'banned':
        return jsonify({'error': 'Cannot delete banned account'}), 403

    user_id = current_user.id

    # 1. Сбор мусора: Ищем видео пользователя, которые используют Shared Content
    user_videos = Video.select().where(Video.room.in_(current_user.rooms))
    hashes_to_check = set()

    for v in user_videos:
        if v.file_hash:
            hashes_to_check.add(v.file_hash)

    # 2. Удаляем записи из БД
    current_user.delete_instance(recursive=True)

    # 3. Проверяем хеши на сиротство
    for f_hash in hashes_to_check:
        count = Video.select().where(Video.file_hash == f_hash).count()
        if count == 0:
            # Никто больше не использует -> удаляем физически
            folder_to_delete = f"{AppConfig.SHARED_CONTENT_PATH}/{f_hash}/"
            print(f"🗑️ Account deletion orphaned a file. Scheduling delete: {folder_to_delete}")
            delete_storage_folder_task.delay(folder_to_delete)

    # 4. Удаляем личную папку (аватарки, старое легаси видео)
    user_folder = f"users/{user_id}/"
    delete_account_files_task.delay(user_id)  # Здесь передаем ID, а не путь, это безопаснее

    session.clear()
    return jsonify({'success': True, 'message': 'Account deleted and cleanup scheduled'})


@api.route('/profile/avatar', methods=['POST'])
@login_required
def upload_avatar(current_user):
    if 'avatar' not in request.files:
        return jsonify({'error': 'No file'}), 400

    file = request.files['avatar']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    # Генерируем путь: users/{id}/avatar_{timestamp}.jpg
    # Timestamp нужен, чтобы избегать кеширования старых аватарок
    filename = f"avatar_{int(time.time())}.jpg"
    storage_path = f"users/{current_user.id}/{filename}"

    # Если была старая аватарка — удаляем её, чтобы не засорять S3/диск
    if current_user.avatar_path:
        storage.delete_file(current_user.avatar_path)

    # Читаем байты и сохраняем
    file_bytes = file.read()
    if storage.save_file(file_bytes, storage_path, content_type='image/jpeg'):
        current_user.avatar_path = storage_path
        current_user.save()
        return jsonify({'success': True, 'avatar_url': current_user.to_dict()['avatar_url']})
    else:
        return jsonify({'error': 'Failed to save file'}), 500


@api.route('/profile/avatar', methods=['DELETE'])
@login_required
def delete_avatar(current_user):
    if current_user.avatar_path:
        # Удаляем файл физически
        storage.delete_file(current_user.avatar_path)
        # Очищаем запись в БД
        current_user.avatar_path = None
        current_user.save()

    return jsonify({
        'success': True,
        'avatar_url': current_user.to_dict()['avatar_url']  # Вернется ui-avatars
    })


# --- НОВОСТИ ---


@api.route('/news', methods=['GET'])
def get_news():
    posts = NewsPost.select().order_by(NewsPost.created_at.desc()).limit(10)
    return jsonify({'success': True, 'news': [p.to_dict() for p in posts]})


@api.route('/news', methods=['POST'])
@login_required
@admin_required
def create_news(current_user):
    data = request.json
    content = data.get('content')
    if not content:
        return jsonify({'error': 'Content is required'}), 400

    NewsPost.create(author=current_user, content=content)
    return jsonify({'success': True})


@api.route('/news/<int:post_id>', methods=['PUT'])
@login_required
def update_news(current_user, post_id):
    """Редактирование новости"""
    try:
        post = NewsPost.get_by_id(post_id)

        # Разрешаем редактирование только автору или админу
        if post.author != current_user and current_user.role != 'admin':
            return jsonify({'error': 'Access denied'}), 403

        data = request.json
        content = data.get('content', '').strip()

        if not content:
            return jsonify({'error': 'Content cannot be empty'}), 400

        post.content = content
        post.save()

        return jsonify({'success': True})

    except NewsPost.DoesNotExist:
        return jsonify({'error': 'Post not found'}), 404


# --- КОМНАТЫ ---


@api.route('/rooms', methods=['GET'])
@login_required
def get_rooms(current_user):
    """Список: Мои комнаты + Гостевые комнаты"""
    from app.sockets import ROOM_STATE
    from app import socketio

    rooms_data = []
    my_rooms = (Room
                .select(Room, User)
                .join(User)
                .where(Room.owner == current_user))

    for r in my_rooms:
        data = r.to_dict()
        data['is_owner'] = True  # Флаг для фронта
        rooms_data.append(data)

    banned_ids = RoomBan.select(RoomBan.room_id).where(RoomBan.user == current_user)

    guest_rooms = (Room
    .select(Room, User)
    .join(User)
    .switch(Room)
    .join(RoomAccess)
    .where(
        (RoomAccess.user == current_user) &
        (Room.id.not_in(banned_ids))
    ))

    for r in guest_rooms:
        data = r.to_dict()
        data['is_owner'] = False
        rooms_data.append(data)

    rooms_data.sort(key=lambda x: x['created_at'], reverse=True)

    for r_dict in rooms_data:
        room_uuid = r_dict['uuid']
        try:
            participants = socketio.server.manager.rooms.get('/', {}).get(room_uuid, set())
            r_dict['online_count'] = len(participants)
        except:
            r_dict['online_count'] = 0

        state = ROOM_STATE.get(room_uuid, {})
        video_id = state.get('video_id')
        current_video_title = None

        if video_id:
            try:
                vid = Video.get_or_none(Video.id == video_id)
                if vid: current_video_title = vid.title
            except:
                pass

        r_dict['now_playing'] = current_video_title

    return jsonify({'success': True, 'rooms': rooms_data})


@api.route('/rooms', methods=['POST'])
@login_required
def create_room(current_user):
    # 1. Проверяем, одобрен ли аккаунт
    if current_user.status not in ['approved', 'admin']:
        return jsonify({'error': 'Wait for admin approval'}), 403

    current_count = Room.select().where(Room.owner == current_user).count()
    if current_count >= AppConfig.MAX_ROOMS_COUNT:
        return jsonify({
            'error': f'Достигнут лимит комнат ({AppConfig.MAX_ROOMS_COUNT})',
            'code': 'limit_reached'
        }), 400

    data = request.json or {}
    name = data.get('name')

    # Дефолтные значения и приведение типов
    is_private = bool(data.get('is_private', False))
    header_color = data.get('header_color', '#0d6efd')  # Синий по умолчанию
    allow_guest_control = bool(data.get('allow_guest_control', False))
    voice_chat_enabled = bool(data.get('voice_chat_enabled', False))

    if not name:
        return jsonify({'error': 'Name is required'}), 400

    try:
        room = Room.create(
            owner=current_user,
            name=name,
            is_private=is_private,
            header_color=header_color,
            allow_guest_control=allow_guest_control,
            has_voice_chat=voice_chat_enabled
        )
        return jsonify({'success': True, 'uuid': str(room.uuid)})

    except Exception as e:
        print(f"Error creating room: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@api.route('/rooms/<uuid:room_uuid>', methods=['GET'])
@login_required
def get_room_details(current_user, room_uuid):
    try:
        room = Room.get(Room.uuid == room_uuid)

        if RoomBan.select().where((RoomBan.room == room) & (RoomBan.user == current_user)).exists():
            return jsonify({'error': 'You are banned from this room', 'code': 'banned'}), 403

        is_owner = (room.owner == current_user)
        is_admin = current_user.role == 'admin'

        # Проверка доступа
        has_access = True
        if room.is_private:
            if not is_owner:
                # Проверяем в таблице RoomAccess
                has_access = RoomAccess.select().where(
                    (RoomAccess.room == room) &
                    (RoomAccess.user == current_user)
                ).exists()

        # Если доступа нет, отдаем минимальную инфу (без видео)
        if not has_access:
            return jsonify({
                'success': True,
                'room': {
                    'uuid': str(room.uuid),
                    'name': room.name,
                    'is_private': True,
                    'owner_name': room.owner.username  # Чтобы знать, к кому стучаться
                },
                'videos': [],  # Не показываем видео
                'is_owner': False,
                'has_access': False  # Флаг для фронтенда
            })

        # Если доступ есть — отдаем всё как раньше
        videos = Video.select().where(Video.room == room).order_by(Video.created_at)

        return jsonify({
            'success': True,
            'room': room.to_dict(),
            'videos': [v.to_dict() for v in videos],
            'is_owner': is_owner,
            'is_private': room.is_private,
            'allow_guest_control': room.allow_guest_control,
            'has_voice_chat': room.has_voice_chat,  # [NEW]
            'has_access': True
        })

    except Room.DoesNotExist:
        return redirect("/", 404)


@api.route('/rooms/<uuid:room_uuid>', methods=['DELETE'])
@login_required
def delete_room(current_user, room_uuid):
    try:
        room = Room.get(Room.uuid == room_uuid)

        # Проверка прав: владелец или админ
        if room.owner != current_user:
            return jsonify({'error': 'Access denied'}), 403

        room_folder = f"users/{room.owner_id}/rooms/{room.uuid}/"
        delete_storage_folder_task.delay(room_folder)
        room.delete_instance(recursive=True)

        return jsonify({'success': True})
    except Room.DoesNotExist:
        return jsonify({'error': 'Not found'}), 404


@api.route('/rooms/<uuid:room_uuid>', methods=['PUT'])
@login_required
def update_room_settings(current_user, room_uuid):
    try:
        room = Room.get(Room.uuid == room_uuid)
        if room.owner != current_user:
            return jsonify({'error': 'Access denied'}), 403

        data = request.json

        if 'name' in data: room.name = data['name']
        if 'header_color' in data: room.header_color = data['header_color']
        if 'is_private' in data: room.is_private = bool(data['is_private'])
        if 'allow_guest_control' in data: room.allow_guest_control = bool(data['allow_guest_control'])

        room.save()

        # Уведомляем сокеты об обновлении (опционально, чтобы обновить заголовок у всех)
        socketio.emit('room_updated', {
            'name': room.name,
            'header_color': room.header_color
        }, to=str(room_uuid))

        return jsonify({'success': True})
    except Room.DoesNotExist:
        return jsonify({'error': 'Not found'}), 404


@api.route('/rooms/<uuid:room_uuid>/leave', methods=['DELETE'])
@login_required
def leave_room(current_user, room_uuid):
    """Покинуть чужую комнату (удалить себя из RoomAccess)"""
    try:
        room = Room.get(Room.uuid == room_uuid)

        if room.owner == current_user:
            return jsonify({'error': 'Owner cannot leave room, delete it instead'}), 400

        query = RoomAccess.delete().where(
            (RoomAccess.room == room) &
            (RoomAccess.user == current_user)
        )
        deleted = query.execute()

        if deleted:
            return jsonify({'success': True})
        else:
            return jsonify({'error': 'You are not in this room'}), 400

    except Room.DoesNotExist:
        return jsonify({'error': 'Not found'}), 404


@api.route('/rooms/<uuid:room_uuid>/bans', methods=['GET'])
@login_required
def get_room_bans(current_user, room_uuid):
    """Список забаненных"""
    room = Room.get(Room.uuid == room_uuid)
    if room.owner != current_user: return jsonify({'error': 'Access denied'}), 403

    bans = RoomBan.select().where(RoomBan.room == room)
    return jsonify({'success': True, 'bans': [{
        'user_id': b.user.id,
        'username': b.user.username,
        'avatar_url': b.user.to_dict()['avatar_url']
    } for b in bans]})


@api.route('/rooms/<uuid:room_uuid>/bans', methods=['POST'])
@login_required
def ban_user_in_room(current_user, room_uuid):
    """Забанить пользователя"""
    room = Room.get(Room.uuid == room_uuid)
    if room.owner != current_user: return jsonify({'error': 'Access denied'}), 403

    target_id = request.json.get('user_id')
    target_user = User.get_by_id(target_id)

    if target_user == current_user:
        return jsonify({'error': 'Cannot ban self'}), 400

    RoomBan.get_or_create(room=room, user=target_user)

    # Кикаем из сокетов
    socketio.emit('you_are_banned', {}, to=f"user_{target_id}")

    return jsonify({'success': True})


@api.route('/rooms/<uuid:room_uuid>/bans/<int:user_id>', methods=['DELETE'])
@login_required
def unban_user_in_room(current_user, room_uuid, user_id):
    """Разбанить"""
    room = Room.get(Room.uuid == room_uuid)
    if room.owner != current_user: return jsonify({'error': 'Access denied'}), 403

    query = RoomBan.delete().where(
        (RoomBan.room == room) &
        (RoomBan.user_id == user_id)
    )
    query.execute()
    return jsonify({'success': True})


@api.route('/rooms/<uuid:room_uuid>/access', methods=['GET'])
@login_required
def get_room_access_list(current_user, room_uuid):
    """Список тех, кому разрешен вход (для приватных комнат)"""
    try:
        room = Room.get(Room.uuid == room_uuid)
        if room.owner != current_user:
            return jsonify({'error': 'Access denied'}), 403

        access_entries = RoomAccess.select().where(RoomAccess.room == room)
        users = [entry.user.to_dict() for entry in access_entries]

        return jsonify({'success': True, 'users': users})
    except Room.DoesNotExist:
        return jsonify({'error': 'Not found'}), 404


@api.route('/rooms/<uuid:room_uuid>/access/<int:user_id>', methods=['DELETE'])
@login_required
def revoke_room_access(current_user, room_uuid, user_id):
    """Удалить из списка доступа"""
    try:
        room = Room.get(Room.uuid == room_uuid)
        if room.owner != current_user:
            return jsonify({'error': 'Access denied'}), 403

        query = RoomAccess.delete().where(
            (RoomAccess.room == room) &
            (RoomAccess.user_id == user_id)
        )
        query.execute()
        return jsonify({'success': True})
    except:
        return jsonify({'error': 'Error'}), 500


# --- ВИДЕО И ЗАГРУЗКА ---

@api.route('/rooms/<uuid:room_uuid>/upload', methods=['POST'])
@login_required
def upload_video(current_user, room_uuid):
    """Загрузка видеофайла"""
    try:
        room = Room.get(Room.uuid == room_uuid)
        if room.owner != current_user and not room.allow_guest_control:
            return jsonify({'error': 'Only owner can upload'}), 403
    except Room.DoesNotExist:
        return jsonify({'error': 'Room not found'}), 404

    if 'video' not in request.files:
        return jsonify({'error': 'No file part'}), 400

    file = request.files['video']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    # --- ПРОВЕРКА 1: Лимит количества видео ---
    video_count = Video.select().where(Video.room == room).count()
    if video_count >= AppConfig.MAX_ROOM_VIDEOS:
        return jsonify({
            'error': f'В комнате максимум {AppConfig.MAX_ROOM_VIDEOS} видео. Удалите старые.',
            'code': 'limit_reached'
        }), 400

    # --- ПРОВЕРКА 2: Размер одного файла ---
    # Перематываем в конец, чтобы узнать реальный размер
    file.seek(0, 2)
    file_size = file.tell()
    file.seek(0)

    if file_size > AppConfig.MAX_VIDEO_SIZE_BYTES:
        return jsonify({
            'error': f'Файл слишком большой. Максимум {AppConfig.MAX_VIDEO_SIZE_MB} МБ.',
            'code': 'file_too_large'
        }), 400

    # --- ПРОВЕРКА 3: Квота на комнату (3 ГБ) ---
    current_storage_size = Video.select(fn.SUM(Video.file_size)).where(Video.room == room).scalar() or 0
    if (current_storage_size + file_size) > AppConfig.MAX_ROOM_STORAGE_BYTES:
        return jsonify({
            'error': f'Превышен лимит хранилища комнаты ({AppConfig.MAX_ROOM_STORAGE_MB} МБ).',
            'code': 'storage_limit_reached'
        }), 400

    # --- ПРОВЕРКА 4: Дубликаты (Хеш) ---
    file_hash = calculate_file_hash(file)

    # 4.1 Проверяем дубли ВНУТРИ комнаты (запрещаем)
    duplicate_in_room = Video.select().where(
        (Video.room == room) & (Video.file_hash == file_hash)
    ).exists()

    if duplicate_in_room:
        return jsonify({
            'error': 'Такое видео уже есть в этой комнате.',
            'code': 'duplicate_video'
        }), 400

    # 4.2 Проверяем дубли ГЛОБАЛЬНО (для переиспользования)
    # Ищем любое ГОТОВОЕ видео с таким же хэшем
    existing_source = Video.select().where(
        (Video.file_hash == file_hash) &
        (Video.status == 'ready')
    ).first()

    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)

        # Если файл уже есть на сервере -> Мгновенное создание
        if existing_source:
            print(f"♻️ Fast-upload (Deduplication): Using content from video {existing_source.id}")
            video = Video.create(
                room=room,
                title=filename,
                storage_path=existing_source.storage_path,  # Копируем путь
                status='ready',  # Сразу готово
                file_hash=file_hash,
                file_size=file_size,  # Берем размер из текущей загрузки (он проверен)
                duration=existing_source.duration  # Копируем длительность
            )

            socketio.emit('playlist_refresh', {}, to=str(room_uuid))
            return jsonify({
                'success': True,
                'message': 'Video added instantly (deduplicated)',
                'video_id': video.id
            })

        # Если файла нет -> Полная загрузка
        video = Video.create(
            room=room,
            title=filename,
            storage_path='',
            status='uploading',
            file_hash=file_hash,
            file_size=file_size,
        )

        temp_path = os.path.join(AppConfig.UPLOAD_FOLDER, f"temp_{video.id}_{filename}")
        file.save(temp_path)
        if file_size > AppConfig.HEAVY_VIDEO_THRESHOLD_BYTES:
            target_queue = CeleryConfig.QUEUE_HEAVY
            print(f"⚖️ Task routed to HEAVY queue (Size: {file_size / 1024 / 1024:.2f} MB)")
        else:
            target_queue = CeleryConfig.QUEUE_FAST
            print(f"🚀 Task routed to FAST queue (Size: {file_size / 1024 / 1024:.2f} MB)")

            # 4. Ставим статус 'queued' перед отправкой
        video.status = 'queued'
        video.save()

        # 5. Используем apply_async для указания очереди
        process_video_task.apply_async(
            args=[video.id, temp_path],
            queue=target_queue
        )

        socketio.emit('playlist_refresh', {}, to=str(room_uuid))
        return jsonify({'success': True, 'message': 'Added to processing queue', 'video_id': video.id})

    return jsonify({'error': 'Invalid file type'}), 400


@api.route('/videos/<int:video_id>', methods=['DELETE'])
@login_required
def delete_video(current_user, video_id):
    try:
        video = Video.get_by_id(video_id)
        if video.room.owner != current_user and not video.room.allow_guest_control:
            return jsonify({'error': 'Access denied'}), 403

        room_uuid = video.room.uuid
        room_uuid_str = str(room_uuid)
        file_hash = video.file_hash

        # --- [NEW] Проверка: Играет ли это видео сейчас? ---
        from app.sockets import ROOM_STATE
        if room_uuid_str in ROOM_STATE:
            current_state = ROOM_STATE[room_uuid_str]
            if current_state.get('video_id') == video.id:
                # Сбрасываем стейт
                ROOM_STATE[room_uuid_str]['video_id'] = None
                ROOM_STATE[room_uuid_str]['timestamp'] = 0
                ROOM_STATE[room_uuid_str]['paused'] = True
                socketio.emit('stop_playback', {}, to=room_uuid_str)

        # --- УМНОЕ УДАЛЕНИЕ ФАЙЛОВ ---
        other_refs_count = Video.select().where(
            (Video.file_hash == file_hash) &
            (Video.id != video.id)
        ).count()

        if other_refs_count == 0 and video.storage_path:
            folder_to_delete = os.path.dirname(video.storage_path)
            if folder_to_delete:
                if not folder_to_delete.endswith('/'): folder_to_delete += '/'
                delete_storage_folder_task.delay(folder_to_delete)

        video.delete_instance()

        socketio.emit('playlist_refresh', {}, to=room_uuid_str)
        return jsonify({'success': True})

    except Video.DoesNotExist:
        return jsonify({'error': 'Not found'}), 404


@api.route('/videos/<int:video_id>', methods=['PUT'])
@login_required
def rename_video(current_user, video_id):
    """Переименование видео"""
    try:
        video = Video.get_by_id(video_id)
        if video.room.owner != current_user and not video.room.allow_guest_control:
            return jsonify({'error': 'Access denied'}), 403

        data = request.json
        new_title = data.get('title', '').strip()
        if not new_title:
            return jsonify({'error': 'Title cannot be empty'}), 400

        video.title = new_title
        video.save()
        socketio.emit('playlist_refresh', {}, to=str(video.room.uuid))
        return jsonify({'success': True, 'title': video.title})

    except Video.DoesNotExist:
        return jsonify({'error': 'Not found'}), 404


# --- ADMIN PANEL ---

@api.route('/admin/stats', methods=['GET'])
@login_required
@admin_required
def get_admin_stats(current_user):
    """Статистика для дашборда (Кэш 5 минут)"""
    cache_key = "admin_global_stats"

    # Сбрасывайте кэш при разработке, или уменьшите ttl
    cached_data = get_cache(cache_key)
    if cached_data:
        return jsonify(json.loads(cached_data))

    # 1. Базовая статистика (как было)
    total_seconds = DailyWatchStat.select(fn.SUM(DailyWatchStat.total_seconds)).scalar() or 0
    total_hours = float(round(total_seconds / 3600, 1))

    # 2. Статистика хранилища (S3 или Local)
    # Считаем объем файлов в БД
    total_storage_bytes = Video.select(fn.SUM(Video.file_size)).scalar() or 0
    total_storage_gb = float(round(total_storage_bytes / (1024 ** 3), 2))

    # Получаем реальное место на диске (внутри контейнера/сервера)
    # Если используете S3, это покажет место на диске сервера, где лежат temp файлы
    total, used, free = shutil.disk_usage("/")
    disk_info = {
        'total_gb': round(total / (1024**3), 1),
        'used_gb': round(used / (1024**3), 1),
        'free_gb': round(free / (1024**3), 1),
        'percent': round((used / total) * 100, 1)
    }

    # 3. Статусы видео (Важно для мониторинга застреваний)
    video_stats = {
        'ready': Video.select().where(Video.status == 'ready').count(),
        'processing': Video.select().where(Video.status == 'processing').count(),
        'uploading': Video.select().where(Video.status == 'uploading').count(),
        'error': Video.select().where(Video.status == 'error').count()
    }

    # 4. Нагрузка системы (Load Average) - работает на Linux/Mac
    try:
        load_1, load_5, load_15 = os.getloadavg()
    except:
        load_1, load_5, load_15 = 0, 0, 0

    stats = {
        'users_total': User.select().count(),
        'users_new': User.select().where(User.status.in_(['new', 'tg_verified'])).count(),
        'rooms_total': Room.select().count(),
        'videos_total': Video.select().count(),
        'total_watch_hours': total_hours,
        'storage_used_gb': total_storage_gb,
        # Новые данные
        'disk_info': disk_info,
        'video_stats': video_stats,
        'system_load': [round(load_1, 2), round(load_5, 2), round(load_15, 2)]
    }

    set_cache(cache_key, json.dumps(stats), ttl=120) # Уменьшил TTL до 60 сек для админки

    return jsonify(stats)


@api.route('/admin/users', methods=['GET'])
@login_required
@admin_required
def get_all_users(current_user):
    """Список пользователей (Кэш 5 минут)"""
    cache_key = "admin_users_list"

    # 1. Проверяем кэш
    cached_data = get_cache(cache_key)
    if cached_data:
        return jsonify(json.loads(cached_data))

    # 2. Выполняем запрос
    users = User.select().order_by(User.created_at.desc())

    users_data = []
    for u in users:
        # Тяжелый подзапрос для каждого юзера
        total_sec = DailyWatchStat.select(fn.SUM(DailyWatchStat.total_seconds)).where(
            DailyWatchStat.user == u).scalar() or 0
        total_hours = float(round(total_sec / 3600, 1))

        d = u.to_dict()
        d['total_hours'] = total_hours
        users_data.append(d)

    response = {'users': users_data}

    # 3. Сохраняем
    set_cache(cache_key, json.dumps(response), ttl=300)

    return jsonify(response)


@api.route('/admin/users/<int:user_id>/details', methods=['GET'])
@login_required
@admin_required
def get_user_admin_details(current_user, user_id):
    """Детальная инфа для модалки (Кэш 5 минут)"""
    cache_key = f"admin_user_details_{user_id}"

    cached_data = get_cache(cache_key)
    if cached_data:
        return jsonify(json.loads(cached_data))

    try:
        user = User.get_by_id(user_id)

        rooms_count = Room.select().where(Room.owner == user).count()
        videos_count = Video.select().join(Room).where(Room.owner == user).count()

        today = datetime.now().date()
        date_start = today - timedelta(days=29)

        stats_query = DailyWatchStat.select().where(
            (DailyWatchStat.user == user) &
            (DailyWatchStat.date >= date_start)
        ).order_by(DailyWatchStat.date.desc())

        history = []
        for s in stats_query:
            history.append({
                'date': s.date.isoformat(),
                'seconds': s.total_seconds,
                'hours': round(s.total_seconds / 3600, 1)
            })

        response = {
            'user': user.to_dict(),
            'rooms_count': rooms_count,
            'videos_count': videos_count,
            'history': history,
            'approved_by': user.approved_by.username if user.approved_by else None
        }

        set_cache(cache_key, json.dumps(response), ttl=300)
        return jsonify(response)

    except User.DoesNotExist:
        return jsonify({'error': 'User not found'}), 404


@api.route('/admin/users/<int:user_id>/status', methods=['POST'])
@login_required
@admin_required
def update_user_status(current_user, user_id):
    """Бан / Разбан / Аппрув / Смена роли"""
    data = request.json
    new_status = data.get('status')
    new_role = data.get('role')

    try:
        user = User.get_by_id(user_id)

        if user.id == current_user.id:
            return jsonify({'error': 'Cannot change own status'}), 400

        if new_status:
            user.status = new_status
            if new_status == 'approved':
                user.approved_by = current_user

        if new_role:
            user.role = new_role

        user.save()

        # [NEW] Инвалидация кэша
        # Удаляем общий список, так как статус юзера изменился
        delete_cache("admin_users_list")
        # Удаляем детальную инфу конкретного юзера
        delete_cache(f"admin_user_details_{user_id}")
        # Удаляем общую статистику (кол-во new users могло измениться)
        delete_cache("admin_global_stats")

        return jsonify({'success': True})
    except User.DoesNotExist:
        return jsonify({'error': 'User not found'}), 404


@api.route('/admin/users/<int:user_id>', methods=['DELETE'])
@login_required
@admin_required
def delete_user_force(current_user, user_id):
    """Принудительное удаление пользователя админом"""
    try:
        user = User.get_by_id(user_id)
        if user.id == current_user.id:
            return jsonify({'error': 'Cannot delete self'}), 400

        user_folder = f"users/{user.id}/"
        delete_storage_folder_task.delay(user_folder)
        user.delete_instance(recursive=True)

        # [NEW] Инвалидация кэша
        delete_cache("admin_users_list")
        delete_cache(f"admin_user_details_{user_id}")
        delete_cache("admin_global_stats")

        return jsonify({'success': True})
    except User.DoesNotExist:
        return jsonify({'error': 'User not found'}), 404