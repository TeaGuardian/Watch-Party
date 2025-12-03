#/app/__init__.py
from flask import Flask, render_template, send_from_directory, redirect, session
from flask_socketio import SocketIO
import sys
import os

from core.models import User

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.decorators import login_required, admin_required
from config import AppConfig, StorageConfig
from core.database import db

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
        # 1. Ручная проверка авторизации
        user_id = session.get('user_id')
        if not user_id:
            return redirect('/login')

        current_user = User.get_or_none(User.id == user_id)
        if not current_user or current_user.status == 'banned':
            session.clear()
            return redirect('/login')

        r_uuid_str = str(room_uuid)
        user_conns = ACTIVE_CONNECTIONS.get(current_user.id, {})
        if r_uuid_str not in user_conns and len(user_conns) >= AppConfig.MAX_OPENED_ROOMS:
            return redirect('/')

        return render_template('room.html',
                               room_uuid=str(room_uuid),
                               max_video_size=AppConfig.MAX_VIDEO_SIZE_BYTES)

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