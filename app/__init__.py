#/app/__init__.py
from flask import Flask, render_template, send_from_directory, redirect, session
from flask_socketio import SocketIO
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.decorators import login_required, admin_required
from config import AppConfig, StorageConfig
from core.database import db
from core.models import User, Room

# Инициализируем SocketIO (пока без логики, она будет позже)
socketio = SocketIO(cors_allowed_origins="*")
from app.sockets import ACTIVE_CONNECTIONS


def create_app():
    app = Flask(__name__,
                template_folder='../templates',
                static_folder='../static')

    app.config.from_object(AppConfig)

    # Регистрируем Blueprint с API
    from .routes import api
    app.register_blueprint(api)

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
        # 1. Проверка авторизации
        user_id = session.get('user_id')
        if not user_id:
            return redirect('/login')

        current_user = User.get_or_none(User.id == user_id)
        if not current_user or current_user.status == 'banned':
            session.clear()
            return redirect('/login')

        # [NEW] 2. Получаем комнату из БД, чтобы проверить настройки
        room = Room.get_or_none(Room.uuid == room_uuid)
        if not room:
            return redirect('/')  # Комната не найдена

        # 3. Проверка лимитов подключений
        r_uuid_str = str(room_uuid)
        user_conns = ACTIVE_CONNECTIONS.get(current_user.id, {})

        # Если юзер еще не в этой комнате, но у него уже открыто макс. кол-во других комнат
        if r_uuid_str not in user_conns and len(user_conns) >= AppConfig.MAX_OPENED_ROOMS:
            return redirect('/')

        # [NEW] 4. Выбор шаблона
        template_name = 'voiced_room.html' if room.has_voice_chat else 'room.html'

        return render_template(template_name,
                               room_uuid=str(room_uuid),
                               max_video_size=AppConfig.MAX_VIDEO_SIZE_BYTES,
                               current_user=current_user)

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