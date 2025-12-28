# /app/__init__.py
from flask import Flask, render_template, send_from_directory, redirect, session, request, url_for
from flask_socketio import SocketIO
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from werkzeug.middleware.proxy_fix import ProxyFix
from app.decorators import login_required, admin_required
from config import AppConfig, StorageConfig, RedisConfig
from core.database import db
from core.models import User, Room, Video

# --- ИСПРАВЛЕНИЕ: Инициализация SocketIO только один раз ---
socketio = SocketIO(
    cors_allowed_origins="*",
    message_queue=RedisConfig.URL,
    manage_session=True # Важно для работы сессий
)

# Импортируем логику сокетов ПОСЛЕ создания объекта socketio
from app import sockets
from app.sockets import ACTIVE_CONNECTIONS, ROOM_STATE

def create_app():
    app = Flask(__name__,
                template_folder='../templates',
                static_folder='../static')

    app.config.from_object(AppConfig)
    #app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    # Регистрируем Blueprint с API
    from .routes import api
    app.register_blueprint(api)
    socketio.init_app(app, async_mode='eventlet')

    # Инициализация SocketIO с приложением
    socketio.init_app(app)

    from . import sockets

    # --- Peewee Connection Handling ---
    # Flask многопоточный, открываем/закрываем соединение для каждого запроса

    @app.before_request
    def before_request():
        if db.is_closed():
            db.connect()

    @app.teardown_request
    def _db_close(exc):
        if not db.is_closed():
            db.close()

    @app.route('/')
    def index():
        return render_template('index.html')

    @app.route('/favicon.ico', methods=['GET'])
    def favicon():
        return send_from_directory(app.static_folder, 'ico.ico', mimetype='image/vnd.microsoft.icon')

    @app.route('/login')
    def login_page():
        return render_template('login.html')

    @app.route('/policy')
    def policy_page():
        return render_template('policy.html', config=AppConfig)

    @app.route('/room/<uuid:room_uuid>')
    def room_page(room_uuid):
        # 1. Сначала ищем комнату (нужна для мета-тегов)
        room = Room.get_or_none(Room.uuid == room_uuid)
        if not room:
            return redirect('/')

        # 2. Определяем, кто пришел: Бот или Человек?
        user_agent = request.headers.get('User-Agent', '').lower()
        is_bot = any(
            bot in user_agent for bot in ['telegrambot', 'facebookexternalhit', 'whatsapp', 'twitterbot', 'vkshare'])

        # 3. Подготовка данных для превью (OG Tags)
        # Получаем имя приглашающего из URL (?ref=Username) или берем владельца
        inviter_name = request.args.get('ref')
        if not inviter_name:
            inviter_name = room.owner.username

        meta_tags = {}

        if room.is_private:
            # --- ЛОГИКА ДЛЯ ПРИВАТНОЙ КОМНАТЫ ---
            meta_tags['title'] = "🔒 Приватная комната"
            meta_tags['description'] = f"{inviter_name} приглашает вас в приватную комнату для совместного просмотра."
            # Можно добавить картинку-заглушку
            # meta_tags['image'] = url_for('static', filename='img/private_room.jpg', _external=True)
        else:
            # --- ЛОГИКА ДЛЯ ПУБЛИЧНОЙ КОМНАТЫ ---

            # Считаем зрителей через SocketIO
            r_uuid_str = str(room_uuid)
            try:
                # Достаем список сидов в комнате
                participants = socketio.server.manager.rooms.get('/', {}).get(r_uuid_str, set())
                online_count = len(participants)
            except:
                online_count = 0

            # Узнаем текущее видео
            now_playing = "Ничего не играет"
            state = ROOM_STATE.get(r_uuid_str)
            if state and state.get('video_id'):
                try:
                    vid = Video.get_or_none(Video.id == state['video_id'])
                    if vid:
                        now_playing = vid.title
                except:
                    pass

            meta_tags['title'] = f"Watch Party: {room.name}"
            # Формируем описание: Владелец, Зрители, Контент, Приглашение
            desc_lines = [
                f"👑 Владелец: {room.owner.username}",
                f"👥 Зрителей: {online_count}",
                f"🎬 Сейчас играет: {now_playing}",
                f"✨ {inviter_name} приглашает вас присоединиться!"
            ]
            meta_tags['description'] = " | ".join(desc_lines)

        # 4. Если это БОТ — отдаем ему легкую страницу только с мета-тегами
        if is_bot:
            owner_avatar_url = room.owner.to_dict()['avatar_url']
            if owner_avatar_url.startswith('/'):
                image_url = request.url_root.rstrip('/') + owner_avatar_url
            else:
                image_url = owner_avatar_url
            meta_tags['image'] = image_url
            return render_template('meta_preview.html', meta=meta_tags)

        # ==========================================
        # ДАЛЕЕ СТАНДАРТНАЯ ЛОГИКА АВТОРИЗАЦИИ ДЛЯ ЛЮДЕЙ
        # ==========================================

        user_id = session.get('user_id')
        if not user_id:
            # Важно: редиректим на логин с сохранением 'next', чтобы вернуть юзера после входа
            return redirect(f'/login?next=/room/{room_uuid}')

        current_user = User.get_or_none(User.id == user_id)
        if not current_user or current_user.status == 'banned':
            session.clear()
            return redirect('/login')

        # Проверка лимитов подключений (ваш старый код)
        r_uuid_str = str(room_uuid)
        user_conns = ACTIVE_CONNECTIONS.get(current_user.id, {})
        if r_uuid_str not in user_conns and len(user_conns) >= AppConfig.MAX_OPENED_ROOMS:
            return redirect('/')

        template_name = 'voiced_room.html' if room.has_voice_chat else 'room.html'

        # Передаем meta_tags и в обычный шаблон, чтобы если ссылку кинут в чат внутри страницы, она тоже была красивой
        return render_template(template_name,
                               room_uuid=str(room_uuid),
                               max_video_size=AppConfig.MAX_VIDEO_SIZE_BYTES,
                               video_retention_hours=AppConfig.MAX_VIDEO_RETENTION_HOURS,
                               current_user=current_user,
                               meta=meta_tags)

    @app.route('/content/<path:filename>')
    def serve_content(filename):
        if StorageConfig.USE_S3:
            return "Using S3 storage", 404

        # AppConfig.LOCAL_STORAGE_PATH - это путь к папке saved_files
        return send_from_directory(AppConfig.LOCAL_STORAGE_PATH, filename)

    @app.route('/admin')
    @login_required
    @admin_required
    def admin_page(current_user):
        return render_template('admin.html')

    from . import routes

    return app