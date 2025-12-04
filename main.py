#/main.py
import os
import eventlet
eventlet.monkey_patch()
from app import create_app, socketio
from core.models import create_tables, User

# Создаем приложение
app = create_app()

def setup():
    """Предварительная настройка БД"""
    print("--- Initializing Database ---")
    create_tables() # safe=True внутри models.py не даст упасть ошибке

    if not User.select().where(User.username == 'admin').exists():
        try:
            u = User(username='admin', role='admin', status='approved', cookie_consent=True)
            u.set_password('admin')
            u.save()
            print("!!! Created default admin (admin/admin) !!!")
        except Exception as e:
            print(f"Admin creation skipped: {e}")

# Запускаем настройку при импорте (для Gunicorn)
setup()

if __name__ == "__main__":
    # Локальный запуск без Gunicorn (для тестов)
    print("--- Starting Web Server (Local) ---")
    socketio.run(app, host='0.0.0.0', port=5000, debug=True, allow_unsafe_werkzeug=True)